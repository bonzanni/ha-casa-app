"""The process that owns the built-in secrets store: it answers `op` from it and serves its page.

One process opens the store and keeps it open. It listens on two UNIX
sockets, each answered by a thread per connection:

- The `op` socket. Every `op` call is a short connection to it
  (`vault_op_client`), answered by `vault_op`. Only the socket's owner can
  connect to it.

  One request per connection: a JSON object `{"args": [...], "stdin": text or
  null}`, read until the client closes its sending side; the answer is a JSON
  object `{"code": int, "stdout": text, "stderr": text}`, the exit status and
  the output `op` would have given.

- The page socket, when one is asked for: plain HTTP/1.0 for the front proxy
  (nginx), one request per connection, answered by `vault_page`. Its
  directory and the socket are open to one group, the proxy's, and to nobody
  else. A body is read only up to the page's own limit.

The log names each `op` command and how it ended, and nothing else of it:
arguments and standard input carry the secrets. For the page it names the
method, the route taken and the status, never the path, which carries a
secret's id.
"""
from __future__ import annotations

import argparse
import contextlib
import grp
import http.server
import json
import logging
import os
import signal
import socketserver
import sys
import threading
import time
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import vault_op
import vault_page
from vault_op_client import SOCKET_PATH
from vault_store import Store, StoreError

logger = logging.getLogger("vault_service")

KEY_VARIABLE = "CASA_VAULT_KEY"

_MAX_REQUEST = 1024 * 1024
# The words of the commands the store answers. Only these are ever logged:
# any other argument, even where a command word is expected, may be a secret.
_COMMAND_WORDS = frozenset({"--version", "read", "item", "list", "get", "create",
                            "edit", "delete"})
# How often the serving loop looks whether it was asked to stop.
_STOP_CHECK_S = 0.2


def serve(directory: Path, socket_path: Path, vault_name: str, env_key: str | None,
          ready_fd: int | None, stop: threading.Event | None = None,
          page_socket: Path | None = None, page_group: str | None = None) -> None:
    """Serve the store in `directory` on `socket_path` until `stop` is set.

    `env_key` is the store's key when it is supplied from outside (see
    `Store.open`). `ready_fd` is a descriptor to write one newline to, and
    close, once clients can connect to every socket asked for. Without a
    `stop` this never returns.

    `page_socket`, when given, is where the secrets page is served too; its
    directory and the socket are given to the group named `page_group`. A
    page that cannot be served (no such group, a path that cannot be bound)
    is logged in one line and costs nothing else.

    A store that cannot be opened (its key is missing or wrong, its file is
    damaged or unreadable) is not served and not touched. The process still
    reports itself ready and stays up: whoever waits for it is not held up,
    a supervisor does not restart it in a loop, and every `op` call fails
    with "built-in store unreachable" until somebody puts the store right
    and restarts. The page, when asked for, is served and says the
    secrets are unavailable; the reason is in the log.
    """
    stop = stop or threading.Event()
    store, page = None, None
    try:
        store = Store.open(Path(directory), env_key)
    except Exception as exc:  # noqa: BLE001 - whatever the reason, the process stays up
        # One line and no traceback. The store's own errors say which file
        # and why; any other message was written by a library and may quote
        # what it read, so only the kind of failure is named.
        why = type(exc).__name__ + (f": {exc}" if isinstance(exc, StoreError) else "")
        logger.error("the built-in store is not served: %s", why)
        page = vault_page.Page(None)
    with contextlib.ExitStack() as listeners:
        # Leaving the block stops the page first, then waits for the `op`
        # requests still being answered.
        server = None
        if store is not None:
            server = listeners.enter_context(_listening(Path(socket_path), store, vault_name))
        page_served = False
        if page_socket is not None:
            try:
                listeners.enter_context(_page_listening(
                    Path(page_socket), page_group, page or vault_page.Page(store)))
                page_served = True
            except Exception as exc:  # noqa: BLE001 - a page that cannot start costs the page only
                # The `op` socket and readiness go on without it. Only the
                # kind of failure is named, as for the store.
                logger.warning("the secrets page is not served (%s)", type(exc).__name__)
        # Logged before the signal, so whoever waits for ready finds these
        # lines already written and none of them lands among its own.
        if server is not None:
            logger.info("serving the built-in store on %s", socket_path)
        if page_served:
            logger.info("serving the secrets page on %s", page_socket)
        _signal_ready(ready_fd)
        if server is None:
            stop.wait()
        else:
            while not stop.is_set():
                server.handle_request()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the built-in secrets store.")
    parser.add_argument("--dir", type=Path, required=True,
                        help="the directory that holds the store")
    parser.add_argument("--socket", type=Path, default=Path(SOCKET_PATH),
                        help="the UNIX socket to listen on")
    parser.add_argument("--vault-name", default="Casa",
                        help="the one vault name the store answers to")
    parser.add_argument("--ready-fd", type=int,
                        help="a descriptor to write a newline to once ready")
    parser.add_argument("--page-socket", type=Path,
                        help="a UNIX socket to serve the secrets page on as well")
    parser.add_argument("--page-group",
                        help="the group that may connect to the page socket")
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Raised in the main thread wherever it is; `serve` then unwinds, which
    # lets the requests being answered finish, and the process exits cleanly.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    serve(options.dir, options.socket, options.vault_name,
          os.environ.get(KEY_VARIABLE), options.ready_fd,
          page_socket=options.page_socket, page_group=options.page_group)


class _Server(socketserver.ThreadingUnixStreamServer):
    """One thread per connection; the store serialises what they do."""

    # Clients that find the queue of waiting connections full are turned
    # away at once, so the queue is long enough for a burst of callers.
    request_queue_size = 128
    timeout = _STOP_CHECK_S

    def __init__(self, socket_path: str, store: Store, vault_name: str):
        self.store = store
        self.vault_name = vault_name
        super().__init__(socket_path, _Connection)


class _Connection(socketserver.StreamRequestHandler):
    # A peer that never finishes sending would hold its thread, and the
    # service's stop, forever. The client gives up after 60 seconds, so a
    # connection still silent at 70 is closed without an answer.
    timeout = 70

    def handle(self) -> None:
        try:
            raw = self.rfile.read(_MAX_REQUEST + 1)
            if len(raw) > _MAX_REQUEST:
                # The client reads its answer only after it has sent
                # everything, so the rest is read, and dropped, first.
                while self.rfile.read(65536):
                    pass
                logger.info("refused a request larger than 1 MiB")
                answer = _refusal("the built-in store takes a request of at most 1 MiB")
            else:
                answer = _answer(self.server.store, self.server.vault_name, raw)
            code, out, err = answer
            self.wfile.write(json.dumps({"code": code, "stdout": out, "stderr": err})
                             .encode("ascii"))
        except OSError as exc:
            # The client went away or stopped sending. Only the kind of
            # failure is logged.
            logger.warning("a request was not answered (%s)", type(exc).__name__)


class _PageServer(socketserver.ThreadingUnixStreamServer):
    """One thread per connection to the page; the store serialises what they do.

    The threads are daemons, so a peer that stalls does not hold the stop;
    a request already being answered is waited for (`answering`).
    """

    request_queue_size = 128
    daemon_threads = True

    def __init__(self, socket_path: str, page: vault_page.Page):
        self.page = page
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
        logger.warning("page: a request was not answered (%s)",
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
        with self.server.answering():
            try:
                response = self.server.page.handle(request)
            except Exception as exc:  # noqa: BLE001 - the peer gets an answer, the log one line
                logger.error("page: a request failed (%s)", type(exc).__name__)
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
        logger.info("page %s %s: %s", method, route, int(code))

    def log_message(self, format: str, *args) -> None:
        # The standard library's lines quote the request line; none is written.
        return

    def log_error(self, format: str, *args) -> None:
        return

    def address_string(self) -> str:
        return "page"  # a UNIX peer has no address


def _answer(store: Store, vault_name: str, raw: bytes) -> tuple[int, str, str]:
    try:
        request = json.loads(raw)
        args, stdin = request["args"], request["stdin"]
        understood = (isinstance(args, list) and all(isinstance(arg, str) for arg in args)
                      and (stdin is None or isinstance(stdin, str)))
    except (ValueError, KeyError, TypeError):
        understood = False
    if not understood:
        logger.info("refused a request that is not a command line")
        return _refusal("the built-in store was sent something that is not a request")
    code, out, err = vault_op.run(store, vault_name, args, stdin)
    words = " ".join(arg for arg in args[:2] if arg in _COMMAND_WORDS) or "(other)"
    logger.info("op %s: exit status %d", words, code)
    return code, out, err


def _refusal(message: str) -> tuple[int, str, str]:
    """An answer in the shape of a failed `op`: status 1 and one error line."""
    return 1, "", f"[ERROR] {time.strftime('%Y/%m/%d %H:%M:%S')} {message}\n"


def _listening(socket_path: Path, store: Store, vault_name: str) -> _Server:
    """A server bound to `socket_path` that only the socket's owner can reach."""
    with contextlib.suppress(FileNotFoundError):
        socket_path.unlink()  # left by a run that did not end cleanly
    # The umask keeps the socket from ever being open to anyone else, even
    # between its creation and the chmod.
    before = os.umask(0o077)
    try:
        server = _Server(str(socket_path), store, vault_name)
    finally:
        os.umask(before)
    os.chmod(socket_path, 0o600)
    return server


@contextlib.contextmanager
def _page_listening(socket_path: Path, group: str | None,
                    page: vault_page.Page) -> Iterator[_PageServer]:
    """The page served on `socket_path` in a thread, until the block is left.

    The socket's directory is 0710 and the socket 0660, both given to
    `group` when one is named, so only the owner and that group reach it.
    Leaving the block stops listening and waits for the requests being
    answered. A start that fails raises, with nothing left listening.
    """
    gid = -1 if group is None else grp.getgrnam(group).gr_gid
    directory = socket_path.parent
    # As for the `op` socket, the umask keeps the directory and the socket
    # from ever being open wider than they end up.
    before = os.umask(0o077)
    try:
        directory.mkdir(exist_ok=True)
        os.chown(directory, -1, gid)
        os.chmod(directory, 0o710)
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()  # left by a run that did not end cleanly
        os.umask(0o117)
        server = _PageServer(str(socket_path), page)
    finally:
        os.umask(before)
    try:
        os.chmod(socket_path, 0o660)
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


def _signal_ready(ready_fd: int | None) -> None:
    if ready_fd is None:
        return
    try:
        os.write(ready_fd, b"\n")
        os.close(ready_fd)
    except OSError as exc:
        # Nobody is waiting on a descriptor that is not open; serving goes on.
        logger.warning("readiness was not signalled on descriptor %d (%s)",
                       ready_fd, type(exc).__name__)


if __name__ == "__main__":
    main()
