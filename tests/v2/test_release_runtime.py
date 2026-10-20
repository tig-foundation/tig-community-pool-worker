import tempfile
import unittest

from worker_v2.client import ProtocolError
from worker_v2.runner import Runner
from worker_v2.state import StateError, Store, canonical, digest
from test_worker import FakePool, FakeRuntime, assignment


HYPERGRAPH = 'ghcr.io/tig-foundation/tig-monorepo/hypergraph/runtime@sha256:' + 'c' * 64


class ReleasePool(FakePool):
    """Serves a paired release naming one worker commit, plus the images the pool currently pins."""
    def __init__(self, commit):
        super().__init__(assignment())
        self.release = {'worker': {'commit': commit}, 'runtime_images': {'c001': assignment()['runtime_image']}}
        self.bad_digest = False
        self.release_reads = 0
    def call(self, method, path, body=None):
        if path.endswith('/release'):
            self.release_reads += 1
            return self.release
        result = super().call(method, path, body)
        if path.endswith('/capabilities'):
            result['release_digest'] = '0' * 64 if self.bad_digest else digest(canonical(self.release))
        return result


class RuntimeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.old, self.new = 'a' * 40, 'b' * 40
    def tearDown(self):
        self.store.close()
        self.directory.cleanup()
    def runner(self, pool, commit):
        return Runner(pool, self.store, FakeRuntime(), resource='CPU', compute_type='aws_c7a', workers=2, worker_commit=commit)

    def test_stale_worker_release_refuses_offer_before_any_work_request(self):
        pool = ReleasePool(self.new)
        with self.assertRaisesRegex(StateError, 'different worker release'):
            self.runner(pool, self.old).step()
        self.assertEqual(pool.requests, [])
        self.assertIsNone(self.store.unfinished())
        with self.assertRaises(StateError):
            Runner(pool, self.store, FakeRuntime(), resource='CPU', compute_type='aws_c7a', worker_commit=5)

    def test_matching_worker_release_can_offer_and_complete(self):
        pool = ReleasePool(self.new)
        runner = self.runner(pool, self.new)
        runner.step()
        self.assertEqual(len(pool.requests), 1)
        self.assertEqual(runner.step(drain=True), 'active')

    def test_newly_pinned_images_need_no_worker_update(self):
        # The pool pins another challenge's image: the installed worker keeps requesting work
        # unchanged, because each assignment carries the image it must run.
        pool = ReleasePool(self.new)
        pool.release['runtime_images']['c005'] = HYPERGRAPH
        runner = self.runner(pool, self.new)
        runner.step()
        self.assertEqual(len(pool.requests), 1)
        self.assertGreaterEqual(pool.release_reads, 1)

    def test_changed_release_digest_refuses_offer(self):
        pool = ReleasePool(self.new)
        pool.bad_digest = True
        with self.assertRaises(ProtocolError):
            self.runner(pool, self.new).step()
        self.assertEqual(pool.requests, [])

    def test_custom_master_without_an_installed_commit_skips_the_pairing_check(self):
        pool = ReleasePool(self.new)
        runner = Runner(pool, self.store, FakeRuntime(), resource='CPU', compute_type='aws_c7a', workers=2)
        runner.step()
        self.assertEqual((len(pool.requests), pool.release_reads), (1, 0))

    def test_existing_handed_over_work_drains_after_a_release_change(self):
        pool = ReleasePool(self.old)
        runner = self.runner(pool, self.old)
        runner.step()
        pool.release = {**pool.release, 'worker': {'commit': self.new}}
        self.assertEqual(runner.step(drain=True), 'active')
        self.assertEqual(len(pool.requests), 1)

    def test_stale_unlinked_saved_request_cannot_be_posted(self):
        pool = ReleasePool(self.new)
        self.store.bind(pool.origin, 'member-one')
        self.store.request({'resource': 'CPU', 'compute_type': 'aws_c7a', 'capacity': {'workers': 2}})
        with self.assertRaises(StateError):
            self.runner(pool, self.old).step()
        self.assertEqual(pool.requests, [])

    def test_stale_queued_offer_cannot_be_refreshed(self):
        pool = ReleasePool(self.new)
        original = pool.call
        def queued(method, path, body=None):
            if path.endswith('/work-requests/request-one'):
                return {'state': 'queued', 'benchmark_id': None}
            if path.endswith('/refresh'):
                self.fail('stale queued offer must not be refreshed')
            return original(method, path, body)
        pool.call = queued
        self.store.bind(pool.origin, 'member-one')
        request = self.store.request({'resource': 'CPU', 'compute_type': 'aws_c7a', 'capacity': {'workers': 2}})
        self.store.link_request(request['request_key'], 'request-one')
        with self.assertRaises(StateError):
            self.runner(pool, self.old).step()
        self.assertEqual(pool.requests, [])


if __name__ == '__main__':
    unittest.main()
