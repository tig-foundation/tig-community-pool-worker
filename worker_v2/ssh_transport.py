"""Bounded JSON RPC over a member's pinned SSH connection."""

from collections import deque
from concurrent.futures import Future, TimeoutError
import json
import os
from pathlib import PurePosixPath
import re
import shlex
import subprocess
from threading import Lock, Thread
import uuid

from .helper import MAX_MESSAGE, WIRE_VERSION
from .state import StateError


class HelperError(StateError):
    pass


def ssh_command(config):
    """No shell on the master; quote each remote argument and verify host keys."""
    required = {"host", "user", "identity_file", "known_hosts_file", "python", "code_directory", "config", "slice", "workers"}
    if not isinstance(config, dict) or set(config) != required:
        raise StateError("SSH helper configuration has missing or unknown fields")
    for key, pattern in (("host", r"[A-Za-z0-9][A-Za-z0-9._:-]{0,252}"),
                         ("user", r"[a-z_][a-z0-9_-]{0,31}"),
                         ("slice", r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.slice")):
        if not isinstance(config[key], str) or not re.fullmatch(pattern, config[key]):
            raise StateError("invalid SSH helper " + key)
    for key in ("identity_file", "known_hosts_file", "python", "code_directory", "config"):
        value = config[key]
        if not isinstance(value, str) or not value.startswith("/") or "\x00" in value or "\n" in value or ".." in PurePosixPath(value).parts:
            raise StateError("SSH helper paths must be absolute and contain no traversal")
    if type(config["workers"]) is not int or not 1 <= config["workers"] <= 4096:
        raise StateError("SSH helper workers must be between 1 and 4096")
    unit = "innopool-member-helper-" + uuid.uuid4().hex
    arguments = ["systemd-run", "--quiet", "--scope", "--slice=" + config["slice"],
                 "--unit=" + unit, config["python"], "-m", "worker_v2.helper", "--config", config["config"]]
    remote = "cd " + shlex.quote(config["code_directory"]) + " && exec " + shlex.join(arguments)
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            "-o", "UserKnownHostsFile=" + config["known_hosts_file"],
            "-i", config["identity_file"], config["user"] + "@" + config["host"], remote]


class SSHTransport:
    def __init__(self, config, *, popen=subprocess.Popen):
        self.arguments = ssh_command(config)
        self.popen = popen
        self.process = None
        self.pending = {}
        self.lock, self.writer_lock = Lock(), Lock()
        self.errors = deque(maxlen=8)
        self.failure = None

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None and self.failure is None

    def start(self):
        if self.alive:
            return
        self.close()
        self.failure = None
        self.errors.clear()
        environment = os.environ.copy()
        # SSH configurations may forward environment variables. The member's
        # pool credential must stay in the uploader process.
        environment.pop("POOL_V2_EXECUTION_TOKEN", None)
        self.process = self.popen(self.arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
                                  env=environment)
        process = self.process

        def read_errors():
            for line in process.stderr:
                self.errors.append(line.rstrip()[:1024])

        def read_replies():
            try:
                while True:
                    line = process.stdout.readline(MAX_MESSAGE + 1)
                    if not line:
                        raise HelperError("helper SSH session ended")
                    if len(line.encode()) > MAX_MESSAGE or not line.endswith("\n"):
                        raise HelperError("helper response exceeds the message limit")
                    response = json.loads(line)
                    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                        raise HelperError("invalid helper response")
                    if ("result" if response["ok"] else "error") not in response:
                        raise HelperError("incomplete helper response")
                    with self.lock:
                        future = self.pending.pop(response.get("id"), None)
                    if future is None:
                        raise HelperError("unexpected helper response identifier")
                    if response["ok"]:
                        future.set_result(response["result"])
                    else:
                        future.set_exception(HelperError(str(response["error"])))
            except Exception as error:
                if self.process is process:
                    self._fail(error)

        self.reader = Thread(target=read_replies, daemon=True)
        self.error_reader = Thread(target=read_errors, daemon=True)
        self.reader.start()
        self.error_reader.start()

    def _fail(self, error):
        with self.lock:
            self.failure = error
            pending, self.pending = self.pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(HelperError(str(error)))

    def call(self, operation, payload=None, *, timeout=60):
        if not self.alive:
            raise HelperError("helper SSH session is unavailable")
        identity = uuid.uuid4().hex
        message = {**(payload or {}), "id": identity, "op": operation}
        encoded = json.dumps(message, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > MAX_MESSAGE:
            raise HelperError("helper request exceeds the message limit")
        future = Future()
        with self.lock:
            if self.failure is not None:
                raise HelperError("helper SSH session is unavailable")
            self.pending[identity] = future
        try:
            with self.writer_lock:
                self.process.stdin.write(encoded + "\n")
                self.process.stdin.flush()
        except (OSError, ValueError) as error:
            self._fail(error)
            raise HelperError("cannot send a helper request") from error
        try:
            return future.result(timeout=timeout)
        except TimeoutError as error:
            self._fail(HelperError("helper " + operation + " timed out"))
            self.close()
            raise HelperError("helper " + operation + " timed out") from error

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        self._fail(HelperError("helper SSH session closed"))
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for name in ("reader", "error_reader"):
            thread = getattr(self, name, None)
            if thread:
                thread.join(timeout=5)
        for stream in (process.stdout, process.stderr):
            stream.close()


def check_hello(hello, *, resource, compute_type, workers, master_id, nonce_timeout=1800):
    from .runtime import ARM_TYPES
    expected_arch = "arm64" if compute_type in ARM_TYPES else "amd64"
    expected = {"wire_version": WIRE_VERSION, "arch": expected_arch,
                "resource": resource, "compute_type": compute_type,
                "master_id": master_id, "nonce_timeout_seconds": nonce_timeout}
    if not isinstance(hello, dict) or any(hello.get(key) != value for key, value in expected.items()):
        raise StateError("helper wire version, architecture, compute type, timeout or master identity does not match")
    if type(hello.get("workers")) is not int or hello["workers"] < workers:
        raise StateError("helper has less capacity than the configured offer")
