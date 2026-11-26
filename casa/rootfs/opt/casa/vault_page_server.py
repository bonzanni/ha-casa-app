"""The secrets page served on a UNIX socket, for a front proxy (nginx).

Plain HTTP/1.0, one request per connection, each answered in a thread of its
own by a `vault_page.Page`. A body is read only up to the page's own limit.
The socket is open to its owner and one group, the proxy's, and to nobody
else.

The log names the method, the route taken and the status of each request,
never the path, which carries a secret's id.
"""
from __future__ import annotations

import contextlib
import grp
import http.server
import logging
import os
import socketserver
import sys
import threading
from collections.abc import Callable, Iterator
from http import HTTPStatus
from pathlib import Path

if __package__:
    from . import vault_page
else:
    import vault_page

# How often the serving thread looks whether it was asked to stop.
_STOP_CHECK_S = 0.2


class _PageServer(socketserver.ThreadingUnixStreamServer):
    """One thread per connection to the page; the store serialises what they do.

    The threads are daemons, so a peer that stalls does not hold the stop;
    a request already being answered is waited for (`answering`).
    """

    request_queue_size = 128
    daemon_threads = True

    def __init__(self, socket_path: str, page: vault_page.Page,
                 run: Callable[[Callable[[], vault_page.Response]], vault_page.Response] | None,
                 logger: logging.Logger):
        self.page = page
        self.run = run
        self.logger = logger
        self._answering = 0
        self._answered = threading.Condition()
        super().__init__(socket_path, _PageConnection)

    @contextlib.contextmanager
    def answering(self) -> Iterator[None]:
        with self._answered:
            self._answering += 1
        try:
            yield
        finally:
            with self._answered:
                self._answering -= 1
                self._answered.notify_all()

    def wait_answered(self) -> None:
        with self._answered:
            self._answered.wait_for(lambda: self._answering == 0)

    def handle_error(self, request, client_address) -> None:
        # In place of the standard traceback, whose messages may quote what
        # was read. Only the kind of failure is logged.
        self.logger.warning("page: a request was not answered (%s)",
                            type(sys.exc_info()[1]).__name__)


class _PageConnection(http.server.BaseHTTPRequestHandler):
    """One HTTP request from the front proxy, handed to the page as it came."""

    protocol_version = "HTTP/1.0"
    server_version = "casa"
    sys_version = ""
    # A peer that never finishes sending is dropped, as on the `op` socket.
    timeout = 70

    def __getattr__(self, name: str):
        # Every method reaches the page, which answers those it does not
        # take with 405, in place of the standard library's 501.
        if name.startswith("do_"):
            return self._serve
        raise AttributeError(name)

    def _serve(self) -> None:
        if "?" in self.path:
            # The page takes no query string; a value typed into one must
            # not reach anything that might log or show it.
            self._write(vault_page.plain_response(404, "Not found", "There is no such page."))
            return
        headers: dict[str, str] = {}
        for name, value in self.headers.items():
            headers.setdefault(name.lower(), value)  # the first of a repeated header
        body = b""
        if self.command == "POST":
            length = headers.get("content-length")
            if length is None:
                self._write(vault_page.plain_response(
                    411, "Not done", "This form was sent without its length."))
                return
            length = length.strip()
            if not (length.isascii() and length.isdigit() and len(length) <= 12):
                self._write(vault_page.plain_response(
                    400, "Not done", "This form could not be read."))
                return
            if int(length) > vault_page.MAX_FORM:
                # Answered without reading the body: the page would refuse it.
                self._write(vault_page.plain_response(
                    413, "Not done", "This form is too large to save."))
                return
            body = self.rfile.read(int(length))
            if len(body) != int(length):
                self._write(vault_page.plain_response(
                    400, "Not done", "This form could not be read."))
                return
        request = vault_page.Request(self.command, self.path, headers, body)
        page, run = self.server.page, self.server.run

        def answer() -> vault_page.Response:
            return page.handle(request)

        with self.server.answering():
            try:
                response = answer() if run is None else run(answer)
            except Exception as exc:  # noqa: BLE001 - the peer gets an answer, the log one line
                self.server.logger.error("page: a request failed (%s)", type(exc).__name__)
                response = vault_page.plain_response(
                    500, "Not done", "Something went wrong. Try again.")
            self._write(response)

    def _write(self, response: vault_page.Response) -> None:
        self.send_response(response.status)
        for name, value in response.headers:
            self.send_header(name, value)
        if self.command != "HEAD":
            self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        # No write for an empty body (a redirect): the proxy may already
        # have closed on the headers alone, and even an empty write to a
        # closed peer fails with a broken pipe, for an answer it has whole.
        if self.command != "HEAD" and response.body:
            self.wfile.write(response.body)

    def send_error(self, code: int, message: str | None = None,
                   explain: str | None = None) -> None:
        # In place of the standard library's own page, which quotes the
        # request and lacks the headers every answer carries.
        self._write(vault_page.plain_response(
            code, HTTPStatus(code).phrase, "This request could not be read."))

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # The one line a request leaves. The method only when it is one the
        # page takes, the route by name: the path carries a secret's id.
        command = self.command or ""
        method = command if command in vault_page.METHODS else "other"
        route = vault_page.route_name(command, getattr(self, "path", ""))
        self.server.logger.info("page %s %s: %s", method, route, int(code))

    def log_message(self, format: str, *args) -> None:
        # The standard library's lines quote the request line; none is written.
        return

    def log_error(self, format: str, *args) -> None:
        return

    def address_string(self) -> str:
        return "page"  # a UNIX peer has no address


@contextlib.contextmanager
def listening(socket_path: Path, group: str | None, page: vault_page.Page, *,
              prepared: bool = False,
              run: Callable[[Callable[[], vault_page.Response]], vault_page.Response]
              | None = None,
              logger: logging.Logger) -> Iterator[_PageServer]:
    """The page served on `socket_path` in a thread, until the block is left.

    The socket's directory is made if missing, set to 0710 and given to
    `group` when one is named; the socket is 0660 and given to `group` too,
    so only the owner and that group reach it. With `prepared`, the
    directory is used as found: it is not made, and its owner and mode are
    left alone, as is the socket's group; the socket is still 0660.

    `run` is how one request is answered: it is called with a function that
    answers it and returns what that function returns. Without one, the
    request is answered in its connection's thread.

    Leaving the block stops listening and waits for the requests being
    answered, their answers written. A start that fails raises, with
    nothing left listening.
    """
    gid = -1 if group is None or prepared else grp.getgrnam(group).gr_gid
    directory = socket_path.parent
    # The umask keeps the directory and the socket from ever being open
    # wider than they end up.
    before = os.umask(0o077)
    try:
        if not prepared:
            directory.mkdir(exist_ok=True)
            os.chown(directory, -1, gid)
            os.chmod(directory, 0o710)
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()  # left by a run that did not end cleanly
        os.umask(0o117)
        server = _PageServer(str(socket_path), page, run, logger)
    finally:
        os.umask(before)
    try:
        os.chmod(socket_path, 0o660)
        if not prepared:
            os.chown(socket_path, -1, gid)
        thread = threading.Thread(target=server.serve_forever, args=(_STOP_CHECK_S,),
                                  name="secrets page", daemon=True)
        thread.start()
    except BaseException:
        # A start that fails leaves nothing listening and no socket file.
        server.server_close()
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()
        raise
    try:
        yield server
    finally:
        server.shutdown()
        server.wait_answered()
        server.server_close()
        thread.join()
