"""Operator questions, approvals and consent cards across two bots.

The operator's private chats with two bots share one chat id, so every lane
that used to be keyed by the chat alone is keyed by the bot too: a question,
a challenge or a grant on bot 111 is untouched by anything that happens on
bot 222, and the other way round. Casa's own consent cards always go through
Casa's bot, whichever bot the turn that caused them came from. A scheduled
question keeps the bot it was posted through across a restart.

Uses the two-endpoint harness (bot 111 is Casa's, role ``assistant``; bot 222
is ``finance``; both serve operator 100).
"""
from __future__ import annotations

import asyncio
import types

import pytest

import authz_grants
import scheduled_asks
import tools as tools_mod
import verdict_broker
from authz_grants import (
    AuthzDeps, ChallengeCoordinator, GrantKey, GrantStore, _DENY_DELIVERY_FAILED,
    _DENY_POSTED, canonical_args_hash, make_resident_authz_hook,
    resolve_grant_identity,
)
from channels.tg_address import CASA, TgAddress, scope
from verdict_broker import VerdictBroker

from test_result_broker import _Origin
from two_endpoints import (
    CASA_BOT, FINANCE_BOT, OPERATOR, FakeEndpoint, build_two_endpoints,
)

pytestmark = pytest.mark.asyncio

TOOL = "mcp__plugin_p_p__invoice_reset"
ARTIFACT = "artifact-1"
PROTECTED = {TOOL: {"artifact_id": ARTIFACT, "summary": None}}


class _Endpoint(FakeEndpoint):
    """A two-endpoint fake that also posts and edits keyboards and carries
    continuations, recording each on its own bot."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.posts: list[tuple] = []
        self.edits: list[tuple] = []
        self.continuations: list[dict] = []

    def _user_id_is_operator(self, user_id) -> bool:
        return user_id == OPERATOR

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               short_labels=False, shorts=None, entities=None):
        self.posts.append((chat_id, request_id, str(text), tuple(options)))
        return self._mid()

    async def edit_dm_message(self, chat_id, message_id, text, literal=False):
        self.edits.append((chat_id, message_id, text))
        return True

    async def _dispatch_scheduled_continuation(self, **kw):
        self.continuations.append(kw)
        return True


def _two(*, casa_id: "int | None" = CASA_BOT, casa_ready: bool = True):
    casa = _Endpoint("assistant", bot_id=casa_id, is_casa=True, ready=casa_ready)
    finance = _Endpoint("finance", bot_id=FINANCE_BOT)
    return build_two_endpoints(casa=casa, finance=finance)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    broker = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    monkeypatch.setattr(scheduled_asks, "_ROLE_EPOCHS", {})
    monkeypatch.setattr(scheduled_asks, "_TRIGGER_EPOCHS", {})
    monkeypatch.setattr(scheduled_asks, "_BOOT_REVOCATIONS", [])
    monkeypatch.setattr(scheduled_asks, "_BOOT_RECONCILED", False)
    store = scheduled_asks.ScheduledAskStore(str(tmp_path / "scheduled_asks.json"))
    monkeypatch.setattr(scheduled_asks, "STORE", store)
    return broker


def _register(bot: int, *, rid: str, kind: str = "dm", scheduled: bool = False):
    meta = {"options": ["Yes", "No"], "bot_id": bot, "chat_id": OPERATOR,
            "operator_id": OPERATOR, "_scope": scope(kind, bot, OPERATOR)}
    if scheduled:
        meta["scheduled"] = True
    req, created = verdict_broker.BROKER.register(
        namespace="resident_ask", scope=scope(kind, bot, OPERATOR),
        request_id=rid, timeout_s=300.0, detached=True, meta=meta)
    assert created is True
    return req


def _pending(bot: int, kind: str = "dm") -> list:
    return verdict_broker.BROKER.pending(
        namespace="resident_ask", scope=scope(kind, bot, OPERATOR))


def _key(bot: int) -> GrantKey:
    return GrantKey(operator_id=OPERATOR, bot_id=bot, chat_id=OPERATOR,
                    enforcement_role="finance", artifact_id=ARTIFACT,
                    tool_name=TOOL, args_hash=canonical_args_hash({}))


# ---------------------------------------------------------------------------
# Lanes and /new
# ---------------------------------------------------------------------------


async def test_new_on_one_bot_leaves_the_other_bots_ask_challenge_and_grant():
    """``/new`` on 222 cancels its own lanes and grants, and nothing of 111's
    (the same calls the endpoint's ``/new`` makes, on its own bot)."""
    _register(CASA_BOT, rid="ask-111")
    _register(CASA_BOT, rid="sched-111", scheduled=True)
    _register(CASA_BOT, rid="authz-111", kind="authz")
    _register(FINANCE_BOT, rid="ask-222")
    _register(FINANCE_BOT, rid="authz-222", kind="authz")
    grants = GrantStore()
    grants.mint(_key(CASA_BOT))
    grants.mint(_key(FINANCE_BOT))

    broker = verdict_broker.BROKER
    for kind in ("dm", "authz"):
        broker.cancel_scope(namespace="resident_ask",
                            scope=scope(kind, FINANCE_BOT, OPERATOR),
                            reason="new_session")
    assert grants.purge_chat(FINANCE_BOT, OPERATOR) == 1
    assert scheduled_asks.cancel_non_scheduled_for_chat(
        FINANCE_BOT, OPERATOR, "typed_answer") == 0
    assert scheduled_asks.cancel_for_chat(
        FINANCE_BOT, OPERATOR, "new_session") == 0

    assert sorted(_pending(CASA_BOT)) == ["ask-111", "sched-111"]
    assert _pending(CASA_BOT, "authz") == ["authz-111"]
    assert _pending(FINANCE_BOT) == [] and _pending(FINANCE_BOT, "authz") == []
    assert grants.consume(_key(CASA_BOT)) is True
    assert grants.consume(_key(FINANCE_BOT)) is False


async def test_a_typed_reply_on_one_bot_cancels_only_that_bots_question():
    _register(CASA_BOT, rid="ask-111")
    _register(FINANCE_BOT, rid="ask-222")
    assert scheduled_asks.cancel_non_scheduled_for_chat(
        CASA_BOT, OPERATOR, "typed_answer") == 1
    assert _pending(CASA_BOT) == [] and _pending(FINANCE_BOT) == ["ask-222"]


async def test_a_pending_question_on_one_bot_does_not_block_the_other():
    """A scheduled question on 222 waits for 222's lane only: the human
    question and the challenge open on 111 do not make it busy."""
    _register(CASA_BOT, rid="human-111")
    _register(CASA_BOT, rid="authz-111", kind="authz")
    rec = {"rid": "sched-222", "bot_id": FINANCE_BOT, "chat_id": OPERATOR,
           "operator_id": OPERATOR, "role": "finance", "session_scope": "cron-x",
           "scope": scope("dm", FINANCE_BOT, OPERATOR), "options": ["Yes", "No"]}
    req, created = verdict_broker.BROKER.register(
        namespace="resident_ask", scope=rec["scope"], request_id=rec["rid"],
        timeout_s=300.0, detached=True, supersede=False, require_idle=True,
        idle_scopes=(scope("authz", FINANCE_BOT, OPERATOR),),
        meta=scheduled_asks.broker_meta(rec))
    assert req is not None and created is True
    assert req.meta["bot_id"] == FINANCE_BOT
    # …and a displacement on 111 leaves 222's scheduled question alone
    assert scheduled_asks.displace_scheduled_for_chat(
        CASA_BOT, OPERATOR, "operator_challenge") == 0
    assert _pending(FINANCE_BOT) == ["sched-222"]
    assert scheduled_asks.displace_scheduled_for_chat(
        FINANCE_BOT, OPERATOR, "operator_challenge") == 1


# ---------------------------------------------------------------------------
# Scheduled asks keep their bot across a restart
# ---------------------------------------------------------------------------


def _record(**over) -> dict:
    rec = {"rid": "rid-222", "state": scheduled_asks.STATE_LIVE,
           "role": "finance", "session_scope": "cron-x",
           "scope": scope("dm", FINANCE_BOT, OPERATOR), "bot_id": FINANCE_BOT,
           "chat_id": OPERATOR, "operator_id": OPERATOR, "message_id": 77,
           "options": ["Yes", "No"], "body": "Pay it?", "epoch": None,
           "created_at": 0.0, "expires_at": 1_000.0}
    rec.update(over)
    return rec


async def test_a_stored_question_round_trips_its_bot(tmp_path):
    path = str(tmp_path / "asks.json")
    store = scheduled_asks.ScheduledAskStore(path)
    await store.put(_record())
    reloaded = scheduled_asks.ScheduledAskStore(path)
    assert reloaded.get("rid-222") == _record()


@pytest.mark.parametrize("bot", ["missing", None, CASA, "222", True])
async def test_a_stored_question_without_a_bot_is_refused_at_load(tmp_path, bot):
    path = str(tmp_path / "asks.json")
    store = scheduled_asks.ScheduledAskStore(path)
    bad = _record(rid="bad")
    if bot == "missing":
        del bad["bot_id"]
    else:
        bad["bot_id"] = bot
    await store.put(bad)
    await store.put(_record())
    reloaded = scheduled_asks.ScheduledAskStore(path)
    assert [r["rid"] for r in reloaded.all()] == ["rid-222"]


async def test_boot_restores_a_question_on_its_own_bot():
    """The record says 222 while Casa's bot is 111: the question is restored
    in 222's lane, and nothing is posted or edited on 111."""
    te = _two()
    await scheduled_asks.STORE.put(_record())
    counts = await scheduled_asks.reconcile_at_boot(te.transport, now=0.0)
    assert counts["restored"] == 1
    assert _pending(FINANCE_BOT) == ["rid-222"]
    meta = verdict_broker.BROKER.get_meta(
        namespace="resident_ask", scope=scope("dm", FINANCE_BOT, OPERATOR),
        request_id="rid-222")
    assert meta["bot_id"] == FINANCE_BOT
    assert _pending(CASA_BOT) == []
    assert te.casa.edits == [] and te.casa.continuations == []


async def test_boot_settles_an_expired_question_through_its_own_bot():
    te = _two()
    await scheduled_asks.STORE.put(_record(expires_at=1.0))
    counts = await scheduled_asks.reconcile_at_boot(te.transport, now=5.0)
    assert counts["expired"] == 1
    assert [e[:2] for e in te.finance.edits] == [(OPERATOR, 77)]
    assert len(te.finance.continuations) == 1
    assert te.casa.edits == [] and te.casa.continuations == []


async def test_boot_drops_a_question_whose_bot_has_no_endpoint():
    te = _two()
    await scheduled_asks.STORE.put(_record(bot_id=333,
                                           scope=scope("dm", 333, OPERATOR)))
    counts = await scheduled_asks.reconcile_at_boot(te.transport, now=0.0)
    assert counts["no_endpoint"] == 1
    assert scheduled_asks.STORE.all() == []
    assert te.casa.edits == te.finance.edits == []
    assert te.casa.continuations == te.finance.continuations == []


async def test_a_revocation_in_the_boot_window_settles_only_its_bots_record():
    te = _two()
    await scheduled_asks.STORE.put(_record())
    await scheduled_asks.STORE.put(_record(
        rid="rid-111", bot_id=CASA_BOT, role="assistant",
        scope=scope("dm", CASA_BOT, OPERATOR)))
    scheduled_asks.cancel_for_chat(FINANCE_BOT, OPERATOR, "new_session")
    counts = await scheduled_asks.reconcile_at_boot(te.transport, now=0.0)
    assert counts["revoked_before_reconcile"] == 1 and counts["restored"] == 1
    assert _pending(CASA_BOT) == ["rid-111"] and _pending(FINANCE_BOT) == []


# ---------------------------------------------------------------------------
# Casa's consent cards go through Casa's bot
# ---------------------------------------------------------------------------


async def test_consent_cards_go_to_casas_bot_even_from_another_bots_turn(
        tmp_path, monkeypatch):
    import trigger_reconcile
    from trigger_acks import TriggerAckStore

    te = _two()
    monkeypatch.setattr(authz_grants, "CHALLENGES", ChallengeCoordinator())
    pending = [{"plugin": "elevenlabs", "artifact_id": "art-1",
                "effective": "plg-elevenlabs--voicemail",
                "target": "resident:finance",
                "auth": {"mode": "static_header", "header": "X-API-Key",
                         "tolerance_secs": 300, "secret_owner": "casa"}}]
    turn_on_222 = {"role": "finance", "execution_role": "finance",
                   "channel": "telegram", "chat_id": str(OPERATOR),
                   "user_id": OPERATOR, "message_type": "channel_in",
                   "source": "telegram", "_tg_bot": FINANCE_BOT}
    with _Origin(turn_on_222):
        trigger_reconcile._fire_consent_prompts(
            pending, trigger_registry=None, role_configs={},
            channel_manager=te.manager,
            acks=TriggerAckStore(path=tmp_path / "acks.json"),
            secrets_dir=tmp_path, resolver=None, global_secret_ok=None,
            nonce_by_identity={})
        for _ in range(8):
            await asyncio.sleep(0)
    assert len(te.casa.posts) == 1 and te.finance.posts == []
    [entry] = authz_grants.CHALLENGES._entries.values()
    assert entry.key.address == TgAddress(CASA_BOT, OPERATOR)
    assert entry.scope == scope("authz", CASA_BOT, OPERATOR)
    assert entry.req.meta["bot_id"] == CASA_BOT


async def test_no_consent_card_before_casas_bot_has_an_id():
    import trigger_consent
    te = _two(casa_id=None, casa_ready=False)
    assert trigger_consent.casa_consent_target(te.manager) is None


# ---------------------------------------------------------------------------
# The grant identity: the bot of the endpoint the route resolves to
# ---------------------------------------------------------------------------


def _dm(route, role="finance") -> dict:
    return {"role": role, "execution_role": role, "channel": "telegram",
            "chat_id": str(OPERATOR), "user_id": OPERATOR, "cid": "c",
            "message_type": "channel_in", "source": "telegram",
            "_tg_bot": route}


def _setup(route) -> dict:
    return {**_dm(route, "assistant"), "synthetic": "plugin_setup",
            "plugin_setup_target": "assistant"}


async def test_a_turn_on_222_is_approved_on_222(monkeypatch):
    te = _two()
    monkeypatch.setattr(tools_mod, "_channel_manager", te.manager, raising=False)
    with _Origin(_dm(FINANCE_BOT)):
        identity, why = resolve_grant_identity("finance", ARTIFACT)
    assert why is None and identity.bot_id == FINANCE_BOT

    coord = ChallengeCoordinator()
    hook = make_resident_authz_hook(
        "finance", PROTECTED,
        lambda: AuthzDeps(channel=te.transport, grants=GrantStore(),
                          challenges=coord))
    with _Origin(_dm(FINANCE_BOT)):
        out = await hook({"tool_name": TOOL, "tool_input": {}}, "c-1", {})
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == _DENY_POSTED
    assert len(te.finance.posts) == 1 and te.casa.posts == []
    [entry] = coord._entries.values()
    assert entry.key.bot_id == FINANCE_BOT
    assert entry.scope == scope("authz", FINANCE_BOT, OPERATOR)
    assert entry.req.meta["bot_id"] == FINANCE_BOT


async def test_an_engagement_is_approved_on_its_origins_bot(monkeypatch):
    te = _two()
    monkeypatch.setattr(tools_mod, "_channel_manager", te.manager, raising=False)
    rec = types.SimpleNamespace(
        id="eng-1", kind="specialist", status="active", role_or_type="finance",
        origin={"user_id": OPERATOR, "chat_id": str(OPERATOR),
                "_tg_bot": CASA_BOT})
    with _Origin(_dm(FINANCE_BOT), engagement=rec):
        identity, why = resolve_grant_identity("finance")
    assert why is None and identity.bot_id == CASA_BOT
    assert identity.engagement_id == "eng-1"


async def test_a_route_with_no_endpoint_yields_no_identity(monkeypatch):
    te = _two()
    monkeypatch.setattr(tools_mod, "_channel_manager", te.manager, raising=False)
    with _Origin(_dm(333)):
        assert resolve_grant_identity("finance") == (None, "unsupported_origin")
    o = _dm(FINANCE_BOT)
    del o["_tg_bot"]
    with _Origin(o):
        assert resolve_grant_identity("finance") == (None, "unsupported_origin")


async def test_before_casas_bot_has_an_id_a_protected_call_registers_nothing(
        monkeypatch):
    """A setup turn born before Casa's bot has an id carries the route CASA.
    Its protected call yields no identity (``channel_unavailable``), the hook
    answers as for a challenge that could not be posted, and no grant,
    challenge or broker request exists. Once the bot's id is observed, the
    same call is keyed on that bot."""
    te = _two(casa_id=None, casa_ready=False)
    monkeypatch.setattr(tools_mod, "_channel_manager", te.manager, raising=False)
    grants, coord = GrantStore(), ChallengeCoordinator()
    hook = make_resident_authz_hook(
        "assistant", PROTECTED,
        lambda: AuthzDeps(channel=te.transport, grants=grants, challenges=coord))

    with _Origin(_setup(CASA)):
        assert resolve_grant_identity("assistant") == (None, "channel_unavailable")
        out = await hook({"tool_name": TOOL, "tool_input": {}}, "c-1", {})
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == \
        _DENY_DELIVERY_FAILED
    assert grants._grants == {} and coord._entries == {}
    assert verdict_broker.BROKER._live == {}
    assert te.casa.posts == [] and te.finance.posts == []

    assert te.casa.initialize(CASA_BOT) is True
    with _Origin(_setup(CASA)):
        identity, why = resolve_grant_identity("assistant")
    assert why is None and identity.bot_id == CASA_BOT
