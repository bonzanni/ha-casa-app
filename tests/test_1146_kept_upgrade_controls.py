"""#1146 — the controls around the kept-upgrade red case.

`test_1146_kept_upgrade_reload_failed.py` pins the one arm the change adds: a
kept upgrade whose specialist reload failed and left a live agent whose
component root is not the new version's says so. These pin what must NOT get
that sentence — every reload that swapped the new version in, every case with
no root to compare (the persona-override residual among them) — and that the
root is the one read when the reload returned, not at telling time. Same
harness: the real dispatcher, reload handler, sequencer and kept compensation.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from test_1146_kept_upgrade_reload_failed import (  # noqa: F401 — `h` is a fixture
    ACTIVE, NEW, OLD, PREVIOUS, ROLE, _Txn, _agent, _binding, _cfg, _phrases, _run, h)

pytestmark = pytest.mark.unit      # asyncio_mode = auto (pytest.ini)

REPO = Path(__file__).resolve().parents[1]


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
    assert _phrases(env) == (1, 0)


async def test_a_post_swap_reload_error_keeps_the_active_text(h):
    """`reregister_failed` is raised after `runtime.agents[role]` is written:
    the new version is live, so the error alone must not turn the text."""
    h.reregister_error = RuntimeError("triggers refused")
    seq, env = await _run(h, _Txn())
    assert [r["kind"] for r in seq["reload_errors"]] == ["reregister_failed"]
    assert h.runtime.agents.writes == [ROLE]
    assert seq["loaded_root_after_reload"] == NEW
    assert _phrases(env) == (1, 0)


async def test_override_pre_swap_failure_keeps_the_current_text_the_1146_residual(h):
    """The disclosed residual of #1146. A persona-override binding carries no
    component root, so a failed pre-swap reload of an overridden specialist
    leaves nothing to compare and the kept arm keeps today's active text —
    false here, and why #1146 stays open. An identity comparison was ruled
    out: an unfenced `casa_reload agent` can swap the new version in before
    the sequencer's own reload, and an unchanged identity would then tell the
    previous version as running."""
    h.old.config.binding = _binding(None)
    h.loaded_root = None
    h.registry.fail_load = OSError("scan refused")
    seq, env = await _run(h, _Txn())
    assert h.runtime.agents.writes == []
    assert seq["loaded_root_after_reload"] is None
    assert _phrases(env) == (1, 0)


async def test_override_swap_never_claims_the_previous_version(h, monkeypatch):
    h.old.config.binding = _binding(None)
    h.loaded_root = None
    _not_ready(monkeypatch)
    seq, env = await _run(h, _Txn())
    assert h.runtime.agents.writes == [ROLE]
    assert seq["loaded_root_after_reload"] is None
    assert _phrases(env) == (1, 0)


async def test_no_live_agent_never_claims_the_previous_version(h):
    """A component-default specialist with no live agent (it failed to
    construct at boot): its pre-swap failure leaves no root to compare."""
    del h.runtime.agents[ROLE]
    h.registry.fail_load = OSError("scan refused")
    seq, env = await _run(h, _Txn())
    assert h.runtime.agents.writes == [] and ROLE not in h.runtime.agents
    assert seq["loaded_root_after_reload"] is None
    assert _phrases(env) == (1, 0)


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
    assert _phrases(env) == (0, 1)


async def test_failed_finish_keeps_the_restart_clause_on_the_new_text(h, monkeypatch):
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_swap_removal_disclosure",
                        lambda txn: {"plugin_data_note": "dropped"})
    h.registry.fail_load = OSError("scan refused")
    txn = _Txn()
    txn.fail_finish = True
    seq, env = await _run(h, txn)
    assert (txn.forward_finishes, txn.rollbacks, h.journal_completions) == (1, 0, 0)
    assert _phrases(env) == (0, 1)
    assert env["outcome"].count("refused until Casa restarts") == 1
    assert env["plugin_data_note"] == "dropped"
    assert env["kept_new_version"] is True and env["kind"] == "reload_failed"


@pytest.mark.parametrize("seq", [
    # The reload did not fail: a differing root alone never turns the text.
    {"kind": "postcondition_failed", "reload_errors": [],
     "loaded_root_after_reload": OLD},
    # A root that is not a string is no evidence.
    {"kind": "reload_failed", "reload_errors": [{"kind": "load_error"}],
     "loaded_root_after_reload": SimpleNamespace(root=OLD)},
    # The root the reload left is the new version's.
    {"kind": "reload_failed", "reload_errors": [{"kind": "unexpected"}],
     "loaded_root_after_reload": NEW},
    # No key: the sequencer recorded nothing.
    {"kind": "reload_failed", "reload_errors": [{"kind": "load_error"}]},
], ids=["no-reload-error", "non-string-root", "root-is-target", "no-key"])
async def test_the_active_text_stays_without_the_evidence(seq, monkeypatch):
    import tools as tools_mod

    async def kept(txn):
        return {"kept_new_version": True, "disk_ok": True, "runtime_ok": False}
    monkeypatch.setattr(tools_mod, "_bundle_compensate", kept)
    monkeypatch.setattr(tools_mod, "_swap_removal_disclosure", lambda txn: {})
    env = await tools_mod._bundle_seq_failure(
        SimpleNamespace(slug=ROLE, target_root=NEW), {"ok": False, **seq}, slug=ROLE)
    assert _phrases(env) == (1, 0)


def test_the_documents_no_longer_say_the_sequencer_arm_always_loaded():
    recipe = (REPO / "casa/rootfs/opt/casa/defaults/agents/executors/configurator/"
              "doctrine/recipes/specialist/upgrade.md").read_text(encoding="utf-8")
    recovery = (REPO / "docs/architecture/specialist-bundle-recovery.md").read_text(
        encoding="utf-8")
    assert recipe.count("there the new version IS loaded") == 0
    assert " ".join(recovery.split()).count(
        "comes after the sequencer loaded the new version, and keeps saying it is active") == 0
