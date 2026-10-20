"""Member-owned compute helper. JSON messages travel over authenticated SSH."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
from pathlib import Path
from queue import Queue
import signal
import sys
from threading import Thread, current_thread, main_thread

from .artifacts import DEFAULT_BINARY_HOSTS
from .runtime import DockerRuntime
from .state import Store, StateError, digest


# 2: helpers receive the pinned runtime image inside each assignment instead of
# an install-time image map, so the hello no longer carries runtime_images.
WIRE_VERSION = 2
MAX_MESSAGE = 64 * 1024 * 1024


class Helper:
    """One session owns the helper store and its bounded runtime concurrency."""

    def __init__(self, config, store, runtime):
        self.config, self.store, self.runtime = config, store, runtime
        self.workers = config["workers"]
        if type(self.workers) is not int or not 1 <= self.workers <= 4096:
            raise StateError("helper workers must be between 1 and 4096")
        if not isinstance(config.get("master_id"), str) or not config["master_id"]:
            raise StateError("helper requires a persistent master_id")
        store.bind("member-helper://" + config["master_id"], config["master_id"])
        self.prepared = None

    def hello(self):
        return {"wire_version": WIRE_VERSION, "arch": self.runtime.arch,
                "resource": self.config["resource"],
                "compute_type": self.config["compute_type"], "workers": self.workers,
                "nonce_timeout_seconds": self.config.get("nonce_timeout_seconds", 1800),
                "master_id": self.config["master_id"]}

    def validated_assignment(self, message):
        if message["master_id"] != self.config["master_id"]:
            raise StateError("helper is bound to another master")
        offer = message["offer"]
        if (offer["resource"], offer["compute_type"]) != (
                self.config["resource"], self.config["compute_type"]):
            raise StateError("helper cannot execute this verification compute type")
        payload = message["assignment_payload"]
        if not isinstance(payload, str) or digest(payload) != message["assignment_digest"]:
            raise StateError("helper assignment digest does not match its payload")
        value = json.loads(payload)
        if value.get("api_version") != "2.0":
            raise StateError("helper assignment API version is incompatible")
        self.runtime.validate(value, offer)
        return value

    def validate(self, message):
        value = self.validated_assignment(message)
        return {"validated": value["benchmark_id"], "assignment_digest": message["assignment_digest"]}

    def prepare(self, message):
        value = self.validated_assignment(message)
        if self.prepared is not None and self.prepared != value["benchmark_id"]:
            raise StateError("release the previous helper assignment first")
        payload = message["assignment_payload"]
        self.store.save_assignment(value["benchmark_id"], payload, message["assignment_digest"])
        self.store.acknowledge(value["benchmark_id"], message["handed_over_at"])
        self.runtime.prepare(value)
        self.prepared = value["benchmark_id"]
        return {"prepared": self.prepared, "assignment_digest": digest(payload)}

    def assignment(self, message):
        identity = message["benchmark_id"]
        row = self.store.assignment(identity)
        if self.prepared != identity or row["digest"] != message["assignment_digest"]:
            raise StateError("helper has not prepared this exact assignment")
        return json.loads(row["payload"])

    def cached(self, message):
        value = self.assignment(message)
        nonce = message["nonce"]
        if type(nonce) is not int or not 0 <= nonce < value["num_nonces"]:
            raise StateError("helper nonce is outside the assignment")
        results = self.store.nonces(value["benchmark_id"])
        return results[nonce] if nonce in results else None

    def save(self, message, result):
        leaf, quality = result
        self.store.save_nonce(message["benchmark_id"], message["nonce"], leaf, quality)
        return {"leaf": leaf, "quality": quality,
                "assignment_digest": message["assignment_digest"]}

    def release(self, message):
        value = self.assignment(message)
        self.runtime.release(value)
        self.prepared = None
        return {"released": value["benchmark_id"]}

    def close(self):
        if self.prepared is not None:
            row = self.store.assignment(self.prepared)
            self.runtime.release(json.loads(row["payload"]))
            self.prepared = None


def serve(helper, source=sys.stdin, target=sys.stdout):
    """Read requests separately; all SQLite writes and replies stay on one thread."""
    events = Queue()
    active, closing = {}, False

    def reader():
        try:
            while True:
                line = source.readline(MAX_MESSAGE + 1)
                if not line:
                    break
                if len(line.encode()) > MAX_MESSAGE or not line.endswith("\n"):
                    raise StateError("helper request exceeds the message limit")
                value = json.loads(line)
                if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not 1 <= len(value["id"]) <= 128:
                    raise StateError("helper request requires an identifier")
                events.put(("request", value))
        except Exception as error:
            events.put(("input_error", error))
        finally:
            events.put(("eof", None))

    def reply(message, result=None, error=None):
        value = {"id": message["id"], "ok": error is None}
        value["result" if error is None else "error"] = result if error is None else str(error)
        encoded = json.dumps(value, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > MAX_MESSAGE:
            raise StateError("helper response exceeds the message limit")
        target.write(encoded + "\n")
        target.flush()

    Thread(target=reader, daemon=True).start()
    executor = ThreadPoolExecutor(max_workers=helper.workers)
    previous_handlers = {}
    # Signals stop new work; completed nonce evidence is still saved before exit.
    if current_thread() is main_thread():
        for number in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[number] = signal.signal(number, lambda *_: events.put(("eof", None)))
    try:
        while not closing or active:
            kind, value = events.get()
            if kind == "eof":
                closing = True
                continue
            if kind == "input_error":
                raise value
            if kind == "complete":
                message, future = value
                active.pop(message["id"], None)
                try:
                    result = helper.save(message, future.result())
                    if not closing:
                        reply(message, result)
                except Exception as error:
                    if not closing:
                        reply(message, error=error)
                continue
            message = value
            if closing:
                continue
            try:
                operation = message["op"]
                if message["id"] in active:
                    raise StateError("helper request identifier is already running")
                if operation == "compute":
                    cached = helper.cached(message)
                    if cached is not None:
                        reply(message, helper.save(message, cached))
                        continue
                    if len(active) >= helper.workers:
                        raise StateError("helper has no free nonce capacity")
                    if any((row["benchmark_id"], row["nonce"]) == (message["benchmark_id"], message["nonce"])
                           for row in active.values()):
                        raise StateError("helper nonce is already running")
                    assignment = helper.assignment(message)
                    active[message["id"]] = message
                    future = executor.submit(helper.runtime.compute, assignment, message["nonce"])
                    future.add_done_callback(lambda future, message=message: events.put(("complete", (message, future))))
                elif operation == "hello":
                    reply(message, helper.hello())
                elif operation in ("validate", "prepare", "release"):
                    if active:
                        raise StateError("helper cannot change runtimes during computation")
                    reply(message, getattr(helper, operation)(message))
                else:
                    raise StateError("unknown helper operation")
            except Exception as error:
                reply(message, error=error)
    finally:
        executor.shutdown(wait=True)
        helper.close()
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    directory = Path(config["data_directory"]).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "helper.lock").open("a") as process_lock:
        fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(directory)
        try:
            runtime = DockerRuntime(directory,
                binary_hosts=config.get("binary_hosts", DEFAULT_BINARY_HOSTS),
                nonce_timeout=config.get("nonce_timeout_seconds", 1800),
                resource_limits=config["runtime_limits"])
            if runtime.limits is None:
                raise StateError("helper requires hard runtime_limits")
            runtime.check_resources()
            serve(Helper(config, store, runtime))
        finally:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
