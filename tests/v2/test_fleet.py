from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_worker import FakePool, FakeRuntime, assignment, leaf
from worker_v2.fleet import FleetRuntime, RemoteRuntime
from worker_v2.helper import Helper
from worker_v2.runner import Runner
from worker_v2.ssh_transport import HelperError, SSHTransport, check_hello, ssh_command
from worker_v2.state import Store, StateError, canonical, digest


class Local(FakeRuntime):
    arch = "amd64"
    def check_resources(self):
        pass


class Remote(Local):
    workers = 2
    def __init__(self):
        super().__init__()
        self.disconnected = False
        self.fail_compute = False
    def check_resources(self):
        if self.disconnected:
            raise HelperError("connection lost")
    def prepare(self, value, saved, offer):
        self.check_resources()
        assert saved["acknowledged_at"]
        assert saved["digest"] == digest(saved["payload"])
        super().prepare(value)
    def validate(self, value, saved, offer):
        self.check_resources()
        super().validate(value, offer)
    def compute(self, value, nonce):
        if self.fail_compute:
            self.disconnected = True
            raise HelperError("connection lost")
        return super().compute(value, nonce)
    def close(self):
        pass


def helper_config(directory):
    return {"master_id": "member-master-one", "resource": "CPU", "compute_type": "aws_c7a",
            "workers": 2, "data_directory": str(directory), "nonce_timeout_seconds": 1800}


def connection_config():
    return {"host": "helper.example", "user": "root", "identity_file": "/tmp/member-key",
            "known_hosts_file": "/tmp/member-hosts", "python": sys.executable,
            "code_directory": str(Path(__file__).resolve().parents[2]),
            "config": "/tmp/helper.json", "slice": "membertest.slice", "workers": 2}


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.local, self.remote = Local(), Remote()
        self.fleet = FleetRuntime(self.local, self.store, local_workers=2, remotes=[self.remote],
                                  resource="CPU", compute_type="aws_c7a")
    def tearDown(self):
        self.fleet.close()
        self.store.close()
        self.temp.cleanup()
    def runner(self, pool):
        return Runner(pool, self.store, self.fleet, resource="CPU", compute_type="aws_c7a", workers=self.fleet.workers)

    def test_local_and_helper_share_one_whole_benchmark_and_proofs(self):
        value = assignment()
        value["num_nonces"] = 20
        pool = FakePool(value)
        self.assertEqual(self.runner(pool).step(), "awaiting_verification")
        self.assertTrue(self.local.computed)
        self.assertTrue(self.remote.computed)
        self.assertEqual(sorted(self.local.computed + self.remote.computed), list(range(20)))
        self.assertEqual(pool.result["solution_quality"], list(range(1, 21)))
        self.assertEqual([row["leaf"]["nonce"] for row in pool.proofs["merkle_proofs"]], [0, 2])
        self.assertEqual(len(pool.requests), 1)
        self.assertEqual(self.runner(pool).step(), "active")
        self.assertEqual(self.local.released, 1)
        self.assertEqual(self.remote.released, 1)

    def test_helper_failure_after_handover_is_logged_and_work_continues_locally(self):
        value = assignment()
        value["num_nonces"] = 6
        pool = FakePool(value)
        def refuse(*args, **kwargs):
            raise HelperError("confirmed handover timestamp changed")
        self.remote.prepare = refuse
        with self.assertLogs("innopool-v2-worker", level="WARNING") as logged:
            self.assertEqual(self.runner(pool).step(), "awaiting_verification")
        self.assertTrue(any("helper 1 is unavailable for benchmark" in line and "handover timestamp changed" in line
                            for line in logged.output), logged.output)
        self.assertEqual(sorted(self.local.computed), list(range(6)))
        self.assertEqual(self.remote.computed, [])

    def test_unavailable_helper_prevents_new_offer_before_capacity_is_promised(self):
        self.remote.disconnected = True
        pool = FakePool(assignment())
        with self.assertRaises(HelperError):
            self.runner(pool).step()
        self.assertIsNone(self.store.unfinished())
        self.assertEqual(pool.requests, [])

    def test_disconnected_helper_resumes_missing_work_on_master_after_restart(self):
        pool = FakePool(assignment())
        self.remote.fail_compute = True
        with self.assertRaises(HelperError):
            self.runner(pool).step()
        saved = set(self.store.nonces("fixture-benchmark"))
        self.assertIsNotNone(self.store.unfinished())
        self.assertIsNone(pool.result)
        before = len(self.local.computed)
        self.store.close()
        self.store = Store(self.temp.name)
        self.fleet.store = self.store
        self.runner(pool).step(drain=True)
        self.assertEqual(set(self.local.computed[before:]), set(range(3)) - saved)
        self.assertEqual(pool.result["solution_quality"], [1, 2, 3])
        self.assertEqual(len(pool.requests), 1)

    def test_aggregate_capacity_is_bounded(self):
        with self.assertRaises(StateError):
            FleetRuntime(self.local, self.store, local_workers=4096, remotes=[self.remote],
                         resource="CPU", compute_type="aws_c7a")

    def test_queued_offer_is_not_refreshed_after_helper_disappears(self):
        pool = FakePool(assignment())
        original = pool.call
        refreshed = []
        def call(method, path, body=None):
            if path.endswith('/work-requests'):
                return {'id': 'request-one', 'benchmark_id': None}
            if path.endswith('/work-requests/request-one'):
                return {'state': 'queued', 'benchmark_id': None}
            if path.endswith('/refresh'):
                refreshed.append(body)
                return {}
            return original(method, path, body)
        pool.call = call
        self.assertEqual(self.runner(pool).step(), 'waiting')
        self.assertEqual(len(refreshed), 1)
        self.remote.disconnected = True
        with self.assertRaises(HelperError):
            self.runner(pool).step()
        self.assertEqual(len(refreshed), 1)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.runtime = Local()
        self.config = helper_config(self.temp.name)
        self.helper = Helper(self.config, self.store, self.runtime)
        value = assignment()
        payload = canonical(value)
        self.prepare = {"master_id": self.config["master_id"],
                        "assignment_payload": payload, "assignment_digest": digest(payload),
                        "handed_over_at": "2026-10-12T10:00:00+00:00",
                        "offer": {"resource": "CPU", "compute_type": "aws_c7a"}}
        self.compute = {"benchmark_id": value["benchmark_id"],
                        "assignment_digest": digest(payload), "nonce": 1}
    def tearDown(self):
        self.helper.close()
        self.store.close()
        self.temp.cleanup()

    def test_helper_requires_exact_digest_master_and_confirmed_handover(self):
        for key, value in (("master_id", "another-master"), ("assignment_digest", "f" * 64),
                           ("handed_over_at", None)):
            with self.subTest(key=key):
                with self.assertRaises(StateError):
                    self.helper.prepare({**self.prepare, key: value})
        self.assertEqual(self.runtime.prepared, 0)

    def test_helper_validates_before_handover_without_starting_or_saving_work(self):
        message = {key: value for key, value in self.prepare.items() if key != 'handed_over_at'}
        self.assertEqual(self.helper.validate(message), {
            'validated': 'fixture-benchmark', 'assignment_digest': self.compute['assignment_digest']})
        self.assertEqual(self.runtime.prepared, 0)
        with self.assertRaises(StateError):
            self.store.assignment('fixture-benchmark')

    def test_helper_caches_complete_output_before_reply_and_preserves_it_after_reconnect(self):
        self.helper.prepare(self.prepare)
        self.assertIsNone(self.helper.cached(self.compute))
        self.helper.save(self.compute, self.runtime.compute(assignment(), 1))
        self.helper.close()
        self.store.close()
        self.store = Store(self.temp.name)
        self.runtime = Local()
        self.helper = Helper(self.config, self.store, self.runtime)
        self.helper.prepare(self.prepare)
        self.assertEqual(self.helper.cached(self.compute), (leaf(1), 2))
        self.assertEqual(self.runtime.computed, [])
        with self.assertRaises(StateError):
            self.helper.save(self.compute, (leaf(1), 99))

    def test_helper_rejects_out_of_range_or_changed_assignment_nonces(self):
        self.helper.prepare(self.prepare)
        for changes in ({"nonce": -1}, {"nonce": 3}, {"nonce": True},
                        {"assignment_digest": "f" * 64}):
            with self.subTest(changes=changes):
                with self.assertRaises(StateError):
                    self.helper.cached({**self.compute, **changes})

    def test_handshake_rejects_incompatible_architecture_type_runtime_and_master(self):
        hello = self.helper.hello()
        expected = dict(resource="CPU", compute_type="aws_c7a", workers=2, master_id=self.config["master_id"])
        check_hello(hello, **expected)
        self.assertNotIn("runtime_images", hello)  # images travel inside each assignment now
        for key, value in (("arch", "arm64"), ("compute_type", "aws_c7g"), ("wire_version", 1),
                           ("nonce_timeout_seconds", 60), ("master_id", "other"), ("workers", 1)):
            with self.subTest(key=key):
                with self.assertRaises(StateError):
                    check_hello({**hello, key: value}, **expected)


class TransportTests(unittest.TestCase):
    def test_member_execution_token_is_removed_from_the_ssh_child_environment(self):
        def popen(_arguments, **kwargs):
            self.assertNotIn('POOL_V2_EXECUTION_TOKEN', kwargs['env'])
            return subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], **kwargs)
        transport = SSHTransport(connection_config(), popen=popen)
        with patch.dict(os.environ, {'POOL_V2_EXECUTION_TOKEN': 'master-only-fixture-token'}):
            try:
                transport.start()
            finally:
                transport.close()

    def test_ssh_requires_pinned_host_key_and_quotes_remote_paths(self):
        config = connection_config()
        config["code_directory"] = "/tmp/member 'quoted' $(touch bad)"
        command = ssh_command(config)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertIn("--scope", command[-1])
        self.assertIn("'\"'\"'quoted'\"'\"'", command[-1])
        for key, value in (("host", "-oBad"), ("user", "root;bad"),
                           ("slice", "../bad.slice"), ("config", "/tmp/../bad.json"),
                           ("workers", True)):
            with self.subTest(key=key):
                with self.assertRaises(StateError):
                    ssh_command({**connection_config(), key: value})

    def test_real_pipe_protocol_matches_concurrent_requests_and_reuses_saved_leaves(self):
        with tempfile.TemporaryDirectory() as directory:
            config = helper_config(directory)
            config["nonce_timeout_seconds"] = 3
            tests = str(Path(__file__).resolve().parent)
            stub = """
import json,sys
sys.path.insert(0, sys.argv[2])
from test_worker import FakeRuntime
from worker_v2.helper import Helper,serve
from worker_v2.state import Store
config=json.loads(sys.argv[1]); runtime=FakeRuntime(); runtime.arch='amd64'
store=Store(config['data_directory'])
try: serve(Helper(config,store,runtime))
finally: store.close()
"""
            def popen(_arguments, **kwargs):
                return subprocess.Popen([sys.executable, "-c", stub, json.dumps(config), tests], **kwargs)
            transport = SSHTransport(connection_config(), popen=popen)
            remote = RemoteRuntime(connection_config(), master_id=config["master_id"], resource="CPU",
                compute_type="aws_c7a", nonce_timeout=3, transport=transport)
            value = assignment()
            saved = {"payload": canonical(value), "digest": digest(canonical(value)),
                     "acknowledged_at": "2026-10-12T10:00:00+00:00"}
            offer = {"resource": "CPU", "compute_type": "aws_c7a"}
            try:
                remote.validate(value, saved, offer)
                remote.prepare(value, saved, offer)
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda nonce: remote.compute(value, nonce), [0, 1]))
                self.assertEqual(results, [(leaf(0), 1), (leaf(1), 2)])
                remote.release(value)
                remote.close()
                remote.prepare(value, saved, offer)
                self.assertEqual(remote.compute(value, 1), (leaf(1), 2))
                with self.assertRaises(HelperError):
                    remote.compute(value, 100)
            finally:
                remote.close()


if __name__ == "__main__":
    unittest.main()
