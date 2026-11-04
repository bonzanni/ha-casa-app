"""Slice 5l: a delegate's answer reaches the operator under the delegate's own
header. The resident decides whether to pass an answer on; ``relay_answer``
makes Casa post the delegate's held closing message verbatim as ``📊 <name>`` +
the words, once, through the real Telegram channel, filed in the post map so a
swipe-reply reaches the delegate's desk. Code decides only mechanical facts:
the answer is held for this conversation and not yet posted, the approval
fact captured when it was held, whether a page landed.
"""
from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import result_broker as rb
import tools as tools_mod
from channels import DeliveryOutcome
from output_boundary import IntentKind, InheritedNote, TurnScope
from output_boundary_testing import with_scope

ANSWER = "I filled the form and sent it. Expect a confirmation e-mail within the hour."
DID = "0f3c9a2e-1111-4222-8333-944445555666"
CHAT = 42


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(tools_mod, "_RELAYABLE", OrderedDict())
    pm = rb.PostMap()
    monkeypatch.setattr(rb, "POST_MAP", pm)
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "browser": SimpleNamespace(character=SimpleNamespace(name="Fido")),
        "finance": SimpleNamespace(character=SimpleNamespace(name="Alex"))})
    return pm


def _sends(ids):
    it = iter(ids)

    async def _send(**kw):
        await asyncio.sleep(0)       # a real send suspends; a concurrent call runs here
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        return SimpleNamespace(message_id=nxt)
    return AsyncMock(side_effect=_send)


@pytest.fixture
def channel(monkeypatch):
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(bot=MagicMock(), chat_id=str(CHAT))
    ch._app = MagicMock()
    ch._app.bot.send_message = _sends(range(100, 200))

    class _Manager:
        def get(self, name):
            return ch if name == "telegram" else None
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(), raising=False)
    return ch


def _origin(**over):
    base = {"role": "assistant", "channel": "telegram", "chat_id": CHAT,
            "user_id": CHAT, "cid": "c1"}
    base.update(over)
    return base


def _hold(text=ANSWER, role="browser", origin=None, **kw):
    return tools_mod._hold_answer(DID, role, origin or _origin(), text, **kw)


async def _relay(origin=None, did=DID):
    import agent as agent_mod
    tok = agent_mod.origin_var.set(origin if origin is not None else with_scope(_origin()))
    try:
        out = await tools_mod.relay_answer.handler({"delegation_id": did})
    finally:
        agent_mod.origin_var.reset(tok)
    return out, out["content"][0]["text"]


def _sent(ch) -> list[str]:
    return [c.kwargs["text"] for c in ch._app.bot.send_message.await_args_list]


# --- what is held ---------------------------------------------------------------

@pytest.mark.parametrize("origin, text, held", [
    (_origin(), ANSWER, True),
    (_origin(channel="voice"), ANSWER, False),
    (_origin(channel="webhook"), ANSWER, False),
    (_origin(chat_id=None), ANSWER, False),
    (_origin(delegation_depth=1), ANSWER, False),       # a specialist's own delegation
    (_origin(), "", False),
    (_origin(), "  <silent/> ", False),
])
def test_only_a_residents_telegram_answer_with_words_is_held(origin, text, held):
    assert _hold(text=text, origin=origin) is held
    assert (DID in tools_mod._RELAYABLE) is held


def test_the_held_answers_are_bounded_oldest_first(monkeypatch):
    monkeypatch.setattr(tools_mod, "RELAY_HELD_MAX", 2)
    for i in range(3):
        tools_mod._hold_answer(f"d{i}", "browser", _origin(), ANSWER)
    assert list(tools_mod._RELAYABLE) == ["d1", "d2"]


def test_an_answer_whose_send_is_in_flight_is_never_evicted(monkeypatch):
    """r1, Terra S1: evicting it would turn a second call into the hand-passed
    fallback while the first send is still landing."""
    monkeypatch.setattr(tools_mod, "RELAY_HELD_MAX", 2)
    tools_mod._hold_answer("d0", "browser", _origin(), ANSWER)
    tools_mod._RELAYABLE["d0"].state = "posting"
    for i in (1, 2):
        tools_mod._hold_answer(f"d{i}", "browser", _origin(), ANSWER)
    assert list(tools_mod._RELAYABLE) == ["d0", "d1", "d2"]   # only held ones count
    tools_mod._hold_answer("d3", "browser", _origin(), ANSWER)
    assert list(tools_mod._RELAYABLE) == ["d0", "d2", "d3"]


@pytest.mark.asyncio
async def test_a_posted_answer_is_still_known_as_posted_after_many_newer_ones(
        channel, monkeypatch):
    """r2, Astra S1: only a never-posted answer is evicted, so a call for an id
    Casa forgot is one the person has not seen; a posted one keeps no words."""
    monkeypatch.setattr(tools_mod, "RELAY_HELD_MAX", 2)
    _hold()
    await _relay()
    assert tools_mod._RELAYABLE[DID].text == ""
    for i in range(5):
        tools_mod._hold_answer(f"n{i}", "browser", _origin(), ANSWER)
    out, text = await _relay()
    assert "already posted" in text and len(_sent(channel)) == 1


@pytest.mark.asyncio
async def test_the_header_is_the_name_the_delegate_had_when_it_answered(channel, monkeypatch):
    """r1, Terra S1: a reload or persona change between the answer and the relay
    must not put another name over the delegate's words."""
    _hold()
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "browser": SimpleNamespace(character=SimpleNamespace(name="Alex"))})
    _, text = await _relay()
    assert _sent(channel) == [f"📊 Fido\n{ANSWER}"] and "Fido's answer" in text


# --- the post --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_relay_posts_the_whole_answer_once_under_the_delegates_header(channel, fresh):
    _hold()
    out, text = await _relay()
    assert not out.get("is_error")
    assert _sent(channel) == [f"📊 Fido\n{ANSWER}"]
    (rec,) = fresh.owned(f"relay:{DID}")
    assert (rec.role, rec.kind, rec.operator_id) == ("browser", "relayed_answer", CHAT)
    assert fresh.get(CHAT, 100) is rec          # a swipe-reply routes to the delegate
    assert tools_mod.RELAY_SILENCE_NOTE.format(name="Fido") in text
    assert tools_mod._RELAYABLE[DID].state == "posted"


@pytest.mark.asyncio
async def test_a_second_relay_posts_nothing(channel):
    _hold()
    await _relay()
    out, text = await _relay()
    assert not out.get("is_error") and "already posted" in text
    assert len(_sent(channel)) == 1


@pytest.mark.asyncio
async def test_two_concurrent_relays_post_once(channel):
    _hold()
    results = await asyncio.gather(_relay(), _relay())
    assert len(_sent(channel)) == 1
    assert sum("already posted" in t for _, t in results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [
    _origin(chat_id=7),
    _origin(role="butler"),
])
async def test_another_conversations_answer_is_not_posted(channel, origin):
    _hold()
    out, text = await _relay(with_scope(origin))
    assert out.get("is_error") and "another conversation" in text
    assert _sent(channel) == [] and tools_mod._RELAYABLE[DID].state == "held"


@pytest.mark.asyncio
async def test_an_unheld_id_is_refused_and_says_pass_it_on_yourself(channel):
    out, text = await _relay()
    assert out.get("is_error") and "pass it on yourself" in text
    assert _sent(channel) == []


def _never_connected():
    import httpx
    from telegram.error import TimedOut
    try:
        raise TimedOut() from httpx.ConnectError("refused")
    except TimedOut as exc:
        return exc


@pytest.mark.asyncio
@pytest.mark.parametrize("unsent", ["refused", "never_connected"])
async def test_a_send_proven_unsent_keeps_the_answer_relayable(channel, unsent):
    from telegram.error import BadRequest
    first = BadRequest("chat not found") if unsent == "refused" else _never_connected()
    channel._app.bot.send_message = _sends([first, 300])
    _hold()
    out, text = await _relay()
    assert out.get("is_error") and "did NOT reach" in text
    assert tools_mod._RELAYABLE[DID].state == "held"
    out, _ = await _relay()
    assert not out.get("is_error")
    assert channel._app.bot.send_message.await_count == 2      # 1 failed + 1 landed


@pytest.mark.asyncio
async def test_a_lost_acknowledgement_is_never_posted_again(channel):
    """r1, Astra S1: Telegram may have shown a message whose response timed
    out, so an ambiguous failure is not retried — the person never sees the
    answer twice."""
    from telegram.error import TimedOut
    channel._app.bot.send_message = _sends([TimedOut(), 301])
    _hold()
    out, text = await _relay()
    assert out.get("is_error") and "could not be confirmed" in text
    assert "relay_answer once more" not in text
    out, text = await _relay()
    assert "already posted" in text
    assert channel._app.bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_a_relay_cancelled_mid_send_is_never_posted_again(channel):
    started = asyncio.Event()

    async def _hang(**kw):
        started.set()
        await asyncio.sleep(3600)
    channel._app.bot.send_message = AsyncMock(side_effect=_hang)
    _hold()
    task = asyncio.ensure_future(_relay())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    channel._app.bot.send_message = _sends([302])
    out, text = await _relay()
    assert "already posted" in text
    assert channel._app.bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_a_send_that_raised_after_a_page_landed_is_not_posted_again(channel, fresh):
    from telegram.error import TimedOut
    long = "\n".join(f"row {i}" for i in range(900))
    channel._app.bot.send_message = _sends([301, TimedOut(), 302])
    _hold(text=long)
    out, text = await _relay()
    assert out.get("is_error") and "could not be confirmed" in text
    assert len(fresh.owned(f"relay:{DID}")) == 1
    out, text = await _relay()
    assert "already posted" in text
    assert channel._app.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_a_refusal_after_a_page_landed_is_not_posted_again(channel, fresh):
    """r2, Astra S2: a proven-unsent failure of a LATER page still leaves page 1
    on screen, so the landing evidence keeps it from being retried."""
    from telegram.error import BadRequest
    long = "\n".join(f"row {i}" for i in range(900))
    channel._app.bot.send_message = _sends([401, BadRequest("x"), 402, 403])
    _hold(text=long)
    out, text = await _relay()
    assert out.get("is_error") and "could not be confirmed" in text
    out, text = await _relay()
    assert "already posted" in text
    assert channel._app.bot.send_message.await_count == 2
    assert len(fresh.owned(f"relay:{DID}")) == 1


@pytest.mark.asyncio
async def test_a_channel_that_delivers_nothing_keeps_the_answer_relayable(channel):
    channel._app = None
    _hold()
    out, text = await _relay()
    assert out.get("is_error") and "did NOT reach" in text
    assert tools_mod._RELAYABLE[DID].state == "held"


@pytest.mark.asyncio
async def test_a_casa_line_the_admission_adds_sits_above_the_header(channel):
    """d1, Astra S1: the LABELLED text is admitted, so an inherited note about
    the resident's turn never reads as the delegate's words."""
    note = "Casa: Ellen wrote this without opening “invoice.pdf”."
    origin = with_scope(_origin())
    origin["turn_scope"].arm(InheritedNote(note))
    _hold()
    await _relay(origin)
    (sent,) = _sent(channel)
    assert not sent.startswith("📊 Fido")
    assert sent.index(note) < sent.index("📊 Fido\n") < sent.index(ANSWER)


@pytest.mark.asyncio
async def test_an_answer_held_while_an_approval_waits_is_not_posted(channel):
    _hold(approval_waiting=True)
    out, text = await _relay()
    assert out.get("is_error") and "approval" in text
    assert _sent(channel) == []


@pytest.mark.asyncio
async def test_a_turn_whose_reply_is_the_delivery_refuses(channel, monkeypatch):
    monkeypatch.setattr(TurnScope, "delivers_to_operator", property(lambda self: True))
    _hold()
    out, text = await _relay()
    assert out.get("is_error") and "reply instead" in text
    assert _sent(channel) == []


@pytest.mark.asyncio
async def test_a_refused_relay_is_an_accounted_send_attempt(channel):
    """r1, Astra S2: the call itself is accounted — a relay refused before any
    admission still records a failed attempt, so a buffered turn that closes
    in silence after it keeps its earlier text."""
    origin = with_scope(_origin(message_type="scheduled"))
    scope = origin["turn_scope"]
    out, _ = await _relay(origin, did="not-held")
    assert out.get("is_error")
    assert [(a.tool, a.state) for a in scope.send_attempts] == [("relay_answer", "failed")]
    assert scope.operator_sends == [] and scope.closing_silence_earned is False


@pytest.mark.asyncio
async def test_a_delivered_relay_earns_a_closing_silence_and_a_failed_one_does_not(channel):
    assert "relay_answer" in tools_mod._ACCOUNTED_SENDS
    _hold()
    ok = with_scope(_origin(message_type="scheduled"))
    await _relay(ok)
    assert ok["turn_scope"].closing_silence_earned is True

    tools_mod._RELAYABLE.clear()
    channel._app = None
    _hold()
    failed = with_scope(_origin(message_type="scheduled"))
    await _relay(failed)
    assert failed["turn_scope"].closing_silence_earned is False


# --- the seams that hold ----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_sync_delegation_holds_the_words_before_casas_echo(tmp_path, monkeypatch):
    from test_delegate_to_agent import TestHandoffLine, _origin as d_origin
    monkeypatch.setattr(rb, "echo_lines",
                        lambda events: ["📊 Alex posted to your chat (1 page)."])
    payload, _lines = await TestHandoffLine()._run(
        tmp_path, monkeypatch, mode="sync",
        origin={**d_origin(chat_id=7), "_operator_turn": True})
    did = payload["delegation_id"]
    assert payload["relay"] == tools_mod.RELAY_HINT.format(id=did, name="Alex")
    assert "posted to your chat" in payload["text"]
    held = tools_mod._RELAYABLE[did]
    assert (held.text, held.role, held.chat_id, held.approval_waiting) == (
        "Alex's answer", "finance", 7, False)


@pytest.mark.asyncio
async def test_a_sync_delegation_captures_the_childs_approval_wait_when_held(
        tmp_path, monkeypatch):
    """d1, Astra S2 + Terra S2: the resident's fold pops delegated_approvals at
    this result, so the fact is captured here, from the child's own scope."""
    from test_delegate_to_agent import TestHandoffLine, _origin as d_origin
    import agent as agent_mod
    real = tools_mod._run_delegated_agent_bounded

    async def run_left_waiting(*a, **kw):
        out = await real(*a, **kw)
        # what the runner publishes when its run ended with a grant waiting
        agent_mod.origin_var.get()["turn_scope"].approval_waiting = ("auth-key",)
        return out
    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", run_left_waiting)
    payload, _ = await TestHandoffLine()._run(
        tmp_path, monkeypatch, mode="sync", origin=with_scope(d_origin(chat_id=7)))
    assert tools_mod._RELAYABLE[payload["delegation_id"]].approval_waiting is True


@pytest.mark.asyncio
async def test_a_voice_delegation_holds_nothing():
    assert tools_mod._hold_answer("v", "finance", _origin(channel="voice"), ANSWER) is False


@pytest.mark.asyncio
async def test_a_late_answer_is_held_and_its_notice_says_it_is_relayable(monkeypatch):
    from specialist_registry import DelegationRecord
    published = []

    class _Bus:
        async def notify(self, msg):
            published.append(msg)
    reg = MagicMock()
    reg.complete_delegation = AsyncMock(return_value=None)
    monkeypatch.setattr(tools_mod, "_bus", _Bus())
    monkeypatch.setattr(tools_mod, "_specialist_registry", reg)
    monkeypatch.setattr(rb, "echo_lines",
                        lambda events: ["📊 Alex posted to your chat (1 page)."])
    record = DelegationRecord(id=DID, agent="finance", started_at=0.0,
                              origin=_origin())

    async def _run():
        return tools_mod.DelegatedOutput(text="Alex's late answer", run_subtype="success")
    task = asyncio.ensure_future(_run())
    tools_mod._attach_completion_callback(task, record)
    await task
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.gather(*list(tools_mod._delegation_settle_tasks))
    (msg,) = published
    assert msg.content.relayable is True
    assert "posted to your chat" in msg.content.text
    assert tools_mod._RELAYABLE[DID].text == "Alex's late answer"


def test_the_notice_names_the_full_id_only_when_the_answer_is_held():
    from agent import Agent
    from bus import BusMessage, MessageType
    from specialist_registry import DelegationComplete

    def body(relayable):
        complete = DelegationComplete(delegation_id=DID, agent="finance", status="ok",
                                      text=ANSWER, origin={"user_text": "x"},
                                      relayable=relayable)
        msg = BusMessage(type=MessageType.NOTIFICATION, source="finance",
                         target="assistant", content=complete, channel="telegram",
                         context={"chat_id": CHAT})
        return str(Agent._synthesize_delegation_turn(None, msg).content)
    assert f"call relay_answer with delegation_id {DID}" in body(True)
    assert "relay_answer" not in body(False)


def test_the_setup_courier_turn_tells_the_resident_to_relay():
    import plugin_setup_episodes as pse
    entry = {"granted_tools": ["mcp__plugin_x_srv"], "targets": ["specialist:browser"]}
    role, text = pse._compose({"id": "ep1", "plugin": "x"}, entry, "setup")
    assert role == "assistant" and "relay_answer" in text
    entry["targets"] = ["resident:assistant"]
    _, text = pse._compose({"id": "ep1", "plugin": "x"}, entry, "setup")
    assert "relay_answer" not in text


def test_the_setup_turn_asks_for_the_outcome_in_one_plain_line():
    """casa-test, a Fido upgrade: the setup turn posted "The setup tool returned: Paired with
    Casa Browser 0.1.8." Both branches ask for the outcome alone, in one plain line."""
    import plugin_setup_episodes as pse
    entry = {"granted_tools": ["mcp__plugin_x_srv"], "targets": ["specialist:browser"]}
    _, text = pse._compose({"id": "ep1", "plugin": "x"}, entry, "setup")
    assert ("and to answer with its outcome alone, in one short plain line, without naming "
            "the tool") in text
    entry["targets"] = ["resident:assistant"]
    _, text = pse._compose({"id": "ep1", "plugin": "x"}, entry, "setup")
    assert ("then tell the operator its outcome in one short plain line, in its own terms, "
            "without naming the tool or saying what it returned.") in text
    prompt = " ".join((Path(tools_mod.__file__).resolve().parent / "defaults/agents/assistant"
                       / "prompts/system.md").read_text().split())
    assert ("tell the operator its outcome in one short plain line, in its terms (never "
            "\"the setup tool returned …\")") in prompt


def test_only_the_assistant_is_granted_relay_answer():
    import yaml
    root = Path(tools_mod.__file__).resolve().parent / "defaults"
    tool = "mcp__casa-framework__relay_answer"
    role = yaml.safe_load((root / "roles/resident/assistant/role.yaml").read_text())
    runtime = yaml.safe_load((root / "agents/assistant/runtime.yaml").read_text())
    assert tool in role["tools"]["allowed"] and tool in runtime["tools"]["allowed"]
    for other in ("butler", "concierge"):
        for path in (root / f"roles/resident/{other}/role.yaml",
                     root / f"agents/{other}/runtime.yaml"):
            if path.exists():
                assert tool not in path.read_text(), path
    import specialist_component
    assert "relay_answer" not in repr(vars(specialist_component))
    assert tools_mod.relay_answer in tools_mod.CASA_TOOLS


async def _swipe_reply(ch, monkeypatch, message_id):
    """Drive the channel's real desk route with a reply quoting *message_id*."""
    import specialist_desk
    spawned = []
    monkeypatch.setattr(ch, "default_agent", "assistant", raising=False)
    monkeypatch.setattr(ch, "_sender_is_operator", lambda user: True)
    monkeypatch.setattr(ch, "_rate_limiter", None, raising=False)
    monkeypatch.setattr(ch, "_reserve_desk_or_refuse", AsyncMock(return_value=object()))
    monkeypatch.setattr(ch, "_start_typing", lambda *a, **k: None)
    monkeypatch.setattr(ch, "_spawn_desk_turn", lambda **kw: spawned.append(kw["desk_role"]))
    monkeypatch.setattr(specialist_desk, "desk_target_ok", lambda resident, role: True)
    update = SimpleNamespace(
        message=SimpleNamespace(reply_to_message=SimpleNamespace(message_id=message_id,
                                                                  text="q"),
                                message_id=900, text="and the receipt?"),
        effective_user=SimpleNamespace(id=CHAT, first_name="Op"))
    routed = await ch._maybe_route_desk_reply(update, str(CHAT))
    return routed, spawned


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [
    _origin(),
    # r4, Astra S2: a scheduled turn's origin carries a session label, no user
    _origin(chat_id="interval:morning", user_id=None, message_type="scheduled"),
])
async def test_a_swipe_reply_on_a_relayed_answer_reaches_the_delegates_desk(
        channel, monkeypatch, origin):
    _hold(origin=origin)
    await _relay(with_scope(origin))
    assert _sent(channel) == [f"📊 Fido\n{ANSWER}"]
    routed, spawned = await _swipe_reply(channel, monkeypatch, 100)
    assert routed is True and spawned == ["browser"]


@pytest.mark.asyncio
async def test_an_unfinished_sync_answer_is_not_offered_for_relay(tmp_path, monkeypatch, channel):
    """r6, Terra S1: an answer cut off at max_tokens is never banked
    (`answer_incomplete`), so it is not offered for relay either — Ellen passes
    it on herself, as before 5l (and that retelling is banked as her turn), and
    can say it was cut off. A relay called anyway refuses, posting nothing."""
    import test_delegate_to_agent as tdta
    from test_delegate_to_agent import TestHandoffLine, _origin as d_origin
    real_reset = tdta._FakeSpecialistClient.reset.__func__

    def reset(cls, *a, **kw):
        real_reset(cls, *a, **{**kw, "stop_reason": "max_tokens"})
    monkeypatch.setattr(tdta._FakeSpecialistClient, "reset", classmethod(reset))
    origin = {**d_origin(chat_id=CHAT), "user_id": CHAT, "_operator_turn": True}
    payload, _ = await TestHandoffLine()._run(tmp_path, monkeypatch, mode="sync",
                                              origin=origin)
    assert payload["status"] == "ok" and "relay" not in payload
    import tools as t
    monkeypatch.setattr(t, "_channel_manager", type("M", (), {
        "get": lambda self, n: channel if n == "telegram" else None})())
    out, text = await _relay(with_scope(origin), did=payload["delegation_id"])
    assert out.get("is_error") and "cut off" in text and "pass it on yourself" in text
    assert _sent(channel) == []


@pytest.mark.asyncio
async def test_an_unfinished_late_answer_is_not_offered_for_relay(monkeypatch):
    from specialist_registry import DelegationRecord
    published = []

    class _Bus:
        async def notify(self, msg):
            published.append(msg)
    reg = MagicMock()
    reg.complete_delegation = AsyncMock(return_value=None)
    monkeypatch.setattr(tools_mod, "_bus", _Bus())
    monkeypatch.setattr(tools_mod, "_specialist_registry", reg)
    record = DelegationRecord(id=DID, agent="finance", started_at=0.0, origin=_origin())

    async def _run():
        return tools_mod.DelegatedOutput(text="Alex's late answer, cut", run_subtype="success",
                                         run_stop_reason="max_tokens")
    task = asyncio.ensure_future(_run())
    tools_mod._attach_completion_callback(task, record)
    await task
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.gather(*list(tools_mod._delegation_settle_tasks))
    (msg,) = published
    assert msg.content.relayable is False and tools_mod._RELAYABLE[DID].incomplete is True


# --- slice 5p: only the message the delegate closed its turn with -----------------
# Phone round 10 (2026-11-01): the whole turn text was held, so narration written
# between tool calls was posted with the answer, and a closing `<silent/>` after
# narration reached the person verbatim. The two shapes below are the screenshot's.

NARRATION_THEN_SILENCE = (
    ("The page has a time field. Handing over now.", "request_live_view"),
    ("<silent/>", None),
)
FINAL = ("The time field is empty after the live view, so I can't report a time.\n\n"
         "Source: 1. Special Format Inputs")
NARRATION_THEN_ANSWER = (
    ("Live view is done; observe the page.", "observe"),
    ("Time field is empty. Finish as a mismatch.", "finish"),
    (FINAL, None),
)


def _script_turn(monkeypatch, steps):
    """The fake specialist answers in several text-bearing messages, a tool call
    and its result between them, as a real delegate's turn does."""
    import test_delegate_to_agent as tdta
    from claude_agent_sdk import (AssistantMessage, TextBlock, ToolResultBlock,
                                  ToolUseBlock, UserMessage)
    real = tdta._FakeSpecialistClient.receive_response

    async def receive_response(self):
        async for msg in real(self):
            if not isinstance(msg, AssistantMessage):
                yield msg
                continue
            for i, (words, tool) in enumerate(steps):
                content = [TextBlock(text=words)]
                if tool is not None:
                    content.append(ToolUseBlock(id=f"tu{i}", name=f"mcp__browser__{tool}",
                                                input={}))
                yield AssistantMessage(content=content, model="m")
                if tool is not None:
                    yield UserMessage(content=[ToolResultBlock(tool_use_id=f"tu{i}",
                                                               content="ok")])
    monkeypatch.setattr(tdta._FakeSpecialistClient, "receive_response", receive_response)


async def _delegate(tmp_path, monkeypatch, steps, mode="sync"):
    from test_delegate_to_agent import TestHandoffLine, _origin as d_origin
    _script_turn(monkeypatch, steps)
    origin = {**d_origin(chat_id=CHAT), "user_id": CHAT, "_operator_turn": True}
    import test_delegate_to_agent as tdta
    import plugin_registry
    # an async run outlives the harness's own patches: keep the fake in place
    monkeypatch.setattr(tools_mod, "ClaudeSDKClient", tdta._FakeSpecialistClient)
    monkeypatch.setattr(plugin_registry, "resolve_for",
                        lambda *a, **kw: tdta.ResolutionResult(registry_valid=True))
    payload, _ = await TestHandoffLine()._run(tmp_path, monkeypatch, mode=mode,
                                              origin=origin)
    await asyncio.gather(*list(tools_mod._delegation_callback_tasks))
    for _ in range(5):                  # an async answer settles in a task of its own
        await asyncio.sleep(0)
    await asyncio.gather(*list(tools_mod._delegation_settle_tasks))
    if mode == "async":
        assert payload["status"] == "pending"
    return payload, origin


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_narration_then_a_closing_silence_holds_nothing(tmp_path, monkeypatch, mode):
    seen = []
    real = tools_mod._hold_answer
    monkeypatch.setattr(tools_mod, "_hold_answer",
                        lambda *a, **kw: seen.append(a) or real(*a, **kw))
    payload, _ = await _delegate(tmp_path, monkeypatch, NARRATION_THEN_SILENCE, mode)
    assert len(seen) == 1                # the hold was reached, and declined
    assert "relay" not in payload
    assert payload["delegation_id"] not in tools_mod._RELAYABLE
    if mode == "sync":
        # the assistant still reads the whole turn, as before slice 5l
        assert payload["text"].startswith(
            "The page has a time field. Handing over now.\n\n<silent/>")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_only_the_closing_message_is_held_never_the_narration(
        tmp_path, monkeypatch, mode):
    payload, _ = await _delegate(tmp_path, monkeypatch, NARRATION_THEN_ANSWER, mode)
    assert tools_mod._RELAYABLE[payload["delegation_id"]].text == FINAL
    if mode == "sync":
        assert payload["relay"] == tools_mod.RELAY_HINT.format(
            id=payload["delegation_id"], name="Alex")
        # what the assistant reads and what the delegate's retain banks are the
        # whole turn, unchanged
        assert payload["text"].startswith(
            "Live view is done; observe the page.\n\nTime field is empty. Finish as a "
            "mismatch.\n\n" + FINAL)


@pytest.mark.asyncio
async def test_the_relayed_post_is_the_closing_message_alone(tmp_path, monkeypatch, channel):
    payload, origin = await _delegate(tmp_path, monkeypatch, NARRATION_THEN_ANSWER)
    monkeypatch.setattr(tools_mod, "_channel_manager", type("M", (), {
        "get": lambda self, n: channel if n == "telegram" else None})())
    out, _ = await _relay(with_scope(origin), did=payload["delegation_id"])
    assert not out.get("is_error")
    assert _sent(channel) == [f"📊 Alex\n{FINAL}"]


@pytest.mark.parametrize("words, held", [
    ("Done, the form is sent.\n\n<silent/>", "Done, the form is sent."),
    ("<silent/>\nDone, the form is sent.", "Done, the form is sent."),
])
def test_a_sentinel_beside_the_words_is_dropped_the_words_are_held(words, held):
    assert _hold(text=words) is True
    assert tools_mod._RELAYABLE[DID].text == held


@pytest.mark.asyncio
async def test_an_approval_cut_keeps_the_closing_message_for_the_late_answer(monkeypatch):
    """The #1252 cut shortens `reply_messages` for the desk; what is held is the
    turn's own closing message, whole."""
    from specialist_registry import DelegationRecord
    reg = MagicMock()
    reg.complete_delegation = AsyncMock(return_value=None)

    class _Bus:
        async def notify(self, msg):
            pass
    monkeypatch.setattr(tools_mod, "_bus", _Bus())
    monkeypatch.setattr(tools_mod, "_specialist_registry", reg)
    record = DelegationRecord(id=DID, agent="finance", started_at=0.0, origin=_origin())

    async def _run():
        return tools_mod.DelegatedOutput(
            text="Checking.\n\n" + FINAL, run_subtype="success",
            reply_messages=("Checking.",), final_message=FINAL)
    task = asyncio.ensure_future(_run())
    tools_mod._attach_completion_callback(task, record)
    await task
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.gather(*list(tools_mod._delegation_settle_tasks))
    assert tools_mod._RELAYABLE[DID].text == FINAL


# --- slice 5t: a closing silence is told to the resident as silence -----------------
# casa-test 2026-11-01: Fido posted the start card, wrote one line of narration and
# closed with `<silent/>`; nothing was held (5p), and the assistant read the
# narration as progress and wrote "Fido is still on it." The result now says the
# delegate closed silent.

@pytest.mark.asyncio
async def test_a_sync_closing_silence_is_told_as_silence(tmp_path, monkeypatch):
    payload, _ = await _delegate(tmp_path, monkeypatch, NARRATION_THEN_SILENCE)
    note = tools_mod.SILENT_CLOSE_NOTE.format(name="Alex")
    assert payload["text"].endswith("\n\n" + note)
    assert payload["text"].count(note) == 1


@pytest.mark.asyncio
async def test_a_sync_answer_carries_no_silence_note(tmp_path, monkeypatch):
    payload, _ = await _delegate(tmp_path, monkeypatch, NARRATION_THEN_ANSWER)
    assert "closed its turn with" not in payload["text"]


async def _late_notice(monkeypatch, output):
    from specialist_registry import DelegationRecord
    published = []

    class _Bus:
        async def notify(self, msg):
            published.append(msg)
    reg = MagicMock()
    reg.complete_delegation = AsyncMock(return_value=None)
    monkeypatch.setattr(tools_mod, "_bus", _Bus())
    monkeypatch.setattr(tools_mod, "_specialist_registry", reg)
    record = DelegationRecord(id=DID, agent="finance", started_at=0.0, origin=_origin())

    async def _run():
        return output
    task = asyncio.ensure_future(_run())
    tools_mod._attach_completion_callback(task, record)
    await task
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.gather(*list(tools_mod._delegation_settle_tasks))
    (msg,) = published
    return msg.content


@pytest.mark.asyncio
@pytest.mark.parametrize("text, final, noted", [
    ("Waiting for the run to get ready.\n\n<silent/>", "<silent/>", True),
    ("<silent/>", None, True),
    ("Waiting for the run.\n\nYou're signed in.", "You're signed in.", False),
    ("Done.\n\n<silent/>", "Done.\n\n<silent/>", False),   # words beside the sentinel
    ("", None, False),                                      # no answer is not a silence
])
async def test_a_late_closing_silence_is_told_as_silence(monkeypatch, text, final, noted):
    content = await _late_notice(monkeypatch, tools_mod.DelegatedOutput(
        text=text, run_subtype="success", final_message=final))
    note = tools_mod.SILENT_CLOSE_NOTE.format(name=tools_mod._display_name_for_role("finance"))
    assert (note in content.text) is noted
    assert content.relayable is False or not noted
