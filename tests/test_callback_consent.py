"""The callback-consent DM round and the UNION setup sealing.

A plugin callback opens ``GET /callback/<effective>`` only after the operator
taps Approve on a DM keyboard bound to the callback's consent identity
(plugin + effective name + declaration digest). The flavor mirrors
``trigger_consent``: the ack is persisted in the SYNCHRONOUS commit step (never
after an await), the decision feeds the durable setup-episode ledger, and no
agent continuation is ever dispatched.

The setup round is the shared piece: a plugin that declares BOTH a trigger and
a callback must have ONE round whose membership is the union of both kinds —
sealed before any keyboard posts — so an early Approve on the first keyboard
can never settle the round (and run the plugin's setup tool) while the other
consent is still open.
"""
import asyncio

from broker_helpers import wait_until
from types import SimpleNamespace

import pytest

import callback_consent as cc
import callback_reconcile as cr
import trigger_reconcile as tr
from authz_grants import ChallengeCoordinator
from callback_acks import CallbackAckStore
from plugin_callbacks import ack_identity, declaration_digest
from trigger_acks import TriggerAckStore
from trigger_registry import TriggerRegistry

DECLARED = "authorize"
EFFECTIVE = "plg-gmail--authorize"
DIGEST = declaration_digest({"declared": DECLARED, "effective": EFFECTIVE})
IDENTITY = ack_identity("gmail", EFFECTIVE, DIGEST)

TRIGGER_AUTH = {"mode": "static_header", "header": "X-API-Key",
                "tolerance_secs": 300, "secret_owner": "casa"}


class _FakeChannel:
    def __init__(self) -> None:
        self.posts: list = []
        self.edits: list = []
        self.dispatches: list = []
        self.chat_id = "100"

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        return 55

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return True

    async def _dispatch_button_continuation(self, **kw):
        self.dispatches.append(kw)
        return True


class _FakeChannelManager:
    def __init__(self, telegram=None):
        self._telegram = telegram

    def get(self, name):
        return self._telegram if name == "telegram" else None


def _fresh_env(monkeypatch, tmp_path):
    import verdict_broker
    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    coord = ChallengeCoordinator()
    channel = _FakeChannel()
    acks = CallbackAckStore(path=tmp_path / "callback_acks.json")
    return broker, coord, channel, acks


def _prompt(coord, channel, acks, *, reconcile_cb=None, **over):
    kw = dict(coordinator=coord, channel=channel, chat_id=100, operator_id=100,
              plugin="gmail", artifact_id="art-1", declared=DECLARED,
              effective=EFFECTIVE, declaration_digest=DIGEST, acks=acks,
              reconcile_cb=reconcile_cb)
    kw.update(over)
    return cc.prompt_callback_consent(**kw)


async def _settle(n: int = 8):
    for _ in range(n):
        await asyncio.sleep(0)


def _tap(broker, coord, key, idx, *, actor=100):
    ch = coord._entries[key]
    claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, option_index=idx, actor_id=actor)
    assert not isinstance(claim, str), f"claim rejected: {claim}"
    assert broker.commit(claim) is True
    step = ch.req.meta.get("on_commit_sync")
    if step is not None:
        step(idx)
    return ch


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------



def _released(plugin="gmail"):
    """#451: an obligation the worker may dispatch. The obligation row now
    EXISTS from the moment the reconciler records that Casa owes this artifact
    a setup run, so "setup is authorized" is `gate == "released"`, not merely
    the row's presence."""
    import plugin_setup_episodes as pse
    return [e for e in pse.episodes()
            if e.get("plugin") == plugin and e.get("gate") == "released"]

def test_message_is_the_verbatim_consent_prose():
    assert cc.render_callback_consent_message(
        plugin="gmail", effective=EFFECTIVE) == (
        "\U0001F510 Plugin callback consent\n\n"
        "Plugin 'gmail' wants to receive browser redirects at\n"
        "GET /callback/plg-gmail--authorize (authorization callback — no "
        "agent turn, no memory access).\n\n"
        "Approve to open it; Deny to leave it closed.")


async def test_prompt_posts_the_keyboard(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    handle = _prompt(coord, channel, acks)
    assert handle.created is True
    await handle.settled_post()
    chat_id, _rid, text, options = channel.posts[0]
    assert chat_id == 100
    assert options == ("Approve", "Deny")
    assert "GET /callback/plg-gmail--authorize" in text
    # a callback grants NO turn and NO memory access — say so, and never
    # borrow the trigger prose's role/clearance/auth vocabulary
    assert "no agent turn, no memory access" in text


async def test_duplicate_prompt_is_deduped(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    h1 = _prompt(coord, channel, acks)
    h2 = _prompt(coord, channel, acks)
    assert h1.created is True
    assert h2.created is False
    await h1.settled_post()
    assert len(channel.posts) == 1


async def test_operator_identity_is_the_trigger_consent_rule(monkeypatch,
                                                             tmp_path):
    ch = _FakeChannel()
    ch.chat_id = "-100123"       # a group chat: nobody is the operator
    assert cc.operator_identity(ch) is None
    ch.chat_id = "4242"
    assert cc.operator_identity(ch) == (4242, 4242)


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


async def test_approve_records_the_ack_synchronously(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    order: list = []

    async def _reconcile():
        order.append("reconcile")

    handle = _prompt(coord, channel, acks, reconcile_cb=_reconcile)
    await handle.settled_post()
    key = next(iter(coord._entries))
    _tap(broker, coord, key, 0)
    # persisted BEFORE any await — the commit step is the durability point
    assert acks.get(IDENTITY) is not None
    await _settle()
    assert order == ["reconcile"]
    assert any("✅" in e[2] for e in channel.edits)
    assert channel.dispatches == []          # never an agent continuation


async def test_ack_survives_a_crash_right_after_the_commit_step(
    monkeypatch, tmp_path,
):
    """Crash simulation: the process dies after the synchronous commit step
    and before the async finish hook. A fresh store load must still see the
    consent (otherwise the operator's tap is silently lost)."""
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    ch = coord._entries[next(iter(coord._entries))]
    claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, option_index=0, actor_id=100)
    assert broker.commit(claim) is True
    ch.req.meta["on_commit_sync"](0)
    # nothing else runs — reload the store from disk
    reloaded = CallbackAckStore(path=tmp_path / "callback_acks.json")
    assert reloaded.get(IDENTITY) is not None


async def test_deny_leaves_it_closed(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    fired: list = []

    async def _reconcile():
        fired.append(True)

    handle = _prompt(coord, channel, acks, reconcile_cb=_reconcile)
    await handle.settled_post()
    key = next(iter(coord._entries))
    _tap(broker, coord, key, 1)
    await _settle()
    assert acks.get(IDENTITY) is None
    assert fired == []
    assert any("❌" in e[2] for e in channel.edits)


async def test_expiry_leaves_it_closed(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cc, "CALLBACK_CONSENT_TTL_S", 0.02)
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    await asyncio.sleep(0.1)
    await _settle()
    assert acks.get(IDENTITY) is None
    assert any("⌛" in e[2] for e in channel.edits)


async def test_failed_ack_write_never_opens_the_route(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    fired: list = []

    async def _reconcile():
        fired.append(True)

    def _boom(**kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(acks, "record", _boom)
    handle = _prompt(coord, channel, acks, reconcile_cb=_reconcile)
    await handle.settled_post()
    ch = coord._entries[next(iter(coord._entries))]
    claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, option_index=0, actor_id=100)
    assert broker.commit(claim) is True
    with pytest.raises(RuntimeError):
        ch.req.meta["on_commit_sync"](0)      # the telegram handler swallows
    await _settle()
    assert fired == []
    assert any("internal error" in e[2] for e in channel.edits)


async def test_reconcile_failure_warns_but_keeps_the_ack(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)

    async def _boom():
        raise RuntimeError("reconcile failed")

    handle = _prompt(coord, channel, acks, reconcile_cb=_boom)
    await handle.settled_post()
    _tap(broker, coord, next(iter(coord._entries)), 0)
    await _settle()
    assert acks.get(IDENTITY) is not None
    assert "⚠️" in channel.edits[-1][2]


async def test_revoke_cancels_a_pending_keyboard(monkeypatch, tmp_path):
    """The revoke path kills a plugin's live consent keyboards, so a stale
    Approve tap can never undo it (``cancel_matching(plugin=…)``)."""
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    assert coord.cancel_matching(plugin="gmail") == 1
    await _settle()
    assert acks.get(IDENTITY) is None


async def test_lifecycle_cancel_by_artifact_kills_the_keyboard(monkeypatch,
                                                               tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    assert coord.cancel_matching(artifact="art-1") == 1
    await _settle()
    assert acks.get(IDENTITY) is None


# ---------------------------------------------------------------------------
# union sealing — one setup round, both consent kinds
# ---------------------------------------------------------------------------


def _both_kinds_plugin():
    return SimpleNamespace(
        name="gmail", artifact_id="art-1", path="/store/gmail/art-1",
        version="1.0.0", manifest_name="gmail",
        manifest={"name": "gmail", "casa": {
            "triggers": [{"name": "push", "type": "webhook",
                          "target": "resident:assistant",
                          "auth": {"mode": "static_header",
                                   "header": "X-API-Key"}}],
            "callbacks": [{"name": DECLARED}],
            "setupTool": "setup_gmail"}})


def _wire_episodes(monkeypatch, tmp_path, dispatched):
    import plugin_setup_episodes as pse
    monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
    monkeypatch.setattr(pse, "_lock", None)
    monkeypatch.setattr(pse, "_kick", None)

    async def _dispatch(role, instruction, ctx):
        dispatched.append((role, instruction, ctx))
        return True

    async def _notify(text):
        return None

    pse.configure(
        dispatch=_dispatch, notify_operator=_notify,
        resolve_registry_entry=lambda p: {
            "artifact_id": "art-1", "setup_tool": "setup_gmail",
            "granted_tools": ["gmailsrv"], "targets": ["resident:assistant"]},
        ack_lookup=lambda ident: None, routes_live=lambda p: True)
    return pse


async def test_one_union_round_settles_only_after_both_approvals(
    monkeypatch, tmp_path,
):
    """Union sealing. Approving the trigger first must NOT settle the
    round (and must not run the plugin's setup tool) while the callback
    consent is still open."""
    import authz_grants
    import verdict_broker
    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    coord = ChallengeCoordinator()
    monkeypatch.setattr(authz_grants, "CHALLENGES", coord)
    dispatched: list = []
    pse = _wire_episodes(monkeypatch, tmp_path, dispatched)

    p = _both_kinds_plugin()
    channel = _FakeChannel()
    cm = _FakeChannelManager(channel)
    trig_acks = TriggerAckStore(path=tmp_path / "trigger_acks.json")
    cb_acks = CallbackAckStore(path=tmp_path / "callback_acks.json")
    registry = TriggerRegistry(scheduler=None, app=None, bus=None)
    role_configs = {"assistant": SimpleNamespace(channels=["webhook"])}

    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[p], issues=[])

    def _entries():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]}]

    # Each reconciler computes the OTHER kind's union members through that
    # module's default seams (in production: the process ack singletons and
    # the live registry snapshot).
    monkeypatch.setattr(cr, "_default_acks", lambda: cb_acks)
    monkeypatch.setattr(cr, "_default_entries", lambda: _entries)
    monkeypatch.setattr(cr, "_base_url", lambda: "https://casa.example.org")
    monkeypatch.setattr(tr, "_default_acks", lambda: trig_acks)

    class _Spool:
        def ensure_plugin_dirs(self, plugin): pass
        def write_ready(self, plugin, payload): pass
        def delete_ready(self, plugin): pass
        def write_index_entry(self, path, payload): pass
        def delete_index_entry(self, path): pass

    # 1) the trigger reconcile seals the UNION round — BOTH kinds — and only
    #    then posts its own keyboard
    await tr.reconcile_plugin_triggers(
        trigger_registry=registry, role_configs=role_configs,
        channel_manager=cm, acks=trig_acks,
        secrets_dir=tmp_path / "webhook_secrets", prompt=True,
        resolver=_resolver, global_secret_ok=lambda: True)
    await _settle()
    members = pse._load()["rounds"]["gmail"]["members"]
    assert len(members) == 2
    assert IDENTITY in members

    # 2) the adversarial window: the operator taps Approve on the trigger
    #    keyboard BEFORE the callback reconcile has posted anything. The
    #    round must NOT settle and the setup tool must NOT run.
    trig_key = next(k for k, ch in coord._entries.items()
                    if ch.req.meta.get("kind") == "trigger_consent")
    _tap(broker, coord, trig_key, 0)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if pse.episodes():
            break
    assert _released() == []            # obligation holds: callback still open
    assert dispatched == []

    # 3) the callback reconcile posts the callback keyboard onto that round
    #    (the approved trigger member is not re-listed — its ack exists now —
    #    so it keeps its decision)
    await cr.reconcile_plugin_callbacks(
        trigger_registry=registry, role_configs=role_configs,
        channel_manager=cm, acks=cb_acks, spool=_Spool(),
        resolver=_resolver, entries=_entries, prompt=True)
    await _settle()
    # the decided trigger keyboard is gone; the callback one is now live
    kinds = sorted(ch.req.meta.get("kind") for ch in coord._entries.values())
    assert kinds == ["callback_consent"]
    round_members = pse._load()["rounds"]["gmail"]["members"]
    assert round_members[IDENTITY]["state"] == "open"
    assert sorted(m["state"] for m in round_members.values()) == [
        "approved", "open"]

    # 4) approve the CALLBACK — now the union settles and setup dispatches
    cb_key = next(k for k, ch in coord._entries.items()
                  if ch.req.meta.get("kind") == "callback_consent")
    _tap(broker, coord, cb_key, 0)
    # Wait on the OBSERVABLE settle, not a 1 s poll cap that loses under the
    # loaded parallel gate (#794). This is a POSITIVE wait — the union must
    # settle here — so wait_until (raises on genuine non-settle) is right; the
    # step-2 adversarial poll above stays a bounded poll because it asserts
    # that nothing happens.
    await wait_until(lambda: bool(_released()))
    assert [e["status"] for e in _released()] == ["pending"]
    await pse._worker_pass()
    assert len(dispatched) == 1
    assert "setup_gmail" in dispatched[0][1]


async def test_callback_approval_is_recorded_in_the_round(monkeypatch,
                                                          tmp_path):
    """The approval must land in the setup ledger under the CALLBACK identity,
    inside the same yield-free commit step that persists the ack."""
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    pse = _wire_episodes(monkeypatch, tmp_path, [])
    pse.open_round(plugin="gmail", artifact_id="art-1", identities=[IDENTITY])
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    _tap(broker, coord, next(iter(coord._entries)), 0)
    member = pse._load()["rounds"]["gmail"]["members"][IDENTITY]
    assert member["state"] == "approved"
    assert member["gen"] == acks.get(IDENTITY)["gen"]


async def test_union_membership_is_computed_off_the_event_loop(
    monkeypatch, tmp_path,
):
    """The union compute reads plugin.json for every resolved
    plugin. Both reconcilers to_thread their main compute for exactly that
    reason — the union half must ride in the SAME worker thread, never on the
    loop under the reconcile lock."""
    import threading

    import authz_grants
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(authz_grants, "CHALLENGES", ChallengeCoordinator())
    _wire_episodes(monkeypatch, tmp_path, [])

    p = _both_kinds_plugin()
    cm = _FakeChannelManager(_FakeChannel())
    registry = TriggerRegistry(scheduler=None, app=None, bus=None)
    role_configs = {"assistant": SimpleNamespace(channels=["webhook"])}

    def _resolver(target):
        return SimpleNamespace(registry_valid=True, plugins=[p], issues=[])

    def _entries():
        return [{"name": "gmail", "artifact_id": "art-1",
                 "targets": ["resident:assistant"]}]

    seen: dict[str, str] = {}

    def _spy_callback_union(**kw):
        seen["trigger_side"] = threading.current_thread().name
        return True, [], set()

    def _spy_trigger_union(**kw):
        seen["callback_side"] = threading.current_thread().name
        return True, [], set()

    monkeypatch.setattr(cr, "callback_pending_for_union", _spy_callback_union)
    monkeypatch.setattr(tr, "trigger_pending_for_union", _spy_trigger_union)
    monkeypatch.setattr(cr, "_default_acks",
                        lambda: CallbackAckStore(path=tmp_path / "cb.json"))
    monkeypatch.setattr(cr, "_default_entries", lambda: _entries)
    monkeypatch.setattr(cr, "_base_url", lambda: "https://casa.example.org")

    class _Spool:
        def ensure_plugin_dirs(self, plugin): pass
        def write_ready(self, plugin, payload): pass
        def delete_ready(self, plugin): pass
        def write_index_entry(self, path, payload): pass
        def delete_index_entry(self, path): pass

    await tr.reconcile_plugin_triggers(
        trigger_registry=registry, role_configs=role_configs,
        channel_manager=cm,
        acks=TriggerAckStore(path=tmp_path / "trig.json"),
        secrets_dir=tmp_path / "webhook_secrets", prompt=True,
        resolver=_resolver, global_secret_ok=lambda: True)
    await cr.reconcile_plugin_callbacks(
        trigger_registry=registry, role_configs=role_configs,
        channel_manager=cm,
        acks=CallbackAckStore(path=tmp_path / "cb.json"), spool=_Spool(),
        resolver=_resolver, entries=_entries, prompt=True)
    await _settle()

    main = threading.main_thread().name
    assert set(seen) == {"trigger_side", "callback_side"}
    assert seen["trigger_side"] != main
    assert seen["callback_side"] != main


def test_seal_setup_state_unions_both_kinds(monkeypatch, tmp_path):
    import plugin_setup_episodes as pse
    monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
    trigger_pending = [{
        "plugin": "gmail", "artifact_id": "art-1", "effective": "plg-gmail--push",
        "target": "resident:assistant", "auth": TRIGGER_AUTH,
        "clearance": "public"}]
    callback_pending = [{
        "plugin": "gmail", "artifact_id": "art-1", "declared": DECLARED,
        "effective": EFFECTIVE, "declaration_digest": DIGEST,
        "identity": IDENTITY}]
    nonces = tr.seal_setup_state(trigger_pending=trigger_pending,
                                 callback_pending=callback_pending,
                                 pending_complete=True, candidates=[])
    members = pse._load()["rounds"]["gmail"]["members"]
    assert len(members) == 2
    assert IDENTITY in members and IDENTITY in nonces


def test_seal_setup_state_survives_a_ledger_failure(monkeypatch, tmp_path):
    import plugin_setup_episodes as pse

    def _boom(**kw):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(pse, "open_round", _boom)
    assert tr.seal_setup_state(
        trigger_pending=[], pending_complete=True, candidates=[],
        callback_pending=[{
            "plugin": "gmail", "artifact_id": "art-1", "declared": DECLARED,
            "effective": EFFECTIVE, "declaration_digest": DIGEST,
            "identity": IDENTITY}]) == {}
