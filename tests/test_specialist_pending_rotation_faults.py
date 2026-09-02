"""#810 (INV-SPEC-011, diff review round 5) — a pending prior pair is classified
by its BYTES alone; an I/O failure while reading it propagates and leaves the
pair untouched.

``InstanceDir.complete_pending_rotation`` used to treat an ``OSError`` reading
the tuple temporary like a parse failure, discarding a GENUINE pending pair on a
transient read error — after which a rollback restored the older visible prior
and reported success. Now only a parse or schema failure classifies a pair as
stale or corrupt; the rollback that hits an ``OSError`` refuses
``pending_rotation_failed`` with both temporaries and both prior files exactly
as they were, and a commit that hits one logs it and leaves the pair for the
next completion. Fault injection on the read of the pending temporary; the
assertions are file counts and bytes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_specialist_rollback_owned_generation import (
    _SLUG, _TMP_SIDECAR, _TMP_TUPLE, _ForcedReplaceFailure, _sidecar_paths, _slug_dir,
    _three_generations,
)


def _fail_once_reading(monkeypatch, target: Path) -> dict:
    """Make the FIRST ``open`` of *target* for reading raise ``OSError``; count."""
    import builtins
    import io
    real_open = builtins.open
    real_path_open = Path.open
    fired = {"count": 0}

    def _should_fail(path, mode) -> bool:
        return str(path) == str(target) and "r" in str(mode) and fired["count"] == 0

    def _open(file, mode="r", *a, **k):
        if _should_fail(file, mode):
            fired["count"] += 1
            raise OSError(5, "EIO reading the pending temporary")
        return real_open(file, mode, *a, **k)

    def _path_open(self_path, mode="r", *a, **k):
        if _should_fail(self_path, mode):
            fired["count"] += 1
            raise OSError(5, "EIO reading the pending temporary")
        return real_path_open(self_path, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", _open)
    monkeypatch.setattr(Path, "open", _path_open)
    monkeypatch.setattr(io, "open", _open)
    return fired


def _pending_state(tmp_path: Path, monkeypatch):
    """Three real generations with v3's tuple-prior promotion forced to fail:
    active T3/S3, visible prior T1/S1, the genuine pair T2/S2 pending."""
    holder = {}

    def _install_failure():
        holder["f"] = _ForcedReplaceFailure(monkeypatch, src_suffix=None,
                                            dst_suffix="active.prior.yaml")

    ctx, gens = _three_generations(tmp_path, monkeypatch, before_v3=_install_failure)
    assert holder["f"].count == 1
    slug_dir = _slug_dir(ctx)
    assert (slug_dir / _TMP_TUPLE).exists() and (slug_dir / _TMP_SIDECAR).exists()
    return ctx, gens, slug_dir


def _snapshot(slug_dir: Path) -> dict:
    _, prior_sidecar = _sidecar_paths(slug_dir)
    return {n: (slug_dir / n).read_bytes() if (slug_dir / n).exists() else None
            for n in (_TMP_TUPLE, _TMP_SIDECAR, "active.prior.yaml", "active.yaml",
                      prior_sidecar.name, "owned-plugins.yaml")}


def test_a_rollback_whose_pending_read_fails_refuses_and_leaves_the_pair(
        tmp_path: Path, monkeypatch) -> None:
    """Rollback with the pending pair unreadable ONCE: refused
    ``pending_rotation_failed``; both temporaries still present; every retained
    and active file byte-identical; the registry untouched. Mutant: classify
    the read error as corruption again → the pair is discarded and the
    rollback restores T1 with S1's rows and reports success."""
    import specialist_install

    ctx, gens, slug_dir = _pending_state(tmp_path, monkeypatch)
    reg = ctx.kw["registry_path"]
    before = _snapshot(slug_dir)
    registry_before = reg.read_bytes()
    fired = _fail_once_reading(monkeypatch, slug_dir / _TMP_TUPLE)

    with pytest.raises(specialist_install.SpecialistInstallError) as raised:
        specialist_install.rollback_specialist(
            slug=_SLUG, bundle=True, acks=ctx.acks,
            specialists_dir=ctx.kw["specialists_dir"],
            agents_specialists_dir=ctx.kw["agents_specialists_dir"],
            registry_path=reg, plugin_store_root=ctx.kw["plugin_store_root"],
            ops_dir=ctx.kw["ops_dir"])

    assert raised.value.kind == "pending_rotation_failed"
    assert fired["count"] == 1
    assert _snapshot(slug_dir) == before
    assert sum(1 for n in (_TMP_TUPLE, _TMP_SIDECAR) if (slug_dir / n).exists()) == 2
    assert reg.read_bytes() == registry_before
    assert list(ctx.kw["ops_dir"].glob("*.json")) == []       # no journal was begun


def test_a_commit_whose_pending_read_fails_leaves_the_pair_for_the_next_completion(
        tmp_path: Path, monkeypatch) -> None:
    """The commit-core twin: a no-op recommit of T3 whose pending read fails
    once still returns (the commit is durable), leaves both temporaries and
    the visible prior untouched; the NEXT completion promotes the genuine pair
    T2/S2."""
    from personality_binding import InstanceDir, load_instance_tuple

    ctx, gens, slug_dir = _pending_state(tmp_path, monkeypatch)
    t2, _, s2_bytes, _ = gens["g2"]
    t3 = gens["g3"][0]
    before = _snapshot(slug_dir)
    fired = _fail_once_reading(monkeypatch, slug_dir / _TMP_TUPLE)
    _, prior_sidecar = _sidecar_paths(slug_dir)

    d = InstanceDir(slug_dir)
    d.stage_desired(t3)
    assert d.commit_desired_to_active() == t3
    assert fired["count"] == 1
    after = _snapshot(slug_dir)
    assert {k: v for k, v in after.items()} == before

    d.complete_pending_rotation()

    assert load_instance_tuple(slug_dir / "active.prior.yaml") == t2
    assert prior_sidecar.read_bytes() == s2_bytes
    assert sum(1 for n in (_TMP_TUPLE, _TMP_SIDECAR) if (slug_dir / n).exists()) == 0
