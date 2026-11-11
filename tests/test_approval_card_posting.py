"""An approval challenge registered with a card request gets its card written
inside the coordinator's own post driver, then admitted and posted with the
bold entities Casa computed -- and is never posted for a request that already
settled.

Every case drives the real ``ChallengeCoordinator`` against a fresh real
``VerdictBroker``; the model call is replaced at ``approval_card._ask`` (or,
where a case needs to act at the exact moment the card is ready,
``approval_card.write_card``). Counts are asserted, never statuses.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

import agent as agent_mod
import approval_card
import authz_grants
import output_boundary as ob
import verdict_broker
from approval_card import Card, CardLine, CardRequest, render_card
from text_util import utf16_len
from verdict_broker import VerdictBroker

pytestmark = pytest.mark.asyncio

CASA_BOT = 111
OPERATOR = 4242
TOOL = "mcp__plugin_gmail_gmail__send_email"
CHALLENGE = "🔐 Allow Ellen to use gmail?\nTo: marco@example.com"

REPLY = json.dumps({
    "action": "send an email to Marco",
    "summary": "Sends the March invoice.",
    "lines": [
        {"label": "To", "value": "marco@example.com", "sensitive": False},
        {"label": "Attachment", "value": "invoice-03.pdf", "sensitive": False},
    ],
})
MODEL_TITLE = "🔐 Allow Ellen to send an email to Marco?"
MODEL_BOLD = [MODEL_TITLE, "To:", "Attachment:"]

FALLBACK = Card(title="🔐 Allow Ellen to use gmail?",
                lines=(CardLine("To", "marco@example.com"),))


def _request(**over) -> CardRequest:
    fields = dict(kind="tool", who="Ellen",
                  facts={"tool": "send_email", "to": "marco@example.com"},
                  fallback=FALLBACK)
    fields.update(over)
    return CardRequest(**fields)


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch):
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _fresh_card_cache():
    approval_card.clear_cache()
    yield
    approval_card.clear_cache()


class _Channel:
    """Telegram endpoint double: records every post (with its entities) and
    every edit. A post can be held on ``gate`` to run an interleaving while
    the send is in flight."""

    def __init__(self, *, post_result=77):
        self.posts: list[dict] = []
        self.edits: list[tuple] = []
        self.post_times: dict[str, float] = {}
        self.gate: asyncio.Event | None = None
        self.in_flight = asyncio.Event()
        self._post_result = post_result

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               short_labels=False, shorts=None, urls=None,
                               entities=None):
        self.posts.append({"chat_id": chat_id, "request_id": request_id,
                           "text": text, "options": tuple(options),
                           "entities": entities})
        self.post_times[request_id] = time.monotonic()
        self.in_flight.set()
        if self.gate is not None:
            await self.gate.wait()
        return self._post_result

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return True

    async def _dispatch_button_continuation(self, **_kw):
        return True


class _Ask:
    """Replacement for ``approval_card._ask``: answers *reply*, or holds until
    released, or hangs forever."""

    def __init__(self, reply=REPLY, *, hold=False, hang=False):
        self.reply = reply
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._hold = hold
        self._hang = hang

    async def __call__(self, system, user):
        self.calls += 1
        self.entered.set()
        if self._hang:
            await asyncio.Event().wait()
        if self._hold:
            await self.release.wait()
        return self.reply


def _install_ask(monkeypatch, ask: _Ask) -> _Ask:
    monkeypatch.setattr(approval_card, "_ask", ask)
    return ask


def _register(coordinator, channel, *, key="k", card_request=None,
              challenge_text=CHALLENGE, finish_factory=None):
    return coordinator.register_challenge(
        key, bot_id=CASA_BOT, chat_id=OPERATOR, operator_id=OPERATOR,
        channel=channel, challenge_text=challenge_text,
        finish_factory=finish_factory, card_request=card_request,
    )


def _scope() -> ob.TurnScope:
    return ob.TurnScope(id="turn-1", cid="c1", role="assistant",
                        display_name="Ellen", channel="telegram",
                        message_type="channel_in", markers={})


def _cancel(broker, handle, reason="new_session") -> bool:
    ch = handle._challenge
    return broker.cancel(namespace="resident_ask", scope=ch.scope,
                         request_id=ch.rid, reason=reason)


def _bold_spans(text: str, entities) -> list[str]:
    """Each entity's span of *text*, decoded from UTF-16 code units --
    Telegram's unit -- so an offset counted in code points shows up wrong."""
    units = text.encode("utf-16-le")
    out = []
    for e in entities:
        assert e.type == "bold"
        out.append(units[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le"))
    return out


def _finish_factory(coordinator, channel):
    """The authz flavor's own finish hook, so the retired line asserted below
    is exactly the edit an operator sees."""

    def factory(message_id, req):
        return coordinator._make_finish_hook(
            channel=channel, chat_id=OPERATOR, operator_id=OPERATOR,
            target_role="assistant", enforcement_role="assistant",
            tool_name=TOOL, canonical_json="{}", rid=req.request_id,
            message_id=message_id, req=req,
        )

    return factory


# ---------------------------------------------------------------------------


async def test_card_request_posts_the_model_card_with_entities(
        monkeypatch, _fresh_broker):
    ask = _install_ask(monkeypatch, _Ask())
    channel = _Channel()
    coordinator = authz_grants.ChallengeCoordinator()

    handle = _register(coordinator, channel, card_request=_request())
    assert await handle.settled_post() == "posted"

    assert ask.calls == 1
    assert len(channel.posts) == 1
    post = channel.posts[0]
    display, ents = render_card(Card(
        title=MODEL_TITLE, summary="Sends the March invoice.",
        lines=(CardLine("To", "marco@example.com"),
               CardLine("Attachment", "invoice-03.pdf"))))
    assert post["text"] == display
    assert isinstance(post["text"], ob.Admitted)
    assert [(e.offset, e.length) for e in post["entities"]] == [
        (e.offset, e.length) for e in ents]
    assert _bold_spans(post["text"], post["entities"]) == MODEL_BOLD
    assert post["options"] == ("Approve", "Deny")
    # The challenge stays live with its message id recorded.
    req = handle._challenge.req
    assert req.meta["message_id"] == 77
    assert len(_fresh_broker.pending(namespace="resident_ask",
                                     scope=handle._challenge.scope)) == 1


async def test_settled_while_writing_posts_nothing(monkeypatch, _fresh_broker):
    ask = _install_ask(monkeypatch, _Ask(hold=True))
    channel = _Channel()
    coordinator = authz_grants.ChallengeCoordinator()

    handle = _register(coordinator, channel, card_request=_request(),
                       finish_factory=_finish_factory(coordinator, channel))
    await asyncio.wait_for(ask.entered.wait(), 5)
    assert _cancel(_fresh_broker, handle)
    ask.release.set()

    assert await handle.settled_post() == "inactive"
    await _fresh_broker.drain_hooks()

    req = handle._challenge.req
    assert len(channel.posts) == 0
    assert len(channel.edits) == 0
    assert req._hook is None
    assert "message_id" not in req.meta
    assert req._future.result() == {"outcome": "cancelled",
                                    "reason": "new_session"}


async def test_settled_right_after_writing_posts_nothing(monkeypatch, _fresh_broker):
    channel = _Channel()
    coordinator = authz_grants.ChallengeCoordinator()
    holder: dict = {}
    writes = []

    async def _write_then_settle(req):
        writes.append(req)
        assert _cancel(_fresh_broker, holder["handle"])
        return FALLBACK

    monkeypatch.setattr(approval_card, "write_card", _write_then_settle)
    scope = _scope()
    token = agent_mod.origin_var.set({"turn_scope": scope})
    try:
        holder["handle"] = _register(
            coordinator, channel, card_request=_request(),
            finish_factory=_finish_factory(coordinator, channel))
    finally:
        agent_mod.origin_var.reset(token)

    assert await holder["handle"].settled_post() == "inactive"
    await _fresh_broker.drain_hooks()

    req = holder["handle"]._challenge.req
    assert len(writes) == 1
    assert len(channel.posts) == 0
    assert len(channel.edits) == 0
    # Nothing was admitted: no send record is left open for a card never sent.
    assert len(scope.operator_sends) == 0
    assert req._hook is None
    assert req._future.result() == {"outcome": "cancelled",
                                    "reason": "new_session"}


async def test_settled_during_the_send_is_retired_as_today(
        monkeypatch, _fresh_broker):
    _install_ask(monkeypatch, _Ask())
    channel = _Channel()
    channel.gate = asyncio.Event()
    coordinator = authz_grants.ChallengeCoordinator()

    handle = _register(coordinator, channel, card_request=_request(),
                       finish_factory=_finish_factory(coordinator, channel))
    await asyncio.wait_for(channel.in_flight.wait(), 5)
    assert _cancel(_fresh_broker, handle)
    channel.gate.set()

    assert await handle.settled_post() == "inactive"
    await _fresh_broker.drain_hooks()

    assert len(channel.posts) == 1
    assert channel.edits == [
        (OPERATOR, 77, "🚫 Cancelled — send_email was cancelled by /new")]


async def test_entity_offsets_survive_an_admission_prefix(monkeypatch, _fresh_broker):
    _install_ask(monkeypatch, _Ask())
    channel = _Channel()
    coordinator = authz_grants.ChallengeCoordinator()
    note = "(Ellen 😀 wrote **x** without opening “invoice.pdf”.)"
    scope = _scope()
    scope.arm(ob.InheritedNote(note))

    token = agent_mod.origin_var.set({"turn_scope": scope})
    try:
        handle = _register(coordinator, channel, card_request=_request())
    finally:
        agent_mod.origin_var.reset(token)
    assert await handle.settled_post() == "posted"

    assert len(channel.posts) == 1
    post = channel.posts[0]
    assert post["text"].startswith(note + "\n\n")
    assert post["text"].annotations == (note,)
    # The note's emoji is two UTF-16 units: a shift counted in code points
    # would land every span one unit early.
    assert utf16_len(note) == len(note) + 1
    assert _bold_spans(post["text"], post["entities"]) == MODEL_BOLD
    # The admission's send record is confirmed by the delivered post.
    assert len(scope.operator_sends) == 1
    assert scope.operator_sends_delivered


async def test_burst_of_cards_degrades_to_fallbacks_within_budget(
        monkeypatch, _fresh_broker):
    ask = _install_ask(monkeypatch, _Ask(hang=True))
    channel = _Channel()
    coordinator = authz_grants.ChallengeCoordinator()
    fallbacks = {}
    registered = {}
    handles = []
    for i in range(8):
        fb = Card(title=f"🔐 Allow Ellen to use gmail? ({i})")
        h = _register(coordinator, channel, key=("burst", i),
                      card_request=_request(facts={"n": i}, fallback=fb,
                                            budget_s=0.3))
        rid = h._challenge.rid
        registered[rid] = time.monotonic()
        fallbacks[rid] = render_card(fb)[0]
        handles.append(h)
        # Staggered, so each card's budget is measured from its own start.
        await asyncio.sleep(0.02)

    results = await asyncio.wait_for(
        asyncio.gather(*(h.settled_post() for h in handles)), 10)

    assert results == ["posted"] * 8
    assert len(channel.posts) == 8
    assert ask.calls >= 1
    for post in channel.posts:
        rid = post["request_id"]
        assert post["text"] == fallbacks[rid]
        elapsed = channel.post_times[rid] - registered[rid]
        assert elapsed <= 0.45, (rid, elapsed)


@pytest.mark.parametrize("with_card", [False, True])
async def test_registration_refusal_is_unchanged(monkeypatch, _fresh_broker, with_card):
    ask = _install_ask(monkeypatch, _Ask())
    channel = _Channel()
    coordinator = authz_grants.ChallengeCoordinator()
    text = "x" * 3899 + "😀"
    assert utf16_len(text) == 3901

    handle = _register(coordinator, channel, challenge_text=text,
                       card_request=_request() if with_card else None)
    assert await handle.settled_post() == "inactive"

    assert handle.created is True
    assert handle.refused == "args_too_large"
    assert handle._challenge is None
    assert coordinator._entries == {}
    assert len(_fresh_broker.pending(namespace="resident_ask",
                                     scope=f"authz:{CASA_BOT}:{OPERATOR}")) == 0
    assert len(channel.posts) == 0
    assert ask.calls == 0
