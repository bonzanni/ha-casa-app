"""#1146 — a kept reclassifying upgrade whose specialist reload failed before the
swap must not say the new version is active.

`specialist_upgrade`'s kept sequencer arm (`_bundle_seq_failure` after
`_bundle_compensate` kept the new version) used one text whatever the sequencer
did: "the new version is active and stays active". When `reload_agent` fails at
`specialist_registry.load` it raises before `runtime.agents[role]` is replaced
(`reload.py`'s swap window), so the live specialist is still the previous
version's Agent and the sentence is false.

The red case drives the REAL `reload.dispatch` / `reload.reload_agent` through
the REAL `tools._bundle_reload_and_verify`, then the REAL `_bundle_seq_failure`
and the REAL `_bundle_compensate` kept arm over a transaction double. Only
filesystem-touching leaves are stubbed, the way
`tests/test_reload_disabled_specialist_scopes.py` stubs them: the agent loader,
the policy loader, agent construction, the plugin snapshot and registry reads,
the lifecycle invalidation, the plugin-health regeneration and notify, the
post-reload reconcilers, and the journal completion. Assertions are counts.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit      # asyncio_mode = auto (pytest.ini)

ROLE = "finance"
OLD = "component:finance@1.0.0#sha256:" + "a" * 64
NEW = "component:finance@2.0.0#sha256:" + "b" * 64
ACTIVE = "the new version is active and stays active"
# #1146 residual: the not-loaded text says only what was compared — not the new version.
PREVIOUS = "when that reload returned the specialist was not running the new version"


class _CountingAgents(dict):
    """`runtime.agents`, counting every write."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.writes: list = []

    def __setitem__(self, k, v):
        self.writes.append(k)
        super().__setitem__(k, v)


def _binding(root):
    mode = "override" if root is None else "component-default"
    return SimpleNamespace(mode=mode, component_root=root)


def _cfg(root):
    return SimpleNamespace(
        enabled=True, role=ROLE, triggers=[], channels=[],
        character=SimpleNamespace(name=ROLE.capitalize(), card=""),
        tools=SimpleNamespace(allowed=[]), delegates=[], binding=_binding(root),
    )


def _agent(tag, cfg):
    async def handle_message(msg):
        return None
    agent = MagicMock(name=f"agent-{tag}")
    agent.handle_message = handle_message
    agent.aclose = AsyncMock()
    agent.active_plugin_binding = {}
    agent.config = cfg
    return agent


class _Registry:
    """The specialist-registry stand-in. `fail_load` makes the re-scan raise."""

    def __init__(self, cfg):
        self._configs = {ROLE: cfg}
        self.loads = 0
        self.fail_load: BaseException | None = None

    def load(self, *, roles_dir=None):
        self.loads += 1
        if self.fail_load is not None:
            raise self.fail_load

    def all_configs(self):
        return dict(self._configs)

    def get(self, role):
        return self._configs.get(role)

    def is_disabled(self, role):
        return False

    def disabled_roles(self):
        return []

    def load_failures(self):
        return []


class _Txn:
    """A kept reclassifying upgrade, as `BundleTxn` answers it after activation."""

    def __init__(self, *, target_root=NEW):
        self.slug = ROLE
        self.op = "upgrade"
        self.target_root = target_root
        self.journal_path = "/nonexistent/journal"
        self.kept_reads = 0
        self.forward_finishes = 0
        self.rollbacks = 0
        self.fail_finish = False

    def activation_kept(self):
        self.kept_reads += 1
        return True

    def finish_forward(self):
        self.forward_finishes += 1
        if self.fail_finish:
            raise OSError("finish refused")

    def rollback_disk(self):
        self.rollbacks += 1


@pytest.fixture
async def h(tmp_path, monkeypatch):
    import reload as reload_mod
    import tools as tools_mod
    import specialist_bundle_journal
    from agent_registry import AgentRegistry
    from bus import MessageBus
    from runtime import CasaRuntime

    agents_dir = tmp_path / "agents"
    (agents_dir / "specialists" / ROLE).mkdir(parents=True)
    (tmp_path / "policies").mkdir()
    (tmp_path / "secrets").mkdir()

    st = SimpleNamespace(constructions=0, journal_completions=0, loaded_root=NEW,
                         reregister_error=None)

    monkeypatch.setattr("agent_loader.load_agent_from_dir",
                        lambda *a, **kw: _cfg(st.loaded_root))
    monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("agent_home.provision_agent_home",
                        lambda *, role, home_root, defaults_root: None)

    def construct(*, cfg, runtime, agent_registry=None):
        st.constructions += 1
        return _agent(f"NEW{st.constructions}", cfg)
    monkeypatch.setattr(reload_mod, "_construct_agent", construct)

    real_register = reload_mod._register_and_reconcile_async

    async def register(*a, **kw):
        if st.reregister_error is not None:
            raise st.reregister_error
        return await real_register(*a, **kw)
    monkeypatch.setattr(reload_mod, "_register_and_reconcile_async", register)

    monkeypatch.setattr("plugin_registry.reload_snapshot", lambda: None)
    monkeypatch.setattr("plugin_registry.load_registry", lambda *a, **kw: {})
    monkeypatch.setattr("plugin_registry.owned_entries_for", lambda slug, reg: [])
    monkeypatch.setattr("trigger_reconcile.reconcile_from_runtime", AsyncMock())
    monkeypatch.setattr("callback_reconcile.reconcile_from_runtime", AsyncMock())
    monkeypatch.setattr("event_reconcile.reconcile_plugin_events", AsyncMock())
    monkeypatch.setattr("plugin_setup_episodes.kick", lambda: None)
    monkeypatch.setattr("resident_trigger_secrets.mint_for_specs",
                        lambda specs, *, secrets_dir, role: [])
    monkeypatch.setattr("trigger_reconcile.SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr(tools_mod, "_invalidate_lifecycle", lambda **kw: None)
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health_held", AsyncMock())
    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", AsyncMock())

    def complete(path):
        st.journal_completions += 1
    monkeypatch.setattr(specialist_bundle_journal, "complete", complete)

    old_cfg = _cfg(OLD)
    registry = _Registry(old_cfg)
    bus = MessageBus()
    old = _agent("OLD", old_cfg)
    trigger_registry = MagicMock()
    trigger_registry.plugin_overlay_unavailable.return_value = False
    trigger_registry.callback_overlay_unavailable.return_value = False
    runtime = CasaRuntime(
        agents=_CountingAgents({ROLE: old}), role_configs={}, specialist_registry=registry,
        executor_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_registry=AgentRegistry.build(residents={}, specialists=registry.all_configs()),
        trigger_registry=trigger_registry, mcp_registry=MagicMock(),
        session_registry=MagicMock(), channel_manager=MagicMock(),
        bus=bus, engagement_driver=MagicMock(), claude_code_driver=MagicMock(),
        policy_lib=MagicMock(),
        config_dir=str(tmp_path), agents_dir=str(agents_dir),
        home_root=str(tmp_path / "home"), defaults_root="/opt/casa",
    )
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_role_map", dict(registry.all_configs()))
    monkeypatch.setattr(tools_mod, "_specialist_registry", registry, raising=False)

    st.runtime, st.registry, st.old, st.bus = runtime, registry, old, bus
    try:
        yield st
    finally:
        for name in list(bus.queues):
            bus.unregister(name)
        for t in bus.agent_loop_tasks():
            t.cancel()
        await asyncio.gather(*bus.agent_loop_tasks(), return_exceptions=True)


async def _run(h, txn):
    """The upgrade's post-commit half, as `specialist_upgrade` runs it: the
    sequencer under `_PLUGIN_TOOLS_LOCK`, then the failure envelope."""
    import tools as tools_mod
    async with tools_mod._PLUGIN_TOOLS_LOCK:
        seq = await tools_mod._bundle_reload_and_verify(
            ROLE, removed_artifact_ids=[], targets_removed=[])
    env = await tools_mod._bundle_seq_failure(txn, seq, slug=ROLE)
    return seq, env


def _phrases(env):
    return (env["outcome"].count(ACTIVE), env["outcome"].count(PREVIOUS))


async def test_kept_upgrade_failed_reload_reports_previous_version(h):
    """RED at 4838256c: component-default, the live agent's root is OLD, the
    upgrade's target is NEW, and the reload fails at the registry re-scan —
    before the swap. The outcome said "active and stays active"."""
    h.registry.fail_load = OSError("scan refused")
    txn = _Txn()
    seq, env = await _run(h, txn)

    assert h.constructions == 1, "constructions"
    assert h.registry.loads == 1, "registry_scans"
    assert h.runtime.agents.writes == [], "swaps"
    assert h.runtime.agents[ROLE] is h.old, "prior_instances"
    assert h.runtime.agents[ROLE].config.binding.component_root == OLD, "prior_roots"
    rows = seq["reload_errors"]
    assert len(rows) == 1, "reload_errors"
    assert sum(1 for r in rows if r.get("kind") == "specialist_reload_failed"
               and r.get("scope") == "agent" and r.get("role") == ROLE
               and r.get("message") == "scan refused") == 1, "intended_reload_error"
    assert (txn.kept_reads, txn.forward_finishes, txn.rollbacks,
            h.journal_completions) == (1, 1, 0, 1), "kept_compensation"
    assert env["kind"] == "reload_failed" and env["kept_new_version"] is True
    assert _phrases(env) == (0, 1), "outcome_phrase_counts"


async def test_kept_reload_does_not_identify_unmeasured_previous_version(h):
    """RED at b6020aa9 (#1146 residual): live A (OLD), this upgrade replaced B,
    target C (NEW), reload fails at the registry re-scan. The arm compares the
    live root with the target only, so it may say the specialist was not
    running the new version, never that A was "its previous version"."""
    replaced = "component:finance@1.5.0#sha256:" + "c" * 64
    assert len({OLD, replaced, NEW}) == 3

    h.registry.fail_load = OSError("scan refused")
    txn = _Txn()
    txn.before_tuple_files = {
        "active.yaml": f"root: '{replaced}'\n",
    }

    seq, env = await _run(h, txn)

    assert h.constructions == 1
    assert h.registry.loads == 1
    assert len(h.runtime.agents.writes) == 0
    assert h.runtime.agents[ROLE] is h.old
    assert seq["loaded_root_after_reload"] == OLD

    rows = seq["reload_errors"]
    assert len(rows) == 1
    assert sum(
        r.get("kind") == "specialist_reload_failed"
        and r.get("scope") == "agent"
        and r.get("role") == ROLE
        and r.get("message") == "scan refused"
        for r in rows
    ) == 1
    assert (
        txn.kept_reads,
        txn.forward_finishes,
        txn.rollbacks,
        h.journal_completions,
    ) == (1, 1, 0, 1)
    assert env["kind"] == "reload_failed"
    assert env["kept_new_version"] is True

    fragment = (
        "when that reload returned the specialist "
        "was not running the new version"
    )
    assert (
        env["outcome"].count("previous version"),
        env["outcome"].count(fragment),
    ) == (0, 1), "outcome_phrase_counts"


def test_recovery_doc_does_not_identify_unmeasured_previous_version():
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "docs/architecture/specialist-bundle-recovery.md"
    )
    text = " ".join(path.read_text(encoding="utf-8").split())
    claim = (
        "say the specialist was running its previous version "
        "when the reload returned"
    )
    assert text.count(claim) == 0, "unmeasured_previous_version_claim"
