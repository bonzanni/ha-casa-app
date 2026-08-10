"""A credential path that could not be made private must stop uid-dropped
engagements from running at all.

GHSA-569r-7crq-xr43. Dropping to a uid that can then read `SUPERVISOR_TOKEN` is
worse than not dropping: the Supervisor API returns every add-on's stored
options, so the engagement gets the Claude OAuth token, the GitHub PAT and the
1Password service-account token. If `private_state.enforce()` cannot repair
those modes, Casa cannot honour the guarantee the uid drop exists for.

Deliberately NOT boot-fatal: Telegram, the resident agents and specialist
engagements stay up, so a read-only `/run` or a restored backup degrades instead
of bricking the install.

The design originally carried a process-wide "executors blocked" latch. It was
cut: two review rounds each found a start path it failed to cover, and a latch
also cannot be tested honestly — a test that sets the latch directly proves
nothing about whether production ever sets it. The replacement re-stats the
three credential paths at each point of use, which is what these tests exercise.
"""
from __future__ import annotations

import os
import stat
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


def test_uid_allocation_refuses_while_a_credential_is_exposed(
    exposed: Path, tmp_path: Path,
) -> None:
    """Point of use 1 — the earliest honest refusal. Asserts the OUTCOME: no uid
    is handed out, not that a helper was consulted."""
    from engagement_uids import UidAllocator, UidStateError

    # proc_scanner injected: reconstruct() otherwise folds the HOST's live /proc
    # uids, and any uid >= UID_BASE on the dev box (snap runs services in the
    # 500000s) is read as "a uid was already allocated" and poisons the allocator.
    alloc = UidAllocator(
        counter_path=str(tmp_path / "uid_counter.json"),
        proc_scanner=lambda: set(),
    )
    alloc.reconstruct(known_uids=[], dir_owner_uids=[])

    private_state.enforce()
    with pytest.raises(UidStateError) as exc:
        alloc.allocate()
    assert "readable beyond root" in str(exc.value)


def test_uid_allocation_proceeds_once_the_modes_are_right(tmp_path: Path) -> None:
    """The mirror case. Without it, an allocator that refused unconditionally
    would pass the test above and brick every install."""
    from engagement_uids import UID_BASE, UidAllocator

    # proc_scanner injected: reconstruct() otherwise folds the HOST's live /proc
    # uids, and any uid >= UID_BASE on the dev box (snap runs services in the
    # 500000s) is read as "a uid was already allocated" and poisons the allocator.
    alloc = UidAllocator(
        counter_path=str(tmp_path / "uid_counter.json"),
        proc_scanner=lambda: set(),
    )
    alloc.reconstruct(known_uids=[], dir_owner_uids=[])
    assert alloc.allocate() >= UID_BASE


def test_preflight_refuses_a_fresh_launch_while_a_credential_is_exposed(
    exposed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point of use 2 — the fresh-launch path, which runs long after boot and is
    exactly why a boot-time latch was the wrong carrier."""
    from drivers.claude_code_driver import UidDropRefused, _preflight_uid_drop

    private_state.enforce()

    ws = tmp_path / "ws"
    ws.mkdir()
    rec = type("Rec", (), {"allocated_uid": 200001, "plugin_artifacts": ()})()

    with pytest.raises(UidDropRefused) as exc:
        _preflight_uid_drop(rec, str(ws))
    assert "readable beyond root" in str(exc.value)


def test_preflight_credential_check_precedes_the_uid_checks(
    exposed: Path, tmp_path: Path,
) -> None:
    """Ordering matters for the diagnosis the operator sees: an exposed
    credential must be named even when the record is ALSO unallocated, otherwise
    the real reason is masked by a generic uid complaint."""
    from drivers.claude_code_driver import UidDropRefused, _preflight_uid_drop

    private_state.enforce()
    rec = type("Rec", (), {"allocated_uid": -1, "plugin_artifacts": ()})()

    with pytest.raises(UidDropRefused) as exc:
        _preflight_uid_drop(rec, str(tmp_path / "missing"))
    assert "readable beyond root" in str(exc.value)


def test_absent_credential_paths_refuse_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror-image failure both reviewers asked for. A fresh install has no
    /data/webhook_secret yet and none of /run populated; an absent file exposes
    nothing, and refusing on absence would make a healthy install unable to run
    any engagement at all."""
    monkeypatch.setattr(
        private_state, "_resolve",
        lambda root, declared: os.path.join(str(tmp_path), declared.lstrip("/")),
    )
    report = private_state.enforce()
    assert report.credential_failures == []
    assert private_state.credential_modes_ok() == []

    from engagement_uids import UID_BASE, UidAllocator

    # proc_scanner injected: reconstruct() otherwise folds the HOST's live /proc
    # uids, and any uid >= UID_BASE on the dev box (snap runs services in the
    # 500000s) is read as "a uid was already allocated" and poisons the allocator.
    alloc = UidAllocator(
        counter_path=str(tmp_path / "uid_counter.json"),
        proc_scanner=lambda: set(),
    )
    alloc.reconstruct(known_uids=[], dir_owner_uids=[])
    assert alloc.allocate() >= UID_BASE


def test_a_private_only_failure_refuses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confidentiality loss is not credential loss. A world-readable topic
    ledger is an ERROR in the log; it must not stop engagements, or a single
    stubborn file would take the whole executor fleet down."""
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

    from engagement_uids import UID_BASE, UidAllocator

    # proc_scanner injected: reconstruct() otherwise folds the HOST's live /proc
    # uids, and any uid >= UID_BASE on the dev box (snap runs services in the
    # 500000s) is read as "a uid was already allocated" and poisons the allocator.
    alloc = UidAllocator(
        counter_path=str(tmp_path / "uid_counter.json"),
        proc_scanner=lambda: set(),
    )
    alloc.reconstruct(known_uids=[], dir_owner_uids=[])
    assert alloc.allocate() >= UID_BASE


def test_repairing_the_mode_lifts_the_refusal_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Because the check re-stats at the point of use rather than latching, an
    operator who fixes the mode by hand gets working engagements on the next
    launch — no restart, and no stale "blocked" state to clear."""
    token = _expose(tmp_path, "/run/s6/container_environment/SUPERVISOR_TOKEN")
    monkeypatch.setattr(
        private_state, "_resolve",
        lambda root, declared: os.path.join(str(tmp_path), declared.lstrip("/")),
    )
    assert private_state.credential_modes_ok()
    os.chmod(token, 0o600)
    assert private_state.credential_modes_ok() == []
