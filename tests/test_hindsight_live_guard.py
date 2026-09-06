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
red-case round, 2026-09-06). ARM2 counts requests through an in-process
recorder rather than a loopback listener: the review sandbox denies socket
creation (re-specified 2026-09-06 after the gate-owned review).

Unmarked on purpose: this file runs in the default unit gate.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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


def _run_slow_selection(env: dict[str, str], *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", LIVE_TEST, "-m", "slow",
         "-p", "no:cacheprovider", "-q", "--tb=short", *extra],
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


# A pytest plugin the child loads with `-p`. It rebinds the contract module's
# request function to a recorder that never opens a socket: the reviewer
# sandbox denies socket creation, so a loopback listener (the first shape of
# this arm) failed before the guard was ever reached. The live test's `finally`
# imports `_request` at call time and `run_contract` reaches it through the
# module global, so both the contract run and the cleanup DELETE are counted.
# The import happens inside the hook, not at plugin load: the code root is put
# on sys.path by tests/conftest.py, which loads after `-p` plugins.
RECORDER_PLUGIN = "d1_request_recorder"
RECORDER_SOURCE = """\
import json, os

def pytest_runtest_setup(item):
    import hindsight_provenance_contract as m
    log = os.environ["D1_REQUEST_LOG"]

    def _record(base_url, method, path, body=None, *, timeout=300.0):
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps([method, path]) + "\\n")
        return 200, {}

    m._request = _record
"""


def _recorded(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_slow_selection_skips_when_both_hindsight_variables_are_absent() -> None:
    """ARM1 ≡ ARM3 (pin of record): the tier's own selection, nothing set.

    Base-red reason: exit 1, ``1 failed``, ``KeyError: 'HINDSIGHT_URL'``.
    """
    proc = _run_slow_selection(_child_env())
    _assert_one_skipped(proc)


def test_slow_selection_skips_without_expected_version_and_sends_nothing(tmp_path: Path) -> None:
    """ARM2: the URL set, the version absent — and exactly zero requests.

    Kills a guard on ``HINDSIGHT_URL`` alone and a guard placed inside the
    ``try`` — ``Skipped`` is an exception, so the ``finally``'s DELETE would
    still leave. Base-red reason: ``KeyError: 'HINDSIGHT_EXPECTED_VERSION'``
    and exactly one recorded ``["DELETE", "/v1/default/banks/…"]``.
    """
    (tmp_path / f"{RECORDER_PLUGIN}.py").write_text(RECORDER_SOURCE, encoding="utf-8")
    log = tmp_path / "requests.jsonl"
    inherited = os.environ.get("PYTHONPATH")
    env = _child_env(**{
        URL_VAR: "http://hindsight.invalid",  # never dialled: the recorder owns _request
        "D1_REQUEST_LOG": str(log),
        "PYTHONPATH": str(tmp_path) + (os.pathsep + inherited if inherited else ""),
    })
    proc = _run_slow_selection(env, "-p", RECORDER_PLUGIN)
    assert _recorded(log) == [], (
        f"request(s) left the live test: {_recorded(log)}\n{proc.stdout}{proc.stderr}")
    _assert_one_skipped(proc)


def test_slow_selection_skips_without_url_when_expected_version_is_set() -> None:
    """ARM4 (acceptance round 1): the version set, the URL absent.

    Kills a guard on ``HINDSIGHT_EXPECTED_VERSION`` alone — both other arms
    leave the version unset, so that mutant passes them. Base-red reason:
    exit 1, ``1 failed``, ``KeyError: 'HINDSIGHT_URL'``.
    """
    proc = _run_slow_selection(_child_env(**{VERSION_VAR: "review-version"}))
    _assert_one_skipped(proc)
