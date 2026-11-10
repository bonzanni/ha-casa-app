"""#1053: the plugin-health DM does not announce an install's own intermediate
rows (consent pending, a setup-provided value not wired yet, setup still to
run, not loaded yet, waiting for its specialist) while the configurator run
installing that plugin holds its lease; the pass
run when the lease is released names whatever still stands; a real fault is
sent at once, install or not."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import casa_core
import plugin_health
import tools as tools_mod
from engagement_registry import EngagementRegistry
from plugin_registry import PluginIssue

pytestmark = [pytest.mark.asyncio]


class _Channel:
    is_ready = True

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, message, context):
        from channels import DeliveryOutcome
        self.sent.append(str(message))
        return DeliveryOutcome.DELIVERED


class _CM:
    def __init__(self):
        self.channel = _Channel()

    def get(self, name):
        return self.channel if name == "telegram" else None


def _issue(name, code, target=None):
    return PluginIssue(name, target, "verify", code, None)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A health report path, a channel manager, and a clean configurator lease."""
    import configurator_run
    cm = _CM()
    path = tmp_path / "plugin-health.json"
    monkeypatch.setattr(tools_mod, "_channel_manager", cm)
    monkeypatch.setattr(tools_mod, "_PLUGIN_HEALTH_PATH", str(path))
    monkeypatch.setattr(configurator_run, "LEASE_PATH", str(tmp_path / "lease.json"))
    configurator_run._reset_for_tests()
    yield SimpleNamespace(cm=cm, path=path)
    configurator_run._reset_for_tests()


async def _install_run():
    import configurator_run
    return await configurator_run.acquire(run_key="run-1", via="delegation",
                                          task="install")


def _recorded(*names):
    tools_mod._record_plugin_installer(list(names))


async def _drain():
    import configurator_run
    for _ in range(3):
        pending = list(configurator_run._NOTIFIES)
        if pending:
            await asyncio.gather(*pending)
        await asyncio.sleep(0)


# ---------------------------------------------------------------- classify

@pytest.mark.parametrize("code", [
    "trigger_pending_ack", "callback_pending_ack", "event_pending_ack",
    "setup_env_unprovisioned", "setup_episode_pending", "not_loaded",
    "target_pending"])
async def test_install_phase_codes(code):
    assert plugin_health.is_install_phase(code)


@pytest.mark.parametrize("code", [
    "setup_episode_failed", "setup_episode_stale", "env_unresolved",
    "corrupt_artifact", "reload_required", "reload_failed", "not_ready",
    "authorization_missing", None])
async def test_faults_are_not_install_phase(code):
    assert not plugin_health.is_install_phase(code)


# ------------------------------------------------ an install code never hides a fault

async def test_row_reason_prefers_a_fault_behind_an_install_code():
    row = {"reasons": ["setup_env_unprovisioned", "env_unresolved"]}
    assert tools_mod._row_health_reason(row, {"reasons": row["reasons"]}) \
        == "env_unresolved"


async def test_row_reason_sees_a_fault_a_stale_binding_replaced():
    # verify replaces a configured failure on the row with reload_required;
    # the plugin's own reasons still carry it.
    row = {"reasons": ["reload_required"], "state": "active",
           "active_artifact_id": None}
    assert tools_mod._row_health_reason(
        row, {"reasons": ["setup_env_unprovisioned", "env_unresolved"]}) \
        == "env_unresolved"


async def test_row_reason_keeps_an_install_code_when_nothing_else_stands():
    row = {"reasons": ["reload_required"], "state": "active",
           "active_artifact_id": None}
    assert tools_mod._row_health_reason(row, {"reasons": []}) == "not_loaded"
    row = {"reasons": ["setup_env_unprovisioned"]}
    assert tools_mod._row_health_reason(
        row, {"reasons": ["setup_env_unprovisioned"]}) \
        == "setup_env_unprovisioned"


async def test_row_reason_of_a_fault_is_unchanged():
    row = {"reasons": ["corrupt_artifact", "setup_env_unprovisioned"]}
    assert tools_mod._row_health_reason(row, {}) == "corrupt_artifact"


# ------------------------------------------------------------------ the DM

async def test_live_install_defers_its_install_rows_and_sends_its_faults(wired):
    await _install_run()
    _recorded("gmail")
    plugin_health.write_report(issues=[
        _issue("gmail", "setup_env_unprovisioned"),
        _issue("gmail", "trigger_pending_ack"),
        _issue("gmail", "setup_episode_failed"),
        _issue("other", "setup_episode_pending"),
    ], warnings=[_issue("gmail", "target_pending", "specialist:x")],
        path=wired.path)
    await casa_core.notify_plugin_health(wired.cm, path=str(wired.path))
    assert len(wired.cm.channel.sent) == 1
    msg = wired.cm.channel.sent[0]
    assert "could not finish setting up" in msg           # gmail's fault
    assert "other has a setup step still to finish" in msg  # not being installed
    assert "setup-provided value" not in msg
    assert "approval" not in msg
    assert "waiting for the specialist" not in msg
    # The deferred rows are unmarked: exactly three stay new.
    left = plugin_health.new_fingerprints(plugin_health.load_report(wired.path))
    assert len(left) == 3


async def test_a_pass_with_only_deferred_rows_sends_nothing(wired):
    await _install_run()
    _recorded("gmail")
    plugin_health.write_report(issues=[
        _issue("gmail", "setup_env_unprovisioned"),
        _issue("gmail", "event_pending_ack")], warnings=[], path=wired.path)
    await casa_core.notify_plugin_health(wired.cm, path=str(wired.path))
    assert wired.cm.channel.sent == []
    assert len(plugin_health.new_fingerprints(
        plugin_health.load_report(wired.path))) == 2


async def test_without_an_installing_run_nothing_is_deferred(wired):
    plugin_health.write_report(issues=[
        _issue("gmail", "setup_env_unprovisioned")], warnings=[],
        path=wired.path)
    await casa_core.notify_plugin_health(wired.cm, path=str(wired.path))
    assert len(wired.cm.channel.sent) == 1


async def test_the_install_ending_names_what_still_stands(wired):
    lease = await _install_run()
    _recorded("gmail")
    plugin_health.write_report(issues=[
        _issue("gmail", "setup_env_unprovisioned")], warnings=[],
        path=wired.path)
    await casa_core.notify_plugin_health(wired.cm, path=str(wired.path))
    assert wired.cm.channel.sent == []
    lease.release()
    await _drain()
    assert len(wired.cm.channel.sent) == 1
    assert "gmail is waiting for a setup-provided value" in \
        wired.cm.channel.sent[0]
    assert tools_mod.plugins_under_live_install() == set()


async def test_a_run_that_installed_nothing_runs_no_pass(wired, monkeypatch):
    calls = []

    async def _notify():
        calls.append(1)

    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", _notify)
    lease = await _install_run()
    lease.release()
    await _drain()
    assert calls == []


async def test_a_failing_observer_never_fails_the_transition(tmp_path):
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))

    def _boom(rec):
        raise RuntimeError("observer")

    reg.set_terminal_observer(_boom)
    rec = await reg.create(kind="specialist", role_or_type="finance",
                           driver="in_casa", task="job", origin={})
    await reg.mark_cancelled(rec.id)
    assert rec.status == "cancelled"


# ------------------------------------------------------- who records the install

async def test_a_plugin_mutation_records_its_run(wired, monkeypatch):
    import agent as agent_mod
    import callback_reconcile
    import event_reconcile
    import plugin_registry
    import trigger_reconcile

    monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda: None)
    monkeypatch.setattr(plugin_registry, "snapshot_generation", lambda: 1)
    monkeypatch.setattr(agent_mod, "active_runtime",
                        SimpleNamespace(trigger_registry=object()))
    monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                        lambda plugin_name: {"ready": True, "targets": []})
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda extra: None)
    seen: list = []

    async def _notify():
        seen.append(tools_mod.plugins_under_live_install())

    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", _notify)
    monkeypatch.setattr(trigger_reconcile, "reconcile_from_runtime",
                        AsyncMock(return_value=[]))
    monkeypatch.setattr(callback_reconcile, "reconcile_from_runtime",
                        AsyncMock(return_value=[]))
    monkeypatch.setattr(event_reconcile, "reconcile_plugin_events",
                        AsyncMock(return_value=[]))
    await _install_run()
    await tools_mod._reload_and_verify_targets("gmail", [], expect="present")
    # Recorded before the mutation's own notify pass reads it.
    assert seen == [{"gmail"}]


async def test_an_env_wiring_records_its_run(wired, monkeypatch):
    import plugin_env_conf
    monkeypatch.setattr(plugin_env_conf, "set_entry", lambda *a, **k: None)
    await _install_run()
    await tools_mod.set_plugin_env_reference.handler(
        {"plugin": "gmail", "var_name": "GMAIL_TOKEN",
         "op_ref_or_value": "op://v/i/f"})
    assert tools_mod.plugins_under_live_install() == {"gmail"}


async def test_an_unbound_mutation_records_nothing(wired):
    tools_mod._record_plugin_installer(["gmail"])
    assert tools_mod.plugins_under_live_install() == set()


async def test_a_bundle_install_records_its_scoped_entries(wired, monkeypatch):
    import agent as agent_mod
    import plugin_registry
    import reload as reload_mod

    async def _dispatch(scope, *, runtime, role=None):
        return {"status": "ok"}

    monkeypatch.setattr(reload_mod, "dispatch", _dispatch)
    monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(plugin_registry, "owned_entries_for",
                        lambda slug, reg: [{"name": f"{slug}.bank-feed"},
                                           {"name": f"{slug}.ledger"}])
    monkeypatch.setattr(plugin_registry, "resolve_for",
                        lambda t: SimpleNamespace(plugins=[]))
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda issues: None)
    monkeypatch.setattr(tools_mod, "_invalidate_lifecycle", lambda **k: None)
    monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                        lambda plugin_name: {"ready": True, "targets": []})
    monkeypatch.setattr(agent_mod, "active_runtime",
                        SimpleNamespace(agents={}, agents_dir=None),
                        raising=False)
    seen: list = []

    async def _notify():
        seen.append(tools_mod.plugins_under_live_install())

    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", _notify)
    await _install_run()
    await tools_mod._bundle_reload_and_verify(
        "finance", removed_artifact_ids=[], targets_removed=[])
    assert seen == [{"finance.bank-feed", "finance.ledger"}]


# ------------------------------------------ diff round 1 (Astra): pruning race

async def test_a_notify_while_the_channel_is_down_keeps_the_pass_owed(wired):
    # A pass landing while the run still holds the lease, with the channel
    # down, sends nothing and consumes nothing: the release's pass names it.
    lease = await _install_run()
    _recorded("gmail")
    plugin_health.write_report(issues=[
        _issue("gmail", "setup_env_unprovisioned")], warnings=[],
        path=wired.path)
    wired.cm.channel.is_ready = False
    await casa_core.notify_plugin_health(wired.cm, path=str(wired.path))
    assert wired.cm.channel.sent == []
    wired.cm.channel.is_ready = True
    lease.release()
    await _drain()
    assert len(wired.cm.channel.sent) == 1
    assert "gmail" in wired.cm.channel.sent[0]


# --------------------------- diff round 1 (Astra): faults verify grades codeless

async def test_row_reason_sees_a_missing_program():
    row = {"reasons": ["setup_env_unprovisioned"]}
    verify = {"reasons": ["setup_env_unprovisioned"],
              "tools": [{"verify_bin": "gws", "status": "missing"}],
              "secrets": [{"var": "K", "status": "unprovisioned"}]}
    assert tools_mod._row_health_reason(row, verify) \
        == "system_requirement_missing"


async def test_row_reason_sees_a_secret_that_is_neither_resolved_nor_setup():
    row = {"reasons": ["setup_env_unprovisioned"]}
    verify = {"reasons": ["setup_env_unprovisioned"], "tools": [],
              "secrets": [{"var": "", "status": "unresolved"}]}
    assert tools_mod._row_health_reason(row, verify) == "env_unresolved"


async def test_row_reason_keeps_the_install_code_when_all_else_is_ready():
    row = {"reasons": ["setup_env_unprovisioned"]}
    verify = {"reasons": ["setup_env_unprovisioned"],
              "tools": [{"verify_bin": "gws", "status": "ready"}],
              "secrets": [{"var": "K", "status": "unprovisioned"},
                          {"var": "J", "status": "resolved"}]}
    assert tools_mod._row_health_reason(row, verify) \
        == "setup_env_unprovisioned"


async def test_a_missing_program_is_phrased_as_one():
    assert plugin_health.describe_issue(
        {"name": "gmail", "reason_code": "system_requirement_missing"}) \
        == "gmail is missing a program it needs"


async def test_a_targetless_plugin_gets_a_row_for_a_codeless_fault(tmp_path,
                                                            monkeypatch):
    import callback_reconcile
    import event_reconcile
    import plugin_registry
    import trigger_reconcile

    monkeypatch.setattr(tools_mod, "_PLUGIN_HEALTH_PATH",
                        tmp_path / "health.json")
    for mod in (trigger_reconcile, callback_reconcile, event_reconcile):
        monkeypatch.setattr(mod, "current_issues", lambda: [])
    monkeypatch.setattr(plugin_registry, "resolve_all",
                        lambda: SimpleNamespace(issues=[], warnings=[]))
    monkeypatch.setattr(
        plugin_registry, "load_registry",
        lambda *a, **k: SimpleNamespace(valid=True, entries=[
            {"name": "gmail", "targets": [], "artifact_id": "a" * 64}]))
    monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                        lambda plugin_name: {
                            "ready": False, "targets": [],
                            "reasons": ["setup_env_unprovisioned"],
                            "tools": [{"verify_bin": "gws",
                                       "status": "missing"}],
                            "secrets": [{"var": "K",
                                         "status": "unprovisioned"}]})
    written = {}
    monkeypatch.setattr(plugin_health, "write_report",
                        lambda **kw: written.update(kw))
    tools_mod._regenerate_plugin_health([])
    codes = sorted(i.reason_code for i in written["issues"]
                   if getattr(i, "name", None) == "gmail")
    assert codes == ["setup_env_unprovisioned", "system_requirement_missing"]
