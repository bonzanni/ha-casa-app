"""Per-trigger secret staging + ownership-aware validation (Release A, Task 4)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from webhook_auth import ensure_secret, read_secret


def test_casa_ensure_creates_43char_0600(tmp_path: Path):
    val = ensure_secret("vm", owner="casa", secrets_dir=tmp_path)
    assert val is not None and len(val) == 43
    f = tmp_path / "vm"
    assert f.exists()
    mode = stat.S_IMODE(f.stat().st_mode)
    assert mode == 0o600


def test_casa_ensure_is_idempotent(tmp_path: Path):
    a = ensure_secret("vm", owner="casa", secrets_dir=tmp_path)
    b = ensure_secret("vm", owner="casa", secrets_dir=tmp_path)
    assert a == b and a is not None


def test_casa_short_file_is_invalid(tmp_path: Path):
    (tmp_path / "vm").write_bytes(b"tooshort")
    assert read_secret("vm", owner="casa", secrets_dir=tmp_path) is None


def test_symlink_final_name_rejected(tmp_path: Path):
    target = tmp_path / "elsewhere"
    target.write_bytes(b"x" * 43)
    os.symlink(target, tmp_path / "vm")
    assert read_secret("vm", owner="casa", secrets_dir=tmp_path) is None


def test_provider_ensure_readonly_none_when_absent(tmp_path: Path):
    assert ensure_secret("vm", owner="provider", secrets_dir=tmp_path) is None


def test_provider_accepts_opaque_value(tmp_path: Path):
    opaque = b"whsec_" + b"A1b2C3" * 30  # ~186 bytes, printable ASCII
    (tmp_path / "vm").write_bytes(opaque)
    os.chmod(tmp_path / "vm", 0o600)
    assert read_secret("vm", owner="provider", secrets_dir=tmp_path) == opaque


def test_provider_rejects_empty_and_oversize(tmp_path: Path):
    (tmp_path / "empty").write_bytes(b"")
    assert read_secret("empty", owner="provider", secrets_dir=tmp_path) is None
    (tmp_path / "big").write_bytes(b"a" * 5000)
    assert read_secret("big", owner="provider", secrets_dir=tmp_path) is None


def test_provider_rejects_non_printable(tmp_path: Path):
    (tmp_path / "np").write_bytes(b"abc\x00def")
    assert read_secret("np", owner="provider", secrets_dir=tmp_path) is None


def test_missing_secret_reads_none(tmp_path: Path):
    assert read_secret("nope", owner="casa", secrets_dir=tmp_path) is None


def test_orphan_tmp_files_swept(tmp_path: Path):
    orphan = tmp_path / ".tmp-999-oldjunk"
    orphan.write_bytes(b"junk")
    # Backdate mtime beyond the 60s sweep window.
    old = orphan.stat().st_mtime - 120
    os.utime(orphan, (old, old))
    ensure_secret("vm", owner="casa", secrets_dir=tmp_path)
    assert not orphan.exists()


# ---------------------------------------------------------------------------
# Release B — artifact retirement: retire_secret removes ALL slots
# ---------------------------------------------------------------------------


def test_retire_secret_removes_live_next_and_rotation_state(tmp_path):
    from webhook_auth import retire_secret, rotation_begin

    name = "plg-elevenlabs--voicemail"
    ensure_secret(name, owner="casa", secrets_dir=tmp_path)
    rotation_begin(name, owner="casa", secrets_dir=tmp_path)
    assert (tmp_path / name).exists()
    assert (tmp_path / f"{name}.next").exists()
    assert (tmp_path / f"{name}.rot.json").exists()

    retire_secret(name, secrets_dir=tmp_path)
    assert not (tmp_path / name).exists()
    assert not (tmp_path / f"{name}.next").exists()
    assert not (tmp_path / f"{name}.rot.json").exists()
    # and a later mint starts FRESH (no inheritance)
    fresh = ensure_secret(name, owner="casa", secrets_dir=tmp_path)
    assert fresh is not None


def test_retire_secret_tolerates_missing_files_and_dir(tmp_path):
    from webhook_auth import retire_secret

    retire_secret("never-existed", secrets_dir=tmp_path)          # no raise
    retire_secret("x", secrets_dir=tmp_path / "no-such-dir")      # no raise
    retire_secret("", secrets_dir=tmp_path)                       # no raise


def test_retire_secrets_with_prefix_sweeps_all_slots(tmp_path):
    """Sol shipB-r1 P1-4: revoke retires from the FILESYSTEM inventory by
    prefix — live + .next + .rot.json for every matching base, others kept."""
    from webhook_auth import retire_secrets_with_prefix, rotation_begin

    for base in ("plg-p--a", "plg-p--b", "plg-other--x", "resident-vm"):
        ensure_secret(base, owner="casa", secrets_dir=tmp_path)
    rotation_begin("plg-p--a", owner="casa", secrets_dir=tmp_path)

    retired = retire_secrets_with_prefix("plg-p--", secrets_dir=tmp_path)
    assert retired == ["plg-p--a", "plg-p--b"]
    assert not (tmp_path / "plg-p--a").exists()
    assert not (tmp_path / "plg-p--a.next").exists()
    assert not (tmp_path / "plg-p--a.rot.json").exists()
    assert not (tmp_path / "plg-p--b").exists()
    assert (tmp_path / "plg-other--x").exists()
    assert (tmp_path / "resident-vm").exists()


def test_retire_secrets_with_prefix_tolerates_missing_dir(tmp_path):
    from webhook_auth import retire_secrets_with_prefix

    assert retire_secrets_with_prefix("plg-p--",
                                      secrets_dir=tmp_path / "nope") == []
    assert retire_secrets_with_prefix("", secrets_dir=tmp_path) == []


# ---------------------------------------------------------------------------
# Terra shipB-r2: identity-bound minting — non-inheritance enforced at
# ACTIVATION, independent of whether an earlier retirement succeeded.
# ---------------------------------------------------------------------------


def test_identity_bound_mint_stable_for_same_identity(tmp_path):
    from webhook_auth import ensure_secret_for_identity

    a = ensure_secret_for_identity("plg-p--t", identity="i1",
                                   secrets_dir=tmp_path)
    b = ensure_secret_for_identity("plg-p--t", identity="i1",
                                   secrets_dir=tmp_path)
    assert a is not None and a == b


def test_identity_change_rekeys_even_if_retire_was_skipped(tmp_path):
    """The original P1-4 scenario: the old artifact's secret SURVIVED (a
    failed/skipped retirement); the new identity's activation must never
    reuse it."""
    from webhook_auth import ensure_secret_for_identity

    old = ensure_secret_for_identity("plg-p--t", identity="old-artifact",
                                     secrets_dir=tmp_path)
    new = ensure_secret_for_identity("plg-p--t", identity="new-artifact",
                                     secrets_dir=tmp_path)
    assert new is not None and new != old


def test_unbound_existing_secret_is_rekeyed(tmp_path):
    """A live secret with no .ident sidecar (pre-Release-B mint, crash, or a
    handler lazy mint) has unknown provenance — rekey, never reuse."""
    from webhook_auth import ensure_secret_for_identity

    legacy = ensure_secret("plg-p--t", owner="casa", secrets_dir=tmp_path)
    bound = ensure_secret_for_identity("plg-p--t", identity="i1",
                                       secrets_dir=tmp_path)
    assert bound is not None and bound != legacy


def test_rekey_failure_fails_closed_to_none(tmp_path, monkeypatch):
    """If the stale secret cannot actually be removed, activation returns
    None (trigger stays unrouted with trigger_secret_missing) — NEVER the
    surviving old credential."""
    import webhook_auth
    from webhook_auth import ensure_secret_for_identity

    ensure_secret_for_identity("plg-p--t", identity="old",
                               secrets_dir=tmp_path)
    monkeypatch.setattr(webhook_auth, "retire_secret",
                        lambda name, *, secrets_dir: None)  # retire no-ops
    assert ensure_secret_for_identity("plg-p--t", identity="new",
                                      secrets_dir=tmp_path) is None


def test_retire_removes_identity_sidecar_too(tmp_path):
    from webhook_auth import ensure_secret_for_identity, retire_secret

    ensure_secret_for_identity("plg-p--t", identity="i1",
                               secrets_dir=tmp_path)
    assert (tmp_path / "plg-p--t.ident").exists()
    retire_secret("plg-p--t", secrets_dir=tmp_path)
    assert not (tmp_path / "plg-p--t.ident").exists()


def test_prefix_retirement_covers_ident_sidecars(tmp_path):
    from webhook_auth import (ensure_secret_for_identity,
                              retire_secrets_with_prefix)

    ensure_secret_for_identity("plg-p--t", identity="i1",
                               secrets_dir=tmp_path)
    retired = retire_secrets_with_prefix("plg-p--", secrets_dir=tmp_path)
    assert retired == ["plg-p--t"]
    assert not (tmp_path / "plg-p--t.ident").exists()


def test_usable_webhook_secret_refuses_an_unresolved_reference():
    """Terra r1 (#333): when op:// resolution fails at boot, the raw reference
    must never become the HMAC verification key — a vault path is a
    predictable, non-secret string, so verifying against it is fail-open.
    Blank means every authenticated request is rejected loudly instead."""
    from webhook_auth import usable_webhook_secret
    assert usable_webhook_secret("op://Casa/Webhook/credential") == ""
    assert usable_webhook_secret("real-secret") == "real-secret"
    assert usable_webhook_secret("") == ""


def test_casa_core_filters_the_loaded_webhook_secret():
    """Wiring pin: both sources of the effective webhook secret (resolved env
    and the /data file fallback) pass through usable_webhook_secret."""
    import inspect
    import casa_core
    source = inspect.getsource(casa_core.main)
    assert "usable_webhook_secret(" in source


# ---------------------------------------------------------------------------
# #622 - a short write must never publish a truncated secret.
#
# `os.write` is not guaranteed to consume the whole buffer. `_publish` used a
# single unchecked call, so a short write staged a partial value, fsynced it,
# and hard-linked it into place as the live slot. Nothing raised, and because
# `os.link` never clobbers and no Casa path unlinks a resident slot, the
# truncation was PERMANENT: every later mint re-entered `_publish`, failed to
# clobber, and returned None again - the state did not heal when the disk did.
#
# Two distinct conditions, and they want opposite outcomes:
#   BENIGN  - the write is short but the next one completes. The loop must
#             finish the buffer and publish the WHOLE value.
#   EXHAUSTED - the write is short because the filesystem is full, so the next
#             one raises. Nothing may be published, and the name must be left
#             free so a later attempt can succeed.
#
# The driver targets only descriptors opened under this test's own tmp dir. It
# must never use RLIMIT_FSIZE, which is process-wide while the gate runs under
# `-n auto`.
# ---------------------------------------------------------------------------


def _short_write_under(monkeypatch, root: Path, limit: int, *, then_fail: bool):
    """First write to any fd under *root* stops at *limit*; later writes to
    that fd either complete normally or raise ENOSPC."""
    import errno as _errno

    import webhook_auth

    real_open, real_write = os.open, os.write
    targeted: set[int] = set()
    shortened: list[int] = []

    def fake_open(path, flags, mode=0o777, **kw):
        fd = real_open(path, flags, mode, **kw)
        try:
            if str(path).startswith(str(root)):
                targeted.add(fd)
        except Exception:
            pass
        return fd

    def fake_write(fd, data):
        if fd not in targeted:
            return real_write(fd, data)
        if not shortened:
            shortened.append(fd)
            return real_write(fd, bytes(data)[:limit])
        if then_fail:
            raise OSError(_errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(webhook_auth.os, "open", fake_open)
    monkeypatch.setattr(webhook_auth.os, "write", fake_write)
    return shortened


def test_a_short_write_that_can_finish_publishes_the_whole_value(tmp_path: Path, monkeypatch):
    """BENIGN. Red case: the unchecked `os.write` published the first 20 bytes
    as the live slot, so `read_secret` returned None forever."""
    shortened = _short_write_under(monkeypatch, tmp_path, 20, then_fail=False)

    value = ensure_secret("vm", owner="casa", secrets_dir=tmp_path)

    assert shortened, "premise: the driver actually shortened a write"
    assert value is not None and len(value) == 43
    assert (tmp_path / "vm").stat().st_size == 43
    assert read_secret("vm", owner="casa", secrets_dir=tmp_path) == value


def test_an_exhausted_disk_publishes_nothing_and_leaves_the_name_free(tmp_path: Path, monkeypatch):
    """EXHAUSTED. Red case: no raise, a 20-byte live slot, and three retries on
    a healthy disk all returned None with the slot unchanged - permanent."""
    _short_write_under(monkeypatch, tmp_path, 20, then_fail=True)

    with pytest.raises(OSError):
        ensure_secret("vm", owner="casa", secrets_dir=tmp_path)

    assert not (tmp_path / "vm").exists(), "a partial value reached the live slot"
    assert sorted(p.name for p in tmp_path.iterdir()) == [], "staging file left behind"

    monkeypatch.undo()
    value = ensure_secret("vm", owner="casa", secrets_dir=tmp_path)
    assert value is not None and len(value) == 43


def test_a_short_write_never_publishes_a_truncated_identity_binding(tmp_path: Path, monkeypatch):
    """`_write_ident` has the same shape, and a truncated `.ident` is worse
    than a missing one: it reads as a MISMATCHED identity rather than an absent
    binding, and `ensure_secret_for_identity` retires and re-mints on mismatch.
    Red case: it published a 4-byte binding and returned True."""
    import webhook_auth

    ensure_secret("plg-acme--hook", owner="casa", secrets_dir=tmp_path)
    _short_write_under(monkeypatch, tmp_path, 4, then_fail=True)

    assert webhook_auth._write_ident("plg-acme--hook", "identity-abcdef", tmp_path) is False
    assert not (tmp_path / "plg-acme--hook.ident").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".ident-")]


# ---------------------------------------------------------------------------
# #609 — a scan has THREE answers, not two. `_read_final` collapses absent,
# unreadable, non-regular, symlinked and present-but-invalid to None, which is
# right for the request path (all of them must fail closed) and wrong for
# anything deciding what to DO: only `absent` may be minted into, and only
# `absent` may be reported as "not placed yet".
# ---------------------------------------------------------------------------


def test_probe_discriminates_every_condition_read_secret_collapses(tmp_path: Path):
    """Red case: assert each state against `read_secret` instead and every row
    reads the same — that indistinguishability is the defect."""
    import webhook_auth

    good = ensure_secret("vm", owner="casa", secrets_dir=tmp_path)
    (tmp_path / "short").write_bytes(b"tooshort")
    os.symlink(tmp_path / "vm", tmp_path / "link")
    os.mkdir(tmp_path / "adir")
    blocked = tmp_path / "blocked"
    blocked.write_bytes(b"x" * 43)
    os.chmod(blocked, 0)
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_bytes(b"x")

    def state(name, owner="casa", where=tmp_path):
        return webhook_auth.probe_secret(name, owner=owner, secrets_dir=where)[0]

    assert {
        "missing": state("nope"),
        "valid": state("vm"),
        "wrong_shape": state("short"),
        "unreadable": state("blocked"),
        "symlink": state("link"),
        "directory": state("adir"),
        "file_as_secrets_dir": state("vm", where=not_a_dir),
    } == {
        "missing": "absent",
        "valid": "readable",
        "wrong_shape": "invalid",
        "unreadable": "unreadable",
        "symlink": "unreadable",
        "directory": "unreadable",
        "file_as_secrets_dir": "unreadable",
    }
    # Every one of those is None to the request path — which is correct there.
    assert read_secret("vm", owner="casa", secrets_dir=tmp_path) == good
    assert all(read_secret(n, owner="casa", secrets_dir=tmp_path) is None
               for n in ("nope", "short", "blocked", "link", "adir"))


def test_probe_agrees_with_read_secret_on_whether_bytes_are_usable(tmp_path: Path):
    """The probe must never call a slot readable that the request path would
    refuse, or vice versa — they share `_valid_value` and must stay agreed."""
    import webhook_auth

    ensure_secret("casa-slot", owner="casa", secrets_dir=tmp_path)
    (tmp_path / "prov-slot").write_bytes(b"opaque-provider-value")
    (tmp_path / "bad").write_bytes(b"\x00\x01")
    for name, owner in (("casa-slot", "casa"), ("casa-slot", "provider"),
                        ("prov-slot", "casa"), ("prov-slot", "provider"),
                        ("bad", "casa"), ("bad", "provider"), ("gone", "casa")):
        probed = webhook_auth.probe_secret(name, owner=owner, secrets_dir=tmp_path)[0]
        usable = read_secret(name, owner=owner, secrets_dir=tmp_path) is not None
        assert (probed == "readable") is usable, (name, owner, probed, usable)


def test_a_file_where_the_secrets_dir_should_be_is_not_absent(tmp_path: Path):
    """ENOTDIR must never read as `absent`. If it did, the writer would call
    `ensure_secret`, whose `mkdir` raises EEXIST — on every pass, forever, with
    no Casa surface able to clear it."""
    import webhook_auth

    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")
    assert webhook_auth.probe_secret("vm", owner="casa", secrets_dir=blocker)[0] == "unreadable"
    with pytest.raises(OSError):
        ensure_secret("vm", owner="casa", secrets_dir=blocker)
