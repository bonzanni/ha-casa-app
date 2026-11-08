"""A credential path that could not be made private is reported by
`credential_modes_ok()`, re-read from the filesystem at each call.

GHSA-569r-7crq-xr43. A process running under a dropped uid that can read
`SUPERVISOR_TOKEN` gets every add-on's stored options from the Supervisor API:
the Claude OAuth token, the GitHub PAT and the 1Password service-account token.
`private_state.enforce()` repairs those modes at boot; `credential_modes_ok()`
is the check a uid-dropping consumer runs before starting such a process.

Deliberately NOT boot-fatal: a read-only `/run` or a restored backup degrades
instead of bricking the install.

The design originally carried a process-wide "executors blocked" latch. It was
cut: a latch cannot be tested honestly (a test that sets the latch directly
proves nothing about whether production ever sets it). The replacement re-stats
the three credential paths at each call, which is what these tests exercise.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import private_state

pytestmark = pytest.mark.unit


def _expose(root: Path, rel: str) -> Path:
    """Create a credential file that is readable beyond root."""
    p = root / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("secret", encoding="utf-8")
    os.chmod(p, 0o644)
    return p


@pytest.fixture
def exposed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tree in which one credential path is exposed AND cannot be repaired.

    Patching ``os.chmod`` to fail on that one path is what makes this the real
    production condition rather than a hand-set flag: ``enforce()`` runs, tries,
    fails, and the refusal follows from the filesystem state it leaves behind.
    """
    token = _expose(tmp_path, "/run/s6/container_environment/SUPERVISOR_TOKEN")
    real_chmod = os.chmod

    def boom(path, mode, *args, **kwargs):  # noqa: ANN001
        if str(path) == str(token):
            raise PermissionError("read-only file system")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(private_state.os, "chmod", boom)
    # Every consumer resolves against "/" in production; point them at the tree.
    monkeypatch.setattr(
        private_state, "_resolve",
        lambda root, declared: os.path.join(str(tmp_path), declared.lstrip("/")),
    )
    return tmp_path


def test_the_real_chain_enforce_fails_then_credentials_report_exposed(
    exposed: Path,
) -> None:
    """The end-to-end condition, asserted as an outcome: enforce() reports a
    credential failure, and the point-of-use check independently agrees by
    re-reading the filesystem — it does not consult enforce()'s report."""
    report = private_state.enforce()
    assert report.credential_failures, "enforce() must report the failure"

    offenders = private_state.credential_modes_ok()
    assert offenders
    assert any("SUPERVISOR_TOKEN" in o for o in offenders)


def test_absent_credential_paths_refuse_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror-image failure both reviewers asked for. A fresh install has no
    /data/webhook_secret yet and none of /run populated; an absent file exposes
    nothing, and reporting absence would make a healthy install look exposed."""
    monkeypatch.setattr(
        private_state, "_resolve",
        lambda root, declared: os.path.join(str(tmp_path), declared.lstrip("/")),
    )
    report = private_state.enforce()
    assert report.credential_failures == []
    assert private_state.credential_modes_ok() == []


def test_a_private_only_failure_refuses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confidentiality loss is not credential loss. A world-readable topic
    ledger is an ERROR in the log; it must not be reported as a credential
    offence, or a single stubborn file would block every uid-dropped consumer."""
    ledger = _expose(tmp_path, "/data/topic-ledger.json")
    real_chmod = os.chmod

    def boom(path, mode, *args, **kwargs):  # noqa: ANN001
        if str(path) == str(ledger):
            raise PermissionError("nope")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(private_state.os, "chmod", boom)
    monkeypatch.setattr(
        private_state, "_resolve",
        lambda root, declared: os.path.join(str(tmp_path), declared.lstrip("/")),
    )

    report = private_state.enforce()
    assert report.failures and report.credential_failures == []
    assert private_state.credential_modes_ok() == []


def test_repairing_the_mode_lifts_the_refusal_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Because the check re-stats at the point of use rather than latching, an
    operator who fixes the mode by hand gets a clean report on the next call —
    no restart, and no stale "blocked" state to clear."""
    token = _expose(tmp_path, "/run/s6/container_environment/SUPERVISOR_TOKEN")
    monkeypatch.setattr(
        private_state, "_resolve",
        lambda root, declared: os.path.join(str(tmp_path), declared.lstrip("/")),
    )
    assert private_state.credential_modes_ok()
    os.chmod(token, 0o600)
    assert private_state.credential_modes_ok() == []
