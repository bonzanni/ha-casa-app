"""The service that answers `op` from the built-in store, and its client.

Real sockets and real subprocesses throughout: the service runs in a thread
of the test process (once as a process of its own) and every client is the
`op` program a caller would start.
"""

import json
import logging
import os
import re
import resource
import select
import socket
import stat
import subprocess
import sys
import time

import pytest

import vault_op
import vault_op_client
import vault_service
from test_vault_op import WANTS_STDIN
from vault_harness import (
    SERVICE, Service, builtin_op, install_op, request, run_op,  # noqa: F401
)
from vault_store import DATABASE_NAME, KEY_NAME, Field, Store, StoreError

UNREACHABLE = re.compile(
    r"\[ERROR\] \d{4}/\d\d/\d\d \d\d:\d\d:\d\d built-in store unreachable\n")
ERROR_LINE = re.compile(r"\[ERROR\] \d{4}/\d\d/\d\d \d\d:\d\d:\d\d [^\n]+\n")
CREATE_FROM_TEMPLATE = ["item", "create", "--vault", "Casa", "--format", "json", "-"]


def template(title, value):
    return json.dumps({
        "title": title, "category": "PASSWORD", "tags": ["test"],
        "fields": [{"id": "password", "type": "CONCEALED", "purpose": "PASSWORD",
                    "label": "password", "value": value}]})


def ask(service, *args, stdin=None):
    return request(service.socket, json.dumps({"args": list(args), "stdin": stdin}).encode())


def read_to_end(descriptor, seconds=30):
    """Everything written to a pipe until its writers have all closed it."""
    data = b""
    deadline = time.monotonic() + seconds
    while True:
        readable, _, _ = select.select([descriptor], [], [], max(deadline - time.monotonic(), 0))
        assert readable, "the pipe was not closed"
        chunk = os.read(descriptor, 65536)
        if not chunk:
            return data
        data += chunk


def service_log(caplog):
    return [record for record in caplog.records if record.name == "vault_service"]


# -- the wire ------------------------------------------------------------------

def test_roundtrip_over_the_socket(builtin_op):
    created = ask(builtin_op, *CREATE_FROM_TEMPLATE, stdin=template("over the socket", "hunter2"))
    assert set(created) == {"code", "stdout", "stderr"}
    assert (created["code"], created["stderr"]) == (0, "")
    assert json.loads(created["stdout"])["title"] == "over the socket"

    assert ask(builtin_op, "read", "op://Casa/over the socket/password") == {
        "code": 0, "stdout": "hunter2\n", "stderr": ""}

    missing = ask(builtin_op, "read", "op://Casa/nothing/password")
    assert (missing["code"], missing["stdout"]) == (1, "")
    assert ERROR_LINE.fullmatch(missing["stderr"]) and "isn't an item" in missing["stderr"]

    assert [item.title for item in builtin_op.items()] == ["over the socket"]


@pytest.mark.parametrize("payload", [
    b"not json",
    b"",
    b"[]",
    b'{"args": "item list", "stdin": null}',
    b'{"args": ["--version"], "stdin": 5}',
    b'{"args": ["--version"]}',
    b"\xff\xfe",
], ids=["text", "empty", "not an object", "args not a list", "stdin not text", "no stdin",
        "not UTF-8"])
def test_what_is_not_a_request_is_refused(builtin_op, payload):
    answer = request(builtin_op.socket, payload)
    assert (answer["code"], answer["stdout"]) == (1, "")
    assert ERROR_LINE.fullmatch(answer["stderr"])
    assert ask(builtin_op, "--version")["stdout"] == "2.30.0\n"


def test_oversized_request_is_refused(builtin_op):
    huge = template("too big", "v" * (1024 * 1024))
    refused = run_op(*CREATE_FROM_TEMPLATE, input=huge)
    assert (refused.returncode, refused.stdout) == (1, "")
    assert ERROR_LINE.fullmatch(refused.stderr) and "1 MiB" in refused.stderr
    assert builtin_op.items() == []

    # Just under the limit is an ordinary request, and the service carries on.
    fits = template("fits", "v" * (1024 * 1024 - 4096))
    assert run_op(*CREATE_FROM_TEMPLATE, input=fits).returncode == 0
    assert [item.title for item in builtin_op.items()] == ["fits"]


def test_a_peer_that_never_finishes_sending_is_dropped(builtin_op, monkeypatch, caplog):
    # Longer than the client's own 60 seconds; shortened here for the test.
    assert vault_service._Connection.timeout == 70
    monkeypatch.setattr(vault_service._Connection, "timeout", 0.5)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as silent:
        silent.settimeout(30)
        silent.connect(str(builtin_op.socket))
        silent.sendall(b'{"args": ["--version"], ')  # and never the rest, nor the end
        # Another client is answered meanwhile.
        assert ask(builtin_op, "--version")["stdout"] == "2.30.0\n"
        started = time.monotonic()
        assert silent.recv(65536) == b""  # closed, with no answer
        assert time.monotonic() - started < 10
    assert "a request was not answered (TimeoutError)" in [
        record.getMessage() for record in service_log(caplog)]


# -- starting ------------------------------------------------------------------

def test_socket_is_0600(tmp_path):
    before = os.umask(0)
    os.umask(before)
    service = Service(tmp_path / "vault", tmp_path / "s")
    try:
        mode = os.stat(service.socket).st_mode
        assert stat.S_ISSOCK(mode) and stat.S_IMODE(mode) == 0o600
        # The narrow umask is for the bind only; the process gets its own back.
        after = os.umask(before)
        assert after == before
    finally:
        service.stop()


def test_a_socket_file_left_behind_is_replaced(tmp_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(tmp_path / "s"))
    service = Service(tmp_path / "vault", tmp_path / "s")
    try:
        assert ask(service, "--version") == {"code": 0, "stdout": "2.30.0\n", "stderr": ""}
    finally:
        service.stop()


def test_ready_fd_gets_one_newline_after_bind(tmp_path):
    read_end, write_end = os.pipe()
    # The pipe is filled first, so the service's newline has to wait until
    # this test reads: the service is then held at the moment it signals,
    # and what it has done by that moment can be looked at.
    os.set_blocking(write_end, False)
    filler = 0
    try:
        while True:
            filler += os.write(write_end, b"x" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(write_end, True)

    service = Service(tmp_path / "vault", tmp_path / "s", ready_fd=write_end)
    try:
        deadline = time.monotonic() + 30
        while True:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connection.connect(str(service.socket))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                connection.close()
                assert time.monotonic() < deadline, \
                    "nothing listens while the service waits to signal"
                time.sleep(0.01)
        with connection:
            # Exactly one newline, and the descriptor closed after it.
            assert read_to_end(read_end) == b"x" * filler + b"\n"
            # The connection made before the signal is answered.
            connection.settimeout(30)
            connection.sendall(json.dumps({"args": ["--version"], "stdin": None}).encode())
            connection.shutdown(socket.SHUT_WR)
            assert json.loads(connection.recv(65536))["stdout"] == "2.30.0\n"
    finally:
        os.close(read_end)
        service.stop()


def test_a_ready_fd_that_is_not_open_is_logged_and_the_service_serves(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="vault_service")
    # A descriptor number at the limit can never be an open one.
    not_open = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    service = Service(tmp_path / "vault", tmp_path / "s", ready_fd=not_open)
    try:
        deadline = time.monotonic() + 30
        while not service.socket.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert ask(service, "--version")["code"] == 0
    finally:
        service.stop()
    warnings = [r for r in service_log(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "readiness" in warnings[0].getMessage()


@pytest.mark.parametrize("problem", ["a key that does not open it", "no key at all"])
def test_wrong_key_creates_no_socket_signals_ready_and_stays_up(tmp_path, caplog, problem):
    directory = tmp_path / "vault"
    Store.open(directory, None).create(
        title="kept", category="PASSWORD", tags=[], writer="test",
        fields=[Field("", "password", "CONCEALED", "hunter2")])
    env_key = None
    if problem == "no key at all":
        (directory / KEY_NAME).unlink()
    else:
        env_key = "not-the-key"
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    caplog.set_level(logging.INFO, logger="vault_service")

    service = Service(directory, tmp_path / "s", env_key=env_key)
    try:
        assert service.ready == b"\n"
        assert not service.socket.exists()
        install_op(tmp_path / "bin", service.socket)
        failed = subprocess.run([str(tmp_path / "bin" / "op"), "read", "op://Casa/kept/password"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=30)
        assert (failed.returncode, failed.stdout) == (1, "")
        assert UNREACHABLE.fullmatch(failed.stderr)
        assert service.running()
    finally:
        service.stop()

    # Nothing was created, replaced or removed: the file may be somebody's secrets.
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert before[DATABASE_NAME]
    errors = [r for r in service_log(caplog) if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and str(directory / DATABASE_NAME) in errors[0].getMessage()
    assert "not-the-key" not in caplog.text


def test_a_damaged_store_file_creates_no_socket_signals_ready_and_stays_up(tmp_path, caplog):
    directory = tmp_path / "vault"
    Store.open(directory, None).create(
        title="kept", category="PASSWORD", tags=[], writer="test",
        fields=[Field("", "password", "CONCEALED", "hunter2")])
    database = directory / DATABASE_NAME
    whole = database.read_bytes()
    database.write_bytes(whole[:len(whole) // 2])
    with pytest.raises(Exception) as cannot_open:
        Store.open(directory, None)
    assert not isinstance(cannot_open.value, StoreError)   # not the key: the file itself
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    caplog.set_level(logging.INFO, logger="vault_service")

    service = Service(directory, tmp_path / "s")
    try:
        assert service.ready == b"\n"
        assert not service.socket.exists()
        install_op(tmp_path / "bin", service.socket)
        failed = subprocess.run([str(tmp_path / "bin" / "op"), "read", "op://Casa/kept/password"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=30)
        assert (failed.returncode, failed.stdout) == (1, "")
        assert UNREACHABLE.fullmatch(failed.stderr)
        assert service.running()
    finally:
        service.stop()

    # The damaged file is left exactly as it is, for whoever comes to mend it.
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    # One line that names the kind of failure. Not its message, which a
    # library wrote and which may quote what it read, and no traceback.
    (line,) = service_log(caplog)
    assert line.levelno == logging.ERROR and line.exc_info is None
    assert line.getMessage() == (
        f"the built-in store is not served: {type(cannot_open.value).__name__}")
    assert "Traceback" not in caplog.text


def test_the_service_process_takes_its_key_from_the_environment_and_ends_on_sigterm(tmp_path):
    directory, socket_path = tmp_path / "vault", tmp_path / "s"
    read_end, write_end = os.pipe()
    process = subprocess.Popen(
        [sys.executable, str(SERVICE), "--dir", str(directory), "--socket", str(socket_path),
         "--vault-name", "Casa", "--ready-fd", str(write_end)],
        pass_fds=(write_end,), env={**os.environ, "CASA_VAULT_KEY": "a-key-from-outside"},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    os.close(write_end)
    try:
        assert read_to_end(read_end) == b"\n"
        install_op(tmp_path / "bin", socket_path)
        created = subprocess.run(
            [str(tmp_path / "bin" / "op"), *CREATE_FROM_TEMPLATE],
            input=template("from a process", "s3cr3t-value"), capture_output=True, text=True,
            timeout=30)
        assert (created.returncode, created.stderr) == (0, "")
        process.terminate()
        assert process.wait(timeout=30) == 0
    finally:
        os.close(read_end)
        if process.poll() is None:
            process.kill()
            process.wait()
    out, log = process.communicate()
    assert out == ""
    assert "op item create: exit status 0" in log
    assert "s3cr3t-value" not in log and "a-key-from-outside" not in log

    # The key came from outside: none was written, and that key opens the file.
    assert not (directory / KEY_NAME).exists()
    assert [item.title for item in Store.open(directory, "a-key-from-outside").list()] == [
        "from a process"]


# -- the client ----------------------------------------------------------------

def test_client_does_not_wait_on_an_open_stdin(builtin_op):
    # A caller that leaves standard input alone hands the client whatever it
    # has itself: here a pipe nobody writes to or closes.
    client = subprocess.Popen(["op", "item", "list", "--vault", "Casa", "--format", "json"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        assert client.wait(timeout=5) == 0
        assert (client.stdout.read(), client.stderr.read()) == ("[]\n", "")
    finally:
        if client.poll() is None:
            client.kill()
            client.wait()
        for pipe in (client.stdin, client.stdout, client.stderr):
            pipe.close()


def test_client_reads_the_template_when_the_form_has_one(builtin_op):
    created = run_op(*CREATE_FROM_TEMPLATE, input=template("from a template", "first"))
    assert (created.returncode, created.stderr) == (0, "")
    item_id = json.loads(created.stdout)["id"]

    edited = run_op("item", "edit", item_id, "--vault", "Casa", "--format", "json",
                    input=template("from a template", "sec\u00f6nd"))
    assert (edited.returncode, edited.stderr) == (0, "")
    assert json.loads(edited.stdout)["id"] == item_id

    read = run_op("read", "op://Casa/from a template/password")
    assert (read.returncode, read.stdout, read.stderr) == (0, "sec\u00f6nd\n", "")
    assert len(builtin_op.items()) == 1


def test_client_with_standard_input_closed_fails_with_one_line(builtin_op):
    # `<&-` closes descriptor 0 before the client starts.
    failed = subprocess.run(["sh", "-c", 'exec op "$@" <&-', "sh", *CREATE_FROM_TEMPLATE],
                            capture_output=True, text=True, timeout=30)
    assert (failed.returncode, failed.stdout) == (1, "")
    assert ERROR_LINE.fullmatch(failed.stderr)
    assert "standard input could not be read" in failed.stderr
    assert builtin_op.items() == []


@pytest.mark.parametrize("args, expected", WANTS_STDIN,
                         ids=lambda value: " ".join(value) if isinstance(value, list) else None)
def test_wants_stdin_rules_agree(args, expected):
    assert vault_op_client.wants_stdin(args) is vault_op.wants_stdin(args) is expected


@pytest.mark.parametrize("left_behind", [False, True], ids=["no socket", "a socket nobody serves"])
def test_client_without_a_service_fails_with_one_line(tmp_path, left_behind):
    socket_path = tmp_path / "s"
    if left_behind:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(socket_path))
    install_op(tmp_path / "bin", socket_path)
    started = time.monotonic()
    failed = subprocess.run([str(tmp_path / "bin" / "op"), *CREATE_FROM_TEMPLATE],
                            input=template("nowhere", "s3cr3t-value"), capture_output=True,
                            text=True, timeout=30)
    assert (failed.returncode, failed.stdout) == (1, "")
    assert UNREACHABLE.fullmatch(failed.stderr)
    assert time.monotonic() - started < 10


def test_client_gives_up_on_a_service_that_does_not_answer(tmp_path, monkeypatch, capfd):
    monkeypatch.setattr(vault_op_client, "_TIMEOUT_S", 0.3)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as silent:
        silent.bind(str(tmp_path / "s"))
        silent.listen()
        started = time.monotonic()
        assert vault_op_client.main(["--version"], str(tmp_path / "s")) == 1
        assert time.monotonic() - started < 5
    out, err = capfd.readouterr()
    assert out == "" and UNREACHABLE.fullmatch(err)


def test_twenty_parallel_clients_create_twenty_items(builtin_op):
    clients = [
        subprocess.Popen(
            ["op", "item", "create", "--category", "API Credential", "--title", f"item {n}",
             "--vault", "Casa", "--tags", "parallel", f"token[password]=value-{n}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for n in range(20)]
    try:
        results = [(client.communicate(timeout=120)[1], client.returncode) for client in clients]
    finally:
        for client in clients:
            if client.poll() is None:
                client.kill()
                client.wait()
    assert results == [("", 0)] * 20

    items = builtin_op.items()
    assert {item.title: [field.value for field in item.fields] for item in items} == {
        f"item {n}": [f"value-{n}"] for n in range(20)}
    assert len({item.id for item in items}) == 20


# -- the log -------------------------------------------------------------------

def test_request_log_has_no_argument_values_and_no_template(builtin_op, caplog):
    # Every logger at the level the service process logs at, not only the
    # service's own: a library that logged a value would put it in the same log.
    caplog.set_level(logging.INFO)
    created = run_op(*CREATE_FROM_TEMPLATE, input=template("logged item", "t3mplate-value"))
    assert created.returncode == 0
    item_id = json.loads(created.stdout)["id"]
    assert run_op("item", "edit", item_id, "--vault", "Casa",
                  "api token[password]=s3cr3t-value").returncode == 0
    assert run_op("read", "op://Casa/logged item/api token").stdout == "s3cr3t-value\n"
    # Not a command the store answers, with a value where a word is expected.
    assert run_op("read", "s3cr3tword").returncode == 1
    assert run_op("s3cr3tword").returncode == 1
    assert run_op("item", "get", "logged item", "--vault", "Elsewhere",
                  "--format", "json").returncode == 1

    messages = [record.getMessage() for record in service_log(caplog)]
    assert messages == [
        "op item create: exit status 0",
        "op item edit: exit status 0",
        "op read: exit status 0",
        "op read: exit status 1",
        "op (other): exit status 1",
        "op item get: exit status 1",
    ]
    for leaked in ("s3cr3t-value", "t3mplate-value", "s3cr3tword", "logged item", "api token",
                   item_id, "Elsewhere"):
        assert leaked not in caplog.text
