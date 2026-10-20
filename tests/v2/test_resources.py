from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from worker_v2 import __main__ as entry
from worker_v2.resources import RuntimeLimits
from worker_v2.runtime import DockerRuntime
from worker_v2.state import StateError


LIMITS = {"cpu_millis": 1000, "memory_mib": 1280, "pids_limit": 128,
          "cgroup_parent": "innopoolv2pilot.slice"}
spec = importlib.util.spec_from_file_location("cpu_pilot", Path(__file__).resolve().parents[2] / "tools/run_cpu_pilot.py")
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


class ResourceTests(unittest.TestCase):
    def test_invalid_limits_stop_before_a_work_request(self):
        invalid = [None, {}, False, {**LIMITS, "cpu_millis": True}, {**LIMITS, "cpu_millis": 0},
                   {**LIMITS, "cpu_millis": 0.5}, {**LIMITS, "memory_mib": -1},
                   {**LIMITS, "pids_limit": 0}, {**LIMITS, "memory_mbi": 1280},
                   {**LIMITS, "cgroup_parent": "../../system.slice"}, {**LIMITS, "cgroup_parent": None}]
        with tempfile.TemporaryDirectory() as directory:
            config = {"pool_origin": "https://pool.example", "data_directory": directory,
                      "resource": "CPU", "compute_type": "aws_c7g"}
            path = Path(directory) / "worker.json"
            for value in invalid:
                with self.subTest(value=value), patch.object(entry, "Runner") as runner, \
                        patch.object(entry.Client, "call") as network, patch.dict("os.environ", {"POOL_V2_EXECUTION_TOKEN": "fixture"}):
                    path.write_text(json.dumps({**config, "runtime_limits": value}))
                    with self.assertRaises(StateError):
                        entry.main(["--config", str(path), "--require-resource-limits"])
                    runner.assert_not_called()
                    network.assert_not_called()

    def test_container_limits_survive_recovery_and_recreate_only_owned_mismatches(self):
        with tempfile.TemporaryDirectory() as directory:
            image = "ghcr.io/tig-foundation/tig-monorepo/satisfiability/runtime@sha256:" + "a" * 64
            runtime = DockerRuntime(directory, resource_limits=LIMITS)
            assignment = {"benchmark_id": "resource-test", "settings": {"algorithm_id": "c001_a001", "challenge_id": "c001"},
                          "binary_sha256": "b" * 64, "compute_type": "aws_c7g", "runtime_image": image}
            root, name = runtime._paths(assignment)
            root.mkdir();(root / "c001_a001.so").write_bytes(b"fixture")
            (root / "archive.sha256").write_text(assignment["binary_sha256"])
            (root / "results").mkdir();evidence = root / "results/0.json";evidence.write_text("retain me")
            existing = {"Config": {"Labels": {"innopool.v2.owner": runtime.namespace,
                          "innopool.v2.benchmark": assignment["benchmark_id"]}, "Image": image},
                        "HostConfig": runtime.limits.host_config}
            cases = [None, existing]
            for key in runtime.limits.host_config:
                changed = deepcopy(existing);changed["HostConfig"][key] = 0 if key != "CgroupParent" else "system.slice"
                cases.append(changed)
            for container in cases:
                with self.subTest(container=container), patch.object(runtime, "_inspect", return_value=container), \
                        patch.object(runtime, "_run") as run:
                    runtime.prepare(assignment)
                    commands = [call.args[0] for call in run.call_args_list]
                    if container == existing:
                        self.assertEqual(commands, [["docker", "restart", "--time", "5", name]])
                    else:
                        if container:
                            self.assertEqual(commands[0], ["docker", "rm", "--force", name])
                        create = commands[-1]
                        for flag, expected in (("--cpus", "1.000"), ("--memory", "1280m"), ("--memory-swap", "1280m"),
                                               ("--pids-limit", "128"), ("--cgroup-parent", "innopoolv2pilot.slice")):
                            self.assertEqual(create[create.index(flag) + 1], expected)
                    self.assertEqual(evidence.read_text(), "retain me")
            existing["Config"]["Labels"]["innopool.v2.owner"] = "someone-else"
            with patch.object(runtime, "_inspect", return_value=existing), patch.object(runtime, "_run") as run:
                with self.assertRaises(StateError):runtime.prepare(assignment)
                run.assert_not_called()

    def test_shared_slice_requires_worker_containment_and_finite_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);proc = root / "self.cgroup";group = root / LIMITS["cgroup_parent"];group.mkdir()
            values = {"cpu.max": "100000 100000", "memory.max": str(2 * pilot.GIB), "memory.swap.max": "0"}
            for key, value in values.items():(group / key).write_text(value)
            limits = RuntimeLimits.parse(LIMITS)
            proc.write_text("0::/user.slice/unrelated.scope\n")
            with self.assertRaisesRegex(StateError, "inside"):limits.check_process_group(proc=proc, root=root)
            proc.write_text("0::/innopoolv2pilot.slice/worker.service\n")
            limits.check_process_group(proc=proc, root=root)
            for key, invalid in (("cpu.max", "max 100000"), ("memory.max", "max"), ("memory.swap.max", "max")):
                (group / key).write_text(invalid)
                with self.subTest(key=key), self.assertRaises(StateError):limits.check_process_group(proc=proc, root=root)
                (group / key).write_text(values[key])

    def test_pilot_refuses_missing_or_excessive_aggregate_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {"cpu.max": "100000 100000", "memory.max": str(2 * pilot.GIB), "memory.swap.max": "0",
                      "pids.max": "256", "memory.current": str(pilot.GIB // 2)}
            for key, value in values.items():(root / key).write_text(value)
            self.assertEqual(pilot.check_budget(root), 3 * pilot.GIB // 2)
            for key, invalid in (("cpu.max", "200000 100000"), ("memory.max", str(3 * pilot.GIB)),
                                 ("memory.swap.max", "1"), ("pids.max", "max")):
                (root / key).write_text(invalid)
                with self.subTest(key=key), self.assertRaises(ValueError):pilot.check_budget(root)
                (root / key).write_text(values[key])

    def test_pilot_admission_leaves_host_headroom_and_counts_existing_slice_memory(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(pilot.shutil, "disk_usage", return_value=SimpleNamespace(free=20 * pilot.GIB)):
            info = Path(directory) / "meminfo";info.write_text(f"MemAvailable: {2 * pilot.GIB // 1024} kB\n")
            with self.assertRaisesRegex(StateError, "spare RAM"):pilot.check_headroom(directory, 2 * pilot.GIB, meminfo=info)
            pilot.check_headroom(directory, pilot.GIB // 2, meminfo=info)
            with patch.object(pilot.shutil, "disk_usage", return_value=SimpleNamespace(free=pilot.GIB)), \
                    self.assertRaisesRegex(StateError, "disk"):
                pilot.check_headroom(directory, 0, meminfo=info)

    def test_pilot_rejects_uncapped_or_parallel_or_gpu_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);(root / "run").write_text("fixture")
            (root / "installation-v2.json").write_text(json.dumps({"kind": "innopool-v2-worker", "directory": str(root)}))
            config = {"resource": "CPU", "workers": 1, "runtime_limits": LIMITS}
            (root / "worker.json").write_text(json.dumps(config));pilot.check_worker(root)
            for changed in ({**config, "workers": 2}, {**config, "resource": "GPU"}, {**config, "runtime_limits": None},
                            {**config, "runtime_limits": {**LIMITS, "memory_mib": 2048}},
                            {**config, "runtime_limits": {**LIMITS, "cgroup_parent": "wrong.slice"}}):
                (root / "worker.json").write_text(json.dumps(changed))
                with self.subTest(config=changed), self.assertRaises(StateError):pilot.check_worker(root)
