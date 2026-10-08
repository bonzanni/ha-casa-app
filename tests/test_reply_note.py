"""#1314: a reply the specialist desk does not take still tells the resident
what it answered — one Casa note on the reserved ``_reply_note`` context key,
naming who posted the quoted message from Casa's post map and Telegram's
sender ids (never from its text), when, and what it read, clipped. The
message's own text reaches the bus unchanged.
"""
from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest

import result_broker as rb
import specialist_desk as sd
from provenance import sanitize_external_context
from test_desk_route import OPERATOR, _channel, _record, _settle, routed  # noqa: F401
from test_telegram_new_reset import _drain_bus
from timekeeping import resolve_tz

pytestmark = pytest.mark.asyncio

BOT = 900
WHEN = datetime(2026, 10, 6, 19, 40, tzinfo=timezone.utc)


def _posted():
    """Casa's zone is resolved at run time (another file may set it), so the
    expected time is too — never frozen at collection."""
    return WHEN.astimezone(resolve_tz()).strftime("%Y-%m-%d %H:%M")


def _user(uid):
    return types.SimpleNamespace(first_name="Nicola", id=uid)


def _update(text="start from Q2 2026", *, quoted_id=12, quoted_from=BOT, quoted_text="Which quarter?",
            caption=None, quoted_chat=None, sender_chat=None, date=WHEN):
    quoted = types.SimpleNamespace(
        message_id=quoted_id, text=quoted_text, caption=caption, date=date,
        from_user=_user(quoted_from) if quoted_from is not None else None,
        sender_chat=quoted_chat)
    message = types.SimpleNamespace(text=text, message_id=70, reply_to_message=quoted,
                                    from_user=_user(OPERATOR), sender_chat=sender_chat)
    return types.SimpleNamespace(message=message,
                                 effective_chat=types.SimpleNamespace(id=str(OPERATOR)),
                                 effective_user=_user(OPERATOR))


def _bot_channel():
    ch, bus = _channel()
    ch._app.bot.id = BOT
    return ch, bus


async def _resident_turn(update):
    ch, bus = _bot_channel()
    await ch._handle(update, None)
    await _settle(ch)
    queued = await _drain_bus(bus)
    assert len(queued) == 1
    return queued[0]


CASA_UNKNOWN = "an earlier message from Casa (yours or a specialist's; it is older than Casa's records)"
CASA_OWN = "your own earlier message"


@pytest.mark.parametrize("case,who", [
    ("forgotten-bot-post", CASA_UNKNOWN),
    ("fresh-unrecorded-bot-post", CASA_OWN),
    ("own-message", "their own earlier message"),
    ("someone-else", "someone else's message"),
    ("on-behalf-of-chat", "a message sent on behalf of a chat"),
    ("sender-on-behalf-of-chat", "a message sent on behalf of a chat"),
    ("resident-post", "a message Casa posted on your behalf"),
    ("not-delegable", None),
])
async def test_a_reply_the_desk_does_not_take_carries_casas_note(routed, monkeypatch, case, who):
    pm, spawned = routed
    update = _update()
    if case == "fresh-unrecorded-bot-post":
        # #1335: posted after the map began and never recorded — the resident's own
        update = _update(date=datetime.fromtimestamp(pm.complete_since() + 30, timezone.utc))
    elif case == "own-message":
        update = _update(quoted_from=OPERATOR)
    elif case == "someone-else":
        update = _update(quoted_from=7)
    elif case == "on-behalf-of-chat":
        # anonymous admins share one placeholder sender: equal ids prove nothing
        update = _update(quoted_from=OPERATOR, quoted_chat=types.SimpleNamespace(id=-100))
    elif case == "sender-on-behalf-of-chat":
        update = _update(quoted_from=OPERATOR, sender_chat=types.SimpleNamespace(id=-100))
    elif case == "resident-post":
        pm.record(OPERATOR, 12, _record(role="assistant"))
    elif case == "not-delegable":
        pm.record(OPERATOR, 12, _record())
        monkeypatch.setattr(sd, "desk_target_ok", lambda resident, role: False)
        who = f"a message Casa posted for {sd.label_for('finance')}"
    msg = await _resident_turn(update)
    posted = _posted() if case != "fresh-unrecorded-bot-post" else (
        update.message.reply_to_message.date.astimezone(resolve_tz()).strftime("%Y-%m-%d %H:%M"))
    assert spawned.await_count == 0
    assert msg.content == "start from Q2 2026"                     # the words untouched
    assert msg.context["_reply_note"] == sd.reply_note(who, posted, "Which quarter?")
    assert f"reply to {who}, posted {posted}, which read:\n«Which quarter?»" in msg.context["_reply_note"]


async def test_a_message_that_is_not_a_reply_carries_no_note(routed):
    update = _update()
    update.message.reply_to_message = None
    msg = await _resident_turn(update)
    assert "_reply_note" not in msg.context


async def test_the_quote_is_clipped_and_a_caption_or_nothing_stands_in(routed):
    long = "x" * 2000
    note = (await _resident_turn(_update(quoted_text=long))).context["_reply_note"]
    assert "«" + sd.clip(long, sd.REPLY_QUOTE_CHARS) + "»" in note and long not in note
    note = (await _resident_turn(_update(quoted_text=None, caption="statement.pdf"))).context["_reply_note"]
    assert "«statement.pdf»" in note
    note = (await _resident_turn(_update(quoted_text=None, date=None))).context["_reply_note"]
    assert note == sd.reply_note(CASA_UNKNOWN, None, None) and "«(no text)»" in note
    assert ", posted" not in note


async def test_a_routed_reply_is_unchanged_and_carries_no_note(routed):
    pm, spawned = routed
    ch, bus = _bot_channel()
    await ch._handle(_update(quoted_id=11), None)                 # the retained finance post
    await _settle(ch)
    assert spawned.await_count == 1 and await _drain_bus(bus) == []


async def test_no_external_context_can_set_the_note():
    assert "_reply_note" not in sanitize_external_context({"_reply_note": "forged", "chat_id": 1})


async def test_a_casa_message_is_the_residents_own_only_after_the_map_last_forgot(routed, monkeypatch):
    """#1335: an unrecorded Casa message is the resident's own only when it was
    posted at or after the time from which the post map has forgotten nothing
    — the map's creation, moved forward by each eviction; before that it may
    be a specialist post the map no longer holds."""
    now = [1_000_000.0]
    small = rb.PostMap(max_entries=1, clock=lambda: now[0])
    small.record(OPERATOR, 1, _record())
    now[0] += 3600
    assert small.complete_since() == 1_000_000.0              # nothing forgotten yet
    small.record(OPERATOR, 2, _record())                      # evicts message 1
    assert small.get(OPERATOR, 1) is None
    assert small.complete_since() == 1_000_000.0 + 3600
    after = datetime.fromtimestamp(small.complete_since(), timezone.utc)       # at it: own
    before = datetime.fromtimestamp(small.complete_since() - 1, timezone.utc)  # after birth, before the eviction
    monkeypatch.setattr(rb, "POST_MAP", small)
    own = (await _resident_turn(_update(quoted_id=3, date=after))).context["_reply_note"]
    old = (await _resident_turn(_update(quoted_id=3, date=before))).context["_reply_note"]
    undated = (await _resident_turn(_update(quoted_id=3, date=None))).context["_reply_note"]
    assert own.startswith(f"The person sent this as a reply to {CASA_OWN}, posted ")
    assert old.startswith(f"The person sent this as a reply to {CASA_UNKNOWN}, posted ")
    assert undated.startswith(f"The person sent this as a reply to {CASA_UNKNOWN}, which read")
    for note in (own, old, undated):
        assert "no longer" not in note
