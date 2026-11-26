"""The service that answers `op` from the built-in store, and its client.

Real sockets and real subprocesses throughout: the service runs in a thread
of the test process (once as a process of its own) and every client is the
`op` program a caller would start.
"""

import grp
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
import threading
import time
from urllib.parse import urlencode

import pytest

import vault_op
import vault_op_client
import vault_page
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

    # The service writes its start line from its own thread after it signals
    # ready, so that one line may or may not fall inside this capture.
    start_line = f"serving the built-in store on {builtin_op.socket}"
    messages = [record.getMessage() for record in service_log(caplog)
                if record.getMessage() != start_line]
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


# -- the page ------------------------------------------------------------------
#
# The page's own socket, spoken to the way the front proxy does: HTTP/1.0,
# one request per connection, the answer read to the end of the stream.

INGRESS = "/api/hassio_ingress/abc"
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy":
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'self'; base-uri 'none'",
}


def own_group():
    return grp.getgrgid(os.getgid()).gr_name


def other_own_group():
    """A group this process is in besides its primary one, or its primary one.

    A new file gets the primary group anyway, so only another group shows
    that the service gave it."""
    for gid in os.getgroups():
        if gid != os.getgid():
            try:
                return grp.getgrgid(gid).gr_name
            except KeyError:
                continue
    return own_group()


def page_service(tmp_path, *, page_socket=None, page_group=None, **kwargs):
    return Service(tmp_path / "vault", tmp_path / "s",
                   page_socket=page_socket or tmp_path / "page" / "p.sock",
                   page_group=page_group or own_group(), **kwargs)


@pytest.fixture
def served(tmp_path):
    service = page_service(tmp_path)
    try:
        yield service
    finally:
        service.stop()


def exchange(socket_path, raw, *, close_sending=True):
    """Send `raw` and return the status line, the headers and the body of the answer."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(30)
        connection.connect(str(socket_path))
        connection.sendall(raw)
        if close_sending:
            connection.shutdown(socket.SHUT_WR)
        answer = b""
        while chunk := connection.recv(65536):
            answer += chunk
    head, _, body = answer.partition(b"\r\n\r\n")
    status, *lines = head.decode("latin-1").split("\r\n")
    return status, dict(line.split(": ", 1) for line in lines), body


def page_get(service, path, *, extra=""):
    return exchange(service.page_socket,
                    f"GET {path} HTTP/1.0\r\nX-Ingress-Path: {INGRESS}\r\n{extra}\r\n".encode())


def page_post(service, path, form):
    body = urlencode(form).encode()
    head = (f"POST {path} HTTP/1.0\r\nX-Ingress-Path: {INGRESS}\r\n"
            "X-Forwarded-Proto: https\r\nX-Forwarded-Host: ha.example\r\n"
            "Origin: https://ha.example\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\n"
            f"Content-Length: {len(body)}\r\n\r\n")
    return exchange(service.page_socket, head.encode() + body)


def hidden_inputs(service, get_path, action):
    """The hidden inputs (the code, a field's index) of the form posting to `action`."""
    _, _, body = page_get(service, get_path)
    form = re.search(rb'<form[^>]*action="' + re.escape(f"{INGRESS}/vault{action}".encode())
                     + rb'"[^>]*>(.*?)</form>', body, re.DOTALL)
    return {name.decode(): value.decode() for name, value in re.findall(
        rb'<input type="hidden" name="([a-z]+)" value="([^"]*)">', form.group(1))}


def add_on_page(service, name, label, value):
    form = {**hidden_inputs(service, "/add", "/add"),
            "name": name, "label0": label, "value0": value}
    status, headers, _ = page_post(service, "/add", form)
    assert status.startswith("HTTP/1.0 303"), status
    return headers["Location"].rsplit("/", 1)[1]


def wait_until_connectable(socket_path, seconds=10):
    deadline = time.monotonic() + seconds
    while True:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.connect(str(socket_path))
            return connection
        except (FileNotFoundError, ConnectionRefusedError):
            connection.close()
            assert time.monotonic() < deadline, f"nothing listens on {socket_path}"
            time.sleep(0.01)


def test_the_page_is_served_over_its_own_socket(tmp_path):
    service = page_service(tmp_path)
    try:
        assert ask(service, *CREATE_FROM_TEMPLATE,
                   stdin=template("stored by op", "hunter2"))["code"] == 0
        status, headers, body = page_get(service, "/")
        assert status.startswith("HTTP/1.0 200 ")
        for name, value in PAGE_HEADERS.items():
            assert headers[name] == value
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert headers["Content-Length"] == str(len(body))
        assert headers["Server"].strip() == "casa"  # no version of anything
        assert b"stored by op" in body and b"hunter2" not in body
    finally:
        service.stop()
    # Stopping ends the page's listener too.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as late:
        with pytest.raises(ConnectionRefusedError):
            late.connect(str(service.page_socket))


def test_the_page_socket_directory_and_socket_modes(tmp_path):
    directory = tmp_path / "page"
    directory.mkdir(mode=0o755)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(directory / "p.sock"))  # left by a run that did not end cleanly
    before = os.umask(0)
    os.umask(before)
    group = other_own_group()
    service = page_service(tmp_path, page_group=group)
    try:
        assert os.umask(before) == before  # the narrow umask was for the bind only
        held = os.stat(directory)
        assert stat.S_IMODE(held.st_mode) == 0o710
        mode = os.stat(service.page_socket)
        assert stat.S_ISSOCK(mode.st_mode) and stat.S_IMODE(mode.st_mode) == 0o660
        assert held.st_gid == mode.st_gid == grp.getgrnam(group).gr_gid
        assert page_get(service, "/")[0].startswith("HTTP/1.0 200")
        # The `op` socket keeps its own mode.
        assert stat.S_IMODE(os.stat(service.socket).st_mode) == 0o600
    finally:
        service.stop()


def test_the_op_socket_still_answers_while_the_page_is_served(served):
    # A page connection that has sent half a request holds only its own thread.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as half:
        half.connect(str(served.page_socket))
        half.sendall(b"GET / HTTP/1.0\r\nX-Ingr")
        assert ask(served, "--version") == {"code": 0, "stdout": "2.30.0\n", "stderr": ""}
        assert ask(served, *CREATE_FROM_TEMPLATE, stdin=template("both", "v"))["code"] == 0
        assert b"both" in page_get(served, "/")[2]


def test_a_secret_added_on_the_page_is_read_by_op(served, tmp_path):
    item_id = add_on_page(served, "added on the page", "token", "pagevalue42")
    install_op(tmp_path / "bin", served.socket)
    read = subprocess.run(
        [str(tmp_path / "bin" / "op"), "read", "op://Casa/added on the page/token"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert (read.returncode, read.stdout, read.stderr) == (0, "pagevalue42\n", "")
    [item] = served.items()
    assert (item.id, item.writer) == (item_id, "page")


def test_a_store_that_cannot_be_opened_still_serves_the_page_saying_so(tmp_path, caplog):
    directory = tmp_path / "vault"
    Store.open(directory, None).create(
        title="kept", category="PASSWORD", tags=[], writer="test",
        fields=[Field("", "password", "CONCEALED", "hunter2")])
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    caplog.set_level(logging.INFO, logger="vault_service")

    service = page_service(tmp_path, env_key="not-the-key")
    try:
        assert service.ready == b"\n"
        assert not service.socket.exists()
        status, headers, body = page_get(service, "/")
        assert status.startswith("HTTP/1.0 503")
        assert headers["Cache-Control"] == "no-store"
        assert b"kept" not in body and b"hunter2" not in body
        assert service.running()
    finally:
        service.stop()

    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    (error,) = [r for r in service_log(caplog) if r.levelno >= logging.ERROR]
    reason = error.getMessage().removeprefix("the built-in store is not served: ")
    assert reason.startswith("KeyProblem: ") and str(directory / DATABASE_NAME) in reason
    # The page says the secrets are unavailable; why is for the log, not the page.
    assert b"Secrets unavailable" in body
    assert b"KeyProblem" not in body and DATABASE_NAME.encode() not in body
    assert "not-the-key" not in caplog.text


def test_without_a_page_socket_nothing_changes(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="vault_service")
    service = Service(tmp_path / "vault", tmp_path / "s")
    try:
        assert service.ready == b"\n"
        assert ask(service, "--version")["code"] == 0
        assert sorted(path.name for path in tmp_path.iterdir()) == ["s", "vault"]
        assert not [t for t in threading.enumerate() if t.name == "secrets page"]
    finally:
        service.stop()
    assert [r.getMessage() for r in service_log(caplog)] == [
        f"serving the built-in store on {tmp_path / 's'}", "op --version: exit status 0"]


def test_the_log_names_no_secret(served, caplog):
    # Every logger at the level the service process logs at.
    caplog.set_level(logging.INFO)
    item_id = add_on_page(served, "logname7", "loglabel7", "logvalue7")
    assert page_get(served, "/")[0].startswith("HTTP/1.0 200")
    assert page_get(served, f"/s/{item_id}")[0].startswith("HTTP/1.0 200")
    show = hidden_inputs(served, f"/s/{item_id}", f"/s/{item_id}/show")
    status, _, body = page_post(served, f"/s/{item_id}/show", show)
    assert status.startswith("HTTP/1.0 200") and b"logvalue7" in body
    assert page_get(served, f"/s/{item_id}?logquery7=1")[0].startswith("HTTP/1.0 404")

    assert [r.getMessage() for r in service_log(caplog)
            if r.getMessage().startswith("page ")] == [
        "page GET add: 200",
        "page POST add: 303",
        "page GET list: 200",
        "page GET secret: 200",
        "page GET secret: 200",
        "page POST show: 200",
        "page GET other: 404",
    ]
    for leaked in ("logname7", "loglabel7", "logvalue7", "logquery7", item_id):
        assert leaked not in caplog.text


@pytest.mark.parametrize("raw, expected, reaches_page", [
    (b"POST /add HTTP/1.0\r\nX-Ingress-Path: /a\r\n\r\n", "411", False),
    (b"POST /add HTTP/1.0\r\nX-Ingress-Path: /a\r\nContent-Length: -1\r\n\r\n", "400", False),
    # `int` would take it as 5, and the five bytes would reach the page.
    (b"POST /add HTTP/1.0\r\nX-Ingress-Path: /a\r\nContent-Length: +5\r\n\r\na=bcd", "400",
     False),
    (b"POST /add HTTP/1.0\r\nX-Ingress-Path: /a\r\nContent-Length: 9\r\n\r\nshort", "400",
     False),
    # The page would answer 404 too; what counts is that it never sees the query.
    (b"GET /?q=REVIEW_VALUE HTTP/1.0\r\nX-Ingress-Path: /a\r\n\r\n", "404", False),
    (b"GET / HTTP/1.0\r\nX-Ingress-Path: /a\r\nX-Ingress-Path: \"\r\n\r\n", "200", True),
    (b"GET / HTTP/1.0\r\nX-Ingress-Path: \"\r\nX-Ingress-Path: /a\r\n\r\n", "403", True),
    (b"PUT / HTTP/1.0\r\nX-Ingress-Path: /a\r\n\r\n", "405", True),
    # One byte past the longest header line the listener reads, and nothing after it:
    # bytes left unread when the listener closes would reset the connection, and the
    # answer could be lost on its way to this client.
    (b"GET / HTTP/1.0\r\nX-Long: " + b"a" * (65537 - len(b"X-Long: ")), "431", False),
], ids=["no length", "length not a number", "length with a sign", "body shorter than its length",
        "query", "repeated header, first counts", "repeated header, first refused",
        "other method", "header too long"])
def test_what_the_listener_answers_itself_carries_the_page_headers(
        served, monkeypatch, raw, expected, reaches_page):
    seen = []
    real = vault_page.Page.handle

    def spy(page, request):
        seen.append(request)
        return real(page, request)

    monkeypatch.setattr(vault_page.Page, "handle", spy)
    status, headers, _ = exchange(served.page_socket, raw)
    assert status.split(" ")[1] == expected
    for name, value in PAGE_HEADERS.items():
        assert headers[name] == value
    assert len(seen) == (1 if reaches_page else 0)


def test_a_request_larger_than_the_limit_or_a_stalled_peer_does_not_hold_the_service(
        served, monkeypatch):
    # The same 70 seconds as the `op` socket; shortened here for the test.
    assert vault_service._PageConnection.timeout == 70
    monkeypatch.setattr(vault_service._PageConnection, "timeout", 0.5)
    # Announced too large, and the body never sent: the answer comes without it.
    head = (f"POST /add HTTP/1.0\r\nX-Ingress-Path: {INGRESS}\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\n"
            f"Content-Length: {vault_page.MAX_FORM + 1}\r\n\r\n")
    status, _, body = exchange(served.page_socket, head.encode(), close_sending=False)
    assert status.startswith("HTTP/1.0 413") and b"too large" in body

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as silent:
        silent.settimeout(30)
        silent.connect(str(served.page_socket))
        # Others are answered meanwhile, on both sockets.
        assert page_get(served, "/")[0].startswith("HTTP/1.0 200")
        assert ask(served, "--version")["code"] == 0
        started = time.monotonic()
        assert silent.recv(65536) == b""  # dropped, with no answer
        assert time.monotonic() - started < 10


def test_ready_is_signalled_only_once_the_page_socket_is_bound(tmp_path):
    # As for the `op` socket: with the pipe full, the service is held at the
    # moment it signals, and the page must already be listening then.
    read_end, write_end = os.pipe()
    os.set_blocking(write_end, False)
    filler = 0
    try:
        while True:
            filler += os.write(write_end, b"x" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(write_end, True)

    service = page_service(tmp_path, ready_fd=write_end)
    try:
        with wait_until_connectable(service.page_socket):
            assert read_to_end(read_end) == b"x" * filler + b"\n"
    finally:
        os.close(read_end)
        service.stop()


def test_stopping_waits_for_a_page_request_being_answered(served, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real = vault_page.Page.handle

    def slow(page, request):
        entered.set()
        release.wait(30)
        return real(page, request)

    monkeypatch.setattr(vault_page.Page, "handle", slow)
    answers = []
    client = threading.Thread(target=lambda: answers.append(page_get(served, "/")))
    client.start()
    assert entered.wait(30)
    stopper = threading.Thread(target=served.stop)
    stopper.start()
    time.sleep(0.5)
    assert served.running()  # held by the request being answered
    release.set()
    stopper.join(30)
    client.join(30)
    assert not served.running()
    assert answers[0][0].startswith("HTTP/1.0 200")


def test_the_service_process_serves_the_page_and_ends_on_sigterm(tmp_path):
    page_socket = tmp_path / "page" / "p.sock"
    read_end, write_end = os.pipe()
    process = subprocess.Popen(
        [sys.executable, str(SERVICE), "--dir", str(tmp_path / "vault"),
         "--socket", str(tmp_path / "s"), "--vault-name", "Casa", "--ready-fd", str(write_end),
         "--page-socket", str(page_socket), "--page-group", own_group()],
        pass_fds=(write_end,), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    os.close(write_end)
    try:
        assert read_to_end(read_end) == b"\n"
        status, _, _ = exchange(
            page_socket, f"GET / HTTP/1.0\r\nX-Ingress-Path: {INGRESS}\r\n\r\n".encode())
        assert status.startswith("HTTP/1.0 200")
        process.terminate()
        assert process.wait(timeout=30) == 0
    finally:
        os.close(read_end)
        if process.poll() is None:
            process.kill()
            process.wait()
    out, log = process.communicate()
    assert out == ""
    assert "page GET list: 200" in log
    assert "GET /" not in log  # nothing of the standard library's own lines


# -- a page that cannot start --------------------------------------------------

NO_SUCH_GROUP = "casa-test-no-such-group"


def not_a_member_group():
    """A group this process is not in, so it may not give a file to it."""
    mine = set(os.getgroups()) | {os.getgid()}
    return next((g.gr_name for g in grp.getgrall() if g.gr_gid not in mine), None)


def assert_not_listening(page_socket):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        with pytest.raises(OSError):
            connection.connect(str(page_socket))


def assert_op_answers(service, tmp_path):
    install_op(tmp_path / "bin", service.socket)
    op = str(tmp_path / "bin" / "op")
    created = subprocess.run([op, *CREATE_FROM_TEMPLATE], input=template("still here", "v4lue"),
                             capture_output=True, text=True, timeout=30)
    assert (created.returncode, created.stderr) == (0, "")
    read = subprocess.run([op, "read", "op://Casa/still here/password"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert (read.returncode, read.stdout) == (0, "v4lue\n")


def page_warnings(caplog):
    return [r.getMessage() for r in service_log(caplog) if r.levelno == logging.WARNING]


@pytest.mark.parametrize("case", ["unknown group", "a directory where the socket goes",
                                  "socket path too long", "group not mine"])
def test_a_page_that_cannot_start_costs_only_the_page(tmp_path, caplog, case):
    caplog.set_level(logging.INFO, logger="vault_service")
    page_socket, group, kind = tmp_path / "page" / "p.sock", own_group(), None
    if case == "unknown group":
        with pytest.raises(KeyError):
            grp.getgrnam(NO_SUCH_GROUP)
        group, kind = NO_SUCH_GROUP, "KeyError"
    elif case == "a directory where the socket goes":
        page_socket.mkdir(parents=True)
        kind = "IsADirectoryError"
    elif case == "socket path too long":
        page_socket = tmp_path / ("x" * 120) / "p.sock"
        kind = "OSError"
    else:
        if os.geteuid() == 0:
            pytest.skip("root may give a file to any group")
        group = not_a_member_group()
        if group is None:
            pytest.skip("no group this process is not in")
        kind = "PermissionError"

    service = page_service(tmp_path, page_socket=page_socket, page_group=group)
    try:
        assert service.ready == b"\n"
        assert_op_answers(service, tmp_path)
        assert service.running()
        if case != "a directory where the socket goes":
            assert not page_socket.exists()
        assert_not_listening(page_socket)
    finally:
        service.stop()
    assert page_warnings(caplog) == [f"the secrets page is not served ({kind})"]
    assert not any(r.getMessage().startswith("serving the secrets page")
                   for r in service_log(caplog))


def test_a_page_that_fails_once_bound_leaves_no_socket(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="vault_service")
    page_socket = tmp_path / "page" / "p.sock"
    real_chmod = os.chmod

    def chmod(path, mode, *args, **kwargs):
        if str(path) == str(page_socket):
            raise PermissionError("refused for the test")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(vault_service.os, "chmod", chmod)
    service = page_service(tmp_path)
    try:
        assert service.ready == b"\n"
        assert not page_socket.exists()
        assert_op_answers(service, tmp_path)
    finally:
        service.stop()
    assert page_warnings(caplog) == ["the secrets page is not served (PermissionError)"]


def test_a_store_that_cannot_be_opened_and_a_page_that_cannot_start_still_signal_ready(
        tmp_path, caplog):
    directory = tmp_path / "vault"
    Store.open(directory, None).create(
        title="kept", category="PASSWORD", tags=[], writer="test",
        fields=[Field("", "password", "CONCEALED", "hunter2")])
    caplog.set_level(logging.INFO, logger="vault_service")
    service = page_service(tmp_path, env_key="not-the-key", page_group=NO_SUCH_GROUP)
    try:
        assert service.ready == b"\n"
        assert not service.socket.exists() and not service.page_socket.exists()
        assert service.running()
    finally:
        service.stop()
    assert page_warnings(caplog) == ["the secrets page is not served (KeyError)"]


# -- a peer that closes once it has the answer ---------------------------------

def test_a_redirect_the_proxy_has_already_taken_is_not_logged_as_unanswered(
        served, caplog, monkeypatch):
    # nginx closes the connection as soon as it holds the whole answer; for a
    # redirect that is the headers alone. The race with the service's own
    # writes is narrow, so it is run many times.
    caplog.set_level(logging.INFO, logger="vault_service")
    monkeypatch.setattr(vault_page.Page, "handle", lambda page, request: vault_page.Response(
        303, (("Location", f"{INGRESS}/vault/"),), b""))
    times = 1000
    for _ in range(times):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as proxy:
            proxy.settimeout(30)
            proxy.connect(str(served.page_socket))
            proxy.sendall(f"GET / HTTP/1.0\r\nX-Ingress-Path: {INGRESS}\r\n\r\n".encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = proxy.recv(65536)
                assert chunk
                head += chunk
            assert head.startswith(b"HTTP/1.0 303 ")
    served.stop()
    records = service_log(caplog)
    assert [r for r in records if r.levelno >= logging.WARNING] == []
    assert [r.getMessage() for r in records].count("page GET list: 303") == times


def test_a_peer_gone_before_the_answer_is_written_is_still_logged(served, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="vault_service")
    gone = threading.Event()
    real = vault_page.Page.handle

    def after_the_peer_left(page, request):
        assert gone.wait(30)
        time.sleep(0.2)
        return real(page, request)

    monkeypatch.setattr(vault_page.Page, "handle", after_the_peer_left)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.connect(str(served.page_socket))
        peer.sendall(f"GET / HTTP/1.0\r\nX-Ingress-Path: {INGRESS}\r\n\r\n".encode())
    gone.set()
    served.stop()
    assert [r.getMessage() for r in service_log(caplog) if r.levelno == logging.WARNING] == [
        "page: a request was not answered (BrokenPipeError)"]
