"""A credential path that could not be made private is reported by
`private_state.enforce()` in its report's `credential_failures`.

GHSA-569r-7crq-xr43. A process running under a dropped uid that can read
`SUPERVISOR_TOKEN` gets every add-on's stored options from the Supervisor API:
the Claude OAuth token, the GitHub PAT and the 1Password service-account token.
`private_state.enforce()` repairs those modes at boot.

Deliberately NOT boot-fatal: a read-only `/run` or a restored backup degrades
instead of bricking the install.
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
    fails, and reports what it could not repair.
    """
    token = _expose(tmp_path, "/run/s6/container_environment/SUPERVISOR_TOKEN")
    real_chmod = os.chmod

    def boom(path, mode, *args, **kwargs):  # noqa: ANN001
        if str(path) == str(token):
            raise PermissionError("read-only file system")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(private_state.os, "chmod", boom)
    # enforce() resolves against "/" in production; point it at the tree.
    monkeypatch.setattr(
        private_state, "_resolve",
        lambda root, declared: os.path.join(str(tmp_path), declared.lstrip("/")),
    )
    return tmp_path


def test_the_real_chain_enforce_fails_then_reports_the_credential(
    exposed: Path,
) -> None:
    """The end-to-end condition, asserted as an outcome: enforce() tries, fails,
    and names the credential path it could not make private."""
    report = private_state.enforce()
    assert report.credential_failures, "enforce() must report the failure"
    assert any("SUPERVISOR_TOKEN" in p for p in report.credential_failures)


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


def test_a_private_only_failure_refuses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confidentiality loss is not credential loss. A world-readable
    scheduled-question store is an ERROR in the log; it must not be reported as a
    credential offence."""
    ledger = _expose(tmp_path, "/data/scheduled_asks.json")
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

