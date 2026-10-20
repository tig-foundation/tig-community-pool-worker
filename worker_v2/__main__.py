"""Run a pinned worker without updating code or touching other services."""

import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
from urllib.parse import urlsplit

from .client import Client
from .runner import Runner
from .runtime import DockerRuntime
from .fleet import build_fleet
from .state import Store, StateError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--drain", action="store_true", help="finish saved work without requesting another benchmark")
    parser.add_argument("--require-resource-limits", action="store_true", help="refuse startup without configured hard resource limits")
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    directory = Path(config["data_directory"]).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = logging.getLogger("innopool-v2-worker")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with (directory / "worker.lock").open("a") as process_lock:
        fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(directory)
        runtime = None
        try:
            client = Client(config["pool_origin"], os.environ.get("POOL_V2_EXECUTION_TOKEN"))
            from .artifacts import DEFAULT_BINARY_HOSTS
            artifact_hosts = {urlsplit(client.origin).hostname, *config.get("binary_hosts", DEFAULT_BINARY_HOSTS)}
            runtime = DockerRuntime(directory,
                binary_hosts=artifact_hosts,
                nonce_timeout=config.get("nonce_timeout_seconds", 1800),
                resource_limits=config.get("runtime_limits"))
            if args.require_resource_limits and runtime.limits is None:
                raise StateError("this launch requires runtime_limits")
            runtime, workers = build_fleet(runtime, store, config)
            runtime.check_resources()
            runner = Runner(client, store, runtime, resource=config["resource"],
                            compute_type=config["compute_type"], workers=workers,
                            worker_commit=config.get("worker_commit"))
            stop = threading.Event()
            for number in (signal.SIGINT, signal.SIGTERM):
                signal.signal(number, lambda *_: stop.set())
            while not stop.is_set():
                try:
                    drain=args.drain or (directory/'drain.request').exists()
                    state = runner.step(drain=drain)
                    log.info("benchmark state: %s", state)
                    if drain and store.unfinished() is None:
                        return 0
                except Exception:
                    log.exception("benchmark progress interrupted; keeping assignment and evidence for retry")
                stop.wait(3)
        finally:
            try:
                if runtime is not None and hasattr(runtime, "close"):
                    runtime.close()
            finally:
                store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
