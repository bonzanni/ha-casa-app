"""Tests for the declared private-state inventory and its enforcement pass.

GHSA-569r-7crq-xr43: root-owned runtime state was created world-readable, so a
per-engagement uid (containment Stage 2) could read the Supervisor/HASSIO
bearer tokens, the global webhook secret, sibling engagement stdout, and the
resident agent's transcripts. ``private_state`` is the single source of truth
for which paths are private and at what mode, and its ``enforce()`` repairs
them on every boot — which is what fixes an already-deployed install, where
every affected file already exists at ``0644`` and ``atomic_io`` preserves an
existing file's mode.

The red case that matters most is the OVER-closure one
(``test_config_sync_report_stays_group_and_world_readable``): tightening the
config-sync report would break the shipped configurator recipe with no runtime
signal at all.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import private_state

pytestmark = pytest.mark.unit


def _mode(p: Path | str) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _touch(root: Path, rel: str, mode: int = 0o644) -> Path:
    p = root / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    os.chmod(p, mode)
    return p


def _mkdir(root: Path, rel: str, mode: int = 0o755) -> Path:
    p = root / rel.lstrip("/")
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, mode)
    return p


# --------------------------------------------------------------------------
# The inventory itself — a silently dropped entry is the whole exposure.
# --------------------------------------------------------------------------


def test_inventory_pins_every_path_the_advisory_named() -> None:
    """Every path measured readable by uid 200001 on v0.170.2 must be declared.

    Pinning the list, not a count: a future refactor that drops one of these
    re-opens exactly the hole the advisory describes, and no other test in the
    suite would notice.
    """
    declared = {e.path for e in private_state.FILES + private_state.DIRS}
    assert declared >= {
        "/run/s6/container_environment/SUPERVISOR_TOKEN",
        "/run/s6/container_environment/HASSIO_TOKEN",
        "/data/webhook_secret",
        "/data/sessions.json",
        "/data/topic-ledger.json",
        "/data/jobs.json",
        "/data/plugin-health.json",
        "/data/plugin-setup-episodes.json",
        "/data/callback_acks.json",
        "/data/event_acks.json",
        "/data/webhook_trigger_acks.json",
        "/data/persona_install_acks.json",
        "/data/specialist_install_acks.json",
        "/data/cold-retain-retry",
        "/config/cc-home",
        "/config/.git",
    }


def test_credential_entries_are_exactly_the_three_bearer_paths() -> None:
    """Only these three gate engagement startup; widening that set turns a
    confidentiality regression into a refusal to run engagements at all."""
    creds = {e.path for e in private_state.FILES
             if e.kind == private_state.CREDENTIAL}
    assert creds == {
        "/run/s6/container_environment/SUPERVISOR_TOKEN",
        "/run/s6/container_environment/HASSIO_TOKEN",
        "/data/webhook_secret",
    }


def test_traversal_roots_are_never_declared() -> None:
    """``/data`` and ``/config`` must stay traversable: the dropped uid reaches
    its own workspace and its assigned plugin artifacts through them. Both
    r1 reviewers flagged this independently."""
    declared = {e.path for e in private_state.FILES + private_state.DIRS
                + private_state.DIR_GLOBS + private_state.FILE_GLOBS}
    assert "/data" not in declared
    assert "/config" not in declared
    assert "/var/log" not in declared


def test_every_entry_declares_why() -> None:
    """The inventory is the documentation of record for what each mode
    protects; an entry with no reason cannot be audited later."""
    for entry in (private_state.FILES + private_state.DIRS
                  + private_state.DIR_GLOBS + private_state.FILE_GLOBS):
        assert entry.why.strip(), entry.path
        assert entry.mode & 0o077 == 0, entry.path


# --------------------------------------------------------------------------
# enforce() — the upgrade path.
# --------------------------------------------------------------------------


def test_enforce_repairs_a_preexisting_world_readable_file(tmp_path: Path) -> None:
    """The upgrade case. On a deployed install every affected file ALREADY
    exists at 0644, and atomic_io preserves an existing file's mode — so
    correct write sites alone repair nothing until the next write, which for
    e.g. topic-ledger.json may be days away."""
    secret = _touch(tmp_path, "/data/webhook_secret", 0o644)
    report = private_state.enforce(root=str(tmp_path))
    assert _mode(secret) == 0o600
    assert str(secret) in [c[0] for c in report.changed]


def test_enforce_repairs_directories_without_recursing(tmp_path: Path) -> None:
    """Removing `x` for others on the directory makes everything beneath it
    unreachable, so the pass never has to walk (and never has to decide about)
    the ~198 files inside cc-home."""
    d = _mkdir(tmp_path, "/config/cc-home", 0o755)
    inner = _touch(tmp_path, "/config/cc-home/.claude/transcript.json", 0o644)
    private_state.enforce(root=str(tmp_path))
    assert _mode(d) == 0o700
    assert _mode(inner) == 0o644, "inner files are deliberately left alone"


def test_enforce_covers_the_quarantine_alias_shape(tmp_path: Path) -> None:
    """A corrupt private file is archived aside by os.replace, which PRESERVES
    the pre-upgrade 0644 inode under a name no per-file entry covers
    (session_registry.py -> sessions.json.corrupt, topic_ledger.py ->
    topic-ledger.json.casabak). Globbing the suffix closes the shape rather
    than the two current instances."""
    corrupt = _touch(tmp_path, "/data/sessions.json.corrupt", 0o644)
    casabak = _touch(tmp_path, "/data/topic-ledger.json.casabak", 0o644)
    private_state.enforce(root=str(tmp_path))
    assert _mode(corrupt) == 0o600
    assert _mode(casabak) == 0o600


def test_enforce_covers_every_engagement_log_dir(tmp_path: Path) -> None:
    """Sibling engagement stdout was the worst finding in the advisory: ~21 MB
    per engagement, readable by every other engagement uid."""
    a = _mkdir(tmp_path, "/var/log/casa-engagement-aaa", 0o755)
    b = _mkdir(tmp_path, "/var/log/casa-engagement-bbb", 0o755)
    unrelated = _mkdir(tmp_path, "/var/log/nginx", 0o755)
    private_state.enforce(root=str(tmp_path))
    assert _mode(a) == 0o700
    assert _mode(b) == 0o700
    assert _mode(unrelated) == 0o755


def test_enforce_is_idempotent_and_quiet_on_a_clean_boot(tmp_path: Path) -> None:
    _touch(tmp_path, "/data/webhook_secret", 0o644)
    private_state.enforce(root=str(tmp_path))
    second = private_state.enforce(root=str(tmp_path))
    assert second.changed == []
    assert second.failures == []


def test_enforce_skips_absent_paths_without_failing(tmp_path: Path) -> None:
    """Most of the inventory is created lazily; a fresh install has almost
    none of it. Absence is not a failure and must not refuse anything."""
    report = private_state.enforce(root=str(tmp_path))
    assert report.failures == []
    assert report.credential_failures == []
    assert report.changed == []


def test_enforce_refuses_to_chmod_through_a_symlink(tmp_path: Path) -> None:
    """chmod(2) follows symlinks, so a declared path that is a symlink must be
    skipped and reported, never followed to whatever it points at."""
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    os.chmod(outside, 0o644)
    link = tmp_path / "data" / "webhook_secret"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    report = private_state.enforce(root=str(tmp_path))
    assert _mode(outside) == 0o644, "the symlink target must be untouched"
    assert str(link) in report.skipped_symlinks


def test_enforce_records_a_credential_failure_when_chmod_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential-class repair failure is what makes the release's guarantee
    unmet, and is the only class that refuses engagement startup."""
    secret = _touch(tmp_path, "/data/webhook_secret", 0o644)
    real_chmod = os.chmod

    def boom(path, mode, *args, **kwargs):  # noqa: ANN001
        if str(path) == str(secret):
            raise PermissionError("read-only filesystem")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(private_state.os, "chmod", boom)
    report = private_state.enforce(root=str(tmp_path))
    assert str(secret) in report.credential_failures
    assert report.failures


def test_enforce_does_not_treat_a_private_failure_as_a_credential_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confidentiality loss is not credential loss: it logs and continues, and
    must never refuse engagement startup."""
    ledger = _touch(tmp_path, "/data/topic-ledger.json", 0o644)
    real_chmod = os.chmod

    def boom(path, mode, *args, **kwargs):  # noqa: ANN001
        if str(path) == str(ledger):
            raise PermissionError("nope")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(private_state.os, "chmod", boom)
    report = private_state.enforce(root=str(tmp_path))
    assert report.credential_failures == []
    assert str(ledger) in [f[0] for f in report.failures]


# --------------------------------------------------------------------------
# Over-closure — the silent functional regression, given its own red case.
# --------------------------------------------------------------------------


def test_config_sync_report_stays_group_and_world_readable(tmp_path: Path) -> None:
    """/data/config-sync-report.json must NOT be tightened.

    The `configurator` executor is a dropped-uid claude_code engagement whose
    path_scope hook deliberately allow-lists this one /data file
    (defaults/agents/executors/configurator/hooks.yaml) because its
    reconcile-defaults recipe reads it. 0600 makes that Read return EACCES and
    the recipe fails with no runtime signal — the exact over-closure class that
    is worse than the exposure. It holds no secret: the report serialises
    relative /config paths the configurator can already read.
    """
    report_file = _touch(tmp_path, "/data/config-sync-report.json", 0o644)
    private_state.enforce(root=str(tmp_path))
    assert _mode(report_file) == 0o644
    declared = {e.path for e in private_state.FILES}
    assert "/data/config-sync-report.json" not in declared


def test_plugin_store_artifacts_stay_world_readable(tmp_path: Path) -> None:
    """The dropped process loads its assigned plugin artifacts directly; the
    uid-drop preflight already refuses to start an engagement whose
    --plugin-dir is not world-readable (INV-CONT-004)."""
    artifact = _touch(tmp_path, "/config/plugins/store/x/abc/manifest.json", 0o644)
    registry = _touch(tmp_path, "/config/plugins/registry.json", 0o644)
    private_state.enforce(root=str(tmp_path))
    assert _mode(artifact) == 0o644
    assert _mode(registry) == 0o644


# --------------------------------------------------------------------------
# credential_modes_ok() — the point-of-use check that replaced the latch.
# --------------------------------------------------------------------------


def test_credential_modes_ok_reports_a_world_readable_token(tmp_path: Path) -> None:
    _touch(tmp_path, "/run/s6/container_environment/SUPERVISOR_TOKEN", 0o644)
    offenders = private_state.credential_modes_ok(root=str(tmp_path))
    assert offenders
    assert any("SUPERVISOR_TOKEN" in o for o in offenders)


def test_credential_modes_ok_accepts_repaired_tokens(tmp_path: Path) -> None:
    _touch(tmp_path, "/run/s6/container_environment/SUPERVISOR_TOKEN", 0o644)
    _touch(tmp_path, "/data/webhook_secret", 0o644)
    private_state.enforce(root=str(tmp_path))
    assert private_state.credential_modes_ok(root=str(tmp_path)) == []


def test_credential_modes_ok_treats_absence_as_nothing_to_expose(
    tmp_path: Path,
) -> None:
    """The mirror-image failure both r2 reviewers asked for: a fresh install
    has no /data/webhook_secret yet, and must still start engagements. An
    absent file exposes nothing."""
    assert private_state.credential_modes_ok(root=str(tmp_path)) == []


def test_credential_modes_ok_ignores_group_only_readability_of_private_files(
    tmp_path: Path,
) -> None:
    """Only the three credential paths gate startup. A non-credential file left
    world-readable is an ERROR in the log, not a refusal to run."""
    _touch(tmp_path, "/data/topic-ledger.json", 0o644)
    assert private_state.credential_modes_ok(root=str(tmp_path)) == []
