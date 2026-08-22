"""#670, the two boundaries the red case does not reach.

The accepted red case (`tests/test_resident_refusal_diagnosis.py`) is frozen, so
these live here. Both were found by mutation testing the fix, not by reading it:

* **The `commit` gate.** Flipping `if commit:` to `if True:` in
  `reconcile_resident_binding`'s handler left the red case at 6 passed, so
  nothing pinned it. It matters because `agent_loader.validate_config_repo`
  replays reconciliation with `binding_commit=False` as a REPORT — and
  `tools.config_git_commit` replays that on every operator config commit. A
  report that manufactures ERROR records into the live process log is noise, and
  it would fire on a state the operator has not changed.

* **The record's escaping.** `json.dumps` covers the whole object rather than
  the reason alone, because a persona ref is read from an operator-writable
  tuple file and `binding.v1.json`'s `^...$` pattern admits a trailing newline —
  Python's `re` matches `$` before a final newline. Unescaped, that value splits
  the record into two physical lines whose second parses as fields of its own.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
from pathlib import Path

import pytest

from test_resident_refusal_diagnosis import (
    _AGENTS, _ID, _POLICIES, _REF, _VERSION, _approve, _publish,
)


def _bindings_snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_the_validation_replay_reports_without_manifacturing_a_record(
        tmp_path, monkeypatch, caplog) -> None:
    """`binding_commit=False` emits ZERO records and writes nothing.

    Mutation-checked: flipping the handler's `if commit:` to `if True:` fails
    this test and nothing else in the suite.
    """
    from agent_loader import LoadError, load_agent_from_dir
    from policies import load_policies

    config_dir = tmp_path / "config"
    personas_root = config_dir / "personas"
    bindings_root = tmp_path / "bindings-root"
    personas_root.mkdir(parents=True)
    monkeypatch.setenv("CASA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CASA_BINDINGS_DIR", str(bindings_root))

    policies = load_policies(_POLICIES)
    role = load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies).role_slot
    _publish(personas_root, tmp_path, negative_space="Never condescends.",
             tag="approved")
    _approve(personas_root, bindings_root, role)
    _publish(personas_root, tmp_path, negative_space="Never patronises, ever.",
             tag="changed")

    before = _bindings_snapshot(bindings_root)
    assert before, "the approved tuple must be on disk for this to mean anything"

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LoadError):
            load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies,
                                bindings_dir=str(bindings_root),
                                binding_commit=False)

    assert [r for r in caplog.records
            if r.levelno >= logging.WARNING
            and "persona_binding_reconcile_failed" in r.getMessage()] == []
    assert _bindings_snapshot(bindings_root) == before


def test_a_newline_in_an_operator_written_tuple_cannot_forge_a_record_field(
        tmp_path, monkeypatch, caplog) -> None:
    """A resident `active.yaml` whose `persona_id` ends in a newline produces
    exactly ONE physical record that parses as exactly ONE JSON object.

    The input is reachable, not synthetic: `binding.v1.json` constrains
    `persona_id` with `^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?/[a-z0-9][a-z0-9-]*$`,
    and Python's `re` matches `$` before a final newline, so `"casa/newton\\n"`
    satisfies it. The tuple is written through the production materializer and
    the production digest function, so it passes `verify_instance_tuple` exactly
    as a committed tuple does.
    """
    from agent_loader import LoadError, load_agent_from_dir
    from persona_pack import load_persona_pack
    from personality_binding import (
        InstanceDir, compute_binding_digest, make_instance_tuple,
        materialize_override_binding,
    )
    from policies import load_policies

    config_dir = tmp_path / "config"
    personas_root = config_dir / "personas"
    bindings_root = tmp_path / "bindings-root"
    personas_root.mkdir(parents=True)
    monkeypatch.setenv("CASA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CASA_BINDINGS_DIR", str(bindings_root))

    policies = load_policies(_POLICIES)
    role = load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies).role_slot
    _publish(personas_root, tmp_path, negative_space="Never condescends.",
             tag="approved")

    base = personas_root / _ID / _VERSION
    pack = load_persona_pack(base / "pack", base / "manifest.json")
    clean = materialize_override_binding(
        role=role, persona=pack, override_source=f"operator:{_REF}")
    forged_id = f"{_ID}\n"
    binding = dataclasses.replace(
        clean,
        persona_id=forged_id,
        binding_digest=compute_binding_digest(
            stable_agent_id=clean.stable_agent_id,
            role_checksum=clean.role_checksum,
            persona_id=forged_id,
            persona_version=clean.persona_version,
            persona_checksum=clean.persona_checksum,
            compiler_schema_version=clean.compiler_schema_version,
            dependency_digests=clean.dependency_digests,
            effective_config_digest=clean.effective_config_digest,
        ),
    )
    instance_dir = InstanceDir(bindings_root / "resident-concierge")
    instance_dir.stage_desired(make_instance_tuple(
        root=f"operator:{_REF}", binding=binding, config_snapshot={}))
    instance_dir.commit_desired_to_active()
    assert instance_dir.active().binding.persona_id == forged_id

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LoadError):
            load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies,
                                bindings_dir=str(bindings_root))

    loud = [r for r in caplog.records
            if r.levelno >= logging.WARNING
            and r.getMessage().startswith("persona_binding_reconcile_failed")]
    assert len(loud) == 1
    message = loud[0].getMessage()
    # ONE physical line: the forged newline never reaches the record as syntax.
    assert message.count("\n") == 0
    event, _, payload = message.partition(" ")
    assert event == "persona_binding_reconcile_failed"
    # And it parses as exactly one object, whose ref carries the newline as DATA.
    assert json.loads(payload)["persona_ref"] == f"{forged_id}@{_VERSION}"


def test_a_pack_parked_at_the_wrong_ref_is_not_reported_as_the_pinned_one(
        tmp_path, monkeypatch, caplog) -> None:
    """A foreign pack in the pinned ref's directory must not have its checksum
    reported as the checksum found for that ref (review r1, Terra, S2).

    `agent_loader._pack` resolved a pack by DIRECTORY PATH alone. So replacing
    `casa/newton/0.9.9/` with a valid pack that declares `casa/other@0.9.9`
    loaded successfully, and the record then said `casa/newton@0.9.9` on disk has
    that pack's checksum — a false diagnosis handed to an operator, of exactly
    the ref they would go and inspect.

    `tools._resolve_local_persona` has refused this since #323 — "a pack parked
    at the wrong directory never resolves" — so the loader was diverging from the
    established in-tree rule, not lacking one. The refusal was fail-closed
    either way; what was wrong was what it told the operator.
    """
    from agent_loader import LoadError, load_agent_from_dir
    from policies import load_policies
    from test_persona_install import _write_persona_repo

    config_dir = tmp_path / "config"
    personas_root = config_dir / "personas"
    bindings_root = tmp_path / "bindings-root"
    personas_root.mkdir(parents=True)
    monkeypatch.setenv("CASA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CASA_BINDINGS_DIR", str(bindings_root))

    policies = load_policies(_POLICIES)
    role = load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies).role_slot
    pinned = _publish(personas_root, tmp_path, negative_space="Never condescends.",
                      tag="approved")
    _approve(personas_root, bindings_root, role)

    # A DIFFERENT persona, valid in every respect, parked at the pinned ref's
    # directory — the shape a mis-restored backup or a hand-copied tree produces.
    from persona_pack import load_persona_pack
    foreign_repo = tmp_path / "repo-foreign"
    _write_persona_repo(foreign_repo, persona_id="casa/other", version=_VERSION)
    foreign_checksum = load_persona_pack(
        foreign_repo / "pack", foreign_repo / "manifest.json").checksum
    assert foreign_checksum != pinned
    version_dir = personas_root / _ID / _VERSION
    shutil.rmtree(version_dir)
    version_dir.mkdir(parents=True)
    shutil.copytree(foreign_repo / "pack", version_dir / "pack")
    shutil.copy2(foreign_repo / "manifest.json", version_dir / "manifest.json")

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LoadError):
            load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies,
                                bindings_dir=str(bindings_root))

    loud = [r for r in caplog.records
            if r.levelno >= logging.WARNING
            and r.getMessage().startswith("persona_binding_reconcile_failed")]
    assert len(loud) == 1
    payload = json.loads(loud[0].getMessage().partition(" ")[2])
    assert payload["persona_ref"] == _REF
    assert payload["pinned_checksum"] == pinned
    # The foreign pack's checksum is NOT what was found for this ref: nothing
    # was found for this ref at all.
    assert payload["found_checksum"] is None
    assert payload["reason"] == (
        f"override persona {_REF!r} is unavailable: the pack under "
        f"{personas_root} declares casa/other@{_VERSION}, not {_REF!r}")
