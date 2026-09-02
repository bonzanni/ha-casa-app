from pathlib import Path

import pytest
import yaml

from personality_binding import (
    EMPTY_CONFIG_DIGEST,
    BindingRecord,
    InstanceDir,
    InstanceTuple,
    compute_binding_digest,
    compute_effective_config_digest,
)
from trait_renderer import RENDERER_VERSION


def _digest_inputs(**overrides) -> dict:
    base = {
        "stable_agent_id": "resident:butler",
        "role_checksum": "sha256:" + "1" * 64,
        "persona_id": "casa/tina",
        "persona_version": "0.1.0",
        "persona_checksum": "sha256:" + "2" * 64,
        "compiler_schema_version": RENDERER_VERSION,
        "dependency_digests": (),
        "effective_config_digest": EMPTY_CONFIG_DIGEST,
    }
    base.update(overrides)
    return base


def test_digest_changes_for_every_normative_input() -> None:
    baseline = compute_binding_digest(**_digest_inputs())
    for key, value in {
        "stable_agent_id": "resident:concierge",
        "role_checksum": "sha256:" + "9" * 64,
        "persona_id": "casa/gary",
        "persona_version": "0.2.0",
        "persona_checksum": "sha256:" + "8" * 64,
        "compiler_schema_version": RENDERER_VERSION + "-changed",
        "dependency_digests": ("sha256:" + "7" * 64,),
        "effective_config_digest": "sha256:" + "6" * 64,
    }.items():
        assert compute_binding_digest(**_digest_inputs(**{key: value})) != baseline


def test_digest_input_set_matches_the_normative_eight_fields() -> None:
    assert set(_digest_inputs()) == {
        "stable_agent_id", "role_checksum", "persona_id", "persona_version",
        "persona_checksum", "compiler_schema_version", "dependency_digests",
        "effective_config_digest",
    }


def test_dependency_digests_are_order_independent() -> None:
    a = compute_binding_digest(**_digest_inputs(
        dependency_digests=("sha256:" + "1" * 64, "sha256:" + "2" * 64),
    ))
    b = compute_binding_digest(**_digest_inputs(
        dependency_digests=("sha256:" + "2" * 64, "sha256:" + "1" * 64),
    ))
    assert a == b


def test_empty_config_digest_is_stable_and_deterministic() -> None:
    assert EMPTY_CONFIG_DIGEST == compute_effective_config_digest({})
    assert EMPTY_CONFIG_DIGEST.startswith("sha256:")


def _binding(**overrides) -> BindingRecord:
    from personality_binding import compute_binding_digest as digest_fn
    fields = _digest_inputs(**{k: v for k, v in overrides.items() if k in _digest_inputs()})
    digest = digest_fn(**fields)
    return BindingRecord(
        **fields, mode=overrides.get("mode", "image-default"), binding_digest=digest,
        image_default_root=overrides.get("image_default_root", "casa/tina@0.1.0"),
        component_root=overrides.get("component_root"),
        override_source=overrides.get("override_source"),
    )


def _tuple(binding: BindingRecord) -> InstanceTuple:
    return InstanceTuple(
        root=binding.image_default_root or binding.override_source or "",
        binding=binding, config_snapshot={}, config_digest=binding.effective_config_digest,
    )


# --- InstanceDir: defect #4 regression coverage -----------------------------


def test_fresh_instance_dir_has_no_active_or_desired(tmp_path: Path) -> None:
    d = InstanceDir(tmp_path / "resident-butler")
    assert d.active() is None
    assert d.desired() is None


def test_stage_desired_does_not_touch_active(tmp_path: Path) -> None:
    d = InstanceDir(tmp_path / "resident-butler")
    original = _tuple(_binding())
    d.stage_desired(original)
    assert d.active() is None  # staging alone never activates anything
    assert d.desired() == original


def test_commit_moves_desired_to_active_and_retains_prior(tmp_path: Path) -> None:
    d = InstanceDir(tmp_path / "resident-butler")
    first = _tuple(_binding(persona_version="0.1.0"))
    d.stage_desired(first)
    committed_first = d.commit_desired_to_active()
    assert committed_first == first
    assert d.active() == first
    assert d.desired() is None

    second = _tuple(_binding(persona_version="0.2.0"))
    d.stage_desired(second)
    d.commit_desired_to_active()
    assert d.active() == second
    prior_path = tmp_path / "resident-butler" / "active.prior.yaml"
    assert prior_path.exists()
    from personality_binding import load_instance_tuple
    assert load_instance_tuple(prior_path) == first  # rollback target retained
    assert (prior_path.stat().st_mode & 0o777) == 0o600  # defect #2: same lockdown as siblings


def test_commit_is_crash_retry_idempotent_and_preserves_true_prior(tmp_path: Path) -> None:
    """Regression for defect #1: a crash AFTER active.yaml is rewritten to the
    new candidate but BEFORE desired.yaml is unlinked must be a safe no-op
    retry — it must NOT re-rotate the (now-identical) active into prior and
    clobber the true rollback target."""
    from personality_binding import load_instance_tuple

    d = InstanceDir(tmp_path / "resident-butler")
    first = _tuple(_binding(persona_version="0.1.0"))
    d.stage_desired(first)
    d.commit_desired_to_active()

    second = _tuple(_binding(persona_version="0.2.0"))
    d.stage_desired(second)
    d.commit_desired_to_active()

    prior_path = tmp_path / "resident-butler" / "active.prior.yaml"
    assert load_instance_tuple(prior_path) == first

    # Simulate the interrupted-retry crash window: desired.yaml still holds
    # the very tuple that is already active (i.e. the commit ran to
    # completion on active.yaml but the process died before the final
    # desired.yaml unlink, and now it retries).
    d.stage_desired(second)
    d.commit_desired_to_active()

    assert load_instance_tuple(prior_path) == first  # true prior MUST survive the retry
    assert d.active() == second
    assert d.desired() is None


def test_a_failed_active_write_never_destroys_the_prior_rollback_generation(
        tmp_path: Path, monkeypatch) -> None:
    """#339 (low): the old commit order rotated active -> active.prior.yaml
    BEFORE durably writing the new active. A write failure (disk full, crash)
    in between left prior == active — the previous rollback generation was
    destroyed with nothing gained. The new active must be written first; only
    then is the old active rotated into prior."""
    import personality_binding
    from personality_binding import load_instance_tuple

    d = InstanceDir(tmp_path / "resident-butler")
    gen1 = _tuple(_binding(persona_version="0.1.0"))
    d.stage_desired(gen1)
    d.commit_desired_to_active()
    gen2 = _tuple(_binding(persona_version="0.2.0"))
    d.stage_desired(gen2)
    d.commit_desired_to_active()

    prior_path = tmp_path / "resident-butler" / "active.prior.yaml"
    assert load_instance_tuple(prior_path) == gen1

    gen3 = _tuple(_binding(persona_version="0.3.0"))
    d.stage_desired(gen3)

    def _fail_write(path, tuple_):
        raise OSError("disk full")

    monkeypatch.setattr(
        personality_binding, "atomic_write_instance_tuple", _fail_write)
    with pytest.raises(OSError):
        d.commit_desired_to_active()
    monkeypatch.undo()

    # The failed commit changed NOTHING durable: active is still gen2 and —
    # the point of the fix — prior still holds gen1, not a copy of gen2.
    assert d.active() == gen2
    assert load_instance_tuple(prior_path) == gen1


def test_crash_between_active_write_and_prior_rotation_completes_on_retry(
        tmp_path: Path) -> None:
    """#339 (low), the other half of the reordering: with the new order
    (write active, THEN rotate prior) a crash in between leaves the new
    active in place, desired.yaml still staged, and the copied old active in
    the .rollback-tmp file. The crash-retry recommit must FINISH the
    interrupted rotation (tmp -> prior) instead of leaving prior a
    generation stale."""
    from personality_binding import atomic_write_instance_tuple, load_instance_tuple

    d = InstanceDir(tmp_path / "resident-butler")
    gen1 = _tuple(_binding(persona_version="0.1.0"))
    d.stage_desired(gen1)
    d.commit_desired_to_active()
    gen2 = _tuple(_binding(persona_version="0.2.0"))
    d.stage_desired(gen2)
    d.commit_desired_to_active()

    base = tmp_path / "resident-butler"
    prior_path = base / "active.prior.yaml"
    assert load_instance_tuple(prior_path) == gen1

    # Hand-build the mid-commit crash state for gen3: desired staged, active
    # already rewritten to gen3, the old active (gen2) copied to the
    # rollback-tmp but not yet rotated over prior.
    gen3 = _tuple(_binding(persona_version="0.3.0"))
    d.stage_desired(gen3)
    atomic_write_instance_tuple(base / "active.yaml", gen3)
    atomic_write_instance_tuple(base / "active.yaml.rollback-tmp", gen2)

    committed = d.commit_desired_to_active()
    assert committed == gen3
    assert d.desired() is None
    assert load_instance_tuple(prior_path) == gen2  # rotation completed
    assert not (base / "active.yaml.rollback-tmp").exists()


def test_tampered_nested_binding_missing_field_raises_value_error(tmp_path: Path) -> None:
    """Regression for defect #3: a tampered instance tuple whose nested
    binding drops a required field must raise a path-prefixed ValueError,
    not a bare KeyError."""
    d = InstanceDir(tmp_path / "resident-butler")
    d.stage_desired(_tuple(_binding()))
    d.commit_desired_to_active()
    active_path = tmp_path / "resident-butler" / "active.yaml"
    raw = yaml.safe_load(active_path.read_text(encoding="utf-8"))
    del raw["binding"]["persona_checksum"]
    active_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match=str(active_path)):
        d.active()


def test_discard_desired_leaves_active_untouched(tmp_path: Path) -> None:
    d = InstanceDir(tmp_path / "resident-butler")
    d.stage_desired(_tuple(_binding(persona_version="0.1.0")))
    d.commit_desired_to_active()
    active_before = d.active()

    d.stage_desired(_tuple(_binding(persona_version="0.2.0")))
    d.discard_desired(reason="persona blob unavailable")
    assert d.active() == active_before  # unchanged
    assert d.desired() is None  # no longer readable as a valid desired candidate
    assert (tmp_path / "resident-butler" / "desired.error.yaml").exists()


def test_commit_with_nothing_staged_raises(tmp_path: Path) -> None:
    d = InstanceDir(tmp_path / "resident-butler")
    with pytest.raises(ValueError, match="desired"):
        d.commit_desired_to_active()


def test_tampered_active_file_is_rejected_on_load(tmp_path: Path) -> None:
    d = InstanceDir(tmp_path / "resident-butler")
    d.stage_desired(_tuple(_binding()))
    d.commit_desired_to_active()
    active_path = tmp_path / "resident-butler" / "active.yaml"
    text = active_path.read_text(encoding="utf-8")
    active_path.write_text(text.replace("0.1.0", "9.9.9"), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        d.active()


# --- #372: config_digest is always the digest of the persisted snapshot -----


def _binding_for_config(config: dict) -> BindingRecord:
    return _binding(effective_config_digest=compute_effective_config_digest(config))


def test_make_instance_tuple_derives_config_digest_from_snapshot() -> None:
    from personality_binding import make_instance_tuple
    snapshot = {"region": "eu", "workspace": "main"}
    tuple_ = make_instance_tuple(
        root="casa/x@1.0.0", binding=_binding_for_config(snapshot),
        config_snapshot=snapshot,
    )
    assert tuple_.config_digest == compute_effective_config_digest(snapshot)
    assert tuple_.config_digest == tuple_.binding.effective_config_digest


def test_make_instance_tuple_refuses_a_binding_disagreeing_with_the_snapshot() -> None:
    from personality_binding import make_instance_tuple
    with pytest.raises(ValueError, match="effective_config_digest"):
        make_instance_tuple(
            root="casa/x@1.0.0", binding=_binding_for_config({"other": "config"}),
            config_snapshot={"region": "eu"},
        )


def test_verify_instance_tuple_rejects_a_digest_not_derived_from_the_snapshot() -> None:
    # The #372 pre-guard shape: binding and tuple digests agree with each
    # other but were computed over a mapping that no longer equals the
    # (sanitized) persisted snapshot.
    from personality_binding import _raw_from_tuple, verify_instance_tuple
    stale = _binding_for_config({"region": "eu", "api_key": "plaintext-secret"})
    raw = _raw_from_tuple(InstanceTuple(
        root="casa/x@1.0.0", binding=stale,
        config_snapshot={"region": "eu"},  # sanitized: api_key stripped
        config_digest=stale.effective_config_digest,
    ))
    with pytest.raises(ValueError, match="snapshot"):
        verify_instance_tuple(raw)


def test_atomic_write_refuses_a_mismatched_tuple_as_a_backstop(tmp_path: Path) -> None:
    from personality_binding import atomic_write_instance_tuple
    stale = _binding_for_config({"region": "eu", "api_key": "plaintext-secret"})
    mismatched = InstanceTuple(
        root="casa/x@1.0.0", binding=stale, config_snapshot={"region": "eu"},
        config_digest=stale.effective_config_digest,
    )
    with pytest.raises(ValueError, match="snapshot"):
        atomic_write_instance_tuple(tmp_path / "active.yaml", mismatched)
    assert not (tmp_path / "active.yaml").exists()


def test_atomic_write_refuses_a_split_digest_tuple(tmp_path: Path) -> None:
    """#372 (Sol diff r1): the backstop must check BOTH digest fields — a
    tuple whose top-level digest honestly matches the snapshot while its
    binding retains a digest computed over a secret-bearing mapping would
    otherwise persist the oracle in binding.effective_config_digest."""
    from personality_binding import atomic_write_instance_tuple
    stale = _binding_for_config({"secret": "hunter2"})
    split = InstanceTuple(
        root="casa/x@1.0.0", binding=stale, config_snapshot={},
        config_digest=EMPTY_CONFIG_DIGEST,
    )
    with pytest.raises(ValueError, match="binding"):
        atomic_write_instance_tuple(tmp_path / "active.yaml", split)
    assert not (tmp_path / "active.yaml").exists()


def test_sentinel_digest_raises_the_pre_guard_error_before_schema_validation(
    tmp_path: Path,
) -> None:
    from personality_binding import (
        PRE_GUARD_SENTINEL, _raw_from_tuple, load_instance_tuple,
    )
    good = _binding_for_config({})
    raw = _raw_from_tuple(_tuple(good))
    raw["config_digest"] = PRE_GUARD_SENTINEL
    raw["binding"]["effective_config_digest"] = PRE_GUARD_SENTINEL
    path = tmp_path / "active.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    # The sentinel violates binding.v1.json's digest pattern, so an ordinary
    # schema error would be opaque; the loader must name the real cause.
    with pytest.raises(ValueError, match="secret-digest guard"):
        load_instance_tuple(path)


def _specialist_role() -> "RoleSlot":
    from role_slot import RoleSlot, ResolvedModel
    return RoleSlot(
        role_id="specialist:mtg", kind="specialist", slot="mtg", mission="x",
        resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                      sdk_model="claude-sonnet-4-6", option=None),
        normalized={}, doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
    )


def _judge_persona() -> "PersonaPack":
    from persona_pack import PersonaPack, PersonaManifest
    return PersonaPack(
        persona_id="casa/judge", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Judge", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="adjudicator",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                 "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(), markdown="# Core\n\nJudges rules.\n\n## Negative space\n\nNever guesses.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )


def test_materialize_component_default_binding_is_specialist_only() -> None:
    import pytest
    from personality_binding import materialize_component_default_binding
    from role_slot import RoleSlot, ResolvedModel

    resident_role = RoleSlot(
        role_id="resident:butler", kind="resident", slot="butler", mission="x",
        resolved_model=ResolvedModel(source="fixed", effective="haiku",
                                      sdk_model="claude-haiku-4-5", option=None),
        normalized={}, doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
    )
    with pytest.raises(ValueError, match="specialist-only"):
        materialize_component_default_binding(
            role=resident_role, persona=_judge_persona(), component_root="casa-test/mtg@0.1.0",
        )


def test_materialize_component_default_binding_sets_mode_and_component_root() -> None:
    from personality_binding import materialize_component_default_binding

    binding = materialize_component_default_binding(
        role=_specialist_role(), persona=_judge_persona(), component_root="casa-test/mtg@0.1.0",
    )
    assert binding.mode == "component-default"
    assert binding.component_root == "casa-test/mtg@0.1.0"
    assert binding.image_default_root is None
    assert binding.override_source is None


def test_materialize_override_binding_default_args_produce_the_pre_n1c_digest() -> None:
    """Task N1c extends materialize_override_binding with two new optional
    kwargs (dependency_digests, effective_config_digest) so upgrade_specialist/
    rollback_specialist can preserve an override-bound specialist's persona
    pin while still capturing the new component's dependency closure. This
    pins the additive-only guarantee: a call with NO new kwargs — exactly
    the shape every existing caller (reconcile_resident_binding,
    tools.py's resident_persona_swap) uses — must produce the IDENTICAL
    binding_digest a hand-built compute_binding_digest call (with the same
    implicit defaults _build already used pre-N1c: dependency_digests=(),
    effective_config_digest=EMPTY_CONFIG_DIGEST) produces."""
    from personality_binding import compute_binding_digest, materialize_override_binding

    role = _specialist_role()
    persona = _judge_persona()

    binding = materialize_override_binding(
        role=role, persona=persona, override_source="operator:casa/judge@0.1.0",
    )
    expected_digest = compute_binding_digest(
        stable_agent_id=role.role_id, role_checksum=role.checksum,
        persona_id=persona.persona_id, persona_version=persona.version,
        persona_checksum=persona.checksum, compiler_schema_version=RENDERER_VERSION,
        dependency_digests=(), effective_config_digest=EMPTY_CONFIG_DIGEST,
    )
    assert binding.binding_digest == expected_digest
    assert binding.mode == "override"
    assert binding.override_source == "operator:casa/judge@0.1.0"
    assert binding.dependency_digests == ()
    assert binding.effective_config_digest == EMPTY_CONFIG_DIGEST


# --- #205: load_binding validates exactly once ------------------------------


def test_load_binding_reports_a_malformed_file_with_its_path(tmp_path: Path) -> None:
    """A malformed binding file must raise a path-qualified ValueError.

    #205 removed a duplicate ``jsonschema.validate`` from ``load_binding``:
    ``verify_binding_record`` already validates against the same
    binding.v1.json. The duplicate ran FIRST and raised a bare
    ``jsonschema.ValidationError`` carrying no file context, pre-empting the
    path-prefixed ``ValueError`` the function's own except-block produces —
    so the caller could never tell WHICH binding file was bad. Guard the
    surviving contract: one validation, one error shape, with the path.
    """
    from personality_binding import atomic_write_binding, load_binding

    path = tmp_path / "binding.yaml"
    atomic_write_binding(path, _binding())
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    del raw["persona_checksum"]          # schema-required → validation failure
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match=str(path)):
        load_binding(path)


def test_load_binding_roundtrips_a_well_formed_file(tmp_path: Path) -> None:
    """The happy path still verifies the digest and returns the record —
    dropping the duplicate validate must not drop validation itself."""
    from personality_binding import atomic_write_binding, load_binding

    path = tmp_path / "binding.yaml"
    original = _binding()
    atomic_write_binding(path, original)

    assert load_binding(path) == original


def test_load_binding_still_rejects_a_tampered_digest(tmp_path: Path) -> None:
    """Schema-valid but integrity-broken: the shared verifier's digest
    recomputation is the check that must survive the de-duplication."""
    from personality_binding import atomic_write_binding, load_binding

    path = tmp_path / "binding.yaml"
    atomic_write_binding(path, _binding())
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["persona_version"] = "9.9.9"     # schema-valid, digest no longer matches
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="binding_digest"):
        load_binding(path)


def test_a_tuple_noop_with_differing_desired_sidecar_bytes_replaces_active_without_rotating(
        tmp_path: Path, monkeypatch) -> None:
    """#810 (INV-SPEC-011), re-specifying the #346 pin: the sidecar prior rotates
    with the TUPLE, on the tuple's own no-op predicate — never on the sidecar's
    bytes. A no-op tuple recommit (crash-retry, a duplicate bundle that lost a
    race) followed by a DIFFERING desired sidecar replaces the active document
    and mutates the prior sidecar zero times: the true prior generation's pair
    survives, so a later rollback still activates one generation's tuple with
    that same generation's plugins."""
    import os as _os
    from personality_binding import (
        owned_plugins_desired_path, owned_plugins_path, owned_plugins_prior_path,
        read_owned_plugins,
    )

    base = tmp_path / "mtg"
    d = InstanceDir(base)
    prior_sidecar = owned_plugins_prior_path(base)
    gen1 = {"schema_version": 1, "component_source": {"gen": "one"}, "plugins": []}
    gen2 = {"schema_version": 1, "component_source": {"gen": "two"}, "plugins": []}
    gen3 = {"schema_version": 1, "component_source": {"gen": "three"}, "plugins": []}
    t1 = _tuple(_binding(persona_version="0.1.0"))
    t2 = _tuple(_binding(persona_version="0.2.0"))
    for tuple_, doc in ((t1, gen1), (t2, gen2)):
        d.stage_desired(tuple_)
        d.stage_desired_owned_plugins(doc)
        d.commit_desired_to_active()
        d.commit_owned_plugins_desired_to_active()
    assert read_owned_plugins(prior_sidecar) == gen1      # rotated BY the tuple commit

    mutations = {"count": 0}
    real_replace, real_write = _os.replace, Path.write_bytes

    def _replace(src, dst, *a, **k):
        mutations["count"] += str(dst) == str(prior_sidecar)
        return real_replace(src, dst, *a, **k)

    def _write_bytes(self_path, data, *a, **k):
        mutations["count"] += str(self_path) == str(prior_sidecar)
        return real_write(self_path, data, *a, **k)

    monkeypatch.setattr(_os, "replace", _replace)
    monkeypatch.setattr(Path, "write_bytes", _write_bytes)

    # The SAME tuple recommitted (a no-op) with DIFFERENT sidecar bytes staged.
    d.stage_desired(t2)
    d.stage_desired_owned_plugins(gen3)
    d.commit_desired_to_active()
    d.commit_owned_plugins_desired_to_active()

    assert mutations["count"] == 0
    assert read_owned_plugins(owned_plugins_path(base)) == gen3
    assert read_owned_plugins(prior_sidecar) == gen1
    assert not owned_plugins_desired_path(base).exists()


def test_stale_rollback_tmp_left_by_journal_rollback_is_never_rotated_over_prior(
        tmp_path: Path) -> None:
    """Sol review (#339/#346): the specialist bundle journal restores
    active/prior on rollback but knows nothing about active.yaml.rollback-tmp.
    After a rollback, a stale tmp can hold the SAME tuple as the restored
    active; a later byte-identical recommit taking the no-op branch must
    detect that (parse-and-compare, not blind os.replace) and discard the
    tmp instead of clobbering the true prior with a duplicate."""
    from personality_binding import atomic_write_instance_tuple, load_instance_tuple

    d = InstanceDir(tmp_path / "mtg")
    gen1 = _tuple(_binding(persona_version="0.1.0"))
    d.stage_desired(gen1)
    d.commit_desired_to_active()
    gen2 = _tuple(_binding(persona_version="0.2.0"))
    d.stage_desired(gen2)
    d.commit_desired_to_active()

    base = tmp_path / "mtg"
    prior_path = base / "active.prior.yaml"
    assert load_instance_tuple(prior_path) == gen1

    # Journal-rollback aftermath: a gen3 upgrade crashed mid-commit (tmp holds
    # the then-active gen2), then the journal restored active back to gen2 —
    # so tmp now DUPLICATES active.
    atomic_write_instance_tuple(base / "active.yaml.rollback-tmp", gen2)

    # Byte-identical recommit of gen2 (crash-retry / duplicate bundle).
    d.stage_desired(gen2)
    d.commit_desired_to_active()

    assert d.active() == gen2
    assert load_instance_tuple(prior_path) == gen1  # true prior survives
    assert not (base / "active.yaml.rollback-tmp").exists()


def test_corrupt_rollback_tmp_is_discarded_not_rotated(tmp_path: Path) -> None:
    """A tmp that fails to load (corrupt/tampered) must never become prior."""
    from personality_binding import load_instance_tuple

    d = InstanceDir(tmp_path / "mtg")
    gen1 = _tuple(_binding(persona_version="0.1.0"))
    d.stage_desired(gen1)
    d.commit_desired_to_active()
    gen2 = _tuple(_binding(persona_version="0.2.0"))
    d.stage_desired(gen2)
    d.commit_desired_to_active()

    base = tmp_path / "mtg"
    (base / "active.yaml.rollback-tmp").write_text("{{{ garbage", encoding="utf-8")
    d.stage_desired(gen2)
    d.commit_desired_to_active()

    assert load_instance_tuple(base / "active.prior.yaml") == gen1
    assert not (base / "active.yaml.rollback-tmp").exists()


def test_a_failed_prior_rotation_does_not_fail_the_committed_transition(
        tmp_path: Path, monkeypatch) -> None:
    """Terra review (#339): once the new active is durably written the commit
    has succeeded — a failure rotating tmp -> prior must not raise (the
    caller would then run the pre-commit tuple while disk says otherwise).
    The tmp stays behind as the pending-rotation journal and a later
    recommit completes it."""
    import personality_binding
    from personality_binding import load_instance_tuple

    d = InstanceDir(tmp_path / "mtg")
    gen1 = _tuple(_binding(persona_version="0.1.0"))
    d.stage_desired(gen1)
    d.commit_desired_to_active()
    gen2 = _tuple(_binding(persona_version="0.2.0"))
    d.stage_desired(gen2)
    d.commit_desired_to_active()

    base = tmp_path / "mtg"
    prior_path = base / "active.prior.yaml"
    gen3 = _tuple(_binding(persona_version="0.3.0"))
    d.stage_desired(gen3)

    real_replace = personality_binding.os.replace

    def _fail_prior_rotation(src, dst):
        if str(dst) == str(prior_path):
            raise OSError("EIO on prior rotation")
        return real_replace(src, dst)

    monkeypatch.setattr(personality_binding.os, "replace", _fail_prior_rotation)
    committed = d.commit_desired_to_active()  # must NOT raise
    monkeypatch.undo()

    assert committed == gen3
    assert d.active() == gen3                      # disk agrees with the caller
    assert d.desired() is None
    assert load_instance_tuple(prior_path) == gen1  # stale but intact (never clobbered)
    pending = base / "active.yaml.rollback-tmp"
    assert pending.exists()                        # the pending-rotation journal

    # A later recommit of the same tuple completes the interrupted rotation.
    d.stage_desired(gen3)
    d.commit_desired_to_active()
    assert load_instance_tuple(prior_path) == gen2
    assert not pending.exists()


def test_a_failed_desired_unlink_does_not_fail_the_committed_transition(
        tmp_path: Path, monkeypatch) -> None:
    """Sol round-2 (#339): once the new active is durably written, EVERY
    post-commit cleanup step is best-effort — a failing desired.yaml unlink
    must not raise (the caller would run the pre-commit tuple while disk
    holds the new active). The stale desired is retried by the next no-op
    recommit."""
    d = InstanceDir(tmp_path / "mtg")
    gen1 = _tuple(_binding(persona_version="0.1.0"))
    d.stage_desired(gen1)
    d.commit_desired_to_active()
    gen2 = _tuple(_binding(persona_version="0.2.0"))
    d.stage_desired(gen2)

    import personality_binding
    real_unlink = Path.unlink
    desired = tmp_path / "mtg" / "desired.yaml"

    def _fail_desired_unlink(self, *a, **k):
        if str(self) == str(desired):
            raise OSError("EIO on unlink")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", _fail_desired_unlink)
    committed = d.commit_desired_to_active()  # must NOT raise
    monkeypatch.undo()

    assert committed == gen2
    assert d.active() == gen2
    assert d.desired() == gen2               # stale, retried later
    d.commit_desired_to_active()             # no-op recommit clears it
    assert d.desired() is None


# --- #810 (diff review r2, Sol S2): a partial failure inside the pending-rotation
#     completion must leave a state the NEXT call classifies the same way — never
#     a lone STALE sidecar temporary, never a promoted prior tuple beside a stale
#     prior sidecar with nothing pending. Each pin fails exactly one step of the
#     first call and asserts the retained PAIR after the retry. ------------------


def _seed_pair_state(base: Path, *, active, prior, tuple_temp, active_doc, prior_doc,
                     sidecar_temp_doc) -> None:
    from personality_binding import (
        atomic_write_instance_tuple, owned_plugins_path, owned_plugins_prior_path,
        owned_plugins_rollback_temp_path, write_owned_plugins,
    )
    base.mkdir(parents=True, exist_ok=True)
    atomic_write_instance_tuple(base / "active.yaml", active)
    if prior is not None:
        atomic_write_instance_tuple(base / "active.prior.yaml", prior)
    if tuple_temp is not None:
        atomic_write_instance_tuple(base / "active.yaml.rollback-tmp", tuple_temp)
    write_owned_plugins(owned_plugins_path(base), active_doc)
    if prior_doc is not None:
        write_owned_plugins(owned_plugins_prior_path(base), prior_doc)
    if sidecar_temp_doc is not None:
        write_owned_plugins(owned_plugins_rollback_temp_path(base), sidecar_temp_doc)


def _fail_once_unlink(monkeypatch, target: Path) -> dict:
    """Make the FIRST ``Path.unlink`` of *target* raise ``OSError``; count."""
    real_unlink = Path.unlink
    fired = {"count": 0}

    def _unlink(self_path, *a, **k):
        if str(self_path) == str(target) and fired["count"] == 0:
            fired["count"] += 1
            raise OSError("EIO on unlink")
        return real_unlink(self_path, *a, **k)

    monkeypatch.setattr(Path, "unlink", _unlink)
    return fired


def test_a_stale_pair_whose_sidecar_temporary_unlink_fails_is_still_discarded_on_retry(
        tmp_path: Path, monkeypatch) -> None:
    """Sol (diff review r2): the stale pair T2/S2 (temporaries duplicating the
    active) — the first completion's SIDECAR-temporary unlink fails. The tuple
    temporary must survive that failure, so the retry classifies the pair stale
    again and discards it; the retained pair stays T1/S1. The reverse order
    left a lone stale S2 temporary that the retry promoted over S1."""
    from personality_binding import (
        load_instance_tuple, owned_plugins_prior_path, owned_plugins_rollback_temp_path,
        read_owned_plugins,
    )

    base = tmp_path / "mtg"
    t1, t2 = (_tuple(_binding(persona_version=v)) for v in ("0.1.0", "0.2.0"))
    s1 = {"schema_version": 1, "component_source": {"gen": "one"}, "plugins": []}
    s2 = {"schema_version": 1, "component_source": {"gen": "two"}, "plugins": []}
    _seed_pair_state(base, active=t2, prior=t1, tuple_temp=t2, active_doc=s2, prior_doc=s1,
                     sidecar_temp_doc=s2)
    fired = _fail_once_unlink(monkeypatch, owned_plugins_rollback_temp_path(base))

    with pytest.raises(OSError):
        InstanceDir(base).complete_pending_rotation()
    assert fired["count"] == 1
    assert (base / "active.yaml.rollback-tmp").exists()      # still marks the pair stale

    InstanceDir(base).complete_pending_rotation()

    assert load_instance_tuple(base / "active.prior.yaml") == t1
    assert read_owned_plugins(owned_plugins_prior_path(base)) == s1
    assert not (base / "active.yaml.rollback-tmp").exists()
    assert not owned_plugins_rollback_temp_path(base).exists()


def test_a_genuine_promotion_without_a_sidecar_whose_prior_sidecar_unlink_fails_is_repeated_on_retry(
        tmp_path: Path, monkeypatch) -> None:
    """The outgoing generation T2 owned no sidecar (no sidecar temporary) and
    the prior sidecar S1 must go with the promotion. The prior-sidecar removal
    runs BEFORE the tuple promotion, so when it fails the tuple temporary is
    still pending and the retry repeats both; the retained pair after the
    retry is T2 with NO prior sidecar — never T2 beside a stale S1 with nothing
    left pending."""
    from personality_binding import (
        load_instance_tuple, owned_plugins_prior_path, owned_plugins_rollback_temp_path,
        read_owned_plugins,
    )

    base = tmp_path / "mtg"
    t1, t2, t3 = (_tuple(_binding(persona_version=v)) for v in ("0.1.0", "0.2.0", "0.3.0"))
    s1 = {"schema_version": 1, "component_source": {"gen": "one"}, "plugins": []}
    s3 = {"schema_version": 1, "component_source": {"gen": "three"}, "plugins": []}
    _seed_pair_state(base, active=t3, prior=t1, tuple_temp=t2, active_doc=s3, prior_doc=s1,
                     sidecar_temp_doc=None)
    fired = _fail_once_unlink(monkeypatch, owned_plugins_prior_path(base))

    with pytest.raises(OSError):
        InstanceDir(base).complete_pending_rotation()
    assert fired["count"] == 1
    assert load_instance_tuple(base / "active.prior.yaml") == t1   # nothing promoted yet
    assert (base / "active.yaml.rollback-tmp").exists()

    InstanceDir(base).complete_pending_rotation()

    assert load_instance_tuple(base / "active.prior.yaml") == t2
    assert read_owned_plugins(owned_plugins_prior_path(base)) is None
    assert not (base / "active.yaml.rollback-tmp").exists()
    assert not owned_plugins_rollback_temp_path(base).exists()


def test_a_commit_of_a_sidecarless_generation_whose_prior_sidecar_unlink_fails_keeps_the_pair_pending(
        tmp_path: Path, monkeypatch) -> None:
    """The commit-core twin: T1 active with NO sidecar, a stale prior sidecar
    S0 on disk, committing T2. The prior-sidecar removal fails; the commit still
    succeeds (the new active is durable), the tuple temporary stays pending and
    the visible prior is untouched; the next completion promotes T1 and removes
    S0 — the retained pair is T1 with no sidecar."""
    from personality_binding import (
        load_instance_tuple, owned_plugins_prior_path, read_owned_plugins,
        write_owned_plugins,
    )

    base = tmp_path / "mtg"
    d = InstanceDir(base)
    t1, t2 = (_tuple(_binding(persona_version=v)) for v in ("0.1.0", "0.2.0"))
    d.stage_desired(t1)
    d.commit_desired_to_active()
    s0 = {"schema_version": 1, "component_source": {"gen": "zero"}, "plugins": []}
    write_owned_plugins(owned_plugins_prior_path(base), s0)
    fired = _fail_once_unlink(monkeypatch, owned_plugins_prior_path(base))

    d.stage_desired(t2)
    committed = d.commit_desired_to_active()          # must NOT raise

    assert committed == t2 and d.active() == t2
    assert fired["count"] == 1
    assert (base / "active.yaml.rollback-tmp").exists()
    assert load_instance_tuple(base / "active.prior.yaml") is None

    d.complete_pending_rotation()

    assert load_instance_tuple(base / "active.prior.yaml") == t1
    assert read_owned_plugins(owned_plugins_prior_path(base)) is None
    assert not (base / "active.yaml.rollback-tmp").exists()
