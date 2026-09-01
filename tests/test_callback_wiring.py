"""Authorization-callback WIRING (boot, scheduler, reload scopes,
plugin lifecycle, revoke tool, health).

These pin the load-bearing wiring requirements below:

* **Route-gate completeness** — setup dispatch must NOT fire while a plugin's
  callbacks are dark. The trigger-only ``routes_live`` gate is permissive: a
  callback dark for a NON-consent reason contributes no round member, so the
  trigger approval alone settles the round. ``_callback_and_trigger_routes_live``
  also requires NO ``callback_*`` issue outstanding.
* **Union ack lookup** — the setup-round crash-recovery ack lookup must UNION
  both stores; trigger and callback acks live in disjoint sha256 identity
  spaces.
* **Paired reconcile** — EVERY ``reconcile_from_runtime`` call site runs BOTH
  reconcilers.
* **Lock-stall avoidance** — the SCHEDULED spool sweep/recovery passes run off
  the loop via ``asyncio.to_thread``; the handler's O(1) claim/publish stays
  inline.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

CASA = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"


def _issue(name, reason_code):
    return SimpleNamespace(name=name, reason_code=reason_code, target=None,
                           stage="callbacks", artifact_id="art-1")


# ---------------------------------------------------------------------------
# I1 — routes_live gate consults BOTH trigger AND callback issues
# ---------------------------------------------------------------------------

class TestI1RoutesLiveGate:
    def _wire(self, monkeypatch, *, trigger, callback,
              trigger_ok=True, callback_ok=True,
              trigger_seen=("gmail", "other"), callback_seen=("gmail", "other")):
        # #453: the gate reads ``issue_state`` — ``(ok, issues, observed)`` —
        # not ``current_issues``. The flag is the point: an empty list is the
        # positive claim "no gap", and a recomputation that could not run
        # produces the same empty list, so the gate must be able to tell them
        # apart. #457 added the third field for the other way an empty list
        # lies: a plugin the computation never SAW. ``current_issues`` remains
        # the health-report accessor.
        import casa_core
        import trigger_reconcile
        import callback_reconcile
        monkeypatch.setattr(
            trigger_reconcile, "issue_state",
            lambda resolver=None: trigger_reconcile.IssueState(
                trigger_ok, list(trigger), set(trigger_seen)))
        monkeypatch.setattr(
            callback_reconcile, "issue_state",
            lambda resolver=None: callback_reconcile.IssueState(
                callback_ok, list(callback), set(callback_seen)))
        return casa_core._callback_and_trigger_routes_live

    def test_live_when_no_issue(self, monkeypatch):
        gate = self._wire(monkeypatch, trigger=[], callback=[])
        assert gate("gmail") is True

    def test_dark_on_trigger_issue(self, monkeypatch):
        gate = self._wire(
            monkeypatch, trigger=[_issue("gmail", "trigger_pending_ack")],
            callback=[])
        assert gate("gmail") is False

    def test_dark_on_callback_issue_non_consent(self, monkeypatch):
        # The core I1 hole: a callback dark for a NON-consent reason
        # (no round member) must still block the setup dispatch.
        gate = self._wire(
            monkeypatch, trigger=[],
            callback=[_issue("gmail", "callback_no_target")])
        assert gate("gmail") is False

    def test_dark_on_callback_pending_ack(self, monkeypatch):
        gate = self._wire(
            monkeypatch, trigger=[],
            callback=[_issue("gmail", "callback_pending_ack")])
        assert gate("gmail") is False

    def test_other_plugins_issue_does_not_gate(self, monkeypatch):
        gate = self._wire(
            monkeypatch, trigger=[_issue("other", "trigger_pending_ack")],
            callback=[_issue("other", "callback_no_target")])
        assert gate("gmail") is True

    @pytest.mark.parametrize("half", ["trigger_ok", "callback_ok"])
    def test_dark_when_a_half_could_not_be_computed(self, monkeypatch, half):
        """Either half reporting "I could not evaluate this" keeps the plugin
        dark. Without the flag both halves degraded a crash — or a runtime that
        is not up yet — to an empty list, which is indistinguishable from
        "nothing is wrong" and opens the gate."""
        gate = self._wire(monkeypatch, trigger=[], callback=[],
                          **{half: False})
        assert gate("gmail") is False

    @pytest.mark.parametrize("half", ["trigger_seen", "callback_seen"])
    def test_dark_when_a_half_never_saw_the_plugin(self, monkeypatch, half):
        """#457. A successful, issue-free recomputation that the plugin is
        simply ABSENT from must not read as "this plugin has no gap".

        Two states produce exactly that: an invalid registry, where both
        reconcilers return an empty result by design (and the pass that follows
        swaps in an EMPTY overlay, so every plugin webhook 404s), and one
        artifact that fails to resolve, which becomes a ``stage="resolve"``
        issue in place of the plugin — named by no ``trigger_*``/``callback_*``
        row the gate matches on. ``ok`` is True in both: the computation RAN.
        Presence must therefore be established POSITIVELY."""
        gate = self._wire(monkeypatch, trigger=[], callback=[],
                          **{half: ("other",)})
        assert gate("gmail") is False

    def test_live_requires_presence_in_BOTH_halves(self, monkeypatch):
        """The positive claim is per half, exactly as the issue check is: a
        plugin the trigger pass saw and the callback pass did not is still
        dark."""
        gate = self._wire(monkeypatch, trigger=[], callback=[],
                          trigger_seen=("gmail",), callback_seen=("gmail",))
        assert gate("gmail") is True


class TestI1ObservedIsProduced:
    """The gate's ``observed`` half is only as good as what the reconcilers
    actually report, so pin the producers too — a gate reading a field nothing
    fills is a gate that denies everything."""

    def _resolver(self, *, valid, names):
        from types import SimpleNamespace

        def resolve(target=None):
            return SimpleNamespace(
                registry_valid=valid, generation=1, issues=[], warnings=[],
                plugins=[SimpleNamespace(name=n, artifact_id=f"art-{n}",
                                         path=f"/p/{n}", version="1",
                                         manifest={}, manifest_name=n)
                         for n in names])
        resolve.entries = lambda: [{"name": n, "targets": ["resident:butler"]}
                                   for n in names]
        resolve.generation = 1
        return resolve

    def test_trigger_compute_reports_every_resolved_plugin(self):
        import trigger_reconcile as tr
        desired = tr.compute_desired(
            role_configs={"butler": SimpleNamespace(channels=["webhook"])},
            acks={}, resolver=self._resolver(valid=True, names=["a", "b"]),
            global_secret_ok=lambda: True)
        # Neither declares a trigger, so neither raises an issue — and that is
        # exactly the state the gate used to read as "no gap" for a plugin it
        # had never seen.
        assert desired.issues == []
        assert desired.observed == {"a", "b"}

    def test_trigger_compute_observes_nothing_on_invalid_registry(self):
        import trigger_reconcile as tr
        desired = tr.compute_desired(
            role_configs={"butler": SimpleNamespace(channels=["webhook"])},
            acks={}, resolver=self._resolver(valid=False, names=["a"]),
            global_secret_ok=lambda: True)
        assert desired.issues == []
        assert desired.observed == set()

    def test_callback_compute_reports_every_resolved_plugin(self, monkeypatch):
        import callback_reconcile as cr
        import plugin_store
        monkeypatch.setattr(plugin_store, "manifest_callbacks",
                            lambda manifest, name: [])
        desired = cr.compute_desired(
            role_configs={"butler": object()}, acks={},
            resolver=self._resolver(valid=True, names=["a", "b"]))
        assert desired.issues == []
        assert desired.observed == {"a", "b"}

    def test_callback_compute_observes_nothing_on_invalid_registry(self):
        import callback_reconcile as cr
        desired = cr.compute_desired(
            role_configs={"butler": object()}, acks={},
            resolver=self._resolver(valid=False, names=["a"]))
        assert desired.issues == []
        assert desired.observed == set()


# ---------------------------------------------------------------------------
# I2 — setup-round ack lookup unions trigger + callback stores
# ---------------------------------------------------------------------------

class TestI2AckLookupUnion:
    def test_union_finds_trigger_ack(self, monkeypatch):
        import casa_core
        import trigger_acks
        import callback_acks
        monkeypatch.setattr(trigger_acks.ACKS, "get",
                            lambda ident: {"gen": "tg1"} if ident == "T" else None)
        monkeypatch.setattr(callback_acks.ACKS, "get", lambda ident: None)
        assert casa_core._setup_ack_lookup_union("T") == "tg1"

    def test_union_finds_callback_ack(self, monkeypatch):
        import casa_core
        import trigger_acks
        import callback_acks
        # The core I2 hole: identity lives ONLY in the callback store.
        monkeypatch.setattr(trigger_acks.ACKS, "get", lambda ident: None)
        monkeypatch.setattr(callback_acks.ACKS, "get",
                            lambda ident: {"gen": "cb9"} if ident == "C" else None)
        assert casa_core._setup_ack_lookup_union("C") == "cb9"

    def test_union_none_when_absent(self, monkeypatch):
        import casa_core
        import trigger_acks
        import callback_acks
        monkeypatch.setattr(trigger_acks.ACKS, "get", lambda ident: None)
        monkeypatch.setattr(callback_acks.ACKS, "get", lambda ident: None)
        assert casa_core._setup_ack_lookup_union("X") is None


# ---------------------------------------------------------------------------
# I3 — every reconcile_from_runtime site runs BOTH reconcilers (structural)
# ---------------------------------------------------------------------------

def _reconcile_modules_called(func_node: ast.FunctionDef) -> set[str]:
    """Set of {'trigger_reconcile','callback_reconcile'} whose
    ``.reconcile_from_runtime`` is called somewhere in this function body."""
    found: set[str] = set()
    for node in ast.walk(func_node):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "reconcile_from_runtime"
                and isinstance(node.func.value, ast.Name)):
            found.add(node.func.value.id)
    return found


@pytest.mark.parametrize("relpath", ["reload.py", "tools.py",
                                     "plugin_routing_recovery.py"])
def test_i3_every_reconcile_from_runtime_site_is_paired(relpath):
    """Any function that calls trigger_reconcile.reconcile_from_runtime must
    also call callback_reconcile.reconcile_from_runtime (and vice-versa) — so a
    shared setup round never keeps an open member of the other kind."""
    tree = ast.parse((CASA / relpath).read_text(encoding="utf-8"))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        mods = _reconcile_modules_called(node)
        if not mods:
            continue
        checked += 1
        assert "trigger_reconcile" in mods and "callback_reconcile" in mods, (
            f"{relpath}:{node.name} calls reconcile_from_runtime for {mods} "
            "but not BOTH reconcilers")
    assert checked >= 1, f"no reconcile_from_runtime site found in {relpath}"


@pytest.mark.asyncio
async def test_i3_reload_triggers_scope_reconciles_callbacks(monkeypatch):
    """A reload in a trigger-reconcile scope re-derives the CALLBACK overlay
    too (functional): register a trivial handler for the ``triggers`` scope and
    assert both reconcilers ran with the same runtime."""
    import reload as reload_mod
    import trigger_reconcile
    import callback_reconcile

    async def _fake_handler(runtime, *, role=None):
        return ["reloaded"]

    monkeypatch.setitem(reload_mod._HANDLERS, "triggers", _fake_handler)
    tg = AsyncMock(return_value=[])
    cb = AsyncMock(return_value=[])
    monkeypatch.setattr(trigger_reconcile, "reconcile_from_runtime", tg)
    monkeypatch.setattr(callback_reconcile, "reconcile_from_runtime", cb)

    runtime = SimpleNamespace(trigger_registry=object())
    res = await reload_mod.dispatch("triggers", runtime=runtime, role="gmail")
    assert res["status"] == "ok"
    tg.assert_awaited_once()
    cb.assert_awaited_once()
    assert tg.await_args.args[0] is runtime
    assert cb.await_args.args[0] is runtime
    assert "plugin_callbacks_reconciled" in res["actions"]


def test_i3_boot_pairs_trigger_and_callback_reconcile():
    """casa_core boot invokes BOTH boot reconcile helpers (they are separate
    functions, called adjacently at the boot call site)."""
    src = (CASA / "casa_core.py").read_text(encoding="utf-8")
    assert "async def _boot_reconcile_plugin_triggers" in src
    assert "async def _boot_reconcile_plugin_callbacks" in src
    assert "await _boot_reconcile_plugin_triggers(" in src
    assert "await _boot_reconcile_plugin_callbacks(" in src


# ---------------------------------------------------------------------------
# Lock-stall ruling — scheduled scans go through asyncio.to_thread
# ---------------------------------------------------------------------------

def test_lock_stall_scheduled_scans_use_to_thread():
    """The scheduled sweep + recovery passes MUST run off the event loop via
    asyncio.to_thread (they hold the spool lock for a whole scan); the handler
    fast path is not touched here."""
    src = (CASA / "casa_core.py").read_text(encoding="utf-8")
    flat = " ".join(src.split())   # collapse all whitespace
    assert "asyncio.to_thread(spool.sweep, now=" in flat
    assert "asyncio.to_thread(spool.recovery_pass, now=time.time(), boot=False)" \
        in flat
    # boot recovery once, off-loop, boot=True
    assert "asyncio.to_thread(spool.recovery_pass, now=time.time(), boot=True)" \
        in flat
    # Scheduler registers both interval jobs.
    assert 'id="callback_spool_sweep"' in src
    assert 'id="callback_spool_recovery"' in src


def test_handler_claim_publish_stay_inline():
    """The HTTP handler's claim/publish must remain inline (no to_thread) —
    they are O(1) and moving them off the loop is the wrong direction."""
    src = (CASA / "callback_http.py").read_text(encoding="utf-8")
    assert "to_thread" not in src


# ---------------------------------------------------------------------------
# spool.remove_plugin — recursive delete + guard
# ---------------------------------------------------------------------------

class TestSpoolRemovePlugin:
    def test_removes_plugin_dir(self, tmp_path):
        from callback_spool import CallbackSpool
        spool = CallbackSpool(tmp_path / "cb")
        try:
            spool.ensure_plugin_dirs("gmail")
            assert (spool.root / "gmail").is_dir()
            assert spool.remove_plugin("gmail") is True
            assert not (spool.root / "gmail").exists()
        finally:
            spool.close()

    def test_missing_dir_is_false(self, tmp_path):
        from callback_spool import CallbackSpool
        spool = CallbackSpool(tmp_path / "cb")
        try:
            assert spool.remove_plugin("never") is False
        finally:
            spool.close()

    @pytest.mark.parametrize("bad", ["..", ".", ".index", "a/b", ""])
    def test_unsafe_name_raises(self, tmp_path, bad):
        from callback_spool import CallbackSpool
        spool = CallbackSpool(tmp_path / "cb")
        try:
            with pytest.raises(ValueError):
                spool.remove_plugin(bad)
        finally:
            spool.close()

    def test_closed_spool_noop(self, tmp_path):
        from callback_spool import CallbackSpool
        spool = CallbackSpool(tmp_path / "cb")
        spool.ensure_plugin_dirs("gmail")
        spool.close()
        assert spool.remove_plugin("gmail") is False


# ---------------------------------------------------------------------------
# ACKS.revoke_effective — single-callback off-switch key
# ---------------------------------------------------------------------------

class TestRevokeEffective:
    def _store(self, tmp_path):
        from callback_acks import CallbackAckStore
        return CallbackAckStore(tmp_path / "acks.json")

    def test_revokes_only_matching_effective(self, tmp_path):
        store = self._store(tmp_path)
        store.record("gmail", "plg-gmail--authorize", "d1")
        store.record("gmail", "plg-gmail--refresh", "d2")
        store.record("slack", "plg-slack--authorize", "d3")
        removed = store.revoke_effective("gmail", "plg-gmail--authorize")
        assert [r["effective"] for r in removed] == ["plg-gmail--authorize"]
        # the sibling + other plugin survive
        fresh = self._store(tmp_path)
        from plugin_callbacks import ack_identity
        assert fresh.get(ack_identity("gmail", "plg-gmail--refresh", "d2"))
        assert fresh.get(ack_identity("slack", "plg-slack--authorize", "d3"))
        assert fresh.get(ack_identity("gmail", "plg-gmail--authorize", "d1")) \
            is None

    def test_revoke_effective_any_digest(self, tmp_path):
        store = self._store(tmp_path)
        store.record("gmail", "plg-gmail--authorize", "d-old")
        store.record("gmail", "plg-gmail--authorize", "d-new")
        removed = store.revoke_effective("gmail", "plg-gmail--authorize")
        assert len(removed) == 2


# ---------------------------------------------------------------------------
# callback_ack_revoke tool — revoke one + reconcile + health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCallbackAckRevokeTool:
    async def test_revokes_and_reconciles(self, tmp_path, monkeypatch):
        import agent as agent_mod
        import callback_acks
        import callback_reconcile
        import trigger_reconcile
        import tools

        store = callback_acks.CallbackAckStore(tmp_path / "acks.json")
        store.record("gmail", "plg-gmail--authorize", "d1")
        monkeypatch.setattr(callback_acks, "ACKS", store)

        cb_recon = AsyncMock(return_value=[])
        tg_recon = AsyncMock(return_value=[])
        monkeypatch.setattr(callback_reconcile, "reconcile_from_runtime",
                            cb_recon)
        monkeypatch.setattr(trigger_reconcile, "reconcile_from_runtime",
                            tg_recon)
        monkeypatch.setattr(tools, "_regenerate_plugin_health", lambda x: None)
        cancels = []
        monkeypatch.setattr(tools.CHALLENGES, "cancel_matching",
                            lambda **k: cancels.append(k))
        monkeypatch.setattr(agent_mod, "active_runtime",
                            SimpleNamespace(trigger_registry=object()),
                            raising=False)

        import json
        res = await tools.callback_ack_revoke.handler(
            {"plugin": "gmail", "name": "plg-gmail--authorize"})
        payload = json.loads(res["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["revoked"] == 1
        # BOTH reconcilers ran, with prompt suppressed.
        cb_recon.assert_awaited()
        tg_recon.assert_awaited()
        assert cb_recon.await_args.kwargs.get("prompt") is False
        # pending keyboard cancelled before AND after the reconcile.
        assert cancels.count({"plugin": "gmail"}) == 2
        # the ack is gone from disk
        fresh = callback_acks.CallbackAckStore(tmp_path / "acks.json")
        from plugin_callbacks import ack_identity
        assert fresh.get(
            ack_identity("gmail", "plg-gmail--authorize", "d1")) is None

    async def test_registered_in_casa_tools(self):
        import tools
        names = {t.name for t in tools.CASA_TOOLS}
        assert "callback_ack_revoke" in names


# ---------------------------------------------------------------------------
# plugin removal — durable callback teardown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remove_plugin_callbacks_revokes_and_purges(tmp_path, monkeypatch):
    import callback_acks
    import callback_spool
    import tools

    store = callback_acks.CallbackAckStore(tmp_path / "acks.json")
    store.record("gmail", "plg-gmail--authorize", "d1")
    monkeypatch.setattr(callback_acks, "ACKS", store)

    spool = callback_spool.CallbackSpool(tmp_path / "cb")
    spool.ensure_plugin_dirs("gmail")
    monkeypatch.setattr(callback_spool, "get_spool", lambda: spool)
    try:
        await tools._remove_plugin_callbacks("gmail")
        # ack revoked
        from plugin_callbacks import ack_identity
        fresh = callback_acks.CallbackAckStore(tmp_path / "acks.json")
        assert fresh.get(
            ack_identity("gmail", "plg-gmail--authorize", "d1")) is None
        # spool dir purged
        assert not (spool.root / "gmail").exists()
    finally:
        spool.close()


@pytest.mark.asyncio
async def test_remove_plugin_callbacks_survives_no_spool(monkeypatch):
    import callback_acks
    import callback_spool
    import tools
    monkeypatch.setattr(callback_acks.ACKS, "revoke_plugin", lambda n: [])
    monkeypatch.setattr(callback_spool, "get_spool", lambda: None)
    # No spool wired ⇒ no crash.
    await tools._remove_plugin_callbacks("gmail")


# ---------------------------------------------------------------------------
# health surface includes callback issues
# ---------------------------------------------------------------------------

def test_health_report_includes_callback_issues(tmp_path, monkeypatch):
    import tools
    import callback_reconcile
    import trigger_reconcile
    import plugin_registry

    monkeypatch.setattr(tools, "_PLUGIN_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(trigger_reconcile, "current_issues", lambda: [])
    monkeypatch.setattr(
        callback_reconcile, "current_issues",
        lambda: [_issue("gmail", "callback_pending_ack")])
    # keep the resolver-side inputs empty so only our injected issue lands
    monkeypatch.setattr(
        plugin_registry, "resolve_all",
        lambda: SimpleNamespace(issues=[], warnings=[]))
    monkeypatch.setattr(
        plugin_registry, "load_registry",
        lambda *a, **k: SimpleNamespace(valid=False, entries=[]))

    written = {}
    import plugin_health
    monkeypatch.setattr(
        plugin_health, "write_report",
        lambda **kw: written.update(kw))

    tools._regenerate_plugin_health([])
    codes = {getattr(i, "reason_code", None) for i in written["issues"]}
    assert "callback_pending_ack" in codes


# ---------------------------------------------------------------------------
# boot reconcile — gated orphan GC untouched on invalid registry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_boot_gc_noop_on_invalid_registry(tmp_path, monkeypatch):
    import casa_core
    import callback_spool
    import callback_reconcile
    import plugin_registry

    spool = callback_spool.CallbackSpool(tmp_path / "cb")
    spool.ensure_plugin_dirs("ghost")   # a plugin NOT in the registry
    monkeypatch.setattr(callback_spool, "get_spool", lambda: spool)
    monkeypatch.setattr(callback_spool, "init_spool", lambda *a, **k: spool)

    # reconcile is a no-op here; we only exercise the GC gate
    async def _noop_recon(**kw):
        return []
    monkeypatch.setattr(callback_reconcile, "reconcile_plugin_callbacks",
                        _noop_recon)
    # INVALID registry ⇒ GC must be a NO-OP (membership from a failed load
    # would vaporize every plugin's spool).
    monkeypatch.setattr(
        plugin_registry, "snapshot_registry",
        lambda: SimpleNamespace(valid=False, entries=[]))
    try:
        await casa_core._boot_reconcile_plugin_callbacks(
            trigger_registry=object(), role_configs={})
        assert (spool.root / "ghost").is_dir(), \
            "invalid registry must leave spool dirs untouched"
    finally:
        spool.close()
