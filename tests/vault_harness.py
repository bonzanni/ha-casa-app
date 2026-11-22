"""The built-in secrets store as a caller meets it: a running service and an `op`.

`builtin_op` starts the real service on a socket in the test's temporary
directory and puts an `op` program first on `PATH` that hands its arguments
to the real client. A test then runs Casa's own calling code unchanged: that
code starts `op` as a subprocess and gets its answers from the store.

The `op` program runs the client the way the installed one is run: by file
path, in isolated mode. It also leaves the site directories out, so a client
that imported anything but the standard library would fail to start here.
"""
from __future__ import annotations

import json
import os
import select
import shlex
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import vault_service
from vault_store import Item, Store

VAULT = "Casa"
# What `OP_SERVICE_ACCOUNT_TOKEN` holds on an install that keeps its secrets
# in the built-in store: the callers that ask "is there a token" see one.
MARKER = "casa-builtin-store"
CODE_ROOT = Path(vault_service.__file__).resolve().parent
CLIENT = CODE_ROOT / "vault_op_client.py"
SERVICE = CODE_ROOT / "vault_service.py"

_OP = """\
import importlib.util
import sys

with open({calls!r}, "a") as calls:
    calls.write(" ".join(sys.argv[1:3]) + "\\n")
spec = importlib.util.spec_from_file_location("vault_op_client", {client!r})
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
sys.exit(client.main(sys.argv[1:], {socket!r}))
"""


class Service:
    """`vault_service.serve` in a thread of this process, until `stop()`."""

    def __init__(self, store_dir: Path, socket_path: Path, *, env_key: str | None = None,
                 ready_fd: int | None = None) -> None:
        # The kernel takes a socket path of about a hundred bytes and no more.
        assert len(os.fsencode(socket_path)) <= 100, f"socket path too long: {socket_path}"
        self.store_dir = store_dir
        self.socket = socket_path
        self.calls_file: Path | None = None
        self.ready: bytes | None = None  # what the service wrote to say it is ready
        self._stop = threading.Event()
        waits_here = ready_fd is None
        if waits_here:
            read_end, ready_fd = os.pipe()
        self._thread = threading.Thread(
            target=vault_service.serve,
            args=(store_dir, socket_path, VAULT, env_key, ready_fd),
            kwargs={"stop": self._stop}, daemon=True)
        self._thread.start()
        if waits_here:
            try:
                readable, _, _ = select.select([read_end], [], [], 30)
                assert readable, "the service did not signal that it is ready"
                self.ready = os.read(read_end, 16)
            finally:
                os.close(read_end)

    def running(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(30)
        assert not self._thread.is_alive(), "the service did not stop"

    def items(self) -> list[Item]:
        """What the store's file holds now, read the way a new process would."""
        return Store.open(self.store_dir, None).list()

    def calls(self) -> list[str]:
        """The first two arguments of every `op` that was started, in order."""
        if self.calls_file is None or not self.calls_file.exists():
            return []
        return self.calls_file.read_text().splitlines()


def install_op(bin_dir: Path, socket_path: Path) -> Path:
    """Write an executable `op` into `bin_dir` that asks the service at `socket_path`.

    Returns the file each run of it adds a line to.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls = bin_dir / "calls"
    program = bin_dir / "op.py"
    program.write_text(_OP.format(calls=str(calls), client=str(CLIENT),
                                  socket=str(socket_path)))
    launcher = bin_dir / "op"
    launcher.write_text("#!/bin/sh\nexec {} -I -S {} \"$@\"\n".format(
        shlex.quote(sys.executable), shlex.quote(str(program))))
    launcher.chmod(0o755)
    return calls


def run_op(*args: str, input: str | None = None) -> subprocess.CompletedProcess:
    """Run the `op` on `PATH` as Casa's callers do: text in, text out."""
    return subprocess.run(["op", *args], input=input, capture_output=True, text=True,
                          timeout=60,
                          **({} if input is not None else {"stdin": subprocess.DEVNULL}))


def request(socket_path: Path, payload: bytes) -> dict:
    """Send `payload` as one request, without the client, and return the answer."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(30)
        connection.connect(str(socket_path))
        connection.sendall(payload)
        connection.shutdown(socket.SHUT_WR)
        answer = b""
        while chunk := connection.recv(65536):
            answer += chunk
    return json.loads(answer)


@pytest.fixture
def builtin_op(tmp_path, monkeypatch):
    """A running store, with the environment an install that uses it has."""
    service = Service(tmp_path / "vault", tmp_path / "s")
    try:
        service.calls_file = install_op(tmp_path / "bin", service.socket)
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", MARKER)
        monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", VAULT)
        yield service
    finally:
        service.stop()
