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
  (nginx), one request per connection, answered by `vault_page`, the page
  engine, under Casa's profile (`vault_page_casa`). Its directory and the
  socket are open to one group, the proxy's, and to nobody else. A body is
  read only up to the page's own limit.

The log names each `op` command and how it ended, and nothing else of it:
arguments and standard input carry the secrets. For the page it names the
method, the route taken and the status, never the path, which carries a
secret's id.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import socketserver
import sys
import threading
import time
from pathlib import Path

import vault_op
from vault_op_client import SOCKET_PATH
from vault_page_server import _PageConnection, _PageServer  # noqa: F401 - reachable under this name too
from vault_page_casa import casa_page
from vault_page_server import listening as _page_listening
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
        page = casa_page(None)
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
                    Path(page_socket), page_group, page or casa_page(store),
                    logger=logger))
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
