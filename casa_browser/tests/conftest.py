"""Shared setup for Casa Browser's tests.

The app's code is the ``casa_browser`` package under ``casa_browser/rootfs/opt``; the
repository's top-level ``casa_browser/`` folder is not an import package. Putting the
rootfs ``opt`` directory first on ``sys.path`` makes ``import casa_browser.protocol``
resolve to the shipped package (a regular package wins over the folder's namespace
portion wherever the repository root sits on the path).
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_DIR.parent
_OPT = str(APP_DIR / "rootfs" / "opt")
if _OPT not in sys.path:
    sys.path.insert(0, _OPT)


# A test on the developer's machine must never signal a process it did not start: a
# kill path with its filters broken (a mutation check, a missing fake) otherwise signals
# every process of the user and ends their whole login session. Installed at import
# time, before any test imports the app, so default arguments bound to ``os.kill`` get
# the guard too. Inside the integration container the app's real kills are the subject.
import os as _os

if not Path("/.dockerenv").exists():
    _real_kill, _real_killpg = _os.kill, _os.killpg

    def _descends_from_this_test(pid: int) -> bool:
        me = _os.getpid()
        while pid > 1:
            if pid == me:
                return True
            try:
                pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                return False
        return False

    def _guarded_kill(pid, sig):
        if sig != 0 and not (pid > 0 and pid != _os.getpid() and _descends_from_this_test(pid)):
            raise RuntimeError(f"test guard: refusing signal {sig} to pid {pid}, "
                               "not a process this test started")
        return _real_kill(pid, sig)

    def _guarded_killpg(pgid, sig):
        if sig != 0 and not (pgid > 1 and pgid != _os.getpgrp() and _descends_from_this_test(pgid)):
            raise RuntimeError(f"test guard: refusing signal {sig} to group {pgid}, "
                               "not a group this test started")
        return _real_killpg(pgid, sig)

    _os.kill, _os.killpg = _guarded_kill, _guarded_killpg


import pytest


@pytest.fixture(autouse=True)
def no_forbidden_sdk_calls(request):
    """Every broker test checks uncensored SDK history, including swallowed failures."""
    if request.path.name not in {'test_broker.py', 'test_login.py', 'test_credential_save.py'}:
        yield
        return
    from brokerfakes import ALL_SDK_CALLS, FORBIDDEN_SDK_METHODS
    start = len(ALL_SDK_CALLS)
    evidence = None
    if 'integration' in request.path.parts:
        # Resolve while fixtures are live, so pytest tears this guard down first.
        evidence = request.getfixturevalue('tmp_path') / 'broker' / 'forbidden-sdk-calls.jsonl'
    try:
        yield
    finally:
        assert [c for c in ALL_SDK_CALLS[start:] if c[0] in FORBIDDEN_SDK_METHODS] == []
        if evidence is not None:
            assert not evidence.exists() or evidence.read_bytes() == b''


def pytest_sessionfinish(session, exitstatus):
    """The image lane (``run-integration.sh`` sets ``CASA_IT_NO_SKIP=1``) has no optional
    probe: a skipped test there fails the run, so a green lane means every probe ran."""
    if _os.environ.get("CASA_IT_NO_SKIP") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = len(reporter.stats.get("skipped", ())) if reporter is not None else 0
    if skipped and session.exitstatus == 0:
        sys.stderr.write(f"\n{skipped} integration test(s) skipped: every image probe is mandatory\n")
        session.exitstatus = 1
