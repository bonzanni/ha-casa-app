"""Plugin-event WIRING (boot, scheduler, reload scope, plugin lifecycle,
health merge).

Structurally the sibling of ``tests/test_callback_wiring.py`` (read that
file whole before touching this one — its idioms, including source-level
structural pins for logic nested inside ``casa_core.main()``, are mirrored
here). These pin the load-bearing wiring requirements Task 10 adds:

* **Boot order.** The event spool is initialised, the routing map is
  reconciled, and ONLY THEN does the delivery worker's boot recovery run —
  and that recovery MUST complete before ``start_worker()`` (mirrors the
  callback boot pin at ``casa_core.py``'s ``_boot_reconcile_plugin_callbacks``
  docstring: a routed pair's ready state must be published before anything
  can dispatch against it).
* **Periodic liveness.** The scheduled sweep/recovery jobs exist, run off
  the loop (the lock-stall ruling), and the recovery job additionally
  retries the ROUTING_UNAVAILABLE-sentinel compute on its own — a transient
  reconcile failure must not silently suspend delivery until an unrelated
  plugin-lifecycle mutation happens to retry it.
* **Reconcile pairing.** Every site that reconciles triggers/callbacks
  (reload's trigger-affecting scopes, the 5-mutation lifecycle sequencer)
  also reconciles events.
* **Health merge.** ``event_reconcile.current_issues()`` (a list of DICTS,
  never ``PluginIssue`` instances) is folded into
  ``tools._regenerate_plugin_health``'s report, concatenated directly —
  never through the ``PluginIssue``-attribute-only ``_add``/
  ``_rediscoverable`` helpers.
* **Fail-closed registry.** ``get_installed``/``get_registry_valid`` never
  fabricate membership from an invalid registry snapshot.
* **event_wake is casa-internal only.** The synthetic marker an event nudge
  rides is a RESERVED context key — unreachable from any external ingress —
  and a turn dispatched under it fails ask_user's provenance gate exactly
  like a callback/setup nudge does.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

CASA = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"
_SRC = (CASA / "casa_core.py").read_text(encoding="utf-8")
_FLAT = " ".join(_SRC.split())


# ---------------------------------------------------------------------------
# Boot order — spool init -> boot reconcile -> configure -> recovery ->
# start_worker, with "recovery before start_worker" as the pinned invariant.
# ---------------------------------------------------------------------------


class TestBootOrder:
    def test_event_spool_initialised(self):
        assert "event_spool.init_spool()" in _SRC

    def test_boot_reconcile_events_function_exists(self):
        assert "async def _boot_reconcile_plugin_events(" in _SRC
        assert "await _boot_reconcile_plugin_events(" in _SRC

    def test_boot_order_spool_init_then_reconcile_then_configure(self):
        idx_spool_init = _SRC.index("event_spool.init_spool()")
        idx_boot_reconcile_call = _SRC.index(
            "await _boot_reconcile_plugin_events(")
        idx_configure = _SRC.index("_evep.configure(")
        assert idx_spool_init < idx_boot_reconcile_call < idx_configure

    def test_recovery_runs_before_start_worker(self):
        """The load-bearing pin: event_episodes.recovery(boot=True) MUST be
        awaited before start_worker() is called — otherwise the worker's
        first pass could race the boot reconstruction of the delivery
        ledger."""
        idx_recovery = _SRC.index("await _evep.recovery(boot=True)")
        idx_start_worker = _SRC.index("_evep.start_worker()")
        assert idx_recovery < idx_start_worker

    def test_boot_reconcile_events_paired_after_callbacks(self):
        """Mirrors test_i3_boot_pairs_trigger_and_callback_reconcile: the
        events boot reconcile call site sits AFTER the callbacks one (Task
        10 is explicitly "ordered AFTER the callback block")."""
        idx_callbacks = _SRC.index("await _boot_reconcile_plugin_callbacks(")
        idx_events = _SRC.index("await _boot_reconcile_plugin_events(")
        assert idx_callbacks < idx_events

    def test_boot_reconcile_events_never_fatal(self):
        """The boot seam's docstring/body must degrade to the fail-closed
        sentinel on a compute failure, never crash boot (mirrors triggers/
        callbacks' own never-fatal boot seams)."""
        import casa_core
        import inspect
        src = inspect.getsource(casa_core._boot_reconcile_plugin_events)
        assert "except Exception" in src

    def test_worker_closures_wired_from_the_setup_seams(self):
        """dispatch/notify_operator reuse the SAME late-binding seams the
        setup-episode + callback workers use; get_spool/get_routed are bare
        references to the live module functions (never a one-shot
        snapshot)."""
        start = _SRC.index("_evep.configure(")
        end = _SRC.index(")", start)
        block = _SRC[start:end]
        assert "dispatch=_setup_dispatch" in block
        assert "notify_operator=_setup_notify" in block
        assert "resolve_registry_entry=_event_registry_entry" in block
        assert "get_routed=_evrec.get_routed" in block
        assert "get_installed=_event_installed" in block
        assert "get_registry_valid=_event_registry_valid" in block
        assert "get_spool=event_spool.get_spool" in block


# ---------------------------------------------------------------------------
# Periodic jobs — registered, off-loop, sentinel-retry liveness.
# ---------------------------------------------------------------------------


class TestPeriodicJobs:
    def test_jobs_registered(self):
        assert 'id="event_spool_sweep"' in _SRC
        assert 'id="event_spool_recovery"' in _SRC

    def test_sweep_runs_off_the_loop(self):
        """Lock-stall ruling (mirrors test_lock_stall_scheduled_scans_use_
        to_thread): the scheduled scan holds the spool's lock for a whole
        pass, so it must run via asyncio.to_thread, never inline."""
        assert "asyncio.to_thread( spool.sweep, routed, installed, valid, time.time())" \
            in _FLAT

    def test_recovery_job_uses_periodic_boot_false(self):
        idx = _SRC.index("async def _event_spool_recovery")
        end = _SRC.index("scheduler.add_job(", idx)
        block = _SRC[idx:end]
        assert "_evep.recovery(boot=False)" in block

    def test_periodic_recovery_retries_reconcile_under_sentinel(self):
        """Liveness (Task 10): the periodic job must notice a stuck
        ROUTING_UNAVAILABLE sentinel and retry the compute itself (via the
        SAME kick() the pre-send gate uses on a consent mismatch) — a
        transient compute failure otherwise waits forever for an unrelated
        lifecycle mutation to retry it."""
        import ast

        tree = ast.parse(_SRC)

        # Find the _event_spool_recovery function (nested in main)
        func_def = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and \
               node.name == "_event_spool_recovery":
                func_def = node
                break

        assert func_def is not None, \
            "Function _event_spool_recovery not found"

        # Find the If node whose test contains ROUTING_UNAVAILABLE comparison
        sentinel_if = None
        for stmt in func_def.body:
            if isinstance(stmt, ast.If):
                # Check if the test contains the ROUTING_UNAVAILABLE reference
                test_src = ast.unparse(stmt.test) \
                    if hasattr(ast, 'unparse') else ""
                if "ROUTING_UNAVAILABLE" in test_src:
                    sentinel_if = stmt
                    break

        assert sentinel_if is not None, \
            "If statement with ROUTING_UNAVAILABLE comparison not found"

        # Verify that kick() is called inside the If body
        kick_in_if = False
        for node in ast.walk(sentinel_if):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and \
                   node.func.attr == "kick":
                    kick_in_if = True
                    break

        assert kick_in_if, "kick() call not found inside the If body"

        # Verify that NO kick() call exists outside the If statement
        kick_outside_if = False
        for stmt in func_def.body:
            if stmt is not sentinel_if:
                for node in ast.walk(stmt):
                    if isinstance(node, ast.Call):
                        if isinstance(node.func, ast.Attribute) and \
                           node.func.attr == "kick":
                            kick_outside_if = True
                            break
                if kick_outside_if:
                    break

        assert not kick_outside_if, \
            "kick() call found outside the If statement " \
            "(should only be inside ROUTING_UNAVAILABLE conditional)"


# ---------------------------------------------------------------------------
# Reconcile pairing — reload scope + the 5-mutation lifecycle sequencer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestReloadScopeReconcilesEvents:
    async def test_reload_triggers_scope_reconciles_events(self, monkeypatch):
        import reload as reload_mod
        import trigger_reconcile
        import callback_reconcile
        import event_reconcile

        async def _fake_handler(runtime, *, role=None):
            return ["reloaded"]

        monkeypatch.setitem(reload_mod._HANDLERS, "triggers", _fake_handler)
        tg = AsyncMock(return_value=[])
        cb = AsyncMock(return_value=[])
        ev = AsyncMock(return_value=[])
        monkeypatch.setattr(trigger_reconcile, "reconcile_from_runtime", tg)
        monkeypatch.setattr(callback_reconcile, "reconcile_from_runtime", cb)
        monkeypatch.setattr(event_reconcile, "reconcile_plugin_events", ev)

        runtime = SimpleNamespace(trigger_registry=object())
        res = await reload_mod.dispatch("triggers", runtime=runtime, role="gmail")
        assert res["status"] == "ok"
        ev.assert_awaited_once()
        assert ev.await_args.args[0] is runtime
        assert "plugin_events_reconciled" in res["actions"]

    async def test_reload_event_reconcile_failure_is_non_fatal(self, monkeypatch):
        import reload as reload_mod
        import trigger_reconcile
        import callback_reconcile
        import event_reconcile

        async def _fake_handler(runtime, *, role=None):
            return []

        monkeypatch.setitem(reload_mod._HANDLERS, "agent", _fake_handler)
        monkeypatch.setattr(trigger_reconcile, "reconcile_from_runtime",
                            AsyncMock(return_value=[]))
        monkeypatch.setattr(callback_reconcile, "reconcile_from_runtime",
                            AsyncMock(return_value=[]))

        async def _boom(runtime, **kw):
            raise RuntimeError("event reconcile blew up")

        monkeypatch.setattr(event_reconcile, "reconcile_plugin_events", _boom)
        runtime = SimpleNamespace(trigger_registry=object())
        res = await reload_mod.dispatch("agent", runtime=runtime, role="gmail")
        assert res["status"] == "ok"
        assert "plugin_events_reconciled" not in res["actions"]


@pytest.mark.asyncio
class TestLifecycleSequencerReconcilesEvents:
    async def test_mutation_sequencer_reconciles_events(self, monkeypatch):
        """tools._reload_and_verify_targets — the choke point all 5
        lifecycle mutations funnel through — reconciles events alongside
        triggers/callbacks, passing the SAME runtime."""
        import agent as agent_mod
        import plugin_registry
        import tools as tools_mod
        import event_reconcile

        monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda: None)
        monkeypatch.setattr(plugin_registry, "snapshot_generation", lambda: 1)
        runtime = SimpleNamespace(trigger_registry=object())
        monkeypatch.setattr(agent_mod, "active_runtime", runtime)
        monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                            lambda plugin_name: {"ready": True, "targets": []})
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                            lambda extra: None)

        async def _fake_notify():
            return None

        monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible",
                            _fake_notify)

        import trigger_reconcile
        import callback_reconcile
        monkeypatch.setattr(trigger_reconcile, "reconcile_from_runtime",
                            AsyncMock(return_value=[]))
        monkeypatch.setattr(callback_reconcile, "reconcile_from_runtime",
                            AsyncMock(return_value=[]))
        ev = AsyncMock(return_value=[])
        monkeypatch.setattr(event_reconcile, "reconcile_plugin_events", ev)

        result = await tools_mod._reload_and_verify_targets(
            "p", [], expect="present")
        assert result["ok"] is True
        ev.assert_awaited_once()
        assert ev.await_args.args[0] is runtime

    async def test_mutation_sequencer_survives_event_reconcile_failure(
            self, monkeypatch):
        import agent as agent_mod
        import plugin_registry
        import tools as tools_mod
        import event_reconcile

        monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda: None)
        monkeypatch.setattr(plugin_registry, "snapshot_generation", lambda: 1)
        monkeypatch.setattr(agent_mod, "active_runtime", None)
        monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                            lambda plugin_name: {"ready": True, "targets": []})
        monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                            lambda extra: None)

        async def _fake_notify():
            return None

        monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible",
                            _fake_notify)

        async def _boom(runtime, **kw):
            raise RuntimeError("event reconcile blew up")

        monkeypatch.setattr(event_reconcile, "reconcile_plugin_events", _boom)
        result = await tools_mod._reload_and_verify_targets(
            "p", [], expect="present")
        assert result["ok"] is True  # never fails the mutation


# ---------------------------------------------------------------------------
# #1657 — leaving an artifact drops the subscriber's event consents, so coming
# back to it (a reinstall, or an update back to an earlier revision) asks the
# operator again, exactly as the plugin's trigger and callback consents do.
# The reconcile's own prune cannot be relied on for this: any resolver issue
# anywhere in the registry makes the pass non-prunable.
# ---------------------------------------------------------------------------


def _subscriber_world(monkeypatch, tmp_path):
    """A real ack store and the real reconciler over an injected registry: an
    emitter `gmail`, a subscriber `finance`, and a standing registry-stage
    issue on an unrelated plugin (so no reconcile pass is prunable)."""
    import event_acks
    import event_reconcile
    import plugin_registry
    import plugin_setup_episodes
    import tools as tools_mod
    from plugin_events import subscribe_declaration_digest

    def plugin(name, revision, emits=(), subs=()):
        casa = {}
        if emits:
            casa["emits"] = [{"name": n} for n in emits]
        if subs:
            casa["subscribes"] = [{"plugin": e, "event": ev} for e, ev in subs]
        aid = plugin_registry.compute_artifact_id(
            repo="acme/" + name, revision=revision, subdir="", name=name)
        return SimpleNamespace(name=name, artifact_id=aid, path="/x",
                               version=revision, manifest_name=name,
                               manifest={"name": name, "casa": casa})

    gmail = plugin("gmail", "v1", emits=["mail_arrived"])
    fin_v1 = plugin("finance", "v1", subs=[("gmail", "mail_arrived")])
    fin_v2 = plugin("finance", "v2", subs=[("gmail", "mail_arrived")])
    standing = plugin_registry.PluginIssue(
        name="weather", target="executor:browser", stage="registry",
        reason_code=plugin_registry.IGNORED_EXECUTOR_TARGET, artifact_id=None)
    state = {"plugins": [gmail, fin_v1]}

    def resolver(target=None):
        return SimpleNamespace(registry_valid=True,
                               plugins=list(state["plugins"]),
                               issues=[standing])

    def entries():
        return [{"name": p.name, "artifact_id": p.artifact_id,
                 "targets": ["resident:assistant"]} for p in state["plugins"]]

    store = event_acks.EventAckStore(tmp_path / "event_acks.json")
    monkeypatch.setattr(event_acks, "ACKS", store)
    monkeypatch.setattr(event_reconcile, "_routed", {})
    monkeypatch.setattr(event_reconcile, "_kick_worker", lambda: None)
    runtime = SimpleNamespace(
        role_configs={"assistant": SimpleNamespace(channels=["telegram"])},
        channel_manager=None)

    async def reconcile():
        await event_reconcile.reconcile_plugin_events(
            runtime, acks=store, resolver=resolver, entries=entries,
            prompt=False)

    def desired():
        return event_reconcile.compute_desired(
            role_configs=runtime.role_configs, acks=store,
            resolver=resolver, entries=entries)

    # The lifecycle sequencer, reduced to the event reconcile it runs.
    async def sequencer(name, targets, expect):
        await reconcile()
        return {}

    monkeypatch.setattr(tools_mod, "_reload_and_verify_targets", sequencer)
    monkeypatch.setattr(tools_mod, "_invalidate_lifecycle", lambda **kw: None)
    monkeypatch.setattr(plugin_setup_episodes, "retire_for_removed",
                        lambda plugin: None)

    async def no_callbacks(name):
        return None

    monkeypatch.setattr(tools_mod, "_remove_plugin_callbacks", no_callbacks)
    store.record("finance", fin_v1.artifact_id, "gmail", "mail_arrived",
                 subscribe_declaration_digest(
                     {"plugin": "gmail", "event": "mail_arrived"}),
                 ["resident:assistant"], 1.0)
    return SimpleNamespace(tools=tools_mod, er=event_reconcile, state=state,
                           gmail=gmail, fin_v1=fin_v1, fin_v2=fin_v2,
                           reconcile=reconcile, desired=desired)


def _finance_routed(er) -> bool:
    routed = er.get_routed()
    return "finance" in routed.get(("gmail", "mail_arrived"), {})


@pytest.mark.asyncio
class TestLeavingAnArtifactRevokesItsEventConsent:
    async def test_a_reinstall_after_removal_asks_again(
            self, monkeypatch, tmp_path):
        w = _subscriber_world(monkeypatch, tmp_path)
        await w.reconcile()
        assert _finance_routed(w.er)               # approved and delivering
        assert w.desired().prunable is False       # the prune never runs here

        def removed(name):
            w.state["plugins"] = [w.gmail]
            return {"ok": True, "name": name,
                    "artifact_id": w.fin_v1.artifact_id,
                    "targets": ["resident:assistant"]}

        monkeypatch.setattr(w.tools, "_plugin_remove_sync", removed)
        res = await w.tools.plugin_remove.handler({"name": "finance"})
        assert json.loads(res["content"][0]["text"])["ok"] is True

        w.state["plugins"] = [w.gmail, w.fin_v1]   # the same revision again
        await w.reconcile()
        assert not _finance_routed(w.er)
        pending = w.desired().consent_needed
        assert [(p["subscriber"], p["artifact_id"]) for p in pending] == [
            ("finance", w.fin_v1.artifact_id)]

    async def test_an_update_back_to_an_earlier_revision_asks_again(
            self, monkeypatch, tmp_path):
        w = _subscriber_world(monkeypatch, tmp_path)
        await w.reconcile()
        assert _finance_routed(w.er)

        def updated_to(target, old):
            def core(name, new_ref, expected_revision=None):
                w.state["plugins"] = [w.gmail, target]
                return {"ok": True, "name": name,
                        "targets": ["resident:assistant"],
                        "artifact_id": target.artifact_id,
                        "old_artifact_id": old.artifact_id}
            return core

        monkeypatch.setattr(w.tools, "_resolved_observability",
                            lambda name, manifest=None: {})
        monkeypatch.setattr(w.tools, "_secret_candidates",
                            lambda name, env, manifest=None: {})
        for target, old in ((w.fin_v2, w.fin_v1), (w.fin_v1, w.fin_v2)):
            monkeypatch.setattr(w.tools, "_plugin_update_sync",
                                updated_to(target, old))
            res = await w.tools.plugin_update.handler(
                {"name": "finance", "new_ref": target.version})
            assert json.loads(res["content"][0]["text"])["ok"] is True

        assert not _finance_routed(w.er)
        pending = w.desired().consent_needed
        assert [(p["subscriber"], p["artifact_id"]) for p in pending] == [
            ("finance", w.fin_v1.artifact_id)]


# #1671: the same for a plugin a specialist bundle owns. The bundle tools
# (uninstall, install, upgrade, rollback) change owned plugins through their
# own transaction and sequencer, not plugin_remove/plugin_update, and run the
# REAL handlers and sequencer here. Stubbed: the library transactions (an
# in-memory registry swap), the reload (reduced to the event reconcile it
# runs) and the verify/health/own-chat side effects.

_SLUG = "mtg"
_OWNED = f"{_SLUG}.sub"


def _bundle_world(monkeypatch, tmp_path, events=("mail_arrived",)):
    """The owned plugin subscribes to each of gmail's *events*."""
    import asyncio as _asyncio

    import agent as agent_mod
    import approval_records
    import event_acks
    import event_reconcile
    import plugin_registry
    import reload as reload_mod
    import specialist_bundle_journal
    import specialist_install
    import tools as tools_mod
    import trigger_acks
    from plugin_events import subscribe_declaration_digest

    monkeypatch.setattr(approval_records, "PATH",
                        str(tmp_path / "approval-records.json"))
    monkeypatch.setattr(approval_records, "_RECORDS", None)  # reread PATH
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", _asyncio.Lock())

    def plugin(name, revision, emits=(), subs=(), owned=False):
        casa = {}
        if emits:
            casa["emits"] = [{"name": n} for n in emits]
        if subs:
            casa["subscribes"] = [{"plugin": e, "event": ev} for e, ev in subs]
        aid = plugin_registry.compute_artifact_id(
            repo="acme/" + name, revision=revision, subdir="", name=name)
        return SimpleNamespace(
            name=name, artifact_id=aid, path="/x", version=revision,
            manifest_name=name.split(".")[-1],
            manifest={"name": name, "casa": casa}, owned=owned,
            targets=[f"specialist:{_SLUG}"] if owned else ["resident:assistant"])

    subs = [("gmail", ev) for ev in events]
    gmail = plugin("gmail", "v1", emits=list(events))
    sub_v1 = plugin(_OWNED, "v1", subs=subs, owned=True)
    sub_v2 = plugin(_OWNED, "v2", subs=subs, owned=True)
    standing = plugin_registry.PluginIssue(
        name="weather", target="executor:browser", stage="registry",
        reason_code=plugin_registry.IGNORED_EXECUTOR_TARGET, artifact_id=None)
    state = {"plugins": [gmail, sub_v1]}

    def resolver(target=None):
        return SimpleNamespace(registry_valid=True,
                               plugins=list(state["plugins"]),
                               issues=[standing])

    def entries():
        return [{"name": p.name, "artifact_id": p.artifact_id,
                 "targets": list(p.targets),
                 **({"owner": f"specialist:{_SLUG}"} if p.owned else {})}
                for p in state["plugins"]]

    store = event_acks.EventAckStore(tmp_path / "event_acks.json")
    monkeypatch.setattr(event_acks, "ACKS", store)
    monkeypatch.setattr(event_reconcile, "_routed", {})
    monkeypatch.setattr(event_reconcile, "_kick_worker", lambda: None)
    monkeypatch.setattr(trigger_acks, "ACKS",
                        trigger_acks.TriggerAckStore(tmp_path / "trig.json"))
    runtime = SimpleNamespace(
        role_configs={"assistant": SimpleNamespace(channels=["telegram"])},
        channel_manager=None, agents={}, agents_dir=None)
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)

    # The store is read off the module at call time: a restart replaces it.
    async def reconcile():
        await event_reconcile.reconcile_plugin_events(
            runtime, acks=event_acks.ACKS, resolver=resolver, entries=entries,
            prompt=False)

    def desired():
        return event_reconcile.compute_desired(
            role_configs=runtime.role_configs, acks=event_acks.ACKS,
            resolver=resolver, entries=entries)

    def restart():
        """Casa restarts: the consent store is read again from its file and
        the in-process denial registry is empty."""
        import consent_denials
        monkeypatch.setattr(event_acks, "ACKS", event_acks.EventAckStore(
            tmp_path / "event_acks.json"))
        monkeypatch.setattr(consent_denials, "_denied", set())

    async def turn_off(event):
        """The operator's ``event_ack_revoke`` for one subscription."""
        await event_reconcile.revoke_and_unroute(
            _OWNED, "gmail", event, operator_denied=True, resolver=resolver,
            entries=entries)

    # The agent/agents reload scopes run the event reconcile.
    async def dispatch(scope, *, runtime, role=None):
        await reconcile()
        return {"status": "ok"}

    async def nothing(*a, **k):
        return None

    async def no_engagements(slug, *, reason, acknowledged=None):
        return []

    monkeypatch.setattr(reload_mod, "dispatch", dispatch)
    monkeypatch.setattr(plugin_registry, "reload_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: SimpleNamespace(valid=True))
    monkeypatch.setattr(
        plugin_registry, "owned_entries_for",
        lambda slug, reg: [e for e in entries()
                           if e.get("owner") == f"specialist:{slug}"])
    monkeypatch.setattr(
        plugin_registry, "resolve_for",
        lambda t: SimpleNamespace(plugins=[p for p in state["plugins"]
                                           if t in p.targets]))
    monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                        lambda plugin_name: {"ready": True, "reasons": [],
                                             "targets": []})
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda issues: None)
    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", nothing)
    monkeypatch.setattr(tools_mod, "_record_plugin_installer", lambda names: None)
    monkeypatch.setattr(tools_mod, "_own_chat_hook", nothing)
    monkeypatch.setattr(tools_mod, "_own_chat_disclosure", lambda role: None)
    monkeypatch.setattr(tools_mod, "_bound_persona", lambda slug: None)
    monkeypatch.setattr(tools_mod, "_open_specialist_engagements",
                        lambda slug: [])
    monkeypatch.setattr(tools_mod, "close_specialist_engagements",
                        no_engagements)
    monkeypatch.setattr(tools_mod, "_prune_bundle_receipt", lambda rid: None)
    monkeypatch.setattr(specialist_install, "reclaim_staging_tree",
                        lambda p: None)
    monkeypatch.setattr(specialist_bundle_journal, "complete", lambda p: None)

    # The library transactions, reduced to the owned-plugin swap they commit.
    def swap_to(target):
        before = [p for p in state["plugins"] if p.owned]
        state["plugins"] = [gmail] + ([target] if target else [])
        removed = tuple(p.artifact_id for p in before
                        if target is None or p.artifact_id != target.artifact_id)
        return SimpleNamespace(
            slug=_SLUG, removed_artifact_ids=removed, new_artifact_ids=(),
            removed_owned_names=(), journal_path=str(tmp_path / "j.json"),
            before_entries=[{"name": p.name, "artifact_id": p.artifact_id}
                            for p in before])

    def activated(target):
        return (SimpleNamespace(slug=_SLUG, state="active",
                                last_activation_error=None), swap_to(target))

    monkeypatch.setattr(specialist_install, "uninstall_specialist",
                        lambda **k: swap_to(None))
    monkeypatch.setattr(specialist_install, "commit_specialist_install",
                        lambda *a, **k: activated(sub_v1))
    monkeypatch.setattr(specialist_install, "upgrade_specialist",
                        lambda **k: activated(sub_v2))
    monkeypatch.setattr(specialist_install, "rollback_specialist",
                        lambda **k: activated(sub_v1))

    # The operator approved the owned plugin's subscription on v1.
    store.record(_OWNED, sub_v1.artifact_id, "gmail", "mail_arrived",
                 subscribe_declaration_digest(
                     {"plugin": "gmail", "event": "mail_arrived"}),
                 [f"specialist:{_SLUG}"], 1.0)
    return SimpleNamespace(tools=tools_mod, er=event_reconcile, sub_v1=sub_v1,
                           reconcile=reconcile, desired=desired,
                           restart=restart, turn_off=turn_off)


def _staged_bundle(monkeypatch, tmp_path) -> dict:
    """The tool arguments for a staged component of the bundle's slug."""
    import specialist_receipt
    from specialist_component import load_specialist_component
    from specialist_install import (
        compute_install_root_digest, resolve_dependency_closure)
    from test_specialist_install import _write_component
    staged = _write_component(tmp_path / ".staging" / "staged", slug=_SLUG)
    component = load_specialist_component(staged, staged / "manifest.json")
    digest = compute_install_root_digest(
        component, resolve_dependency_closure(component, staged),
        manifest_bytes=(staged / "manifest.json").read_bytes())
    monkeypatch.setattr(specialist_receipt, "load", lambda rid, *a, **k: (
        SimpleNamespace(receipt_id=rid, receipt_digest="", slug=_SLUG,
                        plugins=())))
    return {"component_id": component.component_id,
            "version": component.version, "slug": _SLUG,
            "staged_dir": str(staged), "root_digest": digest,
            "receipt_id": "7" * 32}


def _owned_routed(er) -> bool:
    return _OWNED in er.get_routed().get(("gmail", "mail_arrived"), {})


@pytest.mark.asyncio
class TestLeavingASpecialistBundleRevokesItsEventConsent:
    async def test_a_reinstall_after_uninstall_asks_again(
            self, monkeypatch, tmp_path):
        from test_tools_specialist_install import _approved_call, _payload
        args = _staged_bundle(monkeypatch, tmp_path)
        w = _bundle_world(monkeypatch, tmp_path)
        await w.reconcile()
        assert _owned_routed(w.er)                 # approved and delivering
        assert w.desired().prunable is False       # the prune never runs here

        res = _payload(await w.tools.specialist_uninstall.handler(
            {"slug": _SLUG}))
        assert res["ok"] is True, res
        res = _payload(await _approved_call(
            w.tools.specialist_install_commit, args))  # the same revision
        assert res["ok"] is True, res

        assert not _owned_routed(w.er)
        pending = w.desired().consent_needed
        assert [(p["subscriber"], p["artifact_id"]) for p in pending] == [
            (_OWNED, w.sub_v1.artifact_id)]

    # #1666: a subscription the operator turned off is remembered in the same
    # store, and leaves with the artifact the same way — also when the
    # subscriber holds nothing but denials there.
    @pytest.mark.parametrize("turned_off", [
        ["mail_read"], ["mail_read", "mail_arrived"]])
    async def test_a_reinstall_after_uninstall_asks_again_for_one_turned_off(
            self, monkeypatch, tmp_path, turned_off):
        from test_tools_specialist_install import _approved_call, _payload
        args = _staged_bundle(monkeypatch, tmp_path)
        w = _bundle_world(monkeypatch, tmp_path,
                          events=("mail_arrived", "mail_read"))
        for event in turned_off:
            await w.turn_off(event)
        # The answer is on file and in effect after a restart — otherwise
        # "asks again" below could not tell a denial the uninstall dropped
        # from one that was never kept. Off by the operator's answer:
        # nothing waits for approval, and what is still approved delivers.
        w.restart()
        await w.reconcile()
        assert w.desired().issues == []
        assert _owned_routed(w.er) is ("mail_arrived" not in turned_off)
        assert w.desired().prunable is False       # the prune never runs here

        res = _payload(await w.tools.specialist_uninstall.handler(
            {"slug": _SLUG}))
        assert res["ok"] is True, res
        w.restart()
        res = _payload(await _approved_call(
            w.tools.specialist_install_commit, args))  # the same revision
        assert res["ok"] is True, res

        # both are unanswered again: neither approved nor turned off
        assert w.er.get_routed() == {}
        assert [(i["name"], i["reason_code"]) for i in w.desired().issues] == [
            (_OWNED, "event_pending_ack"), (_OWNED, "event_pending_ack")]

    async def test_a_rollback_after_upgrade_asks_again(
            self, monkeypatch, tmp_path):
        from test_tools_specialist_install import _approved_call, _payload
        args = _staged_bundle(monkeypatch, tmp_path)
        w = _bundle_world(monkeypatch, tmp_path)
        await w.reconcile()
        assert _owned_routed(w.er)

        res = _payload(await _approved_call(w.tools.specialist_upgrade, args))
        assert res["ok"] is True, res
        res = _payload(await w.tools.specialist_rollback.handler(
            {"slug": _SLUG}))
        assert res["ok"] is True, res

        assert not _owned_routed(w.er)
        pending = w.desired().consent_needed
        assert [(p["subscriber"], p["artifact_id"]) for p in pending] == [
            (_OWNED, w.sub_v1.artifact_id)]


# ---------------------------------------------------------------------------
# Health merge — event_reconcile.current_issues() folds in as dicts.
# ---------------------------------------------------------------------------


def _issue(name, reason_code):
    return {"name": name, "target": None, "stage": "events",
            "reason_code": reason_code, "artifact_id": "art-1"}


class TestHealthMergeIncludesEventIssues:
    def test_event_issues_land_in_the_health_report(self, tmp_path, monkeypatch):
        import tools
        import event_reconcile
        import trigger_reconcile
        import callback_reconcile
        import plugin_registry

        monkeypatch.setattr(tools, "_PLUGIN_HEALTH_PATH", tmp_path / "health.json")
        monkeypatch.setattr(trigger_reconcile, "current_issues", lambda: [])
        monkeypatch.setattr(callback_reconcile, "current_issues", lambda: [])
        monkeypatch.setattr(
            event_reconcile, "current_issues",
            lambda: [_issue("finance", "event_pending_ack")])
        monkeypatch.setattr(
            plugin_registry, "resolve_all",
            lambda: SimpleNamespace(issues=[], warnings=[]))
        monkeypatch.setattr(
            plugin_registry, "load_registry",
            lambda *a, **k: SimpleNamespace(valid=False, entries=[]))

        written = {}
        import plugin_health
        monkeypatch.setattr(
            plugin_health, "write_report", lambda **kw: written.update(kw))

        tools._regenerate_plugin_health([])
        codes = {i.get("reason_code") if isinstance(i, dict)
                 else getattr(i, "reason_code", None)
                 for i in written["issues"]}
        assert "event_pending_ack" in codes

    def test_event_issues_are_dicts_never_plugin_issue_instances(self):
        """Minor-10's own contract: current_issues() rows are concatenated
        directly, never routed through the PluginIssue-attribute-only
        _add()/_rediscoverable() helpers."""
        import tools
        import inspect
        src = inspect.getsource(tools._regenerate_plugin_health)
        assert "event_reconcile.current_issues()" in src
        assert "list(event_issues)" in src

    def test_default_event_routing_is_the_production_sentinel(self):
        """Minor-2: the conftest-level _reset_event_routing fixture resets
        to the ACTUAL production default/fail-back value —
        ROUTING_UNAVAILABLE — not an authoritative empty map. An
        authoritative {} by default across the WHOLE suite would be a
        global accidental-authoritativeness leak: any unrelated test that
        happens to touch event-worker code without realizing it would see
        "routing is known and empty" instead of "routing is unknown",
        which could mask a real must-treat-as-unavailable bug anywhere.
        current_issues() surfaces exactly the one row the sentinel
        licenses — event_routing_unavailable — never a silent []."""
        import event_reconcile
        import event_spool
        assert event_reconcile.get_routed() is event_spool.ROUTING_UNAVAILABLE
        codes = {i["reason_code"] for i in event_reconcile.current_issues()}
        assert "event_routing_unavailable" in codes

    def test_event_routing_ok_fixture_opts_into_an_authoritative_empty_map(
            self, event_routing_ok):
        """The opt-in counterpart: a test that NEEDS an authoritative empty
        routing map (the 4 health-report tests this regression was first
        found in — test_verify_plugin_state.py x3,
        test_plugin_triggers_reconcile.py x1 — whose EXACT issues-list
        assertions choke on the sentinel's own event_routing_unavailable
        row) requests the `event_routing_ok` fixture explicitly, naming the
        need rather than inheriting it silently from a global default."""
        import event_reconcile
        import event_spool
        assert event_reconcile.get_routed() == {}
        assert event_reconcile.get_routed() is not event_spool.ROUTING_UNAVAILABLE
        assert event_reconcile.current_issues() == []


# ---------------------------------------------------------------------------
# Fail-closed registry — get_installed/get_registry_valid.
# ---------------------------------------------------------------------------


class TestFailClosedRegistry:
    def test_registry_valid_true_reports_membership(self, monkeypatch):
        import casa_core
        import plugin_registry
        monkeypatch.setattr(
            plugin_registry, "snapshot_registry",
            lambda: SimpleNamespace(
                valid=True, entries=[{"name": "gmail"}, {"name": "finance"}]))
        assert casa_core._event_registry_valid() is True
        assert casa_core._event_installed() == {"gmail", "finance"}

    def test_invalid_registry_never_fabricates_installed_set(self, monkeypatch):
        """The core fail-closed contract: an invalid snapshot must never
        look like "nothing is installed" in a way that would license the
        worker's sweep to vaporize every subscriber's spool state — it must
        report NO membership at all, gated by get_registry_valid()==False."""
        import casa_core
        import plugin_registry
        monkeypatch.setattr(
            plugin_registry, "snapshot_registry",
            lambda: SimpleNamespace(
                valid=False, entries=[{"name": "ghost"}]))
        assert casa_core._event_registry_valid() is False
        assert casa_core._event_installed() == set()

    def test_worker_pass_does_no_destructive_work_under_invalid_registry(
            self, monkeypatch, tmp_path):
        """Integration-level: wiring event_episodes with THESE fail-closed
        closures against a real spool with an orphaned emitter dir must
        never GC it while the registry is invalid — the destructive
        decision itself is event_spool's own tested contract; this proves
        casa_core's closures feed it the fail-closed inputs."""
        import asyncio
        import event_episodes
        import event_spool
        import event_reconcile
        import casa_core
        import plugin_registry

        monkeypatch.setattr(
            plugin_registry, "snapshot_registry",
            lambda: SimpleNamespace(valid=False, entries=[]))
        spool = event_spool.EventSpool(tmp_path / "events")
        try:
            emitter_dir = spool.root / "ghost"
            emitter_dir.mkdir(parents=True, exist_ok=True)

            monkeypatch.setattr(event_episodes, "_dispatch", None)
            monkeypatch.setattr(event_episodes, "_resolve_registry_entry", None)
            monkeypatch.setattr(event_episodes, "_get_routed",
                                lambda: event_spool.ROUTING_UNAVAILABLE)
            monkeypatch.setattr(event_episodes, "_get_installed",
                                casa_core._event_installed)
            monkeypatch.setattr(event_episodes, "_get_registry_valid",
                                casa_core._event_registry_valid)
            monkeypatch.setattr(event_episodes, "_get_acks", None)
            monkeypatch.setattr(event_episodes, "_get_spool", lambda: spool)
            monkeypatch.setattr(event_episodes, "_notify_operator", None)

            asyncio.run(event_episodes.recovery(boot=True))
            assert emitter_dir.is_dir(), \
                "an invalid registry must leave spool dirs untouched"
        finally:
            spool.close()


# ---------------------------------------------------------------------------
# Emitter spool provisioning (Critical-1) — get_emitters wiring + the
# worker/recovery-driven ensure_emitter_dirs/write_ready calls.
# ---------------------------------------------------------------------------


class TestEmitterProvisioning:
    def test_event_emitters_reports_installed_declaring_plugins(self, monkeypatch):
        import casa_core
        import plugin_registry
        finance = SimpleNamespace(
            name="finance", artifact_id="a1", path="/store/finance/a1",
            version="1.0.0", manifest_name="finance",
            manifest={"name": "finance",
                      "casa": {"emits": [{"name": "invoice-created"}]}})
        reporting = SimpleNamespace(
            name="reporting", artifact_id="a2", path="/store/reporting/a2",
            version="1.0.0", manifest_name="reporting",
            manifest={"name": "reporting", "casa": {}})
        monkeypatch.setattr(
            plugin_registry, "resolve_all",
            lambda: SimpleNamespace(registry_valid=True,
                                    plugins=[finance, reporting], issues=[]))
        assert casa_core._event_emitters() == {"finance"}

    def test_event_emitters_fails_closed_on_invalid_registry(self, monkeypatch):
        import casa_core
        import plugin_registry
        monkeypatch.setattr(
            plugin_registry, "resolve_all",
            lambda: SimpleNamespace(registry_valid=False, plugins=[], issues=[]))
        assert casa_core._event_emitters() == set()

    def test_worker_configure_wires_get_emitters(self):
        start = _SRC.index("_evep.configure(")
        end = _SRC.index(")", start)
        block = _SRC[start:end]
        assert "get_emitters=_event_emitters" in block

    def test_recovery_provisions_a_fresh_emitter_via_production_wiring(
            self, monkeypatch, tmp_path):
        """C1 pin: casa creates NOTHING for an emitter's spool until the
        worker's own recovery/pass provisions it — driven through the SAME
        casa_core closure main() wires (``_event_emitters``), never a
        harness-side ``ensure_emitter_dirs()`` call. Proves a freshly-
        declared emitter can ``emit()`` successfully after ONE recovery
        pass."""
        import asyncio
        import event_episodes
        import event_spool
        import casa_core
        import plugin_registry

        manifest = {"name": "finance",
                    "casa": {"emits": [{"name": "invoice-created"}]}}
        rp = SimpleNamespace(
            name="finance", artifact_id="a1", path="/store/finance/a1",
            version="1.0.0", manifest_name="finance", manifest=manifest)
        monkeypatch.setattr(
            plugin_registry, "resolve_all",
            lambda: SimpleNamespace(registry_valid=True, plugins=[rp], issues=[]))
        monkeypatch.setattr(
            plugin_registry, "snapshot_registry",
            lambda: SimpleNamespace(valid=True, entries=[{"name": "finance"}]))

        spool = event_spool.EventSpool(tmp_path / "events")
        try:
            monkeypatch.setattr(event_episodes, "_dispatch", None)
            monkeypatch.setattr(event_episodes, "_resolve_registry_entry", None)
            monkeypatch.setattr(event_episodes, "_get_routed",
                                lambda: event_spool.ROUTING_UNAVAILABLE)
            monkeypatch.setattr(event_episodes, "_get_installed",
                                casa_core._event_installed)
            monkeypatch.setattr(event_episodes, "_get_registry_valid",
                                casa_core._event_registry_valid)
            monkeypatch.setattr(event_episodes, "_get_emitters",
                                casa_core._event_emitters)
            monkeypatch.setattr(event_episodes, "_get_acks", None)
            monkeypatch.setattr(event_episodes, "_get_spool", lambda: spool)
            monkeypatch.setattr(event_episodes, "_notify_operator", None)

            # Before any pass: nothing has provisioned this emitter's spool
            # tree yet — a real emit() raises exactly as a freshly-installed
            # plugin's would in production today (the bug this pin closes).
            with pytest.raises(FileNotFoundError):
                event_spool.emit(spool.root / "finance", "invoice-created")

            asyncio.run(event_episodes.recovery(boot=True))

            # No harness-side ensure_emitter_dirs() call anywhere above —
            # production's own recovery pass provisioned the dirs.
            path = event_spool.emit(spool.root / "finance", "invoice-created")
            assert path.exists()
            assert spool.read_marker("finance").state == \
                event_spool.MarkerState.PRESENT
        finally:
            spool.close()

    def test_worker_pass_provisions_a_fresh_emitter_via_production_wiring(
            self, monkeypatch, tmp_path):
        """Same production-wiring pin as above, but through _worker_pass()
        (the top-of-pass provisioning, not just recovery())."""
        import asyncio
        import event_episodes
        import event_spool
        import casa_core
        import plugin_registry

        manifest = {"name": "finance",
                    "casa": {"emits": [{"name": "invoice-created"}]}}
        rp = SimpleNamespace(
            name="finance", artifact_id="a1", path="/store/finance/a1",
            version="1.0.0", manifest_name="finance", manifest=manifest)
        monkeypatch.setattr(
            plugin_registry, "resolve_all",
            lambda: SimpleNamespace(registry_valid=True, plugins=[rp], issues=[]))
        monkeypatch.setattr(
            plugin_registry, "snapshot_registry",
            lambda: SimpleNamespace(valid=True, entries=[{"name": "finance"}]))

        spool = event_spool.EventSpool(tmp_path / "events")
        try:
            monkeypatch.setattr(event_episodes, "_dispatch", None)
            monkeypatch.setattr(event_episodes, "_resolve_registry_entry", None)
            monkeypatch.setattr(event_episodes, "_get_routed",
                                lambda: event_spool.ROUTING_UNAVAILABLE)
            monkeypatch.setattr(event_episodes, "_get_installed",
                                casa_core._event_installed)
            monkeypatch.setattr(event_episodes, "_get_registry_valid",
                                casa_core._event_registry_valid)
            monkeypatch.setattr(event_episodes, "_get_emitters",
                                casa_core._event_emitters)
            monkeypatch.setattr(event_episodes, "_get_acks", None)
            monkeypatch.setattr(event_episodes, "_get_spool", lambda: spool)
            monkeypatch.setattr(event_episodes, "_notify_operator", None)

            with pytest.raises(FileNotFoundError):
                event_spool.emit(spool.root / "finance", "invoice-created")

            asyncio.run(event_episodes._worker_pass())

            path = event_spool.emit(spool.root / "finance", "invoice-created")
            assert path.exists()
        finally:
            spool.close()


# ---------------------------------------------------------------------------
# ack_event tool exposure (Important-1) — every wake-dispatchable resident
# role's tools.allowed must carry mcp__casa-framework__ack_event, or a
# headless nudge to that role can never close its own delivery loop.
# ---------------------------------------------------------------------------

_RESIDENT_RUNTIME_PATHS = {
    "assistant": CASA / "defaults" / "agents" / "assistant" / "runtime.yaml",
    "butler": CASA / "defaults" / "agents" / "butler" / "runtime.yaml",
    "concierge": CASA / "defaults" / "agents" / "concierge" / "runtime.yaml",
}


class TestAckEventToolParity:
    @pytest.mark.parametrize("role", sorted(_RESIDENT_RUNTIME_PATHS))
    def test_resident_runtime_carries_ack_event(self, role):
        """Any resident role name is a valid `resident:<role>` delivery
        target (plugin_dispatch.compose, callback_reconcile._reachable) —
        an operator may assign ANY of them, not just assistant. Without
        ack_event granted, that role's headless wake can never call it, and
        the delivery just burns its nudge budget to exhaustion."""
        path = _RESIDENT_RUNTIME_PATHS[role]
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        allowed = data["tools"]["allowed"]
        assert "mcp__casa-framework__ack_event" in allowed, (
            f"{role}.tools.allowed missing ack_event; got {allowed}")

    def test_every_defaults_resident_runtime_is_covered_by_this_parity_check(self):
        """Guards the parity check itself against a FOURTH resident role
        being added later without updating _RESIDENT_RUNTIME_PATHS above —
        walks defaults/agents/*/runtime.yaml directly rather than trusting
        the hardcoded dict alone."""
        agents_dir = CASA / "defaults" / "agents"
        found = {
            p.parent.name: p
            for p in agents_dir.glob("*/runtime.yaml")
            if yaml.safe_load(p.read_text(encoding="utf-8")).get("kind")
            == "resident"
        }
        assert found.keys() == _RESIDENT_RUNTIME_PATHS.keys()


# ---------------------------------------------------------------------------
# event_wake is casa-internal only.
# ---------------------------------------------------------------------------


class TestEventWakeMarkerUnreachable:
    def test_synthetic_key_is_reserved(self):
        from provenance import RESERVED_CONTEXT_KEYS
        assert "synthetic" in RESERVED_CONTEXT_KEYS

    def test_event_wake_value_is_stripped_from_external_context(self):
        """An external caller (webhook payload, /invoke body) cannot forge
        the event_wake marker a casa-composed nudge carries — sanitize_
        external_context strips the WHOLE synthetic key regardless of the
        value an attacker sets it to."""
        from provenance import sanitize_external_context
        forged = {"synthetic": "event_wake", "emitter": "gmail",
                  "event": "new_mail", "other": "kept"}
        out = sanitize_external_context(forged)
        assert "synthetic" not in out
        # only the RESERVED key is stripped — everything else passes
        # through untouched (proves this is a targeted strip, not a wipe).
        assert out == {"emitter": "gmail", "event": "new_mail",
                       "other": "kept"}

    def test_wake_context_shape_matches_the_reserved_marker(self):
        """event_episodes._wake_context produces exactly the marker
        provenance.py reserves — proving the two modules agree on the
        literal value, not just that SOME key is reserved."""
        import event_episodes
        ctx = event_episodes._wake_context("gmail", "new_mail")
        assert ctx["synthetic"] == "event_wake"
        from provenance import RESERVED_CONTEXT_KEYS
        assert set(ctx) <= RESERVED_CONTEXT_KEYS | {"emitter", "event"}
        assert "synthetic" in RESERVED_CONTEXT_KEYS


# ---------------------------------------------------------------------------
# ask_user provenance rejection on an event-dispatched turn.
# ---------------------------------------------------------------------------


def _set_origin(agent_mod, **overrides):
    origin = {
        "role": "assistant",
        "channel": "telegram",
        "chat_id": "500",
        "user_id": 999,
        "message_type": "channel_in",
        "source": "telegram",
        "execution_role": "assistant",
    }
    origin.update(overrides)
    return agent_mod.origin_var.set(origin)


@pytest.mark.asyncio
class TestAskUserRejectsEventDispatchedTurn:
    async def test_ask_user_rejects_event_wake_origin(self, monkeypatch):
        """tools.py:599 — the real provenance classifier, driven with a
        synthetic event_wake context exactly as event_episodes._wake_context
        composes it, must reject ask_user (an event nudge is headless: 'do
        not ask' per its own instruction wording — this is the load-bearing
        enforcement of that rule)."""
        import agent as agent_mod
        import tools as tools_mod
        from unittest.mock import MagicMock

        tools_mod.init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        )
        tok = _set_origin(agent_mod, synthetic="event_wake",
                          emitter="gmail", event="new_mail")
        try:
            res = await tools_mod.ask_user.handler(
                {"question": "Proceed?", "options": ["Yes", "No"]})
        finally:
            agent_mod.origin_var.reset(tok)
        import json
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "unsupported_origin"
