"""A member master combines local compute and explicitly configured SSH helpers."""

import logging
from queue import Empty, Queue
from threading import Lock

from .ssh_transport import HelperError, SSHTransport, check_hello
from .state import StateError

log = logging.getLogger("innopool-v2-worker")


class RemoteRuntime:
    def __init__(self, config, *, master_id, resource, compute_type, nonce_timeout, transport=None):
        self.transport = transport or SSHTransport(config)
        self.workers = config["workers"]
        self.master_id, self.resource, self.compute_type = master_id, resource, compute_type
        self.nonce_timeout = nonce_timeout
        self.saved = None

    def check_resources(self):
        self.transport.start()
        hello = self.transport.call("hello", timeout=30)
        check_hello(hello, resource=self.resource, compute_type=self.compute_type,
                    workers=self.workers, master_id=self.master_id, nonce_timeout=self.nonce_timeout)

    def prepare(self, assignment, saved, offer):
        self.check_resources()
        response = self.transport.call("prepare", {
            "master_id": self.master_id, "offer": offer,
            "assignment_payload": saved["payload"], "assignment_digest": saved["digest"],
            "handed_over_at": saved["acknowledged_at"]}, timeout=420)
        if response != {"prepared": assignment["benchmark_id"], "assignment_digest": saved["digest"]}:
            raise HelperError("helper prepared a different assignment")
        self.saved = saved

    def validate(self, assignment, saved, offer):
        self.check_resources()
        response = self.transport.call("validate", {
            "master_id": self.master_id, "offer": offer,
            "assignment_payload": saved["payload"], "assignment_digest": saved["digest"]})
        if response != {"validated": assignment["benchmark_id"], "assignment_digest": saved["digest"]}:
            raise HelperError("helper validated a different assignment")

    def compute(self, assignment, nonce):
        if self.saved is None:
            raise HelperError("helper assignment is not prepared")
        response = self.transport.call("compute", {
            "benchmark_id": assignment["benchmark_id"], "assignment_digest": self.saved["digest"],
            "nonce": nonce}, timeout=2 * self.nonce_timeout + 60)
        if response.get("assignment_digest") != self.saved["digest"]:
            raise HelperError("helper returned output for another assignment")
        return response["leaf"], response["quality"]

    def release(self, assignment):
        if self.saved is None or not self.transport.alive:
            return  # EOF makes the helper save in-flight work and clean up its runtime.
        response = self.transport.call("release", {
            "benchmark_id": assignment["benchmark_id"], "assignment_digest": self.saved["digest"]})
        if response != {"released": assignment["benchmark_id"]}:
            raise HelperError("helper released another assignment")
        self.saved = None

    def close(self):
        self.transport.close()


class FleetRuntime:
    def __init__(self, local, store, *, local_workers, remotes, resource, compute_type):
        if type(local_workers) is not int or not 0 <= local_workers <= 4096:
            raise StateError("local workers must be between 0 and 4096")
        self.local, self.store = local, store
        self.resource, self.compute_type = resource, compute_type
        self.backends = ([local] if local_workers else []) + list(remotes)
        self.capacities = ([local_workers] if local_workers else []) + [remote.workers for remote in remotes]
        self.workers = sum(self.capacities)
        if not 1 <= self.workers <= 4096:
            raise StateError("aggregate member capacity must be between 1 and 4096")
        self.arch = local.arch if local_workers else None
        self.remotes = list(remotes)
        self.slots, self.lock = Queue(), Lock()
        self.available, self.completed = {}, {}

    def check_resources(self):
        for backend in self.backends:
            backend.check_resources()

    def before_offer(self):
        # Recheck every helper before promising its capacity for another benchmark.
        self.check_resources()
        return self.workers

    def validate(self, assignment, offer):
        if self.local in self.backends:
            self.local.validate(assignment, offer)
        if (assignment["compute_type"], offer["resource"]) != (self.compute_type, self.resource):
            raise StateError("fleet differs from the saved assignment's compute type")
        saved = self.store.assignment(assignment["benchmark_id"])
        handed_over = saved["acknowledged_at"]
        for number, remote in enumerate(self.remotes, 1):
            try:
                remote.validate(assignment, saved, offer)
            except HelperError as error:
                if not handed_over:
                    raise
                log.warning("helper %d cannot take benchmark %s after handover: %s", number, assignment["benchmark_id"], error)

    def prepare(self, assignment):
        saved = self.store.assignment(assignment["benchmark_id"])
        if not saved["acknowledged_at"]:
            raise StateError("master has no confirmed handover")
        offer = {"resource": self.resource, "compute_type": self.compute_type,
                 "capacity": {"workers": self.workers}}
        self.slots = Queue()
        errors = []
        for index, (backend, capacity) in enumerate(zip(self.backends, self.capacities)):
            try:
                if backend is self.local:
                    backend.prepare(assignment)
                else:
                    backend.prepare(assignment, saved, offer)
                self.available[index] = True
                for _ in range(capacity):
                    self.slots.put(index)
            except Exception as error:
                self.available[index] = False
                errors.append(error)
                log.warning("%s is unavailable for benchmark %s: %s", "local compute" if backend is self.local else "helper %d" % index,
                            assignment["benchmark_id"], error)
        if not any(self.available.values()):
            raise HelperError("no compatible member compute is available") from errors[-1]

    def compute(self, assignment, nonce):
        while True:
            try:
                index = self.slots.get(timeout=1)
            except Empty:
                with self.lock:
                    if not any(self.available.values()):
                        raise HelperError("all member compute disconnected")
                continue
            with self.lock:
                if self.available.get(index):
                    break
        try:
            result = self.backends[index].compute(assignment, nonce)
            with self.lock:
                self.completed[index] = self.completed.get(index, 0) + 1
            return result
        except HelperError as error:
            with self.lock:
                self.available[index] = False
            log.warning("helper %d dropped out during nonce %s of benchmark %s: %s", index, nonce, assignment["benchmark_id"], error)
            raise
        finally:
            with self.lock:
                if self.available.get(index):
                    self.slots.put(index)

    def release(self, assignment):
        errors = []
        for backend in self.backends:
            try:
                backend.release(assignment)
            except Exception as error:
                errors.append(error)
        if errors:
            raise errors[0]

    def close(self):
        for remote in self.remotes:
            remote.close()


def build_fleet(local, store, config):
    if not config.get("helpers"):
        return local, config.get("workers", 1)
    if not isinstance(config["helpers"], list) or not isinstance(config.get("master_id"), str) or not config["master_id"]:
        raise StateError("a member master requires helpers and a persistent master_id")
    if any(not isinstance(row, dict) for row in config["helpers"]):
        raise StateError("each helper requires its SSH configuration")
    targets = [(row.get("host"), row.get("config")) for row in config["helpers"]]
    if len(set(targets)) != len(targets):
        raise StateError("the same helper cannot contribute capacity twice")
    remotes = [RemoteRuntime(row, master_id=config["master_id"], resource=config["resource"],
               compute_type=config["compute_type"], nonce_timeout=config.get("nonce_timeout_seconds", 1800))
               for row in config["helpers"]]
    fleet = FleetRuntime(local, store, local_workers=config.get("workers", 1), remotes=remotes,
                         resource=config["resource"], compute_type=config["compute_type"])
    return fleet, fleet.workers
