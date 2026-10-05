"""#1146 — the controls around the kept-upgrade red case.

`test_1146_kept_upgrade_reload_failed.py` pins the one arm the change adds: a
kept upgrade whose specialist reload failed and left a live agent whose
component root is not the new version's says so. These pin what must NOT get
that sentence — every reload that swapped the new version in keeps the active
text, and every failed reload with no root to compare (a persona override, no
live agent, no recorded root) says the running version could not be
established, never "active" — and that the root is the one read when the
reload returned, not at telling time. Same harness: the real dispatcher, reload
handler, sequencer and kept compensation.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from test_1146_kept_upgrade_reload_failed import (  # noqa: F401 — `h` is a fixture
    ACTIVE, NEW, OLD, PREVIOUS, ROLE, _Txn, _agent, _binding, _cfg, _phrases, _run, h)

pytestmark = pytest.mark.unit      # asyncio_mode = auto (pytest.ini)

REPO = Path(__file__).resolve().parents[1]
UNKNOWN = ("which version it was running when that reload returned could not be "
           "established")


def _told(env):
    """(active, not the new version, could not be established) phrase counts."""
    return (*_phrases(env), env["outcome"].count(UNKNOWN))


def _not_ready(monkeypatch):
    """One owned entry whose verify is blocking: `postcondition_failed`."""
    import tools as tools_mod
    monkeypatch.setattr("plugin_registry.owned_entries_for",
                        lambda slug, reg: [{"name": f"{ROLE}.p"}])
    monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                        lambda *, plugin_name: {"ready": False, "reasons": ["mcp_missing"]})
    monkeypatch.setattr(tools_mod, "_bundle_binding_blocked", lambda v: True)


async def test_a_reload_that_swapped_in_the_new_version_keeps_the_active_text(h, monkeypatch):
    _not_ready(monkeypatch)
    seq, env = await _run(h, _Txn())
    assert seq["kind"] == "postcondition_failed" and seq["reload_errors"] == []
    assert h.runtime.agents.writes == [ROLE]
    assert seq["loaded_root_after_reload"] == NEW
    assert _told(env) == (1, 0, 0)


async def test_a_post_swap_reload_error_keeps_the_active_text(h):
    """`reregister_failed` is raised after `runtime.agents[role]` is written:
    the new version is live, so the error alone must not turn the text."""
    h.reregister_error = RuntimeError("triggers refused")
    seq, env = await _run(h, _Txn())
    assert [r["kind"] for r in seq["reload_errors"]] == ["reregister_failed"]
    assert h.runtime.agents.writes == [ROLE]
    assert seq["loaded_root_after_reload"] == NEW
    assert _told(env) == (1, 0, 0)


async def test_override_pre_swap_failure_says_the_running_version_could_not_be_established(h):
    """The #1146 residual, inverted (it pinned the active text before). A
    persona-override binding carries no component root, so a failed pre-swap
    reload of an overridden specialist leaves nothing to compare: the outcome
    says which version is running could not be established — never "active",
    and never "previous version". An identity comparison was ruled out: an
    unfenced `casa_reload agent` can swap the new version in before the
    sequencer's own reload, and an unchanged identity would then tell the
    previous version as running."""
    h.old.config.binding = _binding(None)
    h.loaded_root = None
    h.registry.fail_load = OSError("scan refused")
    seq, env = await _run(h, _Txn())
    assert h.runtime.agents.writes == []
    assert seq["loaded_root_after_reload"] is None
    assert _told(env) == (0, 0, 1)
    assert env["kept_new_version"] is True and env["kind"] == "reload_failed"


async def test_override_post_swap_reload_error_says_it_could_not_be_established(h):
    """An override binding whose swap landed and whose re-registration then
    failed: there is still no root to read, so the same no-evidence text."""
    h.old.config.binding = _binding(None)
    h.loaded_root = None
    h.reregister_error = RuntimeError("triggers refused")
    seq, env = await _run(h, _Txn())
    assert h.runtime.agents.writes == [ROLE]
    assert seq["loaded_root_after_reload"] is None
    assert _told(env) == (0, 0, 1)


async def test_override_swap_never_claims_the_previous_version(h, monkeypatch):
    h.old.config.binding = _binding(None)
    h.loaded_root = None
    _not_ready(monkeypatch)
    seq, env = await _run(h, _Txn())
    assert h.runtime.agents.writes == [ROLE]
    assert seq["loaded_root_after_reload"] is None
    assert _told(env) == (1, 0, 0)


async def test_no_live_agent_never_claims_the_previous_version(h):
    """A component-default specialist with no live agent (it failed to
    construct at boot): its pre-swap failure leaves no root to compare."""
    del h.runtime.agents[ROLE]
    h.registry.fail_load = OSError("scan refused")
    seq, env = await _run(h, _Txn())
    assert h.runtime.agents.writes == [] and ROLE not in h.runtime.agents
    assert seq["loaded_root_after_reload"] is None
    assert _told(env) == (0, 0, 1)


async def test_the_root_read_when_the_reload_returned_decides(h, monkeypatch):
    """The live agent changes AFTER the reload returned, while the sequencer
    regenerates plugin health: the root the reload left is what is told."""
    import tools as tools_mod

    async def swap_in_new(*a, **kw):
        h.runtime.agents[ROLE] = _agent("LATE", _cfg(NEW))
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health_held", swap_in_new)
    h.registry.fail_load = OSError("scan refused")
    seq, env = await _run(h, _Txn())
    assert h.runtime.agents.writes == [ROLE]
    assert h.runtime.agents[ROLE].config.binding.component_root == NEW
    assert seq["loaded_root_after_reload"] == OLD
    assert _told(env) == (0, 1, 0)


async def test_failed_finish_keeps_the_restart_clause_on_the_new_text(h, monkeypatch):
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_swap_removal_disclosure",
                        lambda txn: {"plugin_data_note": "dropped"})
    h.registry.fail_load = OSError("scan refused")
    txn = _Txn()
    txn.fail_finish = True
    seq, env = await _run(h, txn)
    assert (txn.forward_finishes, txn.rollbacks, h.journal_completions) == (1, 0, 0)
    assert _told(env) == (0, 1, 0)
    assert env["outcome"].count("refused until Casa restarts") == 1
    assert env["plugin_data_note"] == "dropped"
    assert env["kept_new_version"] is True and env["kind"] == "reload_failed"


@pytest.mark.parametrize("seq,told", [
    # The reload did not fail: a differing root alone never turns the text.
    ({"kind": "postcondition_failed", "reload_errors": [],
      "loaded_root_after_reload": OLD}, (1, 0, 0)),
    # The root the reload left is the new version's.
    ({"kind": "reload_failed", "reload_errors": [{"kind": "unexpected"}],
      "loaded_root_after_reload": NEW}, (1, 0, 0)),
    # A failed reload with a root that is not a string: no evidence.
    ({"kind": "reload_failed", "reload_errors": [{"kind": "load_error"}],
      "loaded_root_after_reload": SimpleNamespace(root=OLD)}, (0, 0, 1)),
    # A failed reload, no key: the sequencer recorded nothing.
    ({"kind": "reload_failed", "reload_errors": [{"kind": "load_error"}]}, (0, 0, 1)),
    # A failed reload whose recorded root is None.
    ({"kind": "reload_failed", "reload_errors": [{"kind": "load_error"}],
      "loaded_root_after_reload": None}, (0, 0, 1)),
], ids=["no-reload-error", "root-is-target", "non-string-root", "no-key", "none-root"])
async def test_what_each_kind_of_evidence_tells(seq, told, monkeypatch):
    import tools as tools_mod

    async def kept(txn):
        return {"kept_new_version": True, "disk_ok": True, "runtime_ok": False}
    monkeypatch.setattr(tools_mod, "_bundle_compensate", kept)
    monkeypatch.setattr(tools_mod, "_swap_removal_disclosure", lambda txn: {})
    env = await tools_mod._bundle_seq_failure(
        SimpleNamespace(slug=ROLE, target_root=NEW), {"ok": False, **seq}, slug=ROLE)
    assert _told(env) == told


def test_the_documents_no_longer_say_the_sequencer_arm_always_loaded():
    recipe = (REPO / "casa/rootfs/opt/casa/defaults/agents/executors/configurator/"
              "doctrine/recipes/specialist/upgrade.md").read_text(encoding="utf-8")
    recovery = (REPO / "docs/architecture/specialist-bundle-recovery.md").read_text(
        encoding="utf-8")
    assert recipe.count("there the new version IS loaded") == 0
    assert " ".join(recovery.split()).count(
        "comes after the sequencer loaded the new version, and keeps saying it is active") == 0


async def test_override_pre_swap_failure_through_the_upgrade_handler(h, monkeypatch):
    """The #1146 residual through the real `specialist_upgrade` handler: only the
    library commit (returning the kept transaction) and the resume-input check
    are doubled; the sequencer, its reload and the kept compensation are real."""
    import json
    import specialist_install
    import specialist_receipt

    class _Checked:
        ok = True

        class receipt:  # noqa: N801
            receipt_id = "r1"
            receipt_digest = "d"
            plugins = ()

        class component:  # noqa: N801
            component_id = "c"
            version = "2.0.0"
            slug = ROLE
            checksum = "x"

            class role:  # noqa: N801
                role = {}
            default_persona_ref = None
            default_persona_checksum = None
        dependencies = ()
        root_digest = "rd"

    txn = _Txn()
    txn.removed_artifact_ids = []
    monkeypatch.setattr(specialist_install, "validate_resume_inputs", lambda **k: _Checked)
    monkeypatch.setattr(specialist_receipt, "load", lambda rid, **k: object())
    monkeypatch.setattr(specialist_install, "upgrade_specialist",
                        lambda **kw: (SimpleNamespace(state="active"), txn))
    h.old.config.binding = _binding(None)
    h.loaded_root = None
    h.registry.fail_load = OSError("scan refused")
    import tools as tools_mod
    r = await tools_mod.specialist_upgrade.handler({
        "slug": ROLE, "component_id": "c", "version": "2.0.0", "root_digest": "rd",
        "staged_dir": "/nonexistent", "receipt_id": "r1"})
    out = json.loads(r["content"][0]["text"])
    assert out["kind"] == "reload_failed" and out["kept_new_version"] is True
    assert h.runtime.agents.writes == []
    assert (txn.forward_finishes, txn.rollbacks, h.journal_completions) == (1, 0, 1)
    assert _told(out) == (0, 0, 1)
