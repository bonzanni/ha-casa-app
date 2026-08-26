"""Release B — the plugin-trigger reconciler.

The reconciler is the ONE writer of the TriggerRegistry plugin overlay: it
builds the COMPLETE desired overlay (every resolved + assigned + valid +
acked plugin trigger), eagerly mints casa-owned per-trigger secrets for the
routed set, swaps the overlay atomically (stale plugin ingress is swept by
absence), and fires consent prompts for triggers whose ONLY gap is the ack.
Contextual failures surface as recomputable PluginIssues (stage="triggers").
"""
import asyncio
from types import SimpleNamespace

import pytest

import trigger_reconcile as tr
from trigger_acks import TriggerAckStore
from trigger_registry import TriggerRegistry
from plugin_triggers import ack_identity

AUTH = {"mode": "static_header", "header": "X-API-Key",
        "tolerance_secs": 300, "secret_owner": "casa"}


def _manifest(triggers):
    return {"name": "x", "casa": {"triggers": triggers}}


def _trigger(**over):
    t = {"name": "voicemail", "type": "webhook",
         "target": "resident:assistant",
         "auth": {"mode": "static_header", "header": "X-API-Key"}}
    t.update(over)
    return t


def _plugin(name="elevenlabs", artifact_id="art-1", triggers=None):
    return SimpleNamespace(
        name=name, artifact_id=artifact_id, path=f"/store/{name}",
        version="1.0.0",
        manifest=_manifest([_trigger()] if triggers is None else triggers))


def _resolver(plugins_by_target):
    """target (or None) -> list of resolved plugins; registry always valid."""
    def resolve(target):
        return SimpleNamespace(
            registry_valid=True,
            plugins=list(plugins_by_target.get(target, [])))
    return resolve


def _invalid_resolver():
    def resolve(target):
        return SimpleNamespace(registry_valid=False, plugins=[])
    return resolve


def _role_configs(**roles):
    """role -> channels list."""
    out = {}
    for role, channels in roles.items():
        out[role] = SimpleNamespace(channels=list(channels))
    return out


class _FakeTelegram:
    chat_id = "100"

    def __init__(self):
        self.posts = []

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        return 55

    async def edit_dm_message(self, chat_id, message_id, text):
        return True


class _FakeChannelManager:
    def __init__(self, telegram=None):
        self._telegram = telegram

    def get(self, name):
        return self._telegram if name == "telegram" else None


def _registry():
    # Overlay operations never touch the scheduler/app/bus.
    return TriggerRegistry(scheduler=None, app=None, bus=None)


def _ack(acks, plugin="elevenlabs", artifact_id="art-1",
         declared="voicemail", target="resident:assistant", auth=None):
    auth = auth or AUTH
    effective = f"plg-{plugin}--{declared}"
    ident = ack_identity(plugin=plugin, artifact_id=artifact_id,
                         effective=effective, target=target, auth=auth)
    acks.record(identity=ident, plugin=plugin, artifact_id=artifact_id,
                effective=effective, target=target, auth=auth)
    return ident


async def _reconcile(registry, *, plugins_by_target, role_configs, acks,
                     tmp_path, channel_manager=None, prompt=True,
                     global_secret_ok=True, resolver=None):
    return await tr.reconcile_plugin_triggers(
        trigger_registry=registry,
        role_configs=role_configs,
        channel_manager=channel_manager,
        acks=acks,
        secrets_dir=tmp_path / "webhook_secrets",
        prompt=prompt,
        resolver=resolver or _resolver(plugins_by_target),
        global_secret_ok=lambda: global_secret_ok,
    )


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


async def test_valid_acked_plugin_routes_and_mints_secret(tmp_path):
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["telegram", "webhook"]),
        acks=acks, tmp_path=tmp_path)
    assert issues == []
    eff = "plg-elevenlabs--voicemail"
    assert registry.get_webhook_target(eff) == "assistant"
    assert registry.get_clearance(eff) == "public"
    assert registry.get_auth_policy(eff)["mode"] == "static_header"
    # the secret was minted EAGERLY (before any request) so the plugin's
    # setup tool can read it
    assert (tmp_path / "webhook_secrets" / eff).exists()


async def test_artifact_change_rekeys_secret_even_without_retirement(tmp_path):
    """Terra shipB-r2: the credential can never cross an artifact boundary —
    even when NOTHING retired the old secret, the new artifact's activation
    mints a fresh one (identity-bound mint)."""
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p1 = _plugin()
    await _reconcile(
        registry, plugins_by_target={None: [p1], "resident:assistant": [p1]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path)
    eff = "plg-elevenlabs--voicemail"
    old_secret = (tmp_path / "webhook_secrets" / eff).read_bytes()

    # artifact changes; the old secret file deliberately SURVIVES (no
    # retirement ran); operator re-acks the new identity
    _ack(acks, artifact_id="art-2")
    p2 = _plugin(artifact_id="art-2")
    issues = await _reconcile(
        registry, plugins_by_target={None: [p2], "resident:assistant": [p2]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path)
    assert issues == []
    assert registry.get_webhook_target(eff) == "assistant"
    assert (tmp_path / "webhook_secrets" / eff).read_bytes() != old_secret


async def test_regen_health_flag_regenerates_report(monkeypatch, tmp_path):
    """P2 follow-up (v0.98.2): the consent-approve reconcile must regenerate
    plugin-health so a just-acked trigger's stale trigger_pending_ack clears
    at once instead of lingering until the next mutation."""
    import tools as tools_mod
    calls: list = []
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda extra: calls.append(extra))
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await tr.reconcile_plugin_triggers(
        trigger_registry=registry,
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, secrets_dir=tmp_path / "webhook_secrets", prompt=False,
        resolver=_resolver({None: [p], "resident:assistant": [p]}),
        global_secret_ok=lambda: True, regen_health=True)
    assert calls == [[]]


async def test_default_reconcile_does_not_regen_health(monkeypatch, tmp_path):
    """The mutation/boot paths regen health separately — the default reconcile
    must NOT double-regen."""
    import tools as tools_mod
    calls: list = []
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda extra: calls.append(extra))
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path)
    assert calls == []


async def test_regen_health_failure_never_breaks_reconcile(monkeypatch, tmp_path):
    import tools as tools_mod

    def _boom(extra):
        raise RuntimeError("health regen blew up")

    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", _boom)
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await tr.reconcile_plugin_triggers(
        trigger_registry=registry,
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, secrets_dir=tmp_path / "webhook_secrets", prompt=False,
        resolver=_resolver({None: [p], "resident:assistant": [p]}),
        global_secret_ok=lambda: True, regen_health=True)
    assert issues == []  # reconcile still succeeded; the trigger routed
    assert registry.get_webhook_target("plg-elevenlabs--voicemail") == "assistant"


async def test_consent_approve_regenerates_health(monkeypatch, tmp_path):
    """Integration: an operator Approve on the consent keyboard clears the
    stale trigger_pending_ack by regenerating the health report — the exact
    prod symptom this fix targets."""
    import authz_grants
    import tools as tools_mod
    import verdict_broker

    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    coord = authz_grants.ChallengeCoordinator()
    monkeypatch.setattr(authz_grants, "CHALLENGES", coord)
    regen: list = []
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda extra: regen.append(extra))

    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    telegram = _FakeTelegram()
    p = _plugin()
    # reconcile WITH prompting -> a live pending consent keyboard (unacked)
    await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path,
        channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert len(telegram.posts) == 1
    assert regen == []  # nothing regenerated yet — still pending

    # operator taps Approve (claim -> commit -> sync ack step) then settle
    key = next(iter(coord._entries))
    ch = coord._entries[key]
    claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, option_index=0, actor_id=100)
    assert not isinstance(claim, str)
    assert broker.commit(claim) is True
    step = ch.req.meta.get("on_commit_sync")
    if step is not None:
        step(0)
    # the finish hook runs as a broker-driven task and its reconcile uses
    # asyncio.to_thread — poll with real sleeps until it settles.
    for _ in range(100):
        if regen:
            break
        await asyncio.sleep(0.02)

    assert acks.is_acked(_ident_default())
    assert registry.get_webhook_target("plg-elevenlabs--voicemail") == "assistant"
    assert regen == [[]]  # health regenerated exactly once on approve


async def test_reapproval_after_revoke_rekeys_even_if_retire_failed(
    monkeypatch, tmp_path,
):
    """Sol shipB-r3: revoke promises 're-approval mints fresh'. Even when
    EVERY retirement silently failed (the old — possibly compromised —
    secret survives on disk), the re-approval's new generation forces a
    rekey at activation."""
    import webhook_auth

    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    args = dict(
        plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path)
    await _reconcile(registry, **args)
    eff = "plg-elevenlabs--voicemail"
    old_secret = (tmp_path / "webhook_secrets" / eff).read_bytes()

    # revoke, with retirement COMPLETELY broken (nothing is deleted)
    monkeypatch.setattr(webhook_auth, "retire_secrets_with_prefix",
                        lambda prefix, *, secrets_dir: [])
    acks.revoke_plugin("elevenlabs")
    assert (tmp_path / "webhook_secrets" / eff).read_bytes() == old_secret

    # operator re-approves the IDENTICAL tuple → new approval generation
    _ack(acks)
    issues = await _reconcile(registry, **args)
    assert issues == []
    assert (tmp_path / "webhook_secrets" / eff).read_bytes() != old_secret


async def test_unacked_plugin_stays_unrouted_with_pending_issue(tmp_path):
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    issues = await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path)
    assert [i.reason_code for i in issues] == ["trigger_pending_ack"]
    assert issues[0].name == "elevenlabs"
    assert issues[0].stage == "triggers"
    assert registry.get_webhook_target("plg-elevenlabs--voicemail") is None
    # no eager secret for an unrouted trigger
    assert not (tmp_path / "webhook_secrets" / "plg-elevenlabs--voicemail").exists()


async def test_missing_webhook_channel_blocks(tmp_path):
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["telegram"]),  # no webhook
        acks=acks, tmp_path=tmp_path)
    assert [i.reason_code for i in issues] == ["trigger_channel_missing"]
    assert registry.get_webhook_target("plg-elevenlabs--voicemail") is None


async def test_unknown_target_resident_blocks(tmp_path):
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    p = _plugin(triggers=[_trigger(target="resident:ghost")])
    issues = await _reconcile(
        registry, plugins_by_target={None: [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path)
    assert [i.reason_code for i in issues] == ["trigger_channel_missing"]


async def test_unassigned_plugin_routes_nothing(tmp_path):
    """Assignment authority: the plugin declares resident:assistant but its
    registry entry is NOT assigned there (target-scoped resolution excludes
    it) — it must not route."""
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin()
    issues = await _reconcile(
        registry,
        plugins_by_target={None: [p]},  # assigned nowhere resident-side
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path)
    assert [i.reason_code for i in issues] == ["trigger_unassigned_target"]
    assert registry.get_webhook_target("plg-elevenlabs--voicemail") is None


async def test_stale_plugin_overlay_is_swept(tmp_path):
    registry = _registry()
    registry.replace_plugin_overlay({
        "plg-gone--old": {"role": "assistant", "clearance": "public",
                          "auth": AUTH}})
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    issues = await _reconcile(
        registry, plugins_by_target={None: []},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path)
    assert issues == []
    assert registry.get_webhook_target("plg-gone--old") is None


async def test_per_plugin_all_or_nothing(tmp_path):
    """One bad trigger (channel missing) sinks the plugin's WHOLE set — the
    acked+valid sibling must not route either.

    Pins INV-TRIG-003. Red case demonstrated: making compute_desired route the overlay unconditionally fails this test.
    """
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    p = _plugin(triggers=[
        _trigger(),
        _trigger(name="transcript", target="resident:ghost")])
    issues = await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path)
    assert [i.reason_code for i in issues] == ["trigger_channel_missing"]
    assert registry.get_webhook_target("plg-elevenlabs--voicemail") is None


async def test_other_plugins_unaffected_by_one_bad_plugin(tmp_path):
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    good = _plugin()
    bad = _plugin(name="badone", artifact_id="art-9",
                  triggers=[_trigger(target="resident:ghost")])
    issues = await _reconcile(
        registry,
        plugins_by_target={None: [good, bad], "resident:assistant": [good]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path)
    assert [i.name for i in issues] == ["badone"]
    assert registry.get_webhook_target("plg-elevenlabs--voicemail") == "assistant"


async def test_intrinsic_invalid_manifest_is_trigger_invalid(tmp_path):
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    p = _plugin(triggers=[_trigger(target="specialist:finance")])
    issues = await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path)
    assert [i.reason_code for i in issues] == ["trigger_invalid"]


async def test_invalid_registry_fails_closed_to_empty_overlay(tmp_path):
    registry = _registry()
    registry.replace_plugin_overlay({
        "plg-old--x": {"role": "assistant", "clearance": "public",
                       "auth": AUTH}})
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    issues = await _reconcile(
        registry, plugins_by_target={},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path, resolver=_invalid_resolver())
    assert issues == []  # registry-invalid has its own (non-trigger) issues
    assert registry.get_webhook_target("plg-old--x") is None


async def test_hmac_body_requires_global_secret(tmp_path):
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    auth = {"mode": "hmac_body", "header": "X-Webhook-Signature",
            "tolerance_secs": 300, "secret_owner": "casa"}
    p = _plugin(triggers=[_trigger(auth={"mode": "hmac_body"})])
    _ack(acks, auth=auth)
    issues = await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path, global_secret_ok=False)
    assert [i.reason_code for i in issues] == ["trigger_secret_missing"]

    issues = await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path, global_secret_ok=True)
    assert issues == []
    eff = "plg-elevenlabs--voicemail"
    assert registry.get_webhook_target(eff) == "assistant"
    # hmac_body uses the GLOBAL secret — no per-trigger file is minted
    assert not (tmp_path / "webhook_secrets" / eff).exists()


# ---------------------------------------------------------------------------
# consent prompting
# ---------------------------------------------------------------------------


async def test_pending_consent_fires_one_prompt(monkeypatch, tmp_path):
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    import authz_grants
    monkeypatch.setattr(authz_grants, "CHALLENGES",
                        authz_grants.ChallengeCoordinator())
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    telegram = _FakeTelegram()
    p = _plugin()
    issues = await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path,
        channel_manager=_FakeChannelManager(telegram))
    assert [i.reason_code for i in issues] == ["trigger_pending_ack"]
    for _ in range(8):
        await asyncio.sleep(0)
    assert len(telegram.posts) == 1
    assert "/webhook/plg-elevenlabs--voicemail" in telegram.posts[0][2]
    # a second reconcile dedupes onto the live challenge (no second keyboard)
    await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path,
        channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert len(telegram.posts) == 1


async def test_no_prompt_when_other_gaps_remain(monkeypatch, tmp_path):
    """Consent is prompted only when the ack is the ONLY missing piece —
    approving a trigger that still can't route is a broken promise."""
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    import authz_grants
    monkeypatch.setattr(authz_grants, "CHALLENGES",
                        authz_grants.ChallengeCoordinator())
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    telegram = _FakeTelegram()
    p = _plugin(triggers=[
        _trigger(),  # consent missing
        _trigger(name="transcript", target="resident:ghost")])  # channel gap
    await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path,
        channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert telegram.posts == []


async def test_no_prompt_without_operator_channel(monkeypatch, tmp_path):
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    issues = await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path,
        channel_manager=_FakeChannelManager(None))
    # pending_ack stands; nothing crashes; no keyboard anywhere
    assert [i.reason_code for i in issues] == ["trigger_pending_ack"]


async def test_prompt_false_skips_prompting(monkeypatch, tmp_path):
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    telegram = _FakeTelegram()
    p = _plugin()
    await _reconcile(
        registry, plugins_by_target={None: [p], "resident:assistant": [p]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path, prompt=False,
        channel_manager=_FakeChannelManager(telegram))
    for _ in range(8):
        await asyncio.sleep(0)
    assert telegram.posts == []


# ---------------------------------------------------------------------------
# health recomputability
# ---------------------------------------------------------------------------


async def test_current_issues_recomputes_from_active_runtime(
    monkeypatch, tmp_path,
):
    import agent as agent_mod
    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    p = _plugin()
    resolver = _resolver({None: [p], "resident:assistant": [p]})
    runtime = SimpleNamespace(
        trigger_registry=registry,
        role_configs=_role_configs(assistant=["webhook"]),
        channel_manager=None)
    monkeypatch.setattr(agent_mod, "active_runtime", runtime)
    monkeypatch.setattr(tr, "_default_resolver", lambda: resolver)
    monkeypatch.setattr(tr, "_default_acks", lambda: acks)
    monkeypatch.setattr(tr, "SECRETS_DIR", tmp_path / "webhook_secrets")
    issues = tr.current_issues()
    # #606: this registry has never had a reconcile publish to it, so its
    # plugin overlay is the ROUTING_UNAVAILABLE sentinel and the honesty row
    # leads — the state is now stated rather than silent.
    assert [i.reason_code for i in issues] == [
        "trigger_routing_unavailable", "trigger_pending_ack"]
    _ack(acks)
    monkeypatch.setattr(tr, "_default_global_secret_ok", lambda: (lambda: True))
    # #453: the ack alone no longer makes the recomputation clean. The secret
    # this consent identity needs is minted by the RECONCILE, and a setup tool
    # dispatched before that mint would provision the provider against a
    # credential Casa is about to write — so it stays a gap until it exists.
    assert [i.reason_code for i in tr.current_issues()] == [
        "trigger_routing_unavailable", "trigger_secret_missing"]
    desired = tr.compute_desired(
        role_configs=runtime.role_configs, acks=acks, resolver=resolver,
        global_secret_ok=lambda: True)
    tr._mint_secrets(desired, tmp_path / "webhook_secrets")
    # ...and once it does, the SAME recomputation comes back clean. The
    # routing row remains, correctly: nothing here ever ran a reconcile, so the
    # registry still carries no authoritative plugin overlay.
    assert [i.reason_code for i in tr.current_issues()] == [
        "trigger_routing_unavailable"]


async def test_current_issues_without_runtime_is_empty(monkeypatch):
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", None)
    assert tr.current_issues() == []


# ---------------------------------------------------------------------------
# wiring — the runtime seam
# ---------------------------------------------------------------------------


async def test_mutation_sequencer_reconciles_after_verify_before_regen(
    monkeypatch,
):
    """tools._reload_and_verify_targets (the choke point all 5 lifecycle
    mutations funnel through) reconciles LAST — after snapshot reload +
    verify — and BEFORE the health regen that folds the trigger issues."""
    import agent as agent_mod
    import plugin_registry
    import tools as tools_mod

    log: list = []
    monkeypatch.setattr(plugin_registry, "reload_snapshot",
                        lambda: log.append("snapshot"))
    monkeypatch.setattr(plugin_registry, "snapshot_generation", lambda: 1)
    monkeypatch.setattr(agent_mod, "active_runtime", None)
    monkeypatch.setattr(tools_mod, "_tool_verify_plugin_state",
                        lambda plugin_name: (log.append("verify"),
                                             {"ready": True, "targets": []})[1])
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda extra: log.append("regen"))

    async def _fake_notify():
        return None

    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible",
                        _fake_notify)

    async def _spy(runtime, prompt=True):
        log.append("reconcile")
        return []

    monkeypatch.setattr(tr, "reconcile_from_runtime", _spy)
    result = await tools_mod._reload_and_verify_targets(
        "p", [], expect="present")
    assert log == ["snapshot", "verify", "reconcile", "regen"]
    assert result["ok"] is True


async def test_mutation_sequencer_survives_reconcile_failure(monkeypatch):
    import agent as agent_mod
    import plugin_registry
    import tools as tools_mod

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

    async def _boom(runtime, prompt=True):
        raise RuntimeError("reconcile blew up")

    monkeypatch.setattr(tr, "reconcile_from_runtime", _boom)
    result = await tools_mod._reload_and_verify_targets(
        "p", [], expect="present")
    assert result["ok"] is True  # ingress reconcile never fails the mutation


async def test_regenerate_health_folds_recomputable_trigger_issues(
    monkeypatch, tmp_path, event_routing_ok,
):
    """``event_routing_ok`` (Minor-2): the report's issues list is asserted
    EXACT below, which the conftest's actual PRODUCTION-default sentinel's
    own event_routing_unavailable row would otherwise pollute — opts into
    an authoritative empty routing map instead."""
    import plugin_registry
    import tools as tools_mod
    from plugin_registry import PluginIssue

    monkeypatch.setattr(tools_mod, "_PLUGIN_HEALTH_PATH",
                        str(tmp_path / "health.json"))
    monkeypatch.setattr(plugin_registry, "resolve_all",
                        lambda: SimpleNamespace(issues=[], warnings=[]))
    monkeypatch.setattr(plugin_registry, "load_registry",
                        lambda: SimpleNamespace(valid=False, entries=[]))
    trig = PluginIssue(name="elevenlabs", target="resident:assistant",
                       stage="triggers", reason_code="trigger_pending_ack")
    monkeypatch.setattr(tr, "current_issues", lambda: [trig])
    # regen with NO extras (the erasure case pre-fix) still carries the issue
    tools_mod._regenerate_plugin_health([])
    import json as _json
    report = _json.loads((tmp_path / "health.json").read_text())
    assert [i["reason_code"] for i in report["issues"]] == [
        "trigger_pending_ack"]
    # and a SECOND unrelated regen recomputes it again (never erased)
    tools_mod._regenerate_plugin_health([])
    report = _json.loads((tmp_path / "health.json").read_text())
    assert [i["reason_code"] for i in report["issues"]] == [
        "trigger_pending_ack"]


@pytest.mark.parametrize("scope", ["triggers", "agent", "agents", "full"])
async def test_reload_dispatch_reconciles_trigger_scopes(monkeypatch, scope):
    import reload as reload_mod

    calls: list = []

    async def _handler(runtime, role=None, include_env=False):
        return ["did_thing"]

    monkeypatch.setitem(reload_mod._HANDLERS, scope, _handler)

    async def _spy(runtime, prompt=True):
        calls.append(runtime)
        return []

    monkeypatch.setattr(tr, "reconcile_from_runtime", _spy)
    runtime = SimpleNamespace()
    result = await reload_mod.dispatch(scope, runtime=runtime, role="r")
    assert result["status"] == "ok"
    assert calls == [runtime]
    assert "plugin_triggers_reconciled" in result["actions"]


async def test_reload_dispatch_skips_non_trigger_scopes(monkeypatch):
    import reload as reload_mod

    calls: list = []

    async def _handler(runtime, role=None):
        return ["did_thing"]

    monkeypatch.setitem(reload_mod._HANDLERS, "synthetic_scope", _handler)

    async def _spy(runtime, prompt=True):
        calls.append(runtime)
        return []

    monkeypatch.setattr(tr, "reconcile_from_runtime", _spy)
    result = await reload_mod.dispatch("synthetic_scope",
                                       runtime=SimpleNamespace())
    assert result["status"] == "ok"
    assert calls == []


async def test_reload_dispatch_failed_handler_does_not_reconcile(monkeypatch):
    import reload as reload_mod

    calls: list = []

    async def _handler(runtime, role=None):
        raise reload_mod.ReloadError("synthetic", "boom")

    monkeypatch.setitem(reload_mod._HANDLERS, "triggers", _handler)

    async def _spy(runtime, prompt=True):
        calls.append(runtime)
        return []

    monkeypatch.setattr(tr, "reconcile_from_runtime", _spy)
    result = await reload_mod.dispatch("triggers", runtime=SimpleNamespace(),
                                       role="r")
    assert result["status"] == "error"
    assert calls == []


async def test_boot_reconcile_regens_health_only_on_issues(monkeypatch):
    import casa_core
    import tools as tools_mod
    from plugin_registry import PluginIssue

    regen: list = []
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda extra: regen.append(extra))
    recon_calls: list = []

    async def _spy(**kw):
        recon_calls.append(kw)
        return [PluginIssue(name="p", target=None, stage="triggers",
                            reason_code="trigger_pending_ack")]

    monkeypatch.setattr(tr, "reconcile_plugin_triggers", _spy)
    registry = _registry()
    await casa_core._boot_reconcile_plugin_triggers(
        trigger_registry=registry, role_configs={})
    assert len(recon_calls) == 1
    assert recon_calls[0]["prompt"] is False  # telegram is not polling yet
    assert regen == [[]]

    # clean reconcile: plugin_boot's fresh report already lacks trigger
    # issues, so no extra regen churn
    async def _clean(**kw):
        return []

    monkeypatch.setattr(tr, "reconcile_plugin_triggers", _clean)
    regen.clear()
    await casa_core._boot_reconcile_plugin_triggers(
        trigger_registry=registry, role_configs={})
    assert regen == []


async def test_boot_reconcile_failure_is_not_fatal(monkeypatch):
    import casa_core

    async def _boom(**kw):
        raise RuntimeError("boot reconcile blew up")

    monkeypatch.setattr(tr, "reconcile_plugin_triggers", _boom)
    await casa_core._boot_reconcile_plugin_triggers(
        trigger_registry=_registry(), role_configs={})  # must not raise


# ---------------------------------------------------------------------------
# lifecycle — artifact change revokes acks + retires secrets
# ---------------------------------------------------------------------------


def test_invalidate_lifecycle_revokes_artifact_acks_and_retires_secrets(
    monkeypatch, tmp_path,
):
    """plugin_update/plugin_remove call _invalidate_lifecycle with the OLD
    artifact_id: its trigger consents must drop (identity is artifact-bound)
    and the per-trigger secrets must be retired BEFORE any re-approval — a
    new artifact never inherits the old one's credentials."""
    import tools as tools_mod
    import trigger_acks as trigger_acks_mod
    import webhook_auth

    secrets_dir = tmp_path / "webhook_secrets"
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    monkeypatch.setattr(trigger_acks_mod, "ACKS", acks)
    monkeypatch.setattr(tr, "SECRETS_DIR", secrets_dir)
    _ack(acks)
    _ack(acks, plugin="other", artifact_id="art-9", declared="hook")
    eff = "plg-elevenlabs--voicemail"
    webhook_auth.ensure_secret(eff, owner="casa", secrets_dir=secrets_dir)
    webhook_auth.ensure_secret("plg-other--hook", owner="casa",
                               secrets_dir=secrets_dir)

    tools_mod._invalidate_lifecycle(artifact_id="art-1")

    assert not acks.is_acked(_ident_default())
    assert not (secrets_dir / eff).exists()
    # the unrelated plugin's ack + secret survive
    assert (secrets_dir / "plg-other--hook").exists()
    assert len(acks.revoke_plugin("other")) == 1


def test_invalidate_lifecycle_roles_only_keeps_acks(monkeypatch, tmp_path):
    """Unassign passes roles (no artifact_id): the identity-bound consent
    survives — re-assigning the SAME artifact+target+auth restores the route
    without a fresh prompt (the operator already approved that exact tuple)."""
    import tools as tools_mod
    import trigger_acks as trigger_acks_mod

    acks = TriggerAckStore(path=tmp_path / "acks.json")
    monkeypatch.setattr(trigger_acks_mod, "ACKS", acks)
    _ack(acks)
    tools_mod._invalidate_lifecycle(roles=["resident:assistant"])
    assert acks.is_acked(_ident_default())


def test_invalidate_lifecycle_survives_ack_store_failure(monkeypatch, tmp_path):
    """The grant/challenge purge must still run when the trigger-ack sweep
    blows up (never let Release B break the Release A invariants)."""
    import tools as tools_mod
    import trigger_acks as trigger_acks_mod

    class _Boom:
        def revoke_artifact(self, artifact_id):
            raise RuntimeError("store exploded")

    monkeypatch.setattr(trigger_acks_mod, "ACKS", _Boom())
    tools_mod._invalidate_lifecycle(artifact_id="art-1")  # must not raise


def _ident_default():
    return ack_identity(plugin="elevenlabs", artifact_id="art-1",
                        effective="plg-elevenlabs--voicemail",
                        target="resident:assistant", auth=AUTH)


# ---------------------------------------------------------------------------
# the trigger_ack_revoke tool — synchronous unroute
# ---------------------------------------------------------------------------


def _wire_revoke_env(monkeypatch, tmp_path, *, acked=True):
    """A live routed plugin + real registry overlay + patched runtime, so the
    tool exercises the REAL reconcile path (only the resolver is faked)."""
    import json as _json

    import agent as agent_mod
    import plugin_registry
    import tools as tools_mod
    import trigger_acks as trigger_acks_mod

    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    monkeypatch.setattr(trigger_acks_mod, "ACKS", acks)
    if acked:
        _ack(acks)
    p = _plugin()
    resolver = _resolver({None: [p], "resident:assistant": [p]})
    # #454: the reconcilers resolve through their own module seam, which now
    # returns a SNAPSHOT-PINNED resolver rather than calling
    # plugin_registry.resolve_all/resolve_for per target. Patching those two
    # functions no longer reaches the pass, so the fake registry is injected at
    # the seam the reconciler actually uses. BOTH seams: the revoke tool runs
    # the callback reconcile as a pair with the trigger one, and patching only
    # the trigger module would quietly resolve that half against the real,
    # empty snapshot instead of this fake registry.
    import callback_reconcile as cr_mod
    monkeypatch.setattr(tr, "_default_resolver", lambda: resolver)
    monkeypatch.setattr(cr_mod, "_default_resolver", lambda: resolver)
    monkeypatch.setattr(cr_mod, "_default_entries", lambda: (lambda: [
        {"name": "elevenlabs", "artifact_id": "art-1",
         "targets": ["resident:assistant"]}]))
    runtime = SimpleNamespace(
        trigger_registry=registry,
        role_configs=_role_configs(assistant=["webhook"]),
        channel_manager=None)
    monkeypatch.setattr(agent_mod, "active_runtime", runtime)
    monkeypatch.setattr(tr, "SECRETS_DIR", tmp_path / "webhook_secrets")
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health",
                        lambda extra: None)
    # route it via a real reconcile first
    return tools_mod, registry, acks, _json


async def test_trigger_ack_revoke_unroutes_synchronously(monkeypatch, tmp_path):
    tools_mod, registry, acks, _json = _wire_revoke_env(monkeypatch, tmp_path)
    await tr.reconcile_from_runtime(
        __import__("agent").active_runtime, prompt=False)
    eff = "plg-elevenlabs--voicemail"
    assert registry.get_webhook_target(eff) == "assistant"

    # the eager mint routed a secret; revoke must RETIRE it (Sol shipB-r1
    # P1-4: a kept secret would be inherited by the next artifact, since the
    # ack records revoke_artifact would need are deleted right here)
    secrets_dir = tmp_path / "webhook_secrets"
    assert (secrets_dir / eff).exists()

    r = await tools_mod.trigger_ack_revoke.handler({"name": "elevenlabs"})
    payload = _json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["revoked"] == 1
    assert payload["unrouted"] == [eff]
    assert payload["secrets_retired"] == [eff]
    # the route is GONE the moment the tool returns (immediate 404)
    assert registry.get_webhook_target(eff) is None
    assert not acks.is_acked(_ident_default())
    assert not (secrets_dir / eff).exists()


async def test_trigger_ack_revoke_unknown_plugin_is_idempotent(
    monkeypatch, tmp_path,
):
    tools_mod, registry, acks, _json = _wire_revoke_env(
        monkeypatch, tmp_path, acked=False)
    r = await tools_mod.trigger_ack_revoke.handler({"name": "ghost"})
    payload = _json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["revoked"] == 0
    assert payload["unrouted"] == []


async def test_mint_exception_fails_only_that_plugin(monkeypatch, tmp_path):
    """Terra shipB-r1 P1-2 (isolation half): one plugin's secret-mint
    blow-up drops THAT plugin with trigger_secret_missing; the other
    plugin's routes still swap in."""
    import webhook_auth

    registry = _registry()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    _ack(acks)
    _ack(acks, plugin="other", artifact_id="art-9", declared="hook")
    good = _plugin()
    other = _plugin(name="other", artifact_id="art-9",
                    triggers=[_trigger(name="hook")])
    real_ensure = webhook_auth.ensure_secret

    def _selective(name, **kw):
        if name.startswith("plg-other--"):
            raise OSError("disk full")
        return real_ensure(name, **kw)

    monkeypatch.setattr(webhook_auth, "ensure_secret", _selective)
    issues = await _reconcile(
        registry,
        plugins_by_target={None: [good, other],
                           "resident:assistant": [good, other]},
        role_configs=_role_configs(assistant=["webhook"]),
        acks=acks, tmp_path=tmp_path)
    assert [(i.name, i.reason_code) for i in issues] == [
        ("other", "trigger_secret_missing")]
    assert registry.get_webhook_target("plg-elevenlabs--voicemail") == "assistant"
    assert registry.get_webhook_target("plg-other--hook") is None


async def test_compute_failure_fails_closed_to_empty_overlay(
    monkeypatch, tmp_path,
):
    """Terra shipB-r1 P1-2 (backstop half): a total compute failure must not
    RETAIN the old overlay — swap empty (no plugin ingress) and propagate."""
    registry = _registry()
    registry.replace_plugin_overlay({
        "plg-stale--route": {"plugin": "stale", "role": "assistant",
                             "clearance": "public", "auth": AUTH}})
    acks = TriggerAckStore(path=tmp_path / "acks.json")

    def _boom_resolver(target):
        raise RuntimeError("resolver exploded")

    with pytest.raises(RuntimeError):
        await _reconcile(
            registry, plugins_by_target={},
            role_configs=_role_configs(assistant=["webhook"]),
            acks=acks, tmp_path=tmp_path, resolver=_boom_resolver)
    assert registry.get_webhook_target("plg-stale--route") is None


async def test_trigger_ack_revoke_cancels_pending_consent_keyboards(
    monkeypatch, tmp_path,
):
    """Terra shipB-r1 P1-1: a live consent keyboard for the plugin must die
    with the revoke — a stale Approve tap after trigger_ack_revoke can
    never re-ack/re-route."""
    import authz_grants
    import verdict_broker

    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    coord = authz_grants.ChallengeCoordinator()
    monkeypatch.setattr(authz_grants, "CHALLENGES", coord)
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "CHALLENGES", coord)

    tools_mod, registry, acks, _json = _wire_revoke_env(
        monkeypatch, tmp_path, acked=False)
    telegram = _FakeTelegram()
    runtime = __import__("agent").active_runtime
    runtime.channel_manager = _FakeChannelManager(telegram)
    # reconcile WITH prompting → a live pending consent keyboard
    await tr.reconcile_from_runtime(runtime)
    for _ in range(8):
        await asyncio.sleep(0)
    assert len(telegram.posts) == 1
    assert len(coord._entries) == 1
    ch = next(iter(coord._entries.values()))

    await tools_mod.trigger_ack_revoke.handler({"name": "elevenlabs"})
    # the broker record is cancelled: a late tap cannot claim it
    claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, option_index=0, actor_id=100)
    assert isinstance(claim, str)  # rejected (stale/duplicate), never a win
    assert not acks.is_acked(_ident_default())


async def test_revoke_kills_keyboard_posted_by_inflight_reconcile(
    monkeypatch, tmp_path,
):
    """Sol shipB-r2 P1-1: a reconcile already in flight (blocked in compute)
    when the revoke starts posts its consent keyboard AFTER the revoke's
    first cancel — the revoke's FINAL cancel (after its own reconcile, which
    serializes behind the in-flight one; prompts fire under the lock) must
    kill it, so no keyboard survives the revoke call."""
    import threading

    import authz_grants
    import verdict_broker

    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    coord = authz_grants.ChallengeCoordinator()
    monkeypatch.setattr(authz_grants, "CHALLENGES", coord)
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "CHALLENGES", coord)

    tools_mod, registry, acks, _json = _wire_revoke_env(
        monkeypatch, tmp_path, acked=False)
    telegram = _FakeTelegram()
    runtime = __import__("agent").active_runtime
    runtime.channel_manager = _FakeChannelManager(telegram)

    # make the FIRST resolve block until released (the in-flight reconcile
    # sits inside compute, holding _RECONCILE_LOCK, while the revoke runs).
    # #454: wrap the RECONCILER's resolver seam — the pass no longer calls
    # plugin_registry.resolve_all per target, it resolves one pinned snapshot
    # through this seam, so this is where the interleave has to be introduced.
    gate = threading.Event()
    blocked_once = threading.Event()
    real_resolver = tr._default_resolver()

    def _blocking(target=None):
        if not blocked_once.is_set():
            blocked_once.set()
            gate.wait(timeout=5)
        return real_resolver(target)

    monkeypatch.setattr(tr, "_default_resolver", lambda: _blocking)

    inflight = asyncio.create_task(tr.reconcile_from_runtime(runtime))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if blocked_once.is_set():
            break
    assert blocked_once.is_set()

    revoke = asyncio.create_task(
        tools_mod.trigger_ack_revoke.handler({"name": "elevenlabs"}))
    await asyncio.sleep(0.05)  # revoke: first cancel done, waiting on lock
    gate.set()                 # in-flight completes: swap + POST keyboard
    await revoke
    await inflight
    for _ in range(8):
        await asyncio.sleep(0)

    # the in-flight reconcile DID post a keyboard…
    assert len(telegram.posts) == 1
    # …but its broker record is dead: a late Approve tap cannot claim it
    _chat, rid, _text, _opts = telegram.posts[0]
    claim = broker.claim(namespace="resident_ask", scope="authz:100",
                         request_id=rid, option_index=0, actor_id=100)
    assert isinstance(claim, str)
    assert not acks.is_acked(_ident_default())


async def test_trigger_ack_revoke_unroutes_even_if_reconcile_fails(
    monkeypatch, tmp_path,
):
    """The immediate-404 guarantee must not depend on resolver health: a
    reconcile blow-up after the revoke still sweeps the plugin's overlay
    entries (names are injective: plg-<plugin>--…) before the tool returns."""
    tools_mod, registry, acks, _json = _wire_revoke_env(monkeypatch, tmp_path)
    await tr.reconcile_from_runtime(
        __import__("agent").active_runtime, prompt=False)
    eff = "plg-elevenlabs--voicemail"
    assert registry.get_webhook_target(eff) == "assistant"

    async def _boom(runtime, prompt=True):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(tr, "reconcile_from_runtime", _boom)
    r = await tools_mod.trigger_ack_revoke.handler({"name": "elevenlabs"})
    payload = _json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert registry.get_webhook_target(eff) is None
    assert not acks.is_acked(_ident_default())


async def test_reconcile_kicks_setup_worker(monkeypatch, tmp_path):
    # v0.112.0 (impl r5, Terra): EVERY reconcile that publishes the overlay
    # wakes the setup-episode worker so a pending episode gated on a
    # previously-down route dispatches once the route heals — not only the
    # consent finish hook. A plain (prompt=False, no consent) reconcile must
    # still kick.
    import plugin_setup_episodes
    kicked = {"n": 0}
    monkeypatch.setattr(plugin_setup_episodes, "kick",
                        lambda: kicked.__setitem__("n", kicked["n"] + 1))
    registry = _registry()
    await _reconcile(
        registry, plugins_by_target={}, role_configs={}, acks=TriggerAckStore(path=tmp_path / "acks.json"),
        tmp_path=tmp_path, prompt=False)
    assert kicked["n"] >= 1
