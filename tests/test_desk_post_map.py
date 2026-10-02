"""S4 §2: the post map — every physical message Casa sends for a specialist's
post (each page, each fallback chunk, the media message, the link message, a
desk reply's pages) is recorded under its poster AS IT LANDS, keyed by the
canonical (chat_id, message_id), FIFO-bounded, memory-only. A page that
reached the operator is routable even when a later page failed and the hook
withheld the result. The S3 echo ledger gains a per-owner event cap.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import result_broker as rb
import tools as tools_mod
from authz_grants import GrantIdentity
from channels import DeliveryOutcome
from channels.tg_richtext import render_paged
from plugin_grants import PluginContract, ResultContractMap, ToolContract

ARTIFACT = "7" * 64
REPORT = "mcp__plugin_probe_api__report"
SLOT = "report"
LABEL = "📊 Finance"


def _record(**over) -> rb.PostRecord:
    base = dict(role="finance", operator_id=42, plugin="probe", slot=SLOT,
                tool_use_id="call-1", owner="d-1", posted_at=1000.0)
    base.update(over)
    return rb.PostRecord(**base)


# --- the map ---------------------------------------------------------------------

def test_the_map_is_keyed_by_chat_and_message_and_bounded_fifo():
    m = rb.PostMap(max_entries=3)
    m.record(42, 1, _record(tool_use_id="a"))
    m.record(42, 2, _record(tool_use_id="b"))
    m.record(7, 1, _record(tool_use_id="c"))           # another chat, same id
    assert m.get(42, 1).tool_use_id == "a" and m.get(7, 1).tool_use_id == "c"
    assert m.get(42, 3) is None and m.get(43, 1) is None
    m.record(42, 3, _record(tool_use_id="d"))          # evicts (42, 1)
    assert m.get(42, 1) is None and m.get(42, 3).tool_use_id == "d"
    assert rb.POST_MAP_MAX == 4096 and isinstance(rb.POST_MAP, rb.PostMap)


def test_the_map_lists_what_landed_for_an_owner_in_landing_order():
    """Round 6: the map is the complete record of what Casa posted for a
    turn — the desk's outcome predicate reads it by owner (every delivered
    slot, whatever its kind), evicted entries excluded."""
    m = rb.PostMap(max_entries=3)
    m.record(42, 1, _record(tool_use_id="a", owner="t-1", kind=rb.OPERATOR_LINK))
    m.record(42, 2, _record(tool_use_id="b", owner="t-2", kind=rb.OPERATOR_MESSAGE))
    m.record(42, 3, _record(tool_use_id="c", owner="t-1", kind=rb.OPERATOR_FILE))
    assert [r.tool_use_id for r in m.owned("t-1")] == ["a", "c"]
    assert [r.kind for r in m.owned("t-1")] == [rb.OPERATOR_LINK, rb.OPERATOR_FILE]
    assert m.owned("t-9") == [] and m.owned("") == []
    m.record(42, 4, _record(tool_use_id="d", owner="t-2"))         # evicts (42, 1)
    assert [r.tool_use_id for r in m.owned("t-1")] == ["c"]
    assert _record().kind == ""                                    # the kind is optional


def test_the_map_refuses_non_positive_keys_silently():
    m = rb.PostMap()
    m.record(0, 1, _record())
    m.record(42, None, _record())
    m.record("42", 5, _record())
    assert m.get(42, 5) is None and m.get(0, 1) is None


def test_the_echo_ledger_caps_events_per_owner_only_when_asked():
    """The desk's echo ledger is capped (64, oldest dropped); the S3 post
    ledger keeps its v0.340 behaviour — every event of an owner retained."""
    import specialist_desk as sd
    ledger = rb.PostLedger(max_events=3)
    for i in range(5):
        ledger.record("o", rb.PostEvent(str(i), "p", "s", LABEL, 1, None))
    assert [e.tool_use_id for e in ledger.drain("o")] == ["2", "3", "4"]
    assert rb.POSTS._max_events is None
    for i in range(65):
        rb.POSTS.record("delegation-x", rb.PostEvent(str(i), "p", "s", LABEL, 1, None))
    assert len(rb.POSTS.drain("delegation-x")) == 65
    assert sd._ledger()._max_events == 64


# --- the channel records as it lands ------------------------------------------------

def _channel():
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(bot=MagicMock(), chat_id="42")
    app = MagicMock()
    ch._app = app
    return ch, app


def _sends(ids):
    """A bot.send_message double returning a Message-like per call."""
    it = iter(ids)

    async def _send(**kw):
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        return SimpleNamespace(message_id=nxt)
    return AsyncMock(side_effect=_send)


@pytest.mark.asyncio
async def test_every_page_and_fallback_chunk_of_a_message_is_recorded(monkeypatch):
    from telegram.error import BadRequest
    pm = rb.PostMap()
    monkeypatch.setattr(rb, "POST_MAP", pm)
    ch, app = _channel()
    url = "https://e.example/" + "q" * 60
    text = LABEL + "\n" + "a" * 4050 + f"[t]({url})"     # one page, 2 fallback chunks
    assert len(render_paged(text)) == 1
    app.bot.send_message = _sends([BadRequest("x"), 11, 12])
    rec = _record()
    assert await ch.deliver_operator_message(42, text, post=rec) is DeliveryOutcome.DELIVERED
    assert pm.get(42, 11) is rec and pm.get(42, 12) is rec
    assert app.bot.send_message.await_count == 3


@pytest.mark.asyncio
async def test_a_page_that_landed_is_recorded_even_when_a_later_page_fails(monkeypatch):
    from telegram.error import TimedOut
    pm = rb.PostMap()
    monkeypatch.setattr(rb, "POST_MAP", pm)
    ch, app = _channel()
    text = LABEL + "\n" + "\n".join(f"row {i}" for i in range(900))
    assert len(render_paged(text)) >= 2
    app.bot.send_message = _sends([21, TimedOut()])
    with pytest.raises(TimedOut):
        await ch.deliver_operator_message(42, text, post=_record())
    assert pm.get(42, 21) is not None and pm.get(42, 22) is None


@pytest.mark.asyncio
async def test_a_file_and_a_link_are_recorded(monkeypatch):
    pm = rb.PostMap()
    monkeypatch.setattr(rb, "POST_MAP", pm)
    ch, app = _channel()
    app.bot.send_document = AsyncMock(return_value=SimpleNamespace(message_id=31))
    assert await ch.deliver_operator_file(42, b"x", "text", "r.csv", LABEL,
                                          post=_record(slot="export")) is DeliveryOutcome.DELIVERED
    assert pm.get(42, 31).slot == "export"
    text, entities, plain = rb.compose_operator_link("https://e.example/a", label="Open")
    app.bot.send_message = _sends([32])
    assert await ch.deliver_operator_link(42, text, entities, plain,
                                          post=_record(slot="link")) is DeliveryOutcome.DELIVERED
    assert pm.get(42, 32).slot == "link"


@pytest.mark.asyncio
async def test_without_a_record_nothing_is_recorded(monkeypatch):
    pm = rb.PostMap()
    monkeypatch.setattr(rb, "POST_MAP", pm)
    ch, app = _channel()
    app.bot.send_message = _sends([41])
    assert await ch.deliver_operator_message(42, LABEL + "\nhi") is DeliveryOutcome.DELIVERED
    assert pm.get(42, 41) is None


@pytest.mark.asyncio
async def test_a_resident_reply_records_when_its_context_carries_a_record(monkeypatch):
    """A desk reply goes through send_response (admitted); its pages join the
    map under the specialist when the delivery context carries `_post`."""
    from output_boundary import casa_text
    pm = rb.PostMap()
    monkeypatch.setattr(rb, "POST_MAP", pm)
    ch, app = _channel()
    ch._release_typing = lambda *a, **k: None
    body = casa_text(LABEL + "\n" + "\n".join(f"**row {i}**" for i in range(700)))
    pages = render_paged(body)
    assert len(pages) >= 2
    ids = list(range(51, 51 + len(pages)))
    app.bot.send_message = _sends(ids)
    rec = _record(tool_use_id="desk-1")
    assert await ch.send_response(body, {"chat_id": "42", "_post": rec}) is DeliveryOutcome.DELIVERED
    assert all(pm.get(42, i) is rec for i in ids)
    # single page, same contract
    app.bot.send_message = _sends([99])
    assert await ch.send_response(casa_text(LABEL + "\n**one**"), {"chat_id": "42", "_post": rec}) is DeliveryOutcome.DELIVERED
    assert pm.get(42, 99) is rec
    # no record in the context: nothing recorded (every resident reply today)
    app.bot.send_message = _sends([100])
    await ch.send_response(casa_text(LABEL + "\n**one**"), {"chat_id": "42"})
    assert pm.get(42, 100) is None


# --- the hook hands the channel a record built from the identity ------------------

class _Recorder:
    def __init__(self):
        self.calls = []

    async def deliver_operator_message(self, chat_id, text, *, post=None):
        self.calls.append(("message", chat_id, post))
        return DeliveryOutcome.DELIVERED


class _Manager:
    def __init__(self, ch):
        self._ch = ch

    def get(self, name):
        return self._ch if name == "telegram" else None


@pytest.mark.asyncio
async def test_the_hook_passes_a_record_with_the_identitys_role_and_operator(monkeypatch):
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "finance": SimpleNamespace(character=SimpleNamespace(name="Finance"))})
    rec = _Recorder()
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(rec), raising=False)
    store = rb.ReferenceStore(now=lambda: 1000.0)
    tools = {REPORT: ToolContract(ARTIFACT, "probe", "capability", (SLOT,), {},
                                  {SLOT: "operator_message"})}
    cmap = ResultContractMap(tools=tools, plugins={"probe": PluginContract(ARTIFACT, True, frozenset())})
    hook = rb.make_result_hook(cmap, client_id="c1", store=store)
    identity = GrantIdentity(operator_id=42, chat_id=42, enforcement_role="finance",
                             artifact_id=ARTIFACT, engagement_id="", delegation_id="d-9")
    store.open_call(client_id="c1", artifact_id=ARTIFACT, tool_name=REPORT,
                    tool_use_id="call-7", identity=identity, provides=(SLOT,),
                    delivers={SLOT: "operator_message"})
    ref, _ = store.deposit(client_id="c1", slot=SLOT, value="body")
    out = await hook({"hook_event_name": "PostToolUse", "tool_name": REPORT, "tool_input": {},
                      "tool_response": json.dumps({SLOT: ref})}, "call-7", {})
    assert json.loads(out["hookSpecificOutput"]["updatedToolOutput"])["casa_delivery"]["status"] == "delivered"
    (_kind, chat_id, post), = rec.calls
    assert chat_id == 42
    assert post == rb.PostRecord(role="finance", operator_id=42, plugin="probe", slot=SLOT,
                                 tool_use_id="call-7", owner="d-9", posted_at=post.posted_at,
                                 kind=rb.OPERATOR_MESSAGE)
