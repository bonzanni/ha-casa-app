"""The `op` program of an install that keeps its secrets in the built-in store.

It passes its arguments, and its standard input when the command takes a
template, to the store's service over a UNIX socket, prints what the service
answers and exits with the status the service gives. The command itself is
understood by the service (`vault_op`), not here.

One request per connection: a JSON object `{"args": [...], "stdin": text or
null}`, then the sending side is closed; the answer is a JSON object
`{"code": int, "stdout": text, "stderr": text}`.

Only the standard library is imported, and nothing of Casa's: this file is
started once for every `op` call, in Python's isolated mode, where Casa's
modules are not on the import path.
"""
from __future__ import annotations

import json
import socket
import sys
import time

SOCKET_PATH = "/run/casa-vault/store.sock"

# For the whole exchange. A caller that waits on `op` gives up after about
# this long anyway; the store's slowest answer, a new SSH key, takes seconds.
_TIMEOUT_S = 60


def main(argv: list[str], socket_path: str = SOCKET_PATH) -> int:
    """Run the `op` command line `argv`; return its exit status."""
    stdin = None
    if wants_stdin(argv):
        try:
            stdin = sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
        except (OSError, AttributeError, ValueError):
            # AttributeError: with descriptor 0 closed, `sys.stdin` is None.
            _print(sys.stderr, f"[ERROR] {time.strftime('%Y/%m/%d %H:%M:%S')} "
                               "standard input could not be read\n")
            return 1
    try:
        answer = _ask(socket_path, json.dumps({"args": argv, "stdin": stdin}).encode("ascii"))
        code, out, err = answer["code"], answer["stdout"], answer["stderr"]
        if not (isinstance(code, int) and isinstance(out, str) and isinstance(err, str)):
            raise ValueError("not an answer")
    except (OSError, ValueError, KeyError, TypeError):
        # Why is left out on purpose: the caller logs this line, and the one
        # thing it can do with any of the reasons is the same.
        _print(sys.stderr, f"[ERROR] {time.strftime('%Y/%m/%d %H:%M:%S')} "
                           "built-in store unreachable\n")
        return 1
    _print(sys.stdout, out)
    _print(sys.stderr, err)
    return code


def wants_stdin(args: list[str]) -> bool:
    """Whether the command line is one that takes a template on standard input.

    Standard input is read only then. A caller that does not redirect it
    hands this program its own, which may be a pipe that never closes, and
    reading that would wait forever.

    This is `vault_op.wants_stdin`, repeated because that module cannot be
    imported here; a test holds the two against each other.
    """
    command, rest = args[:2], args[2:]
    if command == ["item", "create"]:
        return "-" in rest
    if command == ["item", "edit"]:
        as_json = "--format=json" in rest or ("--format", "json") in zip(rest, rest[1:])
        return as_json and not any("]=" in arg for arg in rest)
    return False


def _ask(socket_path: str, request: bytes) -> dict:
    """Send one request and return the answer, within `_TIMEOUT_S` in all."""
    deadline = time.monotonic() + _TIMEOUT_S

    def left() -> float:
        # Never zero: that would mean "do not wait at all".
        return max(deadline - time.monotonic(), 0.001)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(left())
        connection.connect(socket_path)
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        answer = b""
        while True:
            connection.settimeout(left())
            chunk = connection.recv(65536)
            if not chunk:
                break
            answer += chunk
    return json.loads(answer)


def _print(stream, text: str) -> None:
    # As bytes, so that what is printed does not depend on the locale.
    stream.buffer.write(text.encode("utf-8", "surrogateescape"))
    stream.flush()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
