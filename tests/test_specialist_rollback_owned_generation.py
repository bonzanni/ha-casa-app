"""#810 — a specialist rollback exchanges the WHOLE retained generation: the tuple
AND the owned-plugin set of the generation the retained prior tuple holds.

INV-SPEC-011 (declared): the commit that rotates the active tuple into the prior
rotates the active owned-plugins sidecar into the prior sidecar in the same step,
whatever the sidecar's bytes; a commit that rotates no tuple rotates no sidecar; a
bundle rollback republishes the owned set of the generation it restores; a prior
rotation whose promotion failed stays pending as a pair of temporaries that the
next rollback completes before it reads the retained generation; and a rollback
that cannot swap the registry refuses a generation whose owned set differs.

Every bundle-level case here drives the REAL ``commit_specialist_install``,
``upgrade_specialist``, ``apply_persona_override`` and ``rollback_specialist``
over the shipped bundle fixtures on ``tmp_path``. Assertions are counts, bytes
and the artifact ids the real publish minted — never "no exception".

Specified externally (sol, red-case round, MODE: SPECIFY); acceptance runs
against the tests-only commit that carries this file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import plugin_registry
import specialist_bundle_journal
import specialist_install
import specialist_receipt
from test_specialist_bundle_commit import _Ctx, _owned, _subdir_stub
from test_wholebranch_security_fixes import (
    _load_specialist_persona_role, _publish_installed_copy,
)

try:
    from tests.specialist_fixtures import write_bundled_plugin, write_minimal_component
except ImportError:
    from specialist_fixtures import write_bundled_plugin, write_minimal_component

_SLUG = "mtg"
_TMP_TUPLE = "active.yaml.rollback-tmp"
_TMP_SIDECAR = "owned-plugins.yaml.rollback-tmp"


@pytest.fixture(autouse=True)
def _fresh_registry_snapshot(tmp_path):
    plugin_registry.reload_snapshot(registry_path=tmp_path / "snap-registry.json",
                                    store_root=tmp_path / "snap-store")
    yield


# ---------------------------------------------------------------------------
# Shared definitions (the specification's)
# ---------------------------------------------------------------------------


def generation_rows(doc: "dict | None") -> tuple[tuple[str, str], ...]:
    """Sorted ``(name, artifact_id)`` rows of a sidecar document; absent ⇒ ()."""
    if not doc:
        return ()
    return tuple(sorted((r["name"], r["artifact_id"]) for r in doc.get("plugins") or []))


def registry_rows(reg_path: Path, slug: str = _SLUG) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((e["name"], e["artifact_id"]) for e in _owned(reg_path, slug)))


def _slug_dir(ctx) -> Path:
    return ctx.kw["specialists_dir"] / _SLUG


def _sidecar_paths(slug_dir: Path) -> tuple[Path, Path]:
    from personality_binding import owned_plugins_path, owned_plugins_prior_path
    return owned_plugins_path(slug_dir), owned_plugins_prior_path(slug_dir)


def _temporary_file_count(slug_dir: Path) -> int:
    return sum(1 for n in (_TMP_TUPLE, _TMP_SIDECAR) if (slug_dir / n).exists())


def _bytes_or_none(path: Path) -> "bytes | None":
    """Absence is an ANSWER here (the spec's absent-prior arm), never an OSError:
    a missing prior sidecar must fail an equality, not a file open."""
    return path.read_bytes() if path.exists() else None


def _read_doc(path: Path) -> "dict | None":
    from personality_binding import read_owned_plugins
    return read_owned_plugins(path)


def _mixed_count(slug_dir: Path, reg_path: Path, declared: "list[tuple]") -> int:
    """1 when the release triple (active tuple, active sidecar rows, registry
    rows) equals no declared generation triple, else 0."""
    from personality_binding import InstanceDir
    active_sidecar, _ = _sidecar_paths(slug_dir)
    triple = (InstanceDir(slug_dir).active(),
              generation_rows(_read_doc(active_sidecar)),
              registry_rows(reg_path))
    return 0 if triple in declared else 1


# ---------------------------------------------------------------------------
# Real bundle lifecycle over the shipped fixtures
# ---------------------------------------------------------------------------


def _prep_bundle(tmp_path: Path, monkeypatch, names: "list[str]", *,
                 root: str = "v1", ref: str = "main", sha: str = "a" * 40,
                 base_ctx=None, version: "str | None" = None,
                 required: "tuple[str, ...]" = (), marker: "str | None" = None):
    """``_prep_multi`` with two knobs the spec needs: an explicit manifest
    ``version`` (``None`` keeps the fixture's, so a same-ref generation keeps the
    same install root) and a declared ``required`` config key set (the manifest
    checksum is recomputed over the rewritten schema, as the loader demands).
    ``marker`` is the plugin README text that distinguishes generations; it
    defaults to ``root`` and is set separately when a second directory must
    carry the SAME bytes as an earlier one (a same-ref generation)."""
    import plugin_store
    from specialist_component import compute_component_checksum
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity
    from specialist_registry import InstalledSpecialistIndex

    comp, mpath = write_minimal_component(tmp_path / root, slug=_SLUG)
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    for name in names:
        write_bundled_plugin(comp, name)
        (comp / "plugins" / name / "README.md").write_text(marker or root, encoding="utf-8")
        digest = "sha256:" + plugin_store.content_checksum(comp / "plugins" / name)
        manifest["dependencies"].append({
            "kind": "plugin/implementation", "identifier": name, "digest": digest,
            "source": {"type": "bundled", "path": f"plugins/{name}"}})
    if version is not None:
        manifest["version"] = version
    if required:
        (comp / "config-schema.json").write_text(
            json.dumps({"required": list(required), "secret_names": []}), encoding="utf-8")
        manifest["checksum"] = compute_component_checksum({
            "role/role.yaml": (comp / "role" / "role.yaml").read_bytes(),
            "role/doctrine.md": (comp / "role" / "doctrine.md").read_bytes(),
            "config-schema.json": (comp / "config-schema.json").read_bytes(),
        })
    mpath.write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _subdir_stub(comp, sha))
    idx = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "installed-index"))
    idx.load()
    extra = ({} if base_ctx is None
             else dict(mode="upgrade", target_slug=_SLUG,
                       specialists_dir=tmp_path / "specialists"))
    inspection = specialist_install.inspect_specialist_repo(
        "org/repo", ref, staging_root=tmp_path / f"staging-{root}",
        installed_index=idx, receipts_dir=tmp_path / "receipts", **extra)
    receipt = specialist_receipt.load(inspection.receipt_id,
                                      receipts_dir=tmp_path / "receipts")
    assert receipt is not None
    acks = base_ctx.acks if base_ctx is not None else SpecialistInstallAckStore(
        path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug,
        receipt_digest=inspection.receipt_digest)
    acks.record(identity=identity, component_id=inspection.component_id,
                version=inspection.version, component_checksum=inspection.root_digest,
                slug=inspection.slug, receipt_digest=inspection.receipt_digest)
    common = dict(
        inspection=inspection, receipt=receipt, config={},
        secret_names_provided=frozenset(), acks=acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents",
        registry_path=tmp_path / "registry.json",
        plugin_store_root=tmp_path / "store", ops_dir=tmp_path / "ops")
    if base_ctx is not None:
        common["slug"] = _SLUG
    return _Ctx(acks=acks, inspection=inspection, receipt=receipt, kw=common)


def _install(tmp_path: Path, monkeypatch, names: "list[str]", **knobs):
    ctx = _prep_bundle(tmp_path, monkeypatch, names, **knobs)
    instance, txn = specialist_install.commit_specialist_install(**ctx.kw)
    assert instance.state == "active"
    specialist_bundle_journal.complete(txn.journal_path)
    return ctx


def _upgrade(tmp_path: Path, monkeypatch, ctx, names: "list[str]", *, root: str,
             ref: str, sha: str, config: "dict | None" = None, **knobs):
    up = _prep_bundle(tmp_path, monkeypatch, names, root=root, ref=ref, sha=sha,
                      base_ctx=ctx, **knobs)
    kw = dict(up.kw)
    if config is not None:
        kw["config"] = config
    instance, txn = specialist_install.upgrade_specialist(**kw)
    assert instance.state == "active", instance.last_activation_error
    specialist_bundle_journal.complete(txn.journal_path)
    return up


def _override(tmp_path: Path, monkeypatch, ctx, *, validator=lambda p, b: None):
    from persona_install import apply_persona_override

    specialists_dir = ctx.kw["specialists_dir"]
    persona, role = _load_specialist_persona_role(specialists_dir, _SLUG)
    _publish_installed_copy(persona, specialists_dir, _SLUG, tmp_path, monkeypatch)
    return apply_persona_override(
        target_role_id=f"specialist:{_SLUG}", persona=persona, role=role,
        instance_dir_root=specialists_dir / _SLUG, candidate_validator=validator)


def _rollback(ctx):
    instance, txn = specialist_install.rollback_specialist(
        slug=_SLUG, bundle=True, acks=ctx.acks,
        specialists_dir=ctx.kw["specialists_dir"],
        agents_specialists_dir=ctx.kw["agents_specialists_dir"],
        registry_path=ctx.kw["registry_path"],
        plugin_store_root=ctx.kw["plugin_store_root"], ops_dir=ctx.kw["ops_dir"])
    assert instance.state == "active"
    specialist_bundle_journal.complete(txn.journal_path)
    return instance, txn


def _artifact_id(reg_path: Path, name: str) -> str:
    rows = {e["name"]: e["artifact_id"] for e in _owned(reg_path, _SLUG)}
    return rows[name]


# ---------------------------------------------------------------------------
# R1-R4 — the rollback republishes the owned set of the generation it restores
# ---------------------------------------------------------------------------


def test_r1_install_override_rollback_keeps_the_owned_set(tmp_path: Path, monkeypatch) -> None:
    """R1: install → first override → bundle rollback. The override rotated the
    tuple, so the retained owned generation IS the installed one; the rollback
    republishes it unchanged. RED at base: the override performs no sidecar
    step, the prior sidecar is absent, the rollback reads that as the empty set
    and removes ``mtg.mtg``."""
    from tools import _swap_removal_disclosure

    ctx = _install(tmp_path, monkeypatch, ["mtg"])
    reg = ctx.kw["registry_path"]
    a1 = _artifact_id(reg, "mtg.mtg")
    active_sidecar, prior_sidecar = _sidecar_paths(_slug_dir(ctx))

    overridden = _override(tmp_path, monkeypatch, ctx)
    assert overridden.binding.mode == "override"
    assert _bytes_or_none(prior_sidecar) == active_sidecar.read_bytes()

    _, txn = _rollback(ctx)

    assert registry_rows(reg) == (("mtg.mtg", a1),)
    assert generation_rows(_read_doc(active_sidecar)) == (("mtg.mtg", a1),)
    assert txn.removed_owned_names == ()
    assert _swap_removal_disclosure(txn) == {}


def test_r2_upgrade_then_override_then_rollback_serves_the_upgraded_set(
        tmp_path: Path, monkeypatch) -> None:
    """R2: v1 owns ``mtg.mtg``; v2 owns ``mtg.mtg`` + ``mtg.extra``; an override
    after v2, then a bundle rollback: the tuple is v2's and the owned set is
    v2's, at v2's artifact ids. RED at base: the override makes the tuple
    prior v2 but leaves the sidecar prior at v1 — the rollback restores tuple
    v2 while republishing v1's rows."""
    from personality_binding import InstanceDir

    ctx = _install(tmp_path, monkeypatch, ["mtg"])
    reg = ctx.kw["registry_path"]
    _upgrade(tmp_path, monkeypatch, ctx, ["mtg", "extra"], root="v2", ref="v2",
             sha="b" * 40, version="0.2.0")
    v2_root = InstanceDir(_slug_dir(ctx)).active().root
    a2 = _artifact_id(reg, "mtg.mtg")
    e2 = _artifact_id(reg, "mtg.extra")
    active_sidecar, _ = _sidecar_paths(_slug_dir(ctx))

    _override(tmp_path, monkeypatch, ctx)
    _, txn = _rollback(ctx)

    active = InstanceDir(_slug_dir(ctx)).active()
    assert active.root == v2_root
    assert registry_rows(reg) == (("mtg.extra", e2), ("mtg.mtg", a2))
    assert generation_rows(_read_doc(active_sidecar)) == (("mtg.extra", e2), ("mtg.mtg", a2))
    assert tuple(sorted(txn.new_artifact_ids)) == tuple(sorted((a2, e2)))
    assert txn.removed_artifact_ids == ()
    assert txn.removed_owned_names == ()


def test_r3_same_ref_config_only_upgrade_then_rollback_keeps_the_owned_set(
        tmp_path: Path, monkeypatch) -> None:
    """R3: a same-root, config-only upgrade (declared ``timezone`` Rome → Amsterdam,
    same component bytes, same sidecar bytes) rotates the tuple prior; the
    rollback restores Rome and republishes the same owned set. RED at base: the
    sidecar commit takes its byte-identical no-op branch, no prior sidecar is
    retained, and the rollback republishes the empty set."""
    from personality_binding import InstanceDir, load_instance_tuple

    ctx = _prep_bundle(tmp_path, monkeypatch, ["mtg"], required=("timezone",))
    kw = dict(ctx.kw)
    kw["config"] = {"timezone": "Europe/Rome"}
    instance, txn = specialist_install.commit_specialist_install(**kw)
    assert instance.state == "active"
    specialist_bundle_journal.complete(txn.journal_path)
    reg = ctx.kw["registry_path"]
    a1 = _artifact_id(reg, "mtg.mtg")
    slug_dir = _slug_dir(ctx)
    active_sidecar, _ = _sidecar_paths(slug_dir)
    sidecar_bytes = active_sidecar.read_bytes()
    root_before = InstanceDir(slug_dir).active().root

    up = _prep_bundle(tmp_path, monkeypatch, ["mtg"], root="v1-same-ref", marker="v1",
                      ref="main", sha="a" * 40, base_ctx=ctx, required=("timezone",))
    assert up.inspection.root_digest == ctx.inspection.root_digest
    kw2 = dict(up.kw)
    kw2["config"] = {"timezone": "Europe/Amsterdam"}
    instance, txn = specialist_install.upgrade_specialist(**kw2)
    assert instance.state == "active", instance.last_activation_error
    specialist_bundle_journal.complete(txn.journal_path)
    assert InstanceDir(slug_dir).active().root == root_before
    assert active_sidecar.read_bytes() == sidecar_bytes
    prior = load_instance_tuple(slug_dir / "active.prior.yaml")
    assert prior.config_snapshot == {"timezone": "Europe/Rome"}

    rolled_back, txn = _rollback(ctx)

    assert rolled_back.active.config_snapshot == {"timezone": "Europe/Rome"}
    assert registry_rows(reg) == (("mtg.mtg", a1),)
    assert generation_rows(_read_doc(active_sidecar)) == (("mtg.mtg", a1),)
    assert txn.removed_artifact_ids == ()
    assert txn.removed_owned_names == ()


def test_r4_two_rollbacks_exchange_without_dropping_the_owned_set(
        tmp_path: Path, monkeypatch) -> None:
    """R4: install → override → rollback → rollback. Both exchanges keep the set
    and disclose nothing; the modes alternate. RED at base: the first rollback
    removes the plugin (absent prior sidecar) and its disclosure is non-empty."""
    from personality_binding import InstanceDir
    from tools import _swap_removal_disclosure

    ctx = _install(tmp_path, monkeypatch, ["mtg"])
    reg = ctx.kw["registry_path"]
    a1 = _artifact_id(reg, "mtg.mtg")
    _override(tmp_path, monkeypatch, ctx)

    _, first = _rollback(ctx)
    rows_after_first = registry_rows(reg)
    mode_after_first = InstanceDir(_slug_dir(ctx)).active().binding.mode
    _, second = _rollback(ctx)
    rows_after_second = registry_rows(reg)
    mode_after_second = InstanceDir(_slug_dir(ctx)).active().binding.mode

    assert first.removed_owned_names == ()
    assert second.removed_owned_names == ()
    assert _swap_removal_disclosure(first) == {}
    assert _swap_removal_disclosure(second) == {}
    assert rows_after_first == (("mtg.mtg", a1),)
    assert rows_after_second == (("mtg.mtg", a1),)
    assert mode_after_first == "component-default"
    assert mode_after_second == "override"


# ---------------------------------------------------------------------------
# R5-R9 — the tuple commit owns both prior rotations (InstanceDir unit level)
# ---------------------------------------------------------------------------


def _doc(marker: str, plugins: "list[dict] | None" = None) -> dict:
    return {"schema_version": 1,
            "component_source": {"repo": "acme/mtg", "ref": marker,
                                 "revision": "git:" + "a" * 40, "subdir": ""},
            "plugins": plugins or []}


def _sidecar_bytes(tmp_path: Path, doc: dict) -> bytes:
    """The bytes ``write_owned_plugins`` persists for *doc* — measured through
    the real writer (into a probe file under ``tmp_path``) so an equality here
    is an equality against what the rotation actually copies."""
    from personality_binding import write_owned_plugins
    probe = tmp_path / ".probe" / f"{doc['component_source']['ref']}.yaml"
    probe.parent.mkdir(parents=True, exist_ok=True)
    write_owned_plugins(probe, doc)
    return probe.read_bytes()


def _commit_generation(d, tuple_, doc: "dict | None") -> None:
    """The bundle paths' stage+commit sequence, tuple then sidecar."""
    d.stage_desired(tuple_)
    if doc is not None:
        d.stage_desired_owned_plugins(doc)
    d.commit_desired_to_active()
    if doc is not None:
        d.commit_owned_plugins_desired_to_active()


class _PriorSidecarMutations:
    """Counts every mutation of ``owned-plugins.prior.yaml`` — a direct write
    (the base's copy) or a rename onto it (a promotion) — whichever mechanism
    the implementation uses."""

    def __init__(self, monkeypatch, prior: Path) -> None:
        import personality_binding
        self.count = 0
        self.promoted_sources: list[bytes] = []
        target = str(prior)
        real_replace = os.replace
        real_write_bytes = Path.write_bytes
        real_write_text = Path.write_text

        def _replace(src, dst, *a, **k):
            if str(dst) == target:
                self.count += 1
                try:
                    self.promoted_sources.append(Path(src).read_bytes())
                except OSError:
                    pass
            return real_replace(src, dst, *a, **k)

        def _write_bytes(self_path, data, *a, **k):
            if str(self_path) == target:
                self.count += 1
            return real_write_bytes(self_path, data, *a, **k)

        def _write_text(self_path, data, *a, **k):
            if str(self_path) == target:
                self.count += 1
            return real_write_text(self_path, data, *a, **k)

        monkeypatch.setattr(personality_binding.os, "replace", _replace)
        monkeypatch.setattr(Path, "write_bytes", _write_bytes)
        monkeypatch.setattr(Path, "write_text", _write_text)


def test_r5_tuple_identity_not_sidecar_bytes_controls_the_sidecar_rotation(
        tmp_path: Path, monkeypatch) -> None:
    """R5: (phase one) a DIFFERENT tuple committed with a byte-identical sidecar
    rotates the sidecar prior exactly once; (phase two) the SAME tuple recommitted
    with different sidecar bytes replaces the active sidecar and rotates nothing.
    RED at base in both phases: the sidecar rotation keys on document bytes
    (0 mutations in phase one, 1 in phase two)."""
    from personality_binding import InstanceDir, load_instance_tuple
    from test_personality_binding import _binding, _tuple

    base = tmp_path / _SLUG
    d = InstanceDir(base)
    active_sidecar, prior_sidecar = _sidecar_paths(base)
    t1 = _tuple(_binding(persona_version="0.1.0"))
    t2 = _tuple(_binding(persona_version="0.2.0"))
    s = _doc("gen-s")
    s_bytes = _sidecar_bytes(tmp_path, s)
    s_prime = _doc("gen-s-prime")
    s_prime_bytes = _sidecar_bytes(tmp_path, s_prime)

    _commit_generation(d, t1, s)
    counter = _PriorSidecarMutations(monkeypatch, prior_sidecar)
    _commit_generation(d, t2, s)

    assert counter.count == 1
    assert d.active() == t2
    assert load_instance_tuple(base / "active.prior.yaml") == t1
    assert active_sidecar.read_bytes() == s_bytes
    assert _bytes_or_none(prior_sidecar) == s_bytes

    counter.count = 0
    _commit_generation(d, t2, s_prime)

    assert counter.count == 0
    assert d.active() == t2
    assert load_instance_tuple(base / "active.prior.yaml") == t1
    assert active_sidecar.read_bytes() == s_prime_bytes
    assert _bytes_or_none(prior_sidecar) == s_bytes


def _seed_interrupted_state(tmp_path: Path, *, active, prior, tuple_temp,
                            active_doc, prior_doc, sidecar_temp_bytes: "bytes | None",
                            desired=None):
    """Write an InstanceDir exactly as a crash between the two promotions leaves
    it: every file placed directly, nothing inferred."""
    from personality_binding import atomic_write_instance_tuple, write_owned_plugins
    base = tmp_path / _SLUG
    base.mkdir(parents=True, exist_ok=True)
    atomic_write_instance_tuple(base / "active.yaml", active)
    if desired is not None:
        atomic_write_instance_tuple(base / "desired.yaml", desired)
    if prior is not None:
        atomic_write_instance_tuple(base / "active.prior.yaml", prior)
    if tuple_temp is not None:
        if isinstance(tuple_temp, bytes):
            (base / _TMP_TUPLE).write_bytes(tuple_temp)
        else:
            atomic_write_instance_tuple(base / _TMP_TUPLE, tuple_temp)
    active_sidecar, prior_sidecar = _sidecar_paths(base)
    write_owned_plugins(active_sidecar, active_doc)
    if prior_doc is not None:
        write_owned_plugins(prior_sidecar, prior_doc)
    if sidecar_temp_bytes is not None:
        (base / _TMP_SIDECAR).write_bytes(sidecar_temp_bytes)
    return base


def test_r6_a_genuine_crash_retry_promotes_both_temporaries(tmp_path: Path) -> None:
    """R6: the exact interrupted state — active T3 (desired T3), visible prior
    T1, tuple temporary T2, active sidecar S3, visible prior sidecar S1, sidecar
    temporary S2 — recommitted: BOTH temporaries are promoted. RED at base: the
    tuple temporary is promoted, the sidecar temporary is ignored and stays on
    disk; the prior sidecar stays S1."""
    from personality_binding import InstanceDir, load_instance_tuple
    from test_personality_binding import _binding, _tuple

    t1, t2, t3 = (_tuple(_binding(persona_version=v)) for v in ("0.1.0", "0.2.0", "0.3.0"))
    s1, s2, s3 = _doc("s1"), _doc("s2"), _doc("s3")
    s2_bytes = _sidecar_bytes(tmp_path, s2)
    base = _seed_interrupted_state(
        tmp_path, active=t3, desired=t3, prior=t1, tuple_temp=t2,
        active_doc=s3, prior_doc=s1, sidecar_temp_bytes=s2_bytes)
    _, prior_sidecar = _sidecar_paths(base)

    InstanceDir(base).commit_desired_to_active()

    assert load_instance_tuple(base / "active.prior.yaml") == t2
    assert _bytes_or_none(prior_sidecar) == s2_bytes
    assert _temporary_file_count(base) == 0
    assert sum(1 for n in ("desired.yaml",) if (base / n).exists()) == 0


@pytest.mark.parametrize("tuple_temp_arm", ["equal", "unloadable"])
def test_r7_a_stale_or_corrupt_pair_is_discarded_together(
        tmp_path: Path, tuple_temp_arm: str) -> None:
    """R7: a tuple temporary equal to the active (the #346 stale pair) or
    unloadable discards BOTH temporaries; the true prior pair survives. RED at
    base: the tuple temporary is discarded, the paired sidecar temporary
    survives."""
    from personality_binding import InstanceDir, load_instance_tuple
    from test_personality_binding import _binding, _tuple

    t1, t2 = (_tuple(_binding(persona_version=v)) for v in ("0.1.0", "0.2.0"))
    s1, s2 = _doc("s1"), _doc("s2")
    s1_bytes = _sidecar_bytes(tmp_path, s1)
    tuple_temp = t2 if tuple_temp_arm == "equal" else b"{{{ garbage"
    base = _seed_interrupted_state(
        tmp_path, active=t2, desired=t2, prior=t1, tuple_temp=tuple_temp,
        active_doc=s2, prior_doc=s1, sidecar_temp_bytes=b"STALE: 1\n")
    _, prior_sidecar = _sidecar_paths(base)

    InstanceDir(base).commit_desired_to_active()

    assert load_instance_tuple(base / "active.prior.yaml") == t1
    assert _bytes_or_none(prior_sidecar) == s1_bytes
    assert _temporary_file_count(base) == 0


def test_r8_a_failed_active_write_removes_both_newly_created_temporaries(
        tmp_path: Path, monkeypatch) -> None:
    """R8: staging T2/S2 over active T1/S1 and forcing the active write to
    raise: both temporaries were CREATED (one write each) and both are gone;
    active T1/S1 untouched. RED at base: no sidecar temporary is ever written
    (count 0) — a weaker "both absent afterward" assertion is green at base
    because the second temporary never existed."""
    import personality_binding
    from personality_binding import InstanceDir
    from test_personality_binding import _binding, _tuple

    base = tmp_path / _SLUG
    d = InstanceDir(base)
    t1, t2 = (_tuple(_binding(persona_version=v)) for v in ("0.1.0", "0.2.0"))
    s1, s2 = _doc("s1"), _doc("s2")
    s1_bytes = _sidecar_bytes(tmp_path, s1)
    _commit_generation(d, t1, s1)
    active_sidecar, _ = _sidecar_paths(base)

    writes = {_TMP_TUPLE: 0, _TMP_SIDECAR: 0}
    real_write_bytes = Path.write_bytes

    def _counting_write_bytes(self_path, data, *a, **k):
        if self_path.name in writes:
            writes[self_path.name] += 1
        return real_write_bytes(self_path, data, *a, **k)

    real_atomic = personality_binding.atomic_write_instance_tuple

    def _failing_active_write(path, tuple_):
        if path.name == "active.yaml":
            raise OSError("ENOSPC on active write")
        return real_atomic(path, tuple_)

    monkeypatch.setattr(Path, "write_bytes", _counting_write_bytes)
    monkeypatch.setattr(personality_binding, "atomic_write_instance_tuple", _failing_active_write)
    d.stage_desired(t2)
    d.stage_desired_owned_plugins(s2)
    with pytest.raises(OSError):
        d.commit_desired_to_active()

    assert writes[_TMP_TUPLE] == 1
    assert writes[_TMP_SIDECAR] == 1
    assert _temporary_file_count(base) == 0
    assert d.active() == t1
    assert active_sidecar.read_bytes() == s1_bytes


def test_r9_the_journal_snapshot_captures_the_sidecar_temporary(tmp_path: Path) -> None:
    """R9: the closed journalled-file set gains the sidecar temporary, and the
    before-state snapshot carries its bytes. RED at base: eight entries, the
    path is absent from the snapshot."""
    from specialist_bundle_journal import TUPLE_FILENAMES

    slug_dir = tmp_path / _SLUG
    slug_dir.mkdir()
    s_tmp = b"schema_version: 1\ncomponent_source: {}\nplugins: []\n# S_TMP\n"
    (slug_dir / _TMP_SIDECAR).write_bytes(s_tmp)

    snapshot = specialist_install._tuple_files_snapshot(slug_dir)

    assert len(TUPLE_FILENAMES) == 9
    assert set(snapshot) == set(TUPLE_FILENAMES)
    assert snapshot.get(_TMP_SIDECAR) == s_tmp.decode()


# ---------------------------------------------------------------------------
# R10-R12 — a pending promotion is completed before the rollback reads the prior
# ---------------------------------------------------------------------------


def _three_generations(tmp_path: Path, monkeypatch, *, before_v3=None):
    """install v1 (mtg) → upgrade v2 (mtg + extra) → upgrade v3 (mtg), capturing
    each generation's tuple and sidecar rows as it becomes active. ``before_v3``
    runs after v2 is captured and before v3's upgrade — where a forced failure
    is installed."""
    from personality_binding import InstanceDir

    ctx = _install(tmp_path, monkeypatch, ["mtg"])
    reg = ctx.kw["registry_path"]
    slug_dir = _slug_dir(ctx)
    active_sidecar, _ = _sidecar_paths(slug_dir)
    gens = {}

    def _capture(tag):
        gens[tag] = (InstanceDir(slug_dir).active(), _read_doc(active_sidecar),
                     active_sidecar.read_bytes(), registry_rows(reg))

    _capture("g1")
    _upgrade(tmp_path, monkeypatch, ctx, ["mtg", "extra"], root="v2", ref="v2",
             sha="b" * 40, version="0.2.0")
    _capture("g2")
    if before_v3 is not None:
        before_v3()
    _upgrade(tmp_path, monkeypatch, ctx, ["mtg"], root="v3", ref="v3",
             sha="c" * 40, version="0.3.0")
    _capture("g3")
    return ctx, gens


def _declared(gens) -> "list[tuple]":
    return [(t, generation_rows(doc), rows) for (t, doc, _b, rows) in gens.values()]


class _ForcedReplaceFailure:
    """Raise ``OSError`` on exactly the FIRST ``os.replace`` whose (src, dst)
    names match; count every match that fired."""

    def __init__(self, monkeypatch, *, src_suffix: "str | None", dst_suffix: str) -> None:
        import personality_binding
        self.count = 0
        real_replace = os.replace

        def _replace(src, dst, *a, **k):
            if (str(dst).endswith(dst_suffix)
                    and (src_suffix is None or str(src).endswith(src_suffix))
                    and self.count == 0):
                self.count += 1
                raise OSError("EIO on prior promotion")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(personality_binding.os, "replace", _replace)


def test_r10_a_failed_tuple_prior_promotion_is_completed_before_the_rollback_reads(
        tmp_path: Path, monkeypatch) -> None:
    """R10: v3's commit succeeds but its tuple-prior promotion fails (visible
    prior stays T1/S1, the pair T2/S2 pending as temporaries); the next bundle
    rollback restores T2 — the immediately preceding generation — with S2's
    owned set, and leaves no temporary. RED at base: the rollback reads the
    visible prior T1 and never looks at the temporary holding T2."""
    holder = {}

    def _install_failure():
        holder["f"] = _ForcedReplaceFailure(monkeypatch, src_suffix=None,
                                            dst_suffix="active.prior.yaml")

    ctx, gens = _three_generations(tmp_path, monkeypatch, before_v3=_install_failure)
    reg = ctx.kw["registry_path"]
    slug_dir = _slug_dir(ctx)
    active_sidecar, _ = _sidecar_paths(slug_dir)
    t2, s2_doc, s2_bytes, _ = gens["g2"]
    assert holder["f"].count == 1

    rolled_back, _ = _rollback(ctx)

    assert rolled_back.active == t2
    assert registry_rows(reg) == generation_rows(s2_doc)
    assert active_sidecar.read_bytes() == s2_bytes
    assert _temporary_file_count(slug_dir) == 0
    assert _mixed_count(slug_dir, reg, _declared(gens)) == 0


def test_r11_a_failed_sidecar_prior_promotion_is_completed_before_the_rollback_reads(
        tmp_path: Path, monkeypatch) -> None:
    """R11: v3's tuple-prior promotion succeeds and ONLY the sidecar-prior
    promotion (temporary → ``owned-plugins.prior.yaml``) fails; the next bundle
    rollback promotes the temporary first and republishes S2's set. RED at
    base: no such promotion is ever attempted (``failed == 0``) — the base
    copies the prior directly and has no pending sidecar promotion, so it
    could reach S2 for the wrong reason without this count."""
    holder = {}

    def _install_failure():
        holder["f"] = _ForcedReplaceFailure(
            monkeypatch, src_suffix=_TMP_SIDECAR, dst_suffix="owned-plugins.prior.yaml")

    ctx, gens = _three_generations(tmp_path, monkeypatch, before_v3=_install_failure)
    reg = ctx.kw["registry_path"]
    slug_dir = _slug_dir(ctx)
    active_sidecar, _ = _sidecar_paths(slug_dir)
    t2, s2_doc, s2_bytes, _ = gens["g2"]

    assert holder["f"].count == 1

    rolled_back, _ = _rollback(ctx)

    assert rolled_back.active == t2
    assert registry_rows(reg) == generation_rows(s2_doc)
    assert active_sidecar.read_bytes() == s2_bytes
    assert _temporary_file_count(slug_dir) == 0
    assert _mixed_count(slug_dir, reg, _declared(gens)) == 0


def test_r12a_a_lone_sidecar_temporary_is_promoted_by_a_recommit_and_read_by_the_rollback(
        tmp_path: Path, monkeypatch) -> None:
    """R12 subcase A: active T3/S3, visible prior T2 with sidecar S1, no tuple
    temporary, a lone sidecar temporary holding S2 (the state a failed sidecar
    promotion after a successful tuple promotion leaves). A no-op recommit of T3
    promotes it exactly once; the rollback then republishes S2's set. RED at
    base: the lone temporary is never promoted."""
    from personality_binding import InstanceDir, write_owned_plugins

    ctx, gens = _three_generations(tmp_path, monkeypatch)
    reg = ctx.kw["registry_path"]
    slug_dir = _slug_dir(ctx)
    active_sidecar, prior_sidecar = _sidecar_paths(slug_dir)
    t3 = gens["g3"][0]
    _, s2_doc, s2_bytes, _ = gens["g2"]
    s1_doc = gens["g1"][1]
    write_owned_plugins(prior_sidecar, s1_doc)          # visible prior sidecar S1
    (slug_dir / _TMP_SIDECAR).write_bytes(s2_bytes)      # the lone temporary S2
    assert not (slug_dir / _TMP_TUPLE).exists()
    counter = _PriorSidecarMutations(monkeypatch, prior_sidecar)

    d = InstanceDir(slug_dir)
    d.stage_desired(t3)
    d.commit_desired_to_active()

    assert sum(1 for b in counter.promoted_sources if b == s2_bytes) == 1
    assert _bytes_or_none(prior_sidecar) == s2_bytes
    assert _temporary_file_count(slug_dir) == 0

    _rollback(ctx)

    assert registry_rows(reg) == generation_rows(s2_doc)
    assert active_sidecar.read_bytes() == s2_bytes
    assert _temporary_file_count(slug_dir) == 0


def test_r12b_a_lone_sidecar_temporary_is_promoted_by_the_next_commit_of_any_tuple(
        tmp_path: Path, monkeypatch) -> None:
    """R12 subcase B: the SAME lone temporary S2 and a commit of a DIFFERENT tuple
    T4/S4 — the pending promotion is completed first (exactly once), then the
    rotation proceeds: active T4/S4, prior T3/S3, no temporaries. RED at base:
    the lone temporary is never promoted (0)."""
    from personality_binding import InstanceDir, load_instance_tuple
    from test_personality_binding import _binding, _tuple

    base = tmp_path / _SLUG
    d = InstanceDir(base)
    t3, t4 = (_tuple(_binding(persona_version=v)) for v in ("0.3.0", "0.4.0"))
    s2, s3, s4 = _doc("s2"), _doc("s3"), _doc("s4")
    s2_bytes, s3_bytes, s4_bytes = (_sidecar_bytes(tmp_path, x) for x in (s2, s3, s4))
    _commit_generation(d, t3, s3)
    (base / _TMP_SIDECAR).write_bytes(s2_bytes)
    active_sidecar, prior_sidecar = _sidecar_paths(base)
    counter = _PriorSidecarMutations(monkeypatch, prior_sidecar)

    _commit_generation(d, t4, s4)

    assert sum(1 for b in counter.promoted_sources if b == s2_bytes) == 1
    assert d.active() == t4
    assert load_instance_tuple(base / "active.prior.yaml") == t3
    assert active_sidecar.read_bytes() == s4_bytes
    assert _bytes_or_none(prior_sidecar) == s3_bytes
    assert _temporary_file_count(base) == 0


# ---------------------------------------------------------------------------
# R13 — the direct arm refuses a generation whose owned set differs
# ---------------------------------------------------------------------------


def test_r13_a_direct_rollback_refuses_a_generation_whose_owned_set_differs(
        tmp_path: Path, monkeypatch) -> None:
    """R13: v1 owns ``mtg.mtg``; v2 owns ``mtg.mtg`` + ``mtg.extra``;
    ``rollback_specialist(bundle=False)`` — an arm that cannot swap the
    registry — refuses ``bundle_required`` before ANY write. RED at base: the
    direct arm restores tuple v1 (two tuple writes: staging and the commit)
    and returns, with registry and active sidecar still v2's."""
    import personality_binding

    ctx = _install(tmp_path, monkeypatch, ["mtg"])
    _upgrade(tmp_path, monkeypatch, ctx, ["mtg", "extra"], root="v2", ref="v2",
             sha="b" * 40, version="0.2.0")
    reg = ctx.kw["registry_path"]
    slug_dir = _slug_dir(ctx)
    active_sidecar, _ = _sidecar_paths(slug_dir)
    tuple_before = (slug_dir / "active.yaml").read_bytes()
    sidecar_before = active_sidecar.read_bytes()
    registry_before = reg.read_bytes()

    counts = {"tuple": 0, "sidecar": 0, "journal": 0}
    real_atomic = personality_binding.atomic_write_instance_tuple
    real_write_doc = personality_binding.write_owned_plugins
    real_begin = specialist_bundle_journal.begin

    def _count_tuple(path, tuple_):
        counts["tuple"] += 1
        return real_atomic(path, tuple_)

    def _count_sidecar(path, doc):
        counts["sidecar"] += 1
        return real_write_doc(path, doc)

    def _count_begin(*a, **k):
        counts["journal"] += 1
        return real_begin(*a, **k)

    monkeypatch.setattr(personality_binding, "atomic_write_instance_tuple", _count_tuple)
    monkeypatch.setattr(personality_binding, "write_owned_plugins", _count_sidecar)
    monkeypatch.setattr(specialist_bundle_journal, "begin", _count_begin)

    with pytest.raises(specialist_install.SpecialistInstallError) as raised:
        specialist_install.rollback_specialist(
            slug=_SLUG, bundle=False, specialists_dir=ctx.kw["specialists_dir"],
            agents_specialists_dir=ctx.kw["agents_specialists_dir"])

    assert raised.value.kind == "bundle_required"
    assert counts["tuple"] == 0
    assert counts["sidecar"] == 0
    assert counts["journal"] == 0
    assert (slug_dir / "active.yaml").read_bytes() == tuple_before
    assert active_sidecar.read_bytes() == sidecar_before
    assert reg.read_bytes() == registry_before
