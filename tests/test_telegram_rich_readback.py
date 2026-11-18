"""A reply to one of Casa's rich messages reads that message's text back.

A rich message arrives with ``text`` and ``caption`` both ``None``; its content
is in ``api_kwargs["rich_message"]``. Every place that reads a quoted message's
text (the reply note, a guest summons' note, a reply to a task's post, a file
sent as a reply to a task's post) reads it through ``quoted_text``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

import agent_inbox as ai
from channels.telegram import quoted_text
from telegram_reply_harness import BOT, OPERATOR, _channel
from test_file_handoff_route import FIN, _reply_msg, routed  # noqa: F401
from test_inbound_files import _clean_module_state, wired  # noqa: F401

HEADING = "Invoice 913"
RICH = {"blocks": [{"type": "heading", "text": HEADING, "size": 1},
                   {"type": "paragraph", "text": ["due ", {"type": "bold", "text": "Friday"}]}]}

_DE_JSON = """
import json, sys
from telegram import Message
m = Message.de_json(json.loads(sys.argv[1]), None)
print(json.dumps({"text": m.text, "caption": m.caption, "api_kwargs": dict(m.api_kwargs)}))
"""


def _received_rich(rich: dict, *, from_id: int = BOT, message_id: int = 12) -> NS:
    """A rich message as python-telegram-bot hands it over. The tests run against
    a stub ``telegram`` module, so the real library parses the payload in a
    child process and its fields are copied onto a plain object."""
    payload = {"message_id": message_id, "date": 1795161600,
               "chat": {"id": OPERATOR, "type": "private"},
               "from": {"id": from_id, "is_bot": True, "first_name": "Casa"},
               "rich_message": rich}
    out = subprocess.run([sys.executable, "-c", _DE_JSON, json.dumps(payload)],
                         capture_output=True, text=True, check=True, cwd="/")
    parsed = json.loads(out.stdout)
    return NS(message_id=message_id, text=parsed["text"], caption=parsed["caption"],
              api_kwargs=parsed["api_kwargs"], date=None, sender_chat=None,
              from_user=NS(id=from_id, first_name="Casa"))


def _plain(text: str, *, from_id: int = BOT, message_id: int = 12) -> NS:
    return NS(message_id=message_id, text=text, caption=None, api_kwargs={}, date=None,
              sender_chat=None, from_user=NS(id=from_id, first_name="Casa"))


def test_reply_to_rich_message_with_heading_reads_heading():
    quoted = _received_rich(RICH)
    assert quoted.text is None and quoted.caption is None   # what the library hands over
    read = quoted_text(quoted)
    assert read is not None and read.splitlines()[0] == HEADING
    assert "due Friday" in read


def test_reply_to_plain_message_unchanged():
    assert quoted_text(_plain("Which quarter?")) == "Which quarter?"
    assert quoted_text(NS(text=None, caption="a photo", api_kwargs={})) == "a photo"
    assert quoted_text(NS(text=None, caption=None, api_kwargs={})) is None
    assert quoted_text(NS(text=None, caption=None)) is None


def _reply_update(quoted: NS, text: str = "pay it") -> NS:
    message = NS(text=text, message_id=70, reply_to_message=quoted,
                 from_user=NS(id=OPERATOR, first_name="Sam"), sender_chat=None)
    return NS(message=message, effective_chat=NS(id=str(OPERATOR)),
              effective_user=NS(id=OPERATOR, first_name="Sam"))


def test_reply_note_carries_the_rich_text():
    ch, _bus = _channel()
    note = ch._reply_note_for(_reply_update(_received_rich(RICH)), str(OPERATOR))
    assert HEADING in note and "due Friday" in note and "(no text)" not in note


def test_guest_reply_note_carries_the_rich_text():
    ch, _bus = _channel()
    gm = NS(reply_to_message=_received_rich(RICH))
    note = ch._guest_reply_note_for(gm, "group", "Family")
    assert note is not None and HEADING in note and "due Friday" in note


@pytest.mark.asyncio
async def test_task_reply_carries_the_rich_text(monkeypatch):
    import task_input
    ch, _bus = _channel()
    ch._rate_limiter = None
    submitted = []
    monkeypatch.setattr(task_input, "submit",
                        lambda task_id, text, said=None: submitted.append(text) or "taken")
    ref = NS(kind="delegation", task_id="d-1", executor=FIN)
    assert await ch._route_task_reply(_reply_update(_received_rich(RICH)), str(OPERATOR), ref) \
        == "taken"
    assert submitted == [f"[replying to «{HEADING}»]\npay it"]


@pytest.mark.asyncio
async def test_task_file_carries_the_rich_text(routed, monkeypatch):  # noqa: F811
    ch = routed.ch
    monkeypatch.setattr(ai, "grant_task_file", lambda task_id, path: None)
    ch._engagement_registry = NS(get=lambda task_id: NS(id=task_id))
    delivered = []

    async def deliver(rec, text, **kw):
        delivered.append(kw)
    ch.deliver_operator_reply = deliver
    msg = _reply_msg()
    msg.reply_to_message = _received_rich(RICH, message_id=11)
    ref = NS(kind="job", task_id="j-1", executor=FIN)
    user = NS(id=msg.from_user.id, first_name="Op")
    await ch._route_task_file(msg.chat.id, user, msg, ("task", ref, "reply"))
    assert len(delivered) == 1
    assert delivered[0]["quoted_text"].splitlines()[0] == HEADING
