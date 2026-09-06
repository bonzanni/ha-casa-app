"""Red case for #629: the live Hindsight provenance-contract test SKIPS, and
issues no request, when its configuration is absent.

The scheduled slow tier (``.github/workflows/qa.yml``, step ``Slow pytest``)
selects ``tests/test_hindsight_provenance_contract_live.py`` with ``-m slow``
alone, and no CI tier supplies ``HINDSIGHT_URL`` or
``HINDSIGHT_EXPECTED_VERSION``. At base the live test indexes the URL before
its ``try``, the version inside it, and unconditionally issues a DELETE from
its ``finally`` — so with either variable absent it errors (and, with only the
version absent, still sends the cleanup DELETE) instead of skipping. The mask
``|| true`` on that step hid the error in every scheduled run.

Every arm here runs the REAL selected artifact — a child pytest over the live
test file with the tier's own ``-m slow`` selection — because the declared
behaviour is a collected outcome (``1 skipped``), and a module-level ``skipif``
would satisfy it just as well as an in-function guard. A direct call of the
test function would wrongly reject that equivalent route (specified by the
red-case round, 2026-09-06).

Unmarked on purpose: this file runs in the default unit gate.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_TEST = "tests/test_hindsight_provenance_contract_live.py"
URL_VAR = "HINDSIGHT_URL"
VERSION_VAR = "HINDSIGHT_EXPECTED_VERSION"


def _child_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in (URL_VAR, VERSION_VAR)}
    # The gate materializes the tree read-only: no bytecode, no cache dir.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(overrides)
    return env


def _run_slow_selection(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", LIVE_TEST, "-m", "slow",
         "-p", "no:cacheprovider", "-q", "--tb=short"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


def _summary(out: str) -> str:
    """The last non-empty stdout line — pytest's ``N skipped in …`` summary."""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, f"no pytest output:\n{out}"
    return lines[-1]


def _assert_one_skipped(proc: subprocess.CompletedProcess[str]) -> None:
    out = proc.stdout + proc.stderr
    summary = _summary(proc.stdout)
    assert proc.returncode == 0, f"exit {proc.returncode}, summary {summary!r}:\n{out}"
    assert re.search(r"\b1 skipped\b", summary), f"summary {summary!r}:\n{out}"
    assert not re.search(r"\b(failed|error)\b", summary), f"summary {summary!r}:\n{out}"
    assert "KeyError" not in out, out


class _Recorder(BaseHTTPRequestHandler):
    """Records every request the live test's cleanup or contract run could send."""
    received: list[tuple[str, str]] = []

    def _record(self) -> None:
        self.received.append((self.command, self.path))
        body = json.dumps({}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = _record

    def log_message(self, *_args) -> None:  # keep the child's output clean
        pass


def test_slow_selection_skips_when_both_hindsight_variables_are_absent() -> None:
    """ARM1 ≡ ARM3 (pin of record): the tier's own selection, nothing set.

    Base-red reason: exit 1, ``1 failed``, ``KeyError: 'HINDSIGHT_URL'``.
    """
    proc = _run_slow_selection(_child_env())
    _assert_one_skipped(proc)


def test_slow_selection_skips_without_expected_version_and_sends_nothing() -> None:
    """ARM2: URL points at a recording server, the version is absent.

    Kills a guard on ``HINDSIGHT_URL`` alone and a guard placed inside the
    ``try`` — ``Skipped`` is an exception, so the ``finally``'s DELETE would
    still leave. Base-red reason: ``KeyError: 'HINDSIGHT_EXPECTED_VERSION'``
    and exactly one received ``DELETE``.
    """
    _Recorder.received = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        proc = _run_slow_selection(_child_env(**{URL_VAR: url}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
    assert _Recorder.received == [], (
        f"{len(_Recorder.received)} request(s) left the live test: "
        f"{_Recorder.received}\n{proc.stdout}{proc.stderr}")
    _assert_one_skipped(proc)
