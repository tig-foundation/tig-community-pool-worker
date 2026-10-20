from copy import deepcopy
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from blake3 import blake3

from worker_v2.client import Client, ProtocolError
from worker_v2.proofs import BenchmarkTree, leaf_hash
from worker_v2.runner import Runner
from worker_v2.runtime import DockerRuntime
from worker_v2.state import Store, StateError, canonical, digest


def assignment(resource="CPU"):
    return {"api_version": "2.0", "benchmark_id": "fixture-benchmark", "num_nonces": 3, "num_bundles": 1,
        "algorithm_name": "fixture_algo",
        "settings": {"player_id": "0x"+"1"*40, "block_id": "block-one", "challenge_id": "c001",
                     "algorithm_id": "c001_a001", "track_id": "track"},
        "rand_hash": "0"*32, "fuel_budget": 100, "hyperparameters": None,
        "compute_type": "aws_c7a" if resource == "CPU" else "aws_g4dn",
        "binary_url": "https://mainnet-api.tig.foundation/get-binary-blob?algorithm_id=c001_a001",
        "binary_sha256": "a"*64,
        "runtime_image": "ghcr.io/tig-foundation/tig-monorepo/satisfiability/runtime@sha256:"+"a"*64}


def leaf(nonce):
    return {"nonce": nonce, "runtime_signature": 123, "fuel_consumed": 42, "solution": "fixture", "cpu_arch": "amd64"}


class FakeRuntime:
    def __init__(self): self.computed, self.prepared, self.released = [], 0, 0
    def validate(self, value, offer):
        if value["compute_type"] != offer["compute_type"]: raise StateError("wrong offer")
    def prepare(self, value): self.prepared += 1
    def compute(self, value, nonce):
        self.computed.append(nonce)
        return leaf(nonce), nonce+1
    def release(self, value): self.released += 1


class FakePool(Client):
    def __init__(self, value):
        self.origin = "https://pool.example"
        self.payload = canonical(value)
        self.assignment_digest = digest(self.payload)
        self.state, self.handover, self.result, self.proofs = "accepted", None, None, None
        self.server_version, self.work_enabled = "2.0", True
        self.requests = []
        self.lose_ack, self.lose_request, self.lose_result = False, False, False
    def call(self, method, path, body=None):
        if path.endswith("/capabilities"):
            return {"api_version": self.server_version, "assignment_unit": "whole-benchmark", "work_enabled": self.work_enabled}
        if path.endswith("/member/balance"): return {"id": "member-one"}
        if path.endswith("/work-requests"):
            self.requests.append(body["request_key"])
            if self.lose_request:
                self.lose_request = False
                raise TimeoutError("work request response lost")
            return {"id": "request-one", "benchmark_id": "fixture-benchmark"}
        if path.endswith("/work-requests/request-one"):
            return {"state": "assigned", "benchmark_id": "fixture-benchmark"}
        if path.endswith("/acknowledge"):
            assert body["assignment_digest"] == self.assignment_digest
            self.handover = self.handover or "2026-09-20T12:00:00+00:00"
            if self.lose_ack:
                self.lose_ack = False
                raise TimeoutError("handover committed; response lost")
            return {"handed_over_at": self.handover, "assignment_digest": self.assignment_digest}
        if path.endswith("/results"):
            assert self.handover
            if self.result is not None: assert self.result == body
            self.result = deepcopy(body)
            if self.lose_result:
                self.lose_result = False
                raise TimeoutError("results committed; response lost")
            return {"stored": True}
        if path.endswith("/proofs"):
            assert self.handover and self.result
            self.proofs = body
            self.state = "active"
            return {"stored": True}
        if path.endswith("/benchmarks/fixture-benchmark"):
            return {"state": self.state, "assignment_payload": self.payload, "assignment_digest": self.assignment_digest,
                    "handed_over_at": self.handover, "sampled_nonces": [0,2] if self.result else None}
        raise AssertionError((method,path,body))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def runner(self, pool, runtime, resource="CPU"):
        return Runner(pool, self.store, runtime, resource=resource,
            compute_type="aws_c7a" if resource == "CPU" else "aws_g4dn", workers=2)
    def restart(self):
        self.store.close()
        self.store = Store(self.temp.name)

    def test_cpu_and_gpu_whole_benchmarks_deliver_all_nonces_and_selected_proofs(self):
        for resource in ("CPU", "GPU"):
            pool, runtime = FakePool(assignment(resource)), FakeRuntime()
            runner = self.runner(pool, runtime, resource)
            self.assertEqual(runner.step(), "awaiting_verification")
            self.assertEqual(sorted(runtime.computed), [0,1,2])
            self.assertEqual(pool.result["solution_quality"], [1,2,3])
            self.assertEqual([p["leaf"]["nonce"] for p in pool.proofs["merkle_proofs"]], [0,2])
            self.assertEqual(runner.step(), "active")
            self.assertIsNone(self.store.unfinished())
            self.assertEqual(len(self.store.nonces("fixture-benchmark")), 3)
            # A second simulated member installation needs its own evidence dir.
            self.store.close()
            self.temp.cleanup()
            self.temp = tempfile.TemporaryDirectory(); self.store = Store(self.temp.name)

    def test_lost_ack_never_starts_compute_and_recovers_committed_handover(self):
        pool, runtime = FakePool(assignment()), FakeRuntime()
        pool.lose_ack = True
        with self.assertRaises(TimeoutError): self.runner(pool, runtime).step()
        self.assertEqual(runtime.computed, [])
        self.assertIsNone(self.store.assignment("fixture-benchmark")["acknowledged_at"])
        self.restart()
        self.runner(pool, runtime).step()
        self.assertEqual(self.store.assignment("fixture-benchmark")["acknowledged_at"], pool.handover)
        self.assertEqual(sorted(runtime.computed), [0,1,2])

    def test_lost_request_reuses_saved_key_after_restart(self):
        pool, runtime = FakePool(assignment()), FakeRuntime()
        pool.lose_request = True
        with self.assertRaises(TimeoutError): self.runner(pool, runtime).step()
        self.restart()
        self.runner(pool, runtime).step()
        self.assertEqual(len(pool.requests), 2)
        self.assertEqual(pool.requests[0], pool.requests[1])

    def test_lost_result_retries_without_recomputing_and_pause_allows_proofs(self):
        pool, runtime = FakePool(assignment()), FakeRuntime()
        pool.lose_result = True
        with self.assertRaises(TimeoutError): self.runner(pool, runtime).step()
        self.assertEqual(len(runtime.computed), 3)
        self.restart(); pool.work_enabled = False
        self.runner(pool, runtime).step(drain=True)
        self.assertEqual(len(runtime.computed), 3)
        self.assertIsNotNone(pool.proofs)

    def test_failed_nonce_resumes_saved_whole_assignment_after_restart(self):
        pool, runtime = FakePool(assignment()), FakeRuntime()
        original = runtime.compute
        def fail_second(value, nonce):
            if nonce == 1:
                raise RuntimeError("interrupted nonce")
            return original(value, nonce)
        runtime.compute = fail_second
        runner = Runner(pool, self.store, runtime, resource="CPU", compute_type="aws_c7a", workers=1)
        with self.assertRaises(RuntimeError): runner.step()
        self.assertEqual(list(self.store.nonces("fixture-benchmark")), [0])
        self.restart()
        runtime.compute = original
        # A changed next-offer configuration must not change the already
        # accepted benchmark's CPU identity when the installation restarts.
        self.runner(pool, runtime, resource="GPU").step()
        self.assertEqual(sorted(runtime.computed), [0,1,2])
        self.assertEqual(len(pool.requests), 1)

    def test_failed_container_cleanup_keeps_assignment_recoverable(self):
        pool, runtime = FakePool(assignment()), FakeRuntime()
        runner = self.runner(pool, runtime)
        runner.step()
        with patch.object(runtime, "release", side_effect=RuntimeError("docker unavailable")):
            with self.assertRaises(RuntimeError): runner.step()
        self.assertIsNotNone(self.store.unfinished())
        self.restart()
        self.assertEqual(self.runner(pool, runtime).step(), "active")
        self.assertIsNone(self.store.unfinished())

    def test_incompatible_pool_prevents_any_work_request(self):
        pool, runtime = FakePool(assignment()), FakeRuntime()
        pool.server_version = "1.0"
        with self.assertRaises(ProtocolError): self.runner(pool, runtime).step()
        self.assertEqual(pool.requests, [])
        self.assertIsNone(self.store.unfinished())

    def test_assignment_digest_and_member_binding_cannot_change(self):
        value = assignment(); payload = canonical(value)
        with self.assertRaises(StateError): self.store.save_assignment(value["benchmark_id"], payload, "wrong")
        self.store.save_assignment(value["benchmark_id"], payload, digest(payload))
        changed = canonical({**value, "fuel_budget": 1})
        with self.assertRaises(StateError): self.store.save_assignment(value["benchmark_id"], changed, digest(changed))
        self.store.bind("https://one.example", "member-one")
        with self.assertRaises(StateError): self.store.bind("https://two.example", "member-one")
        with self.assertRaises(StateError): self.store.bind("https://one.example", "member-two")

    def test_no_nonce_evidence_can_precede_handover_and_repeated_leaf_is_immutable(self):
        value = assignment(); payload = canonical(value)
        self.store.save_assignment(value["benchmark_id"], payload, digest(payload))
        with self.assertRaises(StateError): self.store.save_nonce(value["benchmark_id"], 0, leaf(0), 1)
        self.store.acknowledge(value["benchmark_id"], "timestamp")
        self.store.save_nonce(value["benchmark_id"], 0, leaf(0), 1)
        with self.assertRaises(StateError): self.store.save_nonce(value["benchmark_id"], 0, leaf(0), 2)


class ProofTests(unittest.TestCase):
    def root(self, proof):
        value, position, level = leaf_hash(proof["leaf"]), proof["leaf"]["nonce"], 0
        encoded = proof["branch"]
        for offset in range(0, len(encoded), 66):
            depth = int(encoded[offset:offset+2],16)
            position >>= depth-level
            other = bytes.fromhex(encoded[offset+2:offset+66])
            value = blake3(other+value if position&1 else value+other).digest()
            position >>= 1; level = depth+1
        return value.hex()

    def test_recorded_live_tig_proof_reconstructs_its_actual_root(self):
        with gzip.open(Path(__file__).parent / "fixtures/tig-proof.json.gz", "rt") as source:
            fixture = json.load(source)
        self.assertEqual(self.root(fixture["proof"]), fixture["merkle_root"])

    def test_whole_tree_supports_odd_counts_without_batch_offsets(self):
        for count in (1,3,5,8,17):
            tree = BenchmarkTree({n:(leaf(n),n) for n in range(count)}, count)
            for proof in tree.proofs(list(range(count)))["merkle_proofs"]:
                self.assertEqual(self.root(proof), tree.root)
        with self.assertRaises(StateError): BenchmarkTree({1:(leaf(1),1)},1)


class RuntimeIsolationTests(unittest.TestCase):
    def test_pool_archive_checksum_is_verified_before_starting_a_container(self):
        buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode="w:gz") as target:
            item=tarfile.TarInfo("amd64/fixture_algo.so");item.size=3
            target.addfile(item,io.BytesIO(b"bin"))
        archive=buffer.getvalue()
        value=assignment()
        value["binary_sha256"]=hashlib.sha256(archive).hexdigest()
        value["binary_url"]="https://pool.example/api/v2/artifacts/"+value["binary_sha256"]
        with tempfile.TemporaryDirectory() as directory, patch("platform.machine",return_value="x86_64"):
            runtime=DockerRuntime(directory,binary_hosts={"pool.example"})
            runtime.validate(value,{"resource":"CPU","compute_type":"aws_c7a"})
            with patch("worker_v2.runtime.build_opener") as opener,patch.object(runtime,"_inspect",return_value=None),patch.object(runtime,"_run") as run:
                opener.return_value.open.return_value=io.BytesIO(archive)
                runtime.prepare(value)
                self.assertEqual(run.call_args.args[0][:2],["docker","run"])
                self.assertEqual((runtime._paths(value)[0]/"c001_a001.so").read_bytes(),b"bin")
                run.reset_mock()
                value["binary_sha256"]="b"*64
                opener.return_value.open.return_value=io.BytesIO(archive)
                with self.assertRaises(StateError):runtime.prepare(value)
                run.assert_not_called()

    def test_unpinned_image_and_foreign_container_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch("platform.machine", return_value="x86_64"):
            runtime = DockerRuntime(directory)
            value = assignment()
            runtime.validate(value, {"resource":"CPU", "compute_type":"aws_c7a"})
            # The pool names the image inside the assignment; only an official content digest is run.
            for image in ("ghcr.io/tig-foundation/tig-monorepo/satisfiability/runtime:latest",
                          "docker.io/tig-foundation/tig-monorepo/satisfiability/runtime@sha256:"+"a"*64, None, 7):
                with self.subTest(image=image), self.assertRaises(StateError):
                    runtime.validate({**value, "runtime_image": image}, {"resource":"CPU", "compute_type":"aws_c7a"})
            missing = dict(value); del missing["runtime_image"]
            with self.assertRaises(StateError): runtime.validate(missing, {"resource":"CPU", "compute_type":"aws_c7a"})
            with patch.object(runtime, "_inspect", return_value={"Config":{"Labels":{"innopool.v2.owner":"another"}}}), patch.object(runtime,"_run") as run:
                with self.assertRaises(StateError): runtime.release(value)
                run.assert_not_called()
