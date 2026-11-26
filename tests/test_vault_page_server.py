"""The secrets page's listener, on its own: how a request is answered, and its directory.

The service's tests (`test_vault_service.py`) cover the listener as Casa
runs it. These cover what Casa does not use: answering through a given
function, and a directory made by somebody else.
"""

import logging
import os
import pathlib
import socket
import stat
import threading

import vault_page
import vault_page_server

LOGGER = logging.getLogger("test_vault_page_server")
BODY = b"<p>the page</p>"


class StandIn:
    """A page that records the thread each request is answered in."""

    def __init__(self, held=None):
        self.threads = []
        self.held = held

    def handle(self, request):
        self.threads.append(threading.current_thread())
        if self.held is not None:
            assert self.held.wait(30)
        return vault_page.Response(200, (("Content-Type", "text/html; charset=utf-8"),), BODY)


def get(socket_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(30)
        connection.connect(str(socket_path))
        connection.sendall(b"GET / HTTP/1.0\r\nX-Ingress-Path: /a\r\n\r\n")
        connection.shutdown(socket.SHUT_WR)
        answer = b""
        while chunk := connection.recv(65536):
            answer += chunk
    return answer


def test_run_answers_through_the_given_function(tmp_path):
    page = StandIn()
    workers, connections = [], []

    def run(answer):
        # Called in the connection's thread; the answer is computed in a
        # worker this test owns, and handed back.
        connections.append(threading.current_thread())
        result = []
        worker = threading.Thread(target=lambda: result.append(answer()), name="worker")
        workers.append(worker)
        worker.start()
        worker.join(30)
        return result[0]

    socket_path = tmp_path / "page" / "p.sock"
    with vault_page_server.listening(socket_path, None, page, run=run, logger=LOGGER):
        answer = get(socket_path)
    assert answer.startswith(b"HTTP/1.0 200 ") and answer.endswith(b"\r\n\r\n" + BODY)
    assert page.threads == workers
    assert connections and connections[0] is not workers[0]


def test_a_prepared_directory_is_left_as_found(tmp_path, monkeypatch):
    directory = tmp_path / "page"
    directory.mkdir()
    os.chmod(directory, 0o2750)
    socket_path = directory / "p.sock"
    calls = []
    real_mkdir, real_path_mkdir = os.mkdir, pathlib.Path.mkdir
    real_chmod, real_chown = os.chmod, os.chown

    def recorded(name, real):
        def wrapper(path, *args, **kwargs):
            calls.append((name, str(path)))
            return real(path, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(os, "mkdir", recorded("mkdir", real_mkdir))
    monkeypatch.setattr(pathlib.Path, "mkdir", recorded("mkdir", real_path_mkdir))
    monkeypatch.setattr(os, "chmod", recorded("chmod", real_chmod))
    monkeypatch.setattr(os, "chown", recorded("chown", real_chown))

    with vault_page_server.listening(socket_path, None, StandIn(), prepared=True,
                                     logger=LOGGER):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o2750
        mode = os.stat(socket_path).st_mode
        assert stat.S_ISSOCK(mode) and stat.S_IMODE(mode) == 0o660
        assert get(socket_path).endswith(BODY)
    assert [call for call in calls if call[1] == str(directory)] == []
    assert ("chown", str(socket_path)) not in calls
    assert ("chmod", str(socket_path)) in calls


def test_without_a_group_the_directory_is_still_made(tmp_path):
    directory = tmp_path / "page"
    socket_path = directory / "p.sock"
    assert not directory.exists()
    with vault_page_server.listening(socket_path, None, StandIn(), logger=LOGGER):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o710
        mode = os.stat(socket_path).st_mode
        assert stat.S_ISSOCK(mode) and stat.S_IMODE(mode) == 0o660
        answer = get(socket_path)
    assert answer.startswith(b"HTTP/1.0 200 ") and answer.endswith(BODY)


def test_leaving_the_block_waits_for_a_request_held_in_run(tmp_path, monkeypatch):
    held, entered = threading.Event(), threading.Event()
    writing, write_released = threading.Event(), threading.Event()

    def run(answer):
        entered.set()
        assert held.wait(30)
        return answer()

    real_write = vault_page_server._PageConnection._write

    def slow_write(connection, response):
        writing.set()
        assert write_released.wait(30)
        return real_write(connection, response)

    monkeypatch.setattr(vault_page_server._PageConnection, "_write", slow_write)
    socket_path = tmp_path / "page" / "p.sock"
    answers = []
    client = threading.Thread(target=lambda: answers.append(get(socket_path)))
    listener = vault_page_server.listening(socket_path, None, StandIn(), run=run, logger=LOGGER)
    listener.__enter__()
    left = threading.Event()

    def leave():
        listener.__exit__(None, None, None)
        left.set()

    leaver = threading.Thread(target=leave)
    try:
        client.start()
        assert entered.wait(30)
        leaver.start()
        assert not left.wait(0.5)  # held by the request inside `run`
        held.set()
        assert writing.wait(30)
        assert not left.wait(0.5)  # held by the answer being written
        write_released.set()
        leaver.join(30)
        client.join(30)
        assert left.is_set() and not client.is_alive()
    finally:
        held.set()
        write_released.set()
        if not leaver.is_alive() and not left.is_set():
            listener.__exit__(None, None, None)
    assert answers and answers[0].startswith(b"HTTP/1.0 200 ") and answers[0].endswith(
        b"\r\n\r\n" + BODY)
