"""Red case for #670 — the refusal's own facts must reach the operator.

Specified by Sol (`rounds-670/redcase-specify-sol.md`). The invariant, narrowed
until every part of it rests on bytes committed at or before the base:

    When a resident's override binding is refused because its pinned persona
    bytes cannot be served, the facts the failure point holds — the persona ref,
    the checksum the binding pins, and the checksum found when a pack resolved
    at all — reach a WARNING-or-higher log record during the failing resident
    load; and no string emitted on that path names a recovery that requires the
    live runtime.

Terminus (`terminus_check` exit 0 before this file existed):
`persona_install.py:991-995` (`require_persona_present` — the in-tree shape for
this exact condition: ref, found, pinned, and no tool),
`personality_binding.py:834-840` (the refusal is computed here),
`personality_binding.py:515-518` (`discard_desired` early-returns, so the reason
has no carrier), `docs/architecture/personality.md:185-188` (INV-PERS-003 —
boot-fatal, not a degraded mode), `tools.py:13492-13495`
(`_resolve_resident_role` answers `runtime_unavailable`, so a persona tool cannot
be run at this failure point).

Everything here goes through production code: the pack is published on disk and
read by `load_persona_pack`, the binding is built by
`materialize_override_binding` and committed by `InstanceDir`, and the failure is
produced by `load_agent_from_dir`. No `PersonaPack` is constructed by hand — a
test that can only reach its state synthetically reproduces nothing.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

_AGENTS = "casa/rootfs/opt/casa/defaults/agents"
_POLICIES = "casa/rootfs/opt/casa/defaults/policies/disclosure.yaml"
_ID = "casa/newton"
_VERSION = "0.9.9"
_REF = f"{_ID}@{_VERSION}"

# The tools that provably need `active_runtime` or the internal socket, so
# naming any of them at a boot-fatal failure point is advice that cannot be
# followed: `tools._resolve_resident_role` answers `runtime_unavailable` when
# `agent.active_runtime` is absent, and `casactl` POSTs to
# /run/casa/internal.sock, which a stopped app does not have. A FIXED
# enumeration on purpose — an open-ended phrase blacklist is the mechanism that
# failed fourteen times in this cluster.
_FORBIDDEN = frozenset({
    "resident_persona_swap", "resident_persona_reset", "persona_apply",
    "persona_install_inspect", "persona_install_commit", "casactl",
})


def _publish(personas_root: Path, tmp_path: Path, *, negative_space: str,
             tag: str) -> str:
    """Publish `casa/newton@0.9.9` into the installed layout and return the
    checksum the production loader computes for it. Two calls with different
    `negative_space` produce two VALID packs with different checksums — the
    "bytes changed under a pinned version" state, with no inconsistency an
    internal check could catch first."""
    from persona_pack import load_persona_pack
    from test_persona_install import _write_persona_repo

    repo = tmp_path / f"repo-{tag}"
    _write_persona_repo(repo, persona_id=_ID, version=_VERSION,
                        negative_space=negative_space)
    dest = personas_root / _ID / _VERSION
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    shutil.copytree(repo / "pack", dest / "pack")
    shutil.copy2(repo / "manifest.json", dest / "manifest.json")
    return load_persona_pack(dest / "pack", dest / "manifest.json").checksum


def _approve(personas_root: Path, bindings_root: Path, role):
    """Commit an ACTIVE override tuple pinning whatever is on disk right now,
    through the real materializer and the real InstanceDir writer."""
    from persona_pack import load_persona_pack
    from personality_binding import (
        InstanceDir, make_instance_tuple, materialize_override_binding,
    )

    base = personas_root / _ID / _VERSION
    pack = load_persona_pack(base / "pack", base / "manifest.json")
    binding = materialize_override_binding(
        role=role, persona=pack, override_source=f"operator:{_REF}")
    instance_dir = InstanceDir(bindings_root / "resident-concierge")
    instance_dir.stage_desired(make_instance_tuple(
        root=f"operator:{_REF}", binding=binding, config_snapshot={}))
    return instance_dir, instance_dir.commit_desired_to_active()


def _emitted(caplog, excinfo) -> list[str]:
    """Every string this failing load put in front of an operator: the fully
    formatted log records, plus the raised exception's whole cause/context
    chain."""
    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    out = [formatter.format(record) for record in caplog.records]
    cursor = excinfo.value
    seen = set()
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        out.append(str(cursor))
        cursor = cursor.__cause__ or cursor.__context__
    return out


@pytest.mark.parametrize("damage", ["changed-bytes", "invalid-pack", "removed-pack"])
def test_a_refused_pinned_override_reports_its_own_pin_and_what_it_found(
        tmp_path, monkeypatch, caplog, damage) -> None:
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

    pinned = _publish(personas_root, tmp_path, negative_space="Never condescends.",
                      tag="approved")
    instance_dir, approved = _approve(personas_root, bindings_root, role)
    assert approved.binding.mode == "override"
    assert f"{approved.binding.persona_id}@{approved.binding.persona_version}" == _REF
    assert approved.binding.persona_checksum == pinned
    assert instance_dir.desired() is None

    version_dir = personas_root / _ID / _VERSION
    if damage == "changed-bytes":
        found = _publish(personas_root, tmp_path,
                         negative_space="Never patronises, ever.", tag="changed")
        assert found != pinned
        expected_reason = (
            f"persona {_REF} on disk has checksum {found} but the binding pins "
            f"{pinned} — bytes changed under a pinned version")
    elif damage == "invalid-pack":
        (version_dir / "pack" / "persona.md").write_text(
            "# Core\n\nToo short.\n\n## Negative space\n\nNever condescends.\n",
            encoding="utf-8")
        found = None
        expected_reason = "Core body must contain 300\u2013500 characters"
    else:
        shutil.rmtree(version_dir)
        found = None
        expected_reason = (
            f"override persona {_REF!r} is unavailable: no pack with a manifest "
            f"under {personas_root} or "
            f"{Path('casa/rootfs/opt/casa/defaults/personas').resolve()}")

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LoadError) as excinfo:
            load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies,
                                bindings_dir=str(bindings_root))

    # (1) The refusal's facts reached the operator. Exactly one record, and the
    # WHOLE parsed object — never a substring: pytest's tmp_path embeds the
    # test's own name, and a substring assertion in this cluster has already
    # passed with its guard mutated off.
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1
    assert loud[0].name == "personality_binding"
    event, _, payload = loud[0].getMessage().partition(" ")
    assert event == "persona_binding_reconcile_failed"
    assert json.loads(payload) == {
        "active_tuple": "present",
        "found_checksum": found,
        "persona_ref": _REF,
        "pinned_checksum": pinned,
        "reason": expected_reason,
        "resident": "resident:concierge",
        "staged_tuple": "absent",
    }

    # (2) The altered bytes were never accepted into the binding, and nothing
    # was staged behind the operator's back.
    assert instance_dir.active() == approved
    assert instance_dir.desired() is None


@pytest.mark.parametrize("damage", ["changed-bytes", "invalid-pack", "removed-pack"])
def test_a_refused_pinned_override_names_no_recovery_that_needs_the_runtime(
        tmp_path, monkeypatch, caplog, damage) -> None:
    """The other half of the invariant, asserted on its own so it is red at the
    base commit in its own right rather than shadowed by the record count above.

    At the base commit the removed-pack variant emits
    `run resident_persona_reset to recover` and the changed-bytes variant emits
    `re-run resident_persona_swap ... or resident_persona_reset to recover` —
    both from inside a process that has just died, where
    `tools._resolve_resident_role` answers `runtime_unavailable` and `casactl`
    has no socket."""
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

    version_dir = personas_root / _ID / _VERSION
    if damage == "changed-bytes":
        _publish(personas_root, tmp_path,
                 negative_space="Never patronises, ever.", tag="changed")
    elif damage == "invalid-pack":
        (version_dir / "pack" / "persona.md").write_text(
            "# Core\n\nToo short.\n\n## Negative space\n\nNever condescends.\n",
            encoding="utf-8")
    else:
        shutil.rmtree(version_dir)

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LoadError) as excinfo:
            load_agent_from_dir(f"{_AGENTS}/concierge", policies=policies,
                                bindings_dir=str(bindings_root))

    assert {tool for tool in _FORBIDDEN
            if any(tool in line for line in _emitted(caplog, excinfo))} == set()
