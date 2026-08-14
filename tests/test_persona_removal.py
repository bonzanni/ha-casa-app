"""#543 — the persona lifecycle's disposal half: the reference authority,
`persona_remove`, the prune sweep, and the ack-ledger revocation generations.

The asset every test here defends is BOOT INTEGRITY. A resident whose active
binding is `mode="override"` reloads its persona pack on every boot
(`agent_loader._activate_resident_binding`), and a resident load failure is
boot-fatal (INV-PERS-003) — so removing bytes any binding still names is not a
tidy-up bug, it is a box that will not start, discovered at the next restart.
Every refusal below is therefore asserted as a REFUSAL WITH NOTHING MUTATED,
not merely as a raised error.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from specialist_install import SpecialistInstallError
from test_persona_install import _write_persona_repo


def _install_persona(personas_root: Path, tmp_path: Path, *, persona_id: str,
                     version: str) -> str:
    """Publish a persona pack straight into the installed layout (the shape
    `commit_persona_install` writes and every loader reads). Returns its
    checksum."""
    import shutil

    from persona_pack import load_persona_pack

    repo = tmp_path / f"repo-{persona_id.replace('/', '-')}-{version}"
    _write_persona_repo(repo, persona_id=persona_id, version=version)
    dest = personas_root / persona_id / version
    dest.mkdir(parents=True)
    shutil.copytree(repo / "pack", dest / "pack")
    shutil.copy2(repo / "manifest.json", dest / "manifest.json")
    return load_persona_pack(dest / "pack", dest / "manifest.json").checksum


def _write_tuple(path: Path, *, personas_root: Path, persona_id: str, version: str,
                 root: str = "casa/newton@0.1.0") -> None:
    """Write an InstanceTuple whose binding is an override on
    persona_id@version. Built through the REAL materializer, so it satisfies
    `verify_instance_tuple` exactly as a committed tuple does — a hand-rolled
    document could pass this suite while the production scan skipped it."""
    from personality_binding import (
        _raw_from_tuple, make_instance_tuple, materialize_override_binding,
    )
    from persona_pack import load_persona_pack
    from test_persona_install import _resident_role

    pack = load_persona_pack(
        personas_root / persona_id / version / "pack",
        personas_root / persona_id / version / "manifest.json")
    binding = materialize_override_binding(
        role=_resident_role(), persona=pack, override_source=f"{persona_id}@{version}")
    tup = make_instance_tuple(root=root, binding=binding, config_snapshot={})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(_raw_from_tuple(tup), sort_keys=False), encoding="utf-8")


@pytest.fixture()
def tree(tmp_path: Path):
    """The four roots the reference scan reads, all empty."""
    roots = {
        "personas": tmp_path / "personas",
        "bindings": tmp_path / "bindings",
        "specialists": tmp_path / "specialists",
        "ops": tmp_path / "specialists" / ".ops",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    return roots


def _refs(tree, **kw):
    from persona_install import persona_references

    return persona_references(
        bindings_dir=tree["bindings"], specialists_dir=tree["specialists"],
        ops_dir=tree["ops"], **kw)


def _remove(tree, tmp_path, *, persona_id: str, version: str, acks=None):
    from persona_install import PersonaInstallAckStore, remove_installed_persona

    return remove_installed_persona(
        persona_id=persona_id, version=version, personas_root=tree["personas"],
        acks=acks if acks is not None else PersonaInstallAckStore(path=tmp_path / "acks.json"),
        bindings_dir=tree["bindings"], specialists_dir=tree["specialists"],
        ops_dir=tree["ops"])


# ---------------------------------------------------------------------------
# The reference authority
# ---------------------------------------------------------------------------


def test_an_unreferenced_persona_is_removed_and_its_directory_is_gone(tree, tmp_path) -> None:
    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")

    result = _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")

    assert result["ok"] is True
    assert not (tree["personas"] / "casa/newton" / "0.1.0").exists()
    # The empty parents are pruned too, so a later `persona_list` shows nothing.
    assert not (tree["personas"] / "casa" / "newton").exists()


@pytest.mark.parametrize("tuple_file", ["active.yaml", "desired.yaml"])
def test_a_resident_binding_refuses_removal(tree, tmp_path, tuple_file: str) -> None:
    """INV-PERS-006 red case: this is the boot-fatal one. A resident's active
    (or staged) override names the persona; removing it would make the next
    boot fail to load that resident."""
    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    _write_tuple(tree["bindings"] / "resident-assistant" / tuple_file,
                 personas_root=tree["personas"], persona_id="casa/newton", version="0.1.0")

    with pytest.raises(SpecialistInstallError) as raised:
        _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")

    assert raised.value.kind == "persona_pinned"
    assert "resident:assistant" in raised.value.detail
    assert (tree["personas"] / "casa/newton" / "0.1.0" / "manifest.json").is_file()


@pytest.mark.parametrize(
    "tuple_file", ["active.yaml", "desired.yaml", "active.prior.yaml",
                   "active.yaml.rollback-tmp"])
def test_a_specialist_tuple_including_the_rollback_temp_refuses_removal(
        tree, tmp_path, tuple_file: str) -> None:
    """`active.prior.yaml` is `rollback_specialist`'s input, and a failed
    prior rotation leaves the old tuple at `active.yaml.rollback-tmp`, which a
    later no-op recommit PROMOTES to prior (Terra design round 2). Both are
    references."""
    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    _write_tuple(tree["specialists"] / "mtg" / tuple_file,
                 personas_root=tree["personas"], persona_id="casa/newton", version="0.1.0")

    with pytest.raises(SpecialistInstallError) as raised:
        _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")

    assert raised.value.kind == "persona_pinned"
    assert "specialist:mtg" in raised.value.detail


def test_a_resident_prior_tuple_is_not_a_reference(tree, tmp_path) -> None:
    """Deliberate asymmetry with the specialist tree: nothing in this codebase
    reads a RESIDENT `active.prior.yaml` (the only prior consumer is
    specialist rollback). Counting it would pin the outgoing persona forever
    after `resident_persona_reset`, since committing the reset rotates the old
    override into prior — the operator could never free the bytes."""
    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    _write_tuple(tree["bindings"] / "resident-assistant" / "active.prior.yaml",
                 personas_root=tree["personas"], persona_id="casa/newton", version="0.1.0")

    assert _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")["ok"] is True


def _write_journal(tree, tmp_path, *, name: str = "mtg.%s.json" % ("a1" * 16),
                   slug: str = "mtg", state: str = "in-progress",
                   persona_id: str = "casa/newton", version: str = "0.1.0") -> Path:
    """A bundle journal capturing an override tuple. Built to the shape the
    journal module itself validates — the callers below assert the module's
    OWN verdict on it, so a fixture that drifts from what boot replays fails
    loudly instead of quietly testing nothing (Terra diff r1: the first
    version of this fixture used a state boot never replays, so the test that
    named it was asserting a premise the code contradicts)."""
    _write_tuple(tmp_path / "captured" / "active.yaml", personas_root=tree["personas"],
                 persona_id=persona_id, version=version)
    captured = (tmp_path / "captured" / "active.yaml").read_text(encoding="utf-8")
    path = tree["ops"] / name
    path.write_text(json.dumps({
        "schema_version": 1, "op": "upgrade", "slug": slug, "state": state,
        "before": {"registry_entries": [], "tuple_files": {"active.yaml": captured},
                   "ack_records": []},
    }), encoding="utf-8")
    return path


def test_an_in_progress_bundle_journal_holds_a_reference(tree, tmp_path) -> None:
    """A journal's captured tuple bytes are rewritten verbatim by
    `BundleTxn.rollback_disk` — from the tool layer's compensation AND from
    the next boot's `reconcile_boot`. A tuple that is not on disk right now
    can therefore come back (Sol design round 1)."""
    import specialist_bundle_journal

    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    path = _write_journal(tree, tmp_path)
    # The premise, asserted against the authority rather than assumed: boot
    # really would replay this journal's captured tuples.
    assert specialist_bundle_journal.replayable_tuple_files(path) is not None

    with pytest.raises(SpecialistInstallError) as raised:
        _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")

    assert raised.value.kind == "persona_pinned"
    assert f"journal:{path.name}" in raised.value.detail


def test_a_complete_journal_holds_no_reference(tree, tmp_path) -> None:
    """`reconcile_boot` prunes a complete journal WITHOUT rolling back, so its
    captured tuples can never be restored."""
    import specialist_bundle_journal

    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    path = _write_journal(tree, tmp_path, state="complete")
    assert specialist_bundle_journal.replayable_tuple_files(path) is None

    assert _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")["ok"] is True


@pytest.mark.parametrize("kind", ["bad-json", "bad-state", "bad-slug", "bad-name"])
def test_a_journal_boot_would_never_replay_holds_no_reference(
        tree, tmp_path, kind: str) -> None:
    """Terra diff r1: boot QUARANTINES an invalid journal — it never restores
    one — so refusing removal for such a file strands the operator until a
    reboot for a reference that cannot exist. The predicate lives in the
    journal module so this scan and boot cannot disagree about it."""
    import specialist_bundle_journal

    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    if kind == "bad-json":
        path = tree["ops"] / f"mtg.{'a1' * 16}.json"
        path.write_text("{not json", encoding="utf-8")
    elif kind == "bad-state":
        path = _write_journal(tree, tmp_path, state="begin")
    elif kind == "bad-slug":
        path = _write_journal(tree, tmp_path, slug="not-the-filename-slug")
    else:
        path = _write_journal(tree, tmp_path, name="not-a-journal-name.json")
    assert specialist_bundle_journal.replayable_tuple_files(path) is None

    assert _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")["ok"] is True


def test_a_journal_that_cannot_be_read_refuses_every_removal(
        tree, tmp_path, monkeypatch) -> None:
    """The one journal state that DOES refuse: a read error is unknown, not
    negative — the same file may read fine at boot and replay its capture."""
    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    path = _write_journal(tree, tmp_path)

    # A real read failure, so the classifier itself produces the verdict —
    # patching the classifier would be patching the thing under test.
    real_read_text = Path.read_text

    def _read_text(self, *args, **kwargs):
        if self.name == path.name:
            raise OSError(5, "EIO")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)

    with pytest.raises(SpecialistInstallError) as raised:
        _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")

    assert raised.value.kind == "references_unavailable"
    assert (tree["personas"] / "casa/newton" / "0.1.0" / "manifest.json").is_file()


def test_a_quarantined_journal_is_skipped(tree, tmp_path) -> None:
    """`reconcile_boot` never replays a `.quarantined` file."""
    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    path = _write_journal(tree, tmp_path)
    path.rename(path.with_suffix(".json.quarantined"))

    assert _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")["ok"] is True


def test_a_tuple_file_that_cannot_be_read_refuses_every_removal(tree, tmp_path) -> None:
    """Sol diff r1 (S1): `load_instance_tuple` RAISES for a file that exists
    but cannot be interpreted — it never reports it as absent. Treating that
    as "no reference" un-pinned a persona a resident's unchanged active
    binding still named, and the next restart could not load that resident."""
    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    damaged = tree["bindings"] / "resident-assistant" / "active.yaml"
    damaged.parent.mkdir(parents=True)
    damaged.write_text("{{{ not a tuple", encoding="utf-8")

    with pytest.raises(SpecialistInstallError) as raised:
        _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")

    assert raised.value.kind == "references_unavailable"
    assert "active.yaml" in raised.value.detail
    assert (tree["personas"] / "casa/newton" / "0.1.0" / "manifest.json").is_file()


# ---------------------------------------------------------------------------
# Removal mechanics
# ---------------------------------------------------------------------------


def test_removing_a_persona_that_is_not_installed_is_typed(tree, tmp_path) -> None:
    with pytest.raises(SpecialistInstallError) as raised:
        _remove(tree, tmp_path, persona_id="casa/nobody", version="9.9.9")
    assert raised.value.kind == "not_installed"


def test_a_traversal_bearing_ref_never_reaches_a_path_join(tree, tmp_path) -> None:
    with pytest.raises(SpecialistInstallError) as raised:
        _remove(tree, tmp_path, persona_id="../../etc", version="0.1.0")
    assert raised.value.kind == "invalid_persona_ref"


def test_removal_revokes_the_install_approval(tree, tmp_path) -> None:
    from persona_install import PersonaInstallAckStore, persona_install_consent_identity

    checksum = _install_persona(tree["personas"], tmp_path,
                                persona_id="casa/newton", version="0.1.0")
    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    identity = persona_install_consent_identity(
        persona_id="casa/newton", version="0.1.0", checksum=checksum)
    acks.record(identity=identity, persona_id="casa/newton", version="0.1.0",
                checksum=checksum)

    result = _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0", acks=acks)

    assert result["revoked_acks"] == 1
    assert acks.is_acked(identity) is False


def test_a_refused_removal_leaves_the_approval_intact(tree, tmp_path) -> None:
    """Revoke-before-delete must not fire on a path that refuses: a pinned
    persona keeps both its bytes AND its approval."""
    from persona_install import PersonaInstallAckStore, persona_install_consent_identity

    checksum = _install_persona(tree["personas"], tmp_path,
                                persona_id="casa/newton", version="0.1.0")
    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    identity = persona_install_consent_identity(
        persona_id="casa/newton", version="0.1.0", checksum=checksum)
    acks.record(identity=identity, persona_id="casa/newton", version="0.1.0",
                checksum=checksum)
    _write_tuple(tree["bindings"] / "resident-assistant" / "active.yaml",
                 personas_root=tree["personas"], persona_id="casa/newton", version="0.1.0")

    with pytest.raises(SpecialistInstallError):
        _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0", acks=acks)

    assert acks.is_acked(identity) is True


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def test_prune_removes_only_the_unreferenced_versions(tree, tmp_path) -> None:
    from persona_install import PersonaInstallAckStore, prune_installed_personas

    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.2.0")
    _install_persona(tree["personas"], tmp_path, persona_id="casa/ellen", version="0.1.0")
    _write_tuple(tree["bindings"] / "resident-assistant" / "active.yaml",
                 personas_root=tree["personas"], persona_id="casa/newton", version="0.2.0")

    result = prune_installed_personas(
        personas_root=tree["personas"],
        acks=PersonaInstallAckStore(path=tmp_path / "acks.json"),
        bindings_dir=tree["bindings"], specialists_dir=tree["specialists"],
        ops_dir=tree["ops"])

    assert sorted(r["ref"] for r in result["removed"]) == ["casa/ellen@0.1.0", "casa/newton@0.1.0"]
    assert [(r["ref"], r["kind"]) for r in result["kept"]] == [("casa/newton@0.2.0", "persona_pinned")]
    assert (tree["personas"] / "casa/newton" / "0.2.0" / "manifest.json").is_file()
    assert not (tree["personas"] / "casa/newton" / "0.1.0").exists()


def test_prune_never_touches_the_staging_directory(tree, tmp_path) -> None:
    from persona_install import PersonaInstallAckStore, prune_installed_personas

    staged = tree["personas"] / ".staging" / "deadbeef"
    staged.mkdir(parents=True)
    (staged / "manifest.json").write_text("{}", encoding="utf-8")

    result = prune_installed_personas(
        personas_root=tree["personas"],
        acks=PersonaInstallAckStore(path=tmp_path / "acks.json"),
        bindings_dir=tree["bindings"], specialists_dir=tree["specialists"],
        ops_dir=tree["ops"])

    assert result == {"removed": [], "kept": []}
    assert staged.is_dir()


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------


def test_list_reports_referrers_and_a_corrupt_install(tree, tmp_path) -> None:
    from persona_install import PersonaInstallAckStore, list_installed_personas

    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    _write_tuple(tree["specialists"] / "mtg" / "active.yaml",
                 personas_root=tree["personas"], persona_id="casa/newton", version="0.1.0")
    broken = tree["personas"] / "casa" / "broken" / "0.1.0"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{}", encoding="utf-8")

    entries = {e["ref"]: e for e in list_installed_personas(
        personas_root=tree["personas"], references=_refs(tree),
        acks=PersonaInstallAckStore(path=tmp_path / "acks.json"))}

    assert entries["casa/newton@0.1.0"]["removable"] is False
    assert entries["casa/newton@0.1.0"]["referenced_by"] == [
        {"referrer": "specialist:mtg", "source": "active.yaml"}]
    # A corrupt install is REPORTED, never omitted — it is exactly what the
    # operator has to see, and what commit_persona_install tells them to remove.
    assert entries["casa/broken@0.1.0"]["invalid"]
    assert entries["casa/broken@0.1.0"]["removable"] is True


# ---------------------------------------------------------------------------
# Revocation generations (the stale-tap race, Sol+Terra design round 3)
# ---------------------------------------------------------------------------


def test_a_tap_that_lands_after_a_revoke_records_nothing(tmp_path) -> None:
    """The consent keyboard captures the generations when it is POSTED. The
    Telegram callback commits and records with no await between — but the
    removal worker is another thread and can revoke in that window, and an
    already-answered challenge can no longer be cancelled. The generation is
    what makes the revoke authoritative."""
    from persona_install import PersonaInstallAckStore, persona_install_consent_identity

    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    identity = persona_install_consent_identity(
        persona_id="casa/newton", version="0.1.0", checksum="deadbeef")
    generations = acks.revocation_generations(persona_id="casa/newton", version="0.1.0")

    acks.revoke(persona_id="casa/newton", version="0.1.0")

    wrote = acks.record(identity=identity, persona_id="casa/newton", version="0.1.0",
                        checksum="deadbeef", expect_generations=generations)
    assert wrote is False
    assert acks.is_acked(identity) is False


def test_a_wildcard_revoke_invalidates_a_pending_prompt_with_no_ack(tmp_path) -> None:
    """The round-3 finding both reviewers converged on: a version that is only
    PENDING appears in neither map, so a per-version generation alone leaves
    exactly that prompt able to re-create the approval a wildcard revoke just
    removed. The wildcard generation is what closes it."""
    from persona_install import PersonaInstallAckStore, persona_install_consent_identity

    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    identity = persona_install_consent_identity(
        persona_id="casa/newton", version="0.1.0", checksum="deadbeef")
    generations = acks.revocation_generations(persona_id="casa/newton", version="0.1.0")

    acks.revoke(persona_id="casa/newton")          # no version, and no ack exists yet

    assert acks.record(identity=identity, persona_id="casa/newton", version="0.1.0",
                       checksum="deadbeef", expect_generations=generations) is False


def test_a_prompt_posted_after_a_revoke_can_still_be_approved(tmp_path) -> None:
    """The generation must not strand the operator: removing a persona and
    immediately re-installing it is a normal thing to do."""
    from persona_install import PersonaInstallAckStore, persona_install_consent_identity

    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    identity = persona_install_consent_identity(
        persona_id="casa/newton", version="0.1.0", checksum="deadbeef")
    acks.revoke(persona_id="casa/newton")

    generations = acks.revocation_generations(persona_id="casa/newton", version="0.1.0")
    assert acks.record(identity=identity, persona_id="casa/newton", version="0.1.0",
                       checksum="deadbeef", expect_generations=generations) is True
    assert acks.is_acked(identity) is True


def test_generations_survive_a_reload_of_the_ledger_file(tmp_path) -> None:
    """The generation lives in the ledger FILE, not in memory: the tap that
    races a revoke is served by a different store instance, and a restart in
    between must not reset it."""
    from persona_install import PersonaInstallAckStore

    PersonaInstallAckStore(path=tmp_path / "acks.json").revoke(persona_id="casa/newton")
    fresh = PersonaInstallAckStore(path=tmp_path / "acks.json")
    assert fresh.revocation_generations(persona_id="casa/newton", version="0.1.0") == (1, 0)


def test_a_malformed_revocations_map_refuses_both_mutations(tmp_path) -> None:
    """Opposite polarity to the ack map's fail-closed read: an unreadable
    generation must never read as 0 (that would let a stale tap through) and
    must never be incremented from an unknown base."""
    from persona_install import PersonaInstallAckStore, PersonaLedgerInvalid

    path = tmp_path / "acks.json"
    path.write_text(json.dumps(
        {"schema_version": 1, "acks": {}, "revocations": {"casa/newton": "not-an-int"}}),
        encoding="utf-8")
    acks = PersonaInstallAckStore(path=path)

    with pytest.raises(PersonaLedgerInvalid):
        acks.revoke(persona_id="casa/newton")
    with pytest.raises(PersonaLedgerInvalid):
        acks.record(identity="ident-unused", persona_id="casa/newton", version="0.1.0",
                    checksum="deadbeef")


def test_an_existing_ledger_without_revocations_keeps_its_acks(tmp_path) -> None:
    """The map is additive under the UNCHANGED schema_version: bumping the
    version would make `_load`'s version check discard every recorded ack on
    the upgrading boot."""
    from persona_install import PersonaInstallAckStore, persona_install_consent_identity

    identity = persona_install_consent_identity(
        persona_id="casa/newton", version="0.1.0", checksum="deadbeef")
    path = tmp_path / "acks.json"
    path.write_text(json.dumps({"schema_version": 1, "acks": {identity: {
        "persona_id": "casa/newton", "version": "0.1.0", "checksum": "deadbeef",
        "ts": 1}}}), encoding="utf-8")

    acks = PersonaInstallAckStore(path=path)
    assert acks.is_acked(identity) is True
    assert acks.revocation_generations(persona_id="casa/newton", version="0.1.0") == (0, 0)


# ---------------------------------------------------------------------------
# The in-lock re-verify on the apply path
# ---------------------------------------------------------------------------


def test_applying_a_persona_removed_mid_flight_stages_nothing(
        tree, tmp_path, monkeypatch) -> None:
    """The race the whole locking scheme exists to close: `persona_apply`
    resolves the pack BEFORE taking MATERIALIZE_LOCK, so a removal can delete
    it in that window. Staging the binding anyway would pin bytes that are
    gone — boot-fatal for a resident at the next restart."""
    import shutil

    from persona_install import apply_persona_override
    from persona_pack import load_persona_pack
    from test_persona_install import _resident_role

    # The re-verify resolves through the production seam, so point it at this
    # tree rather than passing a test-only root — that is the seam a #323-shaped
    # regression would break.
    monkeypatch.setenv("CASA_CONFIG_DIR", str(tmp_path))
    _install_persona(tree["personas"], tmp_path, persona_id="casa/ellen", version="0.1.0")
    persona = load_persona_pack(
        tree["personas"] / "casa/ellen" / "0.1.0" / "pack",
        tree["personas"] / "casa/ellen" / "0.1.0" / "manifest.json")

    # ... the removal lands here, between the resolve and the commit.
    shutil.rmtree(tree["personas"] / "casa/ellen" / "0.1.0")

    instance_root = tree["bindings"] / "resident-assistant"
    with pytest.raises(SpecialistInstallError) as raised:
        apply_persona_override(
            target_role_id="resident:assistant", persona=persona,
            role=_resident_role(), instance_dir_root=instance_root)

    assert raised.value.kind == "persona_unavailable"
    assert not (instance_root / "desired.yaml").exists()
    assert not (instance_root / "active.yaml").exists()


def test_a_damaged_ledger_document_refuses_a_generation_checked_record(tmp_path) -> None:
    """Sol diff r1 (S2): for the ACKS, "unreadable ⇒ empty" manufactures no
    consent. For the GENERATIONS the polarity is inverted — an empty read
    reports generation 0, so a tap that captured 0 before a completed revoke
    would match and re-record the approval that revoke removed."""
    from persona_install import (
        PersonaInstallAckStore, PersonaLedgerInvalid, persona_install_consent_identity,
    )

    path = tmp_path / "acks.json"
    acks = PersonaInstallAckStore(path=path)
    identity = persona_install_consent_identity(
        persona_id="casa/newton", version="0.1.0", checksum="deadbeef")
    generations = acks.revocation_generations(persona_id="casa/newton", version="0.1.0")
    acks.revoke(persona_id="casa/newton", version="0.1.0")

    path.write_text("{ truncated", encoding="utf-8")     # the ledger is damaged

    with pytest.raises(PersonaLedgerInvalid):
        acks.record(identity=identity, persona_id="casa/newton", version="0.1.0",
                    checksum="deadbeef", expect_generations=generations)


def test_a_damaged_ledger_document_refuses_a_revoke(tmp_path) -> None:
    """A revoke that rewrote a document it could not interpret would drop
    every ack the file still validly held and persist a generation derived
    from an unknown base."""
    from persona_install import PersonaInstallAckStore, PersonaLedgerInvalid

    path = tmp_path / "acks.json"
    path.write_text(json.dumps({"schema_version": 99, "acks": {}}), encoding="utf-8")

    with pytest.raises(PersonaLedgerInvalid):
        PersonaInstallAckStore(path=path).revoke(persona_id="casa/newton")


def test_a_revoke_keeps_every_unrelated_approval(tmp_path) -> None:
    """The other half of the same finding: a revoke must remove exactly what it
    matched, never everything the ledger held."""
    from persona_install import PersonaInstallAckStore, persona_install_consent_identity

    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    keep = persona_install_consent_identity(
        persona_id="casa/ellen", version="0.1.0", checksum="cafe")
    drop = persona_install_consent_identity(
        persona_id="casa/newton", version="0.1.0", checksum="deadbeef")
    acks.record(identity=keep, persona_id="casa/ellen", version="0.1.0", checksum="cafe")
    acks.record(identity=drop, persona_id="casa/newton", version="0.1.0", checksum="deadbeef")

    removed = acks.revoke(persona_id="casa/newton")

    assert [r["persona_id"] for r in removed] == ["casa/newton"]
    assert acks.is_acked(keep) is True
    assert acks.is_acked(drop) is False


# ---------------------------------------------------------------------------
# Diff review round 2 — absence vs "cannot tell", and the ledger fence
# ---------------------------------------------------------------------------


def test_a_tuple_path_that_cannot_be_stat_ed_refuses_every_removal(
        tree, tmp_path, monkeypatch) -> None:
    """`Path.is_file()` answers False for ENOENT, ENOTDIR, EBADF and ELOOP
    alike, so a path the kernel merely could not resolve read as absent — and
    absent means unreferenced, which deletes the bytes a resident may still
    name. Only a genuine FileNotFoundError may un-pin a persona."""
    import os

    import persona_install

    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    _write_tuple(tree["bindings"] / "resident-assistant" / "active.yaml",
                 personas_root=tree["personas"], persona_id="casa/newton", version="0.1.0")

    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        if str(path).endswith("active.yaml"):
            raise OSError(40, "ELOOP")      # swallowed by Path.is_file()
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(persona_install.os if hasattr(persona_install, "os") else os,
                        "stat", _stat)
    monkeypatch.setattr(os, "stat", _stat)

    with pytest.raises(SpecialistInstallError) as raised:
        _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")

    assert raised.value.kind == "references_unavailable"
    assert (tree["personas"] / "casa/newton" / "0.1.0" / "manifest.json").is_file()


def test_a_missing_tuple_file_is_still_simply_absent(tree, tmp_path) -> None:
    """The other side of the same coin: a file that is genuinely not there is
    a real answer — every loader reads it as absent too — so it must NOT
    refuse, or nothing could ever be removed."""
    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    (tree["bindings"] / "resident-assistant").mkdir(parents=True)   # no tuple files at all

    assert _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")["ok"] is True


@pytest.mark.parametrize("acks", [None, False, [], "", 0])
def test_a_falsey_ack_map_is_a_damaged_document_not_an_empty_one(tmp_path, acks) -> None:
    """Sol+Terra diff r2: `raw.get("acks") or {}` collapsed every falsey shape
    to an empty map, and `_load` fail-closes to {} for each of them too — so
    the comparison succeeded and the guard waved through exactly the malformed
    documents it exists to refuse."""
    from persona_install import PersonaInstallAckStore, PersonaLedgerInvalid

    path = tmp_path / "acks.json"
    path.write_text(json.dumps({"schema_version": 1, "acks": acks}), encoding="utf-8")

    with pytest.raises(PersonaLedgerInvalid):
        PersonaInstallAckStore(path=path).revoke(persona_id="casa/newton")


def test_a_direct_record_cannot_reset_the_revocation_fence(tmp_path) -> None:
    """Sol diff r2: a record that rewrote a damaged document dropped the
    revocation generations with it, so a consent tap that captured 0 before a
    completed revoke matched again and could republish the removed persona.
    Every record validates the document now — the generation-free one too."""
    from persona_install import (
        PersonaInstallAckStore, PersonaLedgerInvalid, persona_install_consent_identity,
    )

    path = tmp_path / "acks.json"
    acks = PersonaInstallAckStore(path=path)
    stale = acks.revocation_generations(persona_id="casa/newton", version="0.1.0")
    acks.revoke(persona_id="casa/newton", version="0.1.0")
    path.write_text("{ truncated", encoding="utf-8")

    identity = persona_install_consent_identity(
        persona_id="casa/other", version="0.1.0", checksum="cafe")
    with pytest.raises(PersonaLedgerInvalid):
        acks.record(identity=identity, persona_id="casa/other", version="0.1.0",
                    checksum="cafe")

    # Nothing was rewritten, so the fence the revoke established is still the
    # last thing written to the file — a repair is the operator deleting it,
    # deliberately, not a consent write silently resetting it.
    assert path.read_text(encoding="utf-8") == "{ truncated"
    del stale


def test_the_journal_classifier_is_the_one_boot_uses(tree, tmp_path) -> None:
    """The anti-drift property itself: boot's reconcile loop and the persona
    reference scan must dispatch on the SAME classification, not on two copies
    of the rule that agree today."""
    import inspect

    import specialist_bundle_journal

    source = inspect.getsource(specialist_bundle_journal.reconcile_boot)
    assert "classify_journal(path)" in source, (
        "reconcile_boot must consume classify_journal — an independent copy of "
        "the name/shape/state rules is what let the reference scan disagree "
        "with what boot actually replays")
    assert "_valid_payload(payload" not in source


# ---------------------------------------------------------------------------
# Diff review round 3
# ---------------------------------------------------------------------------


def test_a_present_but_null_revocations_map_is_a_damaged_document(tmp_path) -> None:
    """Sol diff r3: `_load_revocations` read a MISSING key and a present
    `null` identically as {} — generation zero. A ledger carrying
    `"revocations": null` therefore manufactured a reset fence that a
    pre-revoke consent tap would match."""
    from persona_install import PersonaInstallAckStore, PersonaLedgerInvalid

    path = tmp_path / "acks.json"
    path.write_text(json.dumps(
        {"schema_version": 1, "acks": {}, "revocations": None}), encoding="utf-8")

    with pytest.raises(PersonaLedgerInvalid):
        PersonaInstallAckStore(path=path).revoke(persona_id="casa/newton")


def test_a_boolean_schema_version_is_not_version_one(tmp_path) -> None:
    """JSON `true` equals 1 in Python, so an inexact comparison accepted a
    document whose schema version was a boolean."""
    from persona_install import PersonaInstallAckStore, PersonaLedgerInvalid

    path = tmp_path / "acks.json"
    path.write_text(json.dumps({"schema_version": True, "acks": {}}), encoding="utf-8")

    with pytest.raises(PersonaLedgerInvalid):
        PersonaInstallAckStore(path=path).revoke(persona_id="casa/newton")


def test_a_bindings_root_that_cannot_be_resolved_refuses_every_removal(
        tree, tmp_path, monkeypatch) -> None:
    """Sol diff r3: the fail-closed treatment stopped above the roots. A root
    that cannot be resolved read as "no bindings at all", so the scan returned
    an empty reference set for a directory it never actually saw — and removal
    then deleted a persona an active resident still named."""
    import os

    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    _write_tuple(tree["bindings"] / "resident-assistant" / "active.yaml",
                 personas_root=tree["personas"], persona_id="casa/newton", version="0.1.0")

    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        if str(path) == str(tree["bindings"]):
            raise OSError(40, "ELOOP")     # swallowed by Path.is_dir()
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", _stat)

    with pytest.raises(SpecialistInstallError) as raised:
        _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")

    assert raised.value.kind == "references_unavailable"
    assert (tree["personas"] / "casa/newton" / "0.1.0" / "manifest.json").is_file()


def test_a_bindings_root_that_is_simply_absent_is_empty(tree, tmp_path) -> None:
    """And the other side: a host with no bindings directory at all has no
    resident references, and removal must work normally."""
    import shutil

    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    shutil.rmtree(tree["bindings"])

    assert _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")["ok"] is True


def test_a_float_schema_version_is_not_version_one(tmp_path) -> None:
    """Sol diff r4: `==` admits JSON `1.0` as well as `true`, because both
    equal 1 in Python — and either then reads as generation zero, which is the
    hole the guard exists to close. The round-3 boolean test did not cover
    this, so the 'exact check' claim was a false premise."""
    from persona_install import PersonaInstallAckStore, PersonaLedgerInvalid

    path = tmp_path / "acks.json"
    path.write_text('{"schema_version": 1.0, "acks": {}}', encoding="utf-8")

    with pytest.raises(PersonaLedgerInvalid):
        PersonaInstallAckStore(path=path).revoke(persona_id="casa/newton")


def test_an_installed_version_that_cannot_be_stat_ed_refuses_removal(
        tree, tmp_path, monkeypatch) -> None:
    """Terra diff r4: the enumeration and the removal target were the last
    predicates still answering "absent" for an ambiguous entry — which reports
    a persona as not installed to the only surface that can remove it."""
    import os

    _install_persona(tree["personas"], tmp_path, persona_id="casa/newton", version="0.1.0")
    target = tree["personas"] / "casa" / "newton" / "0.1.0"

    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        if str(path).startswith(str(target)):
            raise OSError(40, "ELOOP")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", _stat)

    with pytest.raises(SpecialistInstallError) as raised:
        _remove(tree, tmp_path, persona_id="casa/newton", version="0.1.0")

    assert raised.value.kind == "references_unavailable"
