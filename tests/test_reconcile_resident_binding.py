"""Personality Phase A, Task 8: boot-time resident binding reconciliation.

reconcile_resident_binding is the SINGLE commit path for a resident's
active binding. The regressions here pin the core fix: a swap/reset staged
into desired.yaml BEFORE a restart is read, validated, and committed on the
next reconcile — not silently discarded — and an invalid staged candidate
leaves the last-known-good active binding running instead of crash-looping.
"""

from __future__ import annotations

import asyncio
import threading

from personality_binding import (
    IMAGE_DEFAULT_PERSONA_BY_SLOT,
    MATERIALIZE_LOCK,
    InstanceDir,
    InstanceTuple,
    materialize_override_binding,
    reconcile_resident_binding,
)
from persona_pack import PersonaManifest, PersonaPack
from role_slot import ResolvedModel, RoleSlot


def _role() -> RoleSlot:
    resolved = ResolvedModel(
        source="ha_option", effective="haiku", sdk_model="claude-haiku-4-5",
        option="voice_agent_model",
    )
    return RoleSlot(
        role_id="resident:butler", kind="resident", slot="butler",
        mission="Control the household.", resolved_model=resolved,
        normalized={"id": "resident:butler", "persona": {
            "policy": "required",
            "compatibility": ["casa/tina@>=0.1.0 <1.0.0", "casa/gary@>=0.1.0 <1.0.0"]}},
        doctrine="# Core doctrine\n\nControl things.\n", checksum="sha256:" + "1" * 64,
    )


def _persona(persona_id: str, version: str) -> PersonaPack:
    return PersonaPack(
        persona_id=persona_id, version=version, trait_schema_version=1,
        identity={"display_name": persona_id.split("/")[-1].title(), "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="housekeeper",
        traits={"warmth": 3, "formality": 2, "candor": 4, "attunement": 4,
                "curiosity": 3, "levity": 2, "social_energy": 3, "optimism": 3},
        quirks=(), markdown="# Core\n\nKeeps the house running.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )


def _install_override_persona(tmp_path, monkeypatch, persona_id: str, version: str):
    """#543: publish a REAL persona pack under the approved installed root
    (through the `$CASA_CONFIG_DIR` seam) and return it — the arrangement
    `resident_persona_swap`/`persona_apply` produce in production, and the one
    the in-lock presence check requires."""
    from test_persona_install import install_persona_for_apply

    return install_persona_for_apply(
        tmp_path, monkeypatch, persona_id=persona_id, version=version)


def _loaders(personas: dict[str, PersonaPack]):
    def load(ref: str) -> PersonaPack:
        if ref not in personas:
            raise ValueError(f"persona {ref!r} unavailable")
        return personas[ref]
    return load


def test_a_staged_desired_swap_is_promoted_on_the_next_reconcile(tmp_path) -> None:
    """THE regression: resident_persona_swap STAGES a desired tuple and returns;
    only the NEXT boot's reconcile actually activates it. Before the fix,
    reconcile read ONLY active.yaml and a successfully staged swap was silently
    discarded on restart."""
    role = _role()
    default = _persona("casa/tina", "0.1.0")
    override = _persona("casa/gary", "0.2.0")
    instance_dir = InstanceDir(tmp_path / "resident-butler")

    first = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )
    assert first.binding.mode == "image-default"

    override_binding = materialize_override_binding(
        role=role, persona=override, override_source="operator:casa/gary@0.2.0",
    )
    instance_dir.stage_desired(InstanceTuple(
        root="operator:casa/gary@0.2.0", binding=override_binding,
        config_snapshot={}, config_digest=override_binding.effective_config_digest,
    ))
    assert instance_dir.active().binding.mode == "image-default"  # staging alone never activates

    second = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({"casa/gary@0.2.0": override}), instance_dir=instance_dir,
    )
    assert second.binding.mode == "override"
    assert second.binding.persona_id == "casa/gary"
    assert instance_dir.desired() is None       # commit clears desired.yaml
    assert instance_dir.active() == second
    assert (tmp_path / "resident-butler" / "active.prior.yaml").exists()  # rollback target retained


def test_an_invalid_staged_swap_is_rejected_and_active_keeps_running(tmp_path) -> None:
    """A staged desired candidate whose persona blob has since become
    unavailable is discarded with a diagnostic — the resident boots on the
    RETAINED active tuple, never crash-loops."""
    role = _role()
    default = _persona("casa/tina", "0.1.0")
    instance_dir = InstanceDir(tmp_path / "resident-butler")
    first = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )

    missing_binding = materialize_override_binding(
        role=role, persona=_persona("casa/ghost", "0.9.0"), override_source="operator:casa/ghost@0.9.0",
    )
    instance_dir.stage_desired(InstanceTuple(
        root="operator:casa/ghost@0.9.0", binding=missing_binding,
        config_snapshot={}, config_digest=missing_binding.effective_config_digest,
    ))

    second = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}),  # the staged persona is NOT resolvable
        instance_dir=instance_dir,
    )
    assert second == first  # unchanged — boot proceeds on the last-known-good binding
    assert (tmp_path / "resident-butler" / "desired.error.yaml").exists()
    assert instance_dir.desired() is None


def test_tool_stage_and_loader_reconcile_share_the_same_bindings_root(tmp_path, monkeypatch) -> None:
    """Round-trip regression for the bindings-root divergence bug: the
    resident_persona_swap/reset tools STAGE a desired tuple via
    ``tools._stage_and_report``; only the NEXT boot's
    ``reconcile_resident_binding`` (called from agent_loader's
    ``load_agent_from_dir``) actually promotes it. Both sides MUST resolve the
    same on-disk directory for a given slot + ``CASA_BINDINGS_DIR``, or a
    staged swap is silently never activated. Before the fix,
    ``tools._stage_and_report`` hardcoded ``/config/bindings`` while
    ``agent_loader._resident_bindings_root`` honored ``CASA_BINDINGS_DIR`` —
    so this test staged under the real (unwritable, in a test sandbox)
    ``/config/bindings`` and reconcile — reading the tmp dir this test points
    ``CASA_BINDINGS_DIR`` at — never saw the override."""
    import agent_loader
    import tools
    from personality_binding import InstanceDir

    monkeypatch.setenv("CASA_BINDINGS_DIR", str(tmp_path / "bindings-root"))

    role = _role()
    default = _persona("casa/tina", "0.1.0")
    # #543: _stage_and_report re-proves the persona is resolvable under the
    # approved roots INSIDE the materialize lock (a persona_remove can land
    # between the tool resolving the pack and the binding being staged), so
    # the override here must be a pack that is genuinely installed — which is
    # what resident_persona_swap always resolves anyway.
    override = _install_override_persona(tmp_path, monkeypatch, "casa/gary", "0.2.0")

    # Establish the boot-time active binding through the LOADER's own root
    # resolver (mirrors load_agent_from_dir's _activate_resident_binding).
    loader_instance_dir = InstanceDir(
        agent_loader._resident_bindings_root(None) / f"resident-{role.slot}"
    )
    first = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=loader_instance_dir,
    )
    assert first.binding.mode == "image-default"

    # Stage an override via the TOOL's own helper — exactly what
    # resident_persona_swap calls after resolving/validating the persona.
    override_binding = materialize_override_binding(
        role=role, persona=override, override_source="operator:casa/gary@0.2.0",
    )
    # Round-5 F2b: _stage_and_report is now async (its InstanceDir write runs
    # under MATERIALIZE_LOCK, offloaded via asyncio.to_thread) — drive it here.
    asyncio.run(tools._stage_and_report(role.role_id, role.slot, override_binding))

    # The next boot's reconcile, reading through the LOADER's seam, must see
    # and promote the tool-staged override.
    second = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({"casa/gary@0.2.0": override}),
        instance_dir=loader_instance_dir,
    )
    assert second.binding.mode == "override"
    assert second.binding.persona_id == "casa/gary"
    assert loader_instance_dir.active().binding.mode == "override"
    assert loader_instance_dir.active().binding.persona_id == "casa/gary"


def test_a_staged_candidate_identical_to_active_is_a_no_op_and_clears_the_stale_file(tmp_path) -> None:
    """resident_persona_reset staging a return-to-default when already on the
    default must not error or churn — and must not leave a stale desired.yaml."""
    role = _role()
    default = _persona("casa/tina", "0.1.0")
    instance_dir = InstanceDir(tmp_path / "resident-butler")
    first = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )
    from personality_binding import materialize_image_default_binding
    same = materialize_image_default_binding(
        role=role, persona=default, image_default_root="casa/tina@0.1.0",
    )
    instance_dir.stage_desired(InstanceTuple(
        root="casa/tina@0.1.0", binding=same, config_snapshot={}, config_digest=same.effective_config_digest,
    ))
    second = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )
    assert second == first
    assert instance_dir.desired() is None  # the no-op staged file is cleared, not left behind


# ---------------------------------------------------------------------------
# #339 — commit-before-validate. A candidate binding must PROVE it loads
# (persona requirements + the caller-supplied compile/admission check) BEFORE
# desired -> active promotion, and an override reconcile must verify the
# persona bytes on disk still match the binding's pinned persona_checksum.
# ---------------------------------------------------------------------------


def test_a_staged_candidate_that_fails_candidate_validation_is_not_committed(tmp_path) -> None:
    """#339 (high): prompt admission used to run only AFTER reconcile had
    already committed desired -> active, so a schema-valid persona that
    exceeds a compile ceiling was made active and every later boot failed on
    it. The caller-supplied candidate_validator (agent_loader wires the real
    compile) must run BEFORE the commit; on failure the staged candidate is
    discarded and the retained active keeps running."""
    role = _role()
    default = _persona("casa/tina", "0.1.0")
    override = _persona("casa/gary", "0.2.0")
    instance_dir = InstanceDir(tmp_path / "resident-butler")

    first = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )

    override_binding = materialize_override_binding(
        role=role, persona=override, override_source="operator:casa/gary@0.2.0",
    )
    instance_dir.stage_desired(InstanceTuple(
        root="operator:casa/gary@0.2.0", binding=override_binding,
        config_snapshot={}, config_digest=override_binding.effective_config_digest,
    ))

    def _reject_gary(persona, binding):
        if persona.persona_id == "casa/gary":
            raise ValueError("text prompt exceeds admission ceiling")

    second = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({"casa/gary@0.2.0": override}),
        instance_dir=instance_dir, candidate_validator=_reject_gary,
    )
    assert second == first  # retained last-known-good, NOT the bad candidate
    assert instance_dir.active() == first  # nothing was committed to disk
    assert instance_dir.desired() is None
    assert (tmp_path / "resident-butler" / "desired.error.yaml").exists()


def test_reconcile_verifies_the_pinned_persona_checksum_before_activating(tmp_path) -> None:
    """#339 (medium): a staged override pins persona_checksum; if the bytes at
    the (mutable) persona path have changed while the version string stayed
    the same, reconcile used to silently rematerialize + commit a binding for
    whatever bytes were present — bypassing the checksum-bound consent
    contract. Changed bytes must be refused; the retained active keeps
    running."""
    import dataclasses

    role = _role()
    default = _persona("casa/tina", "0.1.0")
    approved = _persona("casa/gary", "0.2.0")
    tampered = dataclasses.replace(approved, checksum="sha256:" + "4" * 64)
    instance_dir = InstanceDir(tmp_path / "resident-butler")

    first = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )

    # The binding was staged against the APPROVED bytes...
    override_binding = materialize_override_binding(
        role=role, persona=approved, override_source="operator:casa/gary@0.2.0",
    )
    instance_dir.stage_desired(InstanceTuple(
        root="operator:casa/gary@0.2.0", binding=override_binding,
        config_snapshot={}, config_digest=override_binding.effective_config_digest,
    ))
    # ...but the loader now returns DIFFERENT bytes for the same id@version.
    second = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({"casa/gary@0.2.0": tampered}),
        instance_dir=instance_dir,
    )
    assert second == first
    assert instance_dir.active().binding.mode == "image-default"
    assert instance_dir.desired() is None
    error_file = tmp_path / "resident-butler" / "desired.error.yaml"
    assert "checksum" in error_file.read_text(encoding="utf-8")


def test_changed_bytes_under_an_active_override_are_never_reactivated(tmp_path) -> None:
    """#339 (medium), the nothing-staged variant: an ACTIVE override is
    re-reconciled on every boot by persona_id@version. If the bytes changed
    (e.g. a restore over /config), the old code committed a fresh binding for
    the new bytes — same silent consent bypass. The changed bytes must not be
    activated; the pinned active tuple is retained unchanged (its own
    load-time fate is the loader's, per INV-PERS-003)."""
    import dataclasses

    role = _role()
    default = _persona("casa/tina", "0.1.0")
    approved = _persona("casa/gary", "0.2.0")
    instance_dir = InstanceDir(tmp_path / "resident-butler")

    reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )
    override_binding = materialize_override_binding(
        role=role, persona=approved, override_source="operator:casa/gary@0.2.0",
    )
    instance_dir.stage_desired(InstanceTuple(
        root="operator:casa/gary@0.2.0", binding=override_binding,
        config_snapshot={}, config_digest=override_binding.effective_config_digest,
    ))
    active = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({"casa/gary@0.2.0": approved}),
        instance_dir=instance_dir,
    )
    assert active.binding.mode == "override"
    approved_checksum = active.binding.persona_checksum

    tampered = dataclasses.replace(approved, checksum="sha256:" + "4" * 64)
    third = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({"casa/gary@0.2.0": tampered}),
        instance_dir=instance_dir,
    )
    assert third.binding.persona_checksum == approved_checksum
    assert instance_dir.active().binding.persona_checksum == approved_checksum


def test_dry_run_reconcile_validates_without_any_instance_dir_write(tmp_path) -> None:
    """#338 (medium): tools.validate_config_repo replays the boot loader for
    validation ONLY, but reconcile used to commit a staged desired -> active
    as a side effect — a validation-only config_git_commit could flip the
    live persona binding before the required restart. commit=False must
    perform the full validation and return the would-be tuple while leaving
    every on-disk file untouched."""
    role = _role()
    default = _persona("casa/tina", "0.1.0")
    override = _persona("casa/gary", "0.2.0")
    instance_dir = InstanceDir(tmp_path / "resident-butler")

    first = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )
    override_binding = materialize_override_binding(
        role=role, persona=override, override_source="operator:casa/gary@0.2.0",
    )
    staged = InstanceTuple(
        root="operator:casa/gary@0.2.0", binding=override_binding,
        config_snapshot={}, config_digest=override_binding.effective_config_digest,
    )
    instance_dir.stage_desired(staged)

    result = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({"casa/gary@0.2.0": override}),
        instance_dir=instance_dir, commit=False,
    )
    assert result.binding.mode == "override"          # the would-be active
    assert instance_dir.active() == first             # ...but nothing moved
    assert instance_dir.desired() == staged           # staged file untouched


def test_dry_run_reconcile_leaves_a_failing_staged_candidate_in_place(tmp_path) -> None:
    """commit=False must not even write desired.error.yaml — a validation
    pass is read-only, and boot must still see (and then discard) the staged
    candidate itself."""
    role = _role()
    default = _persona("casa/tina", "0.1.0")
    instance_dir = InstanceDir(tmp_path / "resident-butler")
    first = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )
    ghost_binding = materialize_override_binding(
        role=role, persona=_persona("casa/ghost", "0.9.0"),
        override_source="operator:casa/ghost@0.9.0",
    )
    staged = InstanceTuple(
        root="operator:casa/ghost@0.9.0", binding=ghost_binding,
        config_snapshot={}, config_digest=ghost_binding.effective_config_digest,
    )
    instance_dir.stage_desired(staged)

    result = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}),  # staged persona unresolvable
        instance_dir=instance_dir, commit=False,
    )
    assert result == first                            # boot outcome: retained active
    assert instance_dir.desired() == staged           # nothing discarded
    assert not (tmp_path / "resident-butler" / "desired.error.yaml").exists()


# ---------------------------------------------------------------------------
# Whole-branch review round 6, F1 — reconcile_resident_binding must stage/commit
# under MATERIALIZE_LOCK. It runs on the boot AND reload load paths, concurrently
# with the now-locked persona-swap tools; off-lock it could overwrite/discard a
# freshly staged swap. These pin the lock coverage (wave-4/5 pattern: patch the
# InstanceDir write primitives to record MATERIALIZE_LOCK.locked()) plus the
# swap-vs-reconcile serialization (reconcile blocks on any other lock holder).
# ---------------------------------------------------------------------------


def test_reconcile_resident_binding_stages_and_commits_under_the_materialize_lock(tmp_path) -> None:
    """Every InstanceDir write reconcile performs — stage_desired,
    commit_desired_to_active, and the discard_desired on both the no-op and
    error paths — must observe MATERIALIZE_LOCK held. RED before the fix (the
    whole body ran off-lock)."""
    role = _role()
    default = _persona("casa/tina", "0.1.0")
    override = _persona("casa/gary", "0.2.0")
    instance_dir = InstanceDir(tmp_path / "resident-butler")

    seen: dict[str, list[bool]] = {"stage": [], "commit": [], "discard": []}
    real_stage = InstanceDir.stage_desired
    real_commit = InstanceDir.commit_desired_to_active
    real_discard = InstanceDir.discard_desired

    def spy_stage(self, tuple_):
        seen["stage"].append(MATERIALIZE_LOCK.locked())
        return real_stage(self, tuple_)

    def spy_commit(self):
        seen["commit"].append(MATERIALIZE_LOCK.locked())
        return real_commit(self)

    def spy_discard(self, *, reason):
        seen["discard"].append(MATERIALIZE_LOCK.locked())
        return real_discard(self, reason=reason)

    def _clear():
        for v in seen.values():
            v.clear()

    override_binding = materialize_override_binding(
        role=role, persona=override, override_source="operator:casa/gary@0.2.0",
    )
    ghost_binding = materialize_override_binding(
        role=role, persona=_persona("casa/ghost", "0.9.0"),
        override_source="operator:casa/ghost@0.9.0",
    )

    import unittest.mock as mock
    with mock.patch.object(InstanceDir, "stage_desired", spy_stage), \
         mock.patch.object(InstanceDir, "commit_desired_to_active", spy_commit), \
         mock.patch.object(InstanceDir, "discard_desired", spy_discard):
        # (1) Fresh install: reconcile stages + commits the image default.
        _clear()
        reconcile_resident_binding(
            role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
            override_persona_loader=_loaders({}), instance_dir=instance_dir,
        )
        assert seen["stage"] and all(seen["stage"]), ("fresh-stage", seen["stage"])
        assert seen["commit"] and all(seen["commit"]), ("fresh-commit", seen["commit"])

        # (2) A staged override → reconcile stages + commits it. (The direct
        # stage here is setup, so clear the record BEFORE reconcile runs.)
        instance_dir.stage_desired(InstanceTuple(
            root="operator:casa/gary@0.2.0", binding=override_binding,
            config_snapshot={}, config_digest=override_binding.effective_config_digest,
        ))
        _clear()
        reconcile_resident_binding(
            role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
            override_persona_loader=_loaders({"casa/gary@0.2.0": override}), instance_dir=instance_dir,
        )
        assert seen["stage"] and all(seen["stage"]), ("override-stage", seen["stage"])
        assert seen["commit"] and all(seen["commit"]), ("override-commit", seen["commit"])

        # (3) Error path: a staged candidate whose persona is unresolvable →
        # reconcile discards it under the lock.
        instance_dir.stage_desired(InstanceTuple(
            root="operator:casa/ghost@0.9.0", binding=ghost_binding,
            config_snapshot={}, config_digest=ghost_binding.effective_config_digest,
        ))
        _clear()
        reconcile_resident_binding(
            role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
            override_persona_loader=_loaders({}), instance_dir=instance_dir,
        )
        assert seen["discard"] and all(seen["discard"]), ("error-discard", seen["discard"])


def test_reconcile_resident_binding_serializes_against_a_concurrent_lock_holder(tmp_path) -> None:
    """Swap-vs-reconcile serialization: while ANOTHER holder (a persona-swap
    tool) owns MATERIALIZE_LOCK, reconcile must BLOCK — it cannot race the swap's
    stage/commit. Hold the lock, run reconcile in a worker thread, prove it does
    not finish until the lock is released."""
    role = _role()
    default = _persona("casa/tina", "0.1.0")
    instance_dir = InstanceDir(tmp_path / "resident-butler")

    done = threading.Event()

    def _run():
        reconcile_resident_binding(
            role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
            override_persona_loader=_loaders({}), instance_dir=instance_dir,
        )
        done.set()

    with MATERIALIZE_LOCK:
        worker = threading.Thread(target=_run)
        worker.start()
        # Reconcile cannot proceed while we hold the lock.
        assert not done.wait(timeout=0.5), "reconcile ran without acquiring MATERIALIZE_LOCK"
    # Lock released — reconcile now completes.
    assert done.wait(timeout=5.0), "reconcile did not finish after the lock was released"
    worker.join(timeout=5.0)
    assert instance_dir.active() is not None
