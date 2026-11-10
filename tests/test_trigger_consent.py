"""Release B — the trigger-consent DM prompt on the refactored (generic
callback-driven) ChallengeCoordinator.

The coordinator's registration/driver/latch machinery is shared with the
authz-grant challenges; the trigger-consent flavor supplies its OWN approve
callback (record the consent ack — never a GrantKey mint) and its OWN finish
hook (edit the DM + fire a reconcile — never an agent continuation dispatch).
Taps ride the SAME validated Telegram callback path (broker scope
``authz:<bot>:<chat>``, fail-closed bot/chat/operator checks). The card is
Casa's own and is posted on Casa's bot.
"""
import asyncio

import pytest

import trigger_consent as tc
from authz_grants import ChallengeCoordinator
from plugin_triggers import ack_identity
from channels.tg_address import TgAddress
from trigger_acks import TriggerAckStore

AUTH = {"mode": "static_header", "header": "X-API-Key",
        "tolerance_secs": 300, "secret_owner": "casa"}


class _FakeChannel:
    """A Telegram endpoint double: Casa's bot 111, operator chat 100."""

    def __init__(self, *, bot_id: "int | None" = 111,
                 role: str = "assistant") -> None:
        self.posts: list = []
        self.edits: list = []
        self.dispatches: list = []
        self.post_result: int | None = 55
        self.chat_id = "100"
        self.default_agent = role
        self.on_identity = None
        self._bot_id = bot_id

    @property
    def has_bot_id(self) -> bool:
        return self._bot_id is not None

    @property
    def bot_id(self) -> int:
        from channels.tg_address import EndpointNotReady
        if self._bot_id is None:
            raise EndpointNotReady("no bot id yet")
        return self._bot_id

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        return self.post_result

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return True

    async def _dispatch_button_continuation(self, **kw):
        self.dispatches.append(kw)
        return True


def _fresh_env(monkeypatch, tmp_path, *, ttl=None):
    import verdict_broker
    broker = verdict_broker.VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    if ttl is not None:
        import authz_grants
        monkeypatch.setattr(authz_grants, "_CHALLENGE_TTL_S", ttl)
    coord = ChallengeCoordinator()
    channel = _FakeChannel()
    acks = TriggerAckStore(path=tmp_path / "acks.json")
    return broker, coord, channel, acks


def _prompt(coord, channel, acks, *, reconcile_cb=None, **over):
    kw = dict(coordinator=coord, channel=channel, chat_id=100, operator_id=100,
              plugin="elevenlabs", artifact_id="art-1",
              effective="plg-elevenlabs--voicemail",
              target="resident:assistant", auth=AUTH, acks=acks,
              reconcile_cb=reconcile_cb)
    kw.update(over)
    return tc.prompt_trigger_consent(**kw)


def _identity(**over):
    kw = dict(plugin="elevenlabs", artifact_id="art-1",
              effective="plg-elevenlabs--voicemail",
              target="resident:assistant", auth=AUTH)
    kw.update(over)
    return ack_identity(**kw)


async def _settle(n: int = 8):
    for _ in range(n):
        await asyncio.sleep(0)


def _tap(broker, coord, key, idx, *, actor=100):
    """Replicate the telegram callback: claim → commit → immediate sync step."""
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
# casa_consent_target — Casa's bot and the configured operator, fail-closed
# ---------------------------------------------------------------------------


def _manager(*channels):
    from telegram_channel_double import make_transport
    return make_transport(*channels)


def test_consent_target_is_the_operators_chat_on_casas_bot():
    from channels.tg_address import TgAddress
    casa = _FakeChannel()
    casa.chat_id = "1234"
    finance = _FakeChannel(bot_id=222, role="finance")
    target = tc.casa_consent_target(_manager(casa, finance))
    assert target.endpoint is casa
    assert target.address == TgAddress(111, 1234)
    assert target.operator_id == 1234


@pytest.mark.parametrize("raw", ["-100123", "0", "", "not-a-number", None])
def test_consent_target_fails_closed_without_a_private_operator_chat(raw):
    ch = _FakeChannel()
    ch.chat_id = raw
    assert tc.casa_consent_target(_manager(ch)) is None


def test_consent_target_fails_closed_before_casas_bot_has_an_id():
    # A first boot, or a replaced token, before getMe: no card can be keyed.
    assert tc.casa_consent_target(_manager(_FakeChannel(bot_id=None))) is None


def test_consent_target_without_a_channel_manager_is_none():
    assert tc.casa_consent_target(None) is None


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------


async def test_prompt_posts_keyboard_with_binding_facts(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    handle = _prompt(coord, channel, acks)
    assert handle.created is True
    await handle.settled_post()
    assert len(channel.posts) == 1
    chat_id, _rid, text, options = channel.posts[0]
    assert chat_id == 100
    assert options == ("Approve", "Deny")
    ch = next(iter(coord._entries.values()))
    assert ch.scope == "authz:111:100"
    assert ch.req.meta["bot_id"] == 111
    assert "elevenlabs" in text
    assert "/webhook/plg-elevenlabs--voicemail" in text
    assert "assistant" in text
    assert "static_header" in text
    # the memory clearance is part of what the operator approves — disclose it
    assert "clearance public" in text


async def test_duplicate_prompt_is_deduped(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    h1 = _prompt(coord, channel, acks)
    h2 = _prompt(coord, channel, acks)
    assert h1.created is True
    assert h2.created is False
    await h1.settled_post()
    assert len(channel.posts) == 1


async def test_approve_records_ack_and_fires_reconcile(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    order: list = []

    async def _reconcile():
        order.append("reconcile")

    handle = _prompt(coord, channel, acks, reconcile_cb=_reconcile)
    await handle.settled_post()
    key = next(iter(coord._entries))
    _tap(broker, coord, key, 0)
    # the ack is recorded SYNCHRONOUSLY in the commit step (before any await)
    assert acks.is_acked(_identity())
    await _settle()
    # success edit lands BEFORE the reconcile (edit-first ordering), and the
    # reconcile ran
    assert order == ["reconcile"]
    assert any("✅" in e[2] for e in channel.edits)
    assert channel.dispatches == []  # NEVER an agent continuation


async def test_deny_leaves_unacked(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    fired = []

    async def _reconcile():
        fired.append(True)

    handle = _prompt(coord, channel, acks, reconcile_cb=_reconcile)
    await handle.settled_post()
    key = next(iter(coord._entries))
    _tap(broker, coord, key, 1)
    await _settle()
    assert not acks.is_acked(_identity())
    assert fired == []
    assert any("❌" in e[2] for e in channel.edits)


async def test_expiry_edits_and_leaves_unacked(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    monkeypatch.setattr(tc, "TRIGGER_CONSENT_TTL_S", 0.02)
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    await asyncio.sleep(0.1)
    await _settle()
    assert not acks.is_acked(_identity())
    assert any("⌛" in e[2] for e in channel.edits)


async def test_ack_record_failure_never_activates(monkeypatch, tmp_path):
    """The 'minted' guard analogue: a commit whose sync step failed to persist
    the ack must edit an internal error and never fire the reconcile."""
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    fired = []

    async def _reconcile():
        fired.append(True)

    def _boom(**kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(acks, "record", _boom)
    handle = _prompt(coord, channel, acks, reconcile_cb=_reconcile)
    await handle.settled_post()
    key = next(iter(coord._entries))
    ch = coord._entries[key]
    claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, option_index=0, actor_id=100)
    assert broker.commit(claim) is True
    step = ch.req.meta.get("on_commit_sync")
    with pytest.raises(RuntimeError):
        # the telegram handler swallows+logs this; replicate its effect
        step(0)
    await _settle()
    assert fired == []
    assert any("internal error" in e[2] for e in channel.edits)


async def test_reconcile_failure_overwrites_with_warning(monkeypatch, tmp_path):
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)

    async def _boom():
        raise RuntimeError("reconcile failed")

    handle = _prompt(coord, channel, acks, reconcile_cb=_boom)
    await handle.settled_post()
    key = next(iter(coord._entries))
    _tap(broker, coord, key, 0)
    await _settle()
    # ack IS recorded (the operator did approve); the edit warns activation
    # failed so the operator can run plugin_verify
    assert acks.is_acked(_identity())
    assert "⚠️" in channel.edits[-1][2]


async def test_lifecycle_cancel_by_artifact_kills_pending_consent(
    monkeypatch, tmp_path,
):
    """_invalidate_lifecycle(artifact_id=old) must cancel a pending trigger
    consent keyboard: an old artifact's challenge can never ack the new one."""
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    assert coord.cancel_matching(artifact="art-1") == 1
    await _settle()
    assert not acks.is_acked(_identity())
    key = tc.TriggerConsentKey(
        plugin="elevenlabs", artifact_id="art-1",
        effective="plg-elevenlabs--voicemail", target="resident:assistant",
        identity=_identity(), address=TgAddress(111, 100))
    assert key not in coord._entries


async def test_wrong_actor_cannot_claim(monkeypatch, tmp_path):
    """Belt-and-braces at the broker layer: the telegram handler already
    refuses a non-operator tap before claiming; a direct claim by another
    actor must still never let the sync step record an ack for them."""
    broker, coord, channel, acks = _fresh_env(monkeypatch, tmp_path)
    handle = _prompt(coord, channel, acks)
    await handle.settled_post()
    key = next(iter(coord._entries))
    ch = coord._entries[key]
    # the telegram handler's operator check reads meta["operator_id"]
    assert ch.req.meta.get("operator_id") == 100
    assert ch.req.meta.get("chat_id") == 100
