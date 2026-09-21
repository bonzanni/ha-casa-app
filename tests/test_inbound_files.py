"""#1036 integration: the read grant, who gets it, the Telegram handler, the tool.

The grant tests go through ``resolve_hooks``' actual output and call the real
``path_scope`` callback. Asserting on a helper's return value would pass while
the grant never reached the matcher, which is the likeliest defect.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import agent_inbox as ai
from config import HooksConfig
from hooks import resolve_hooks

PDF = b"%PDF-1.4\n" + b"x" * 64
OPERATOR = 100
SUPERGROUP = -1001


@pytest.fixture(autouse=True)
def _clean_module_state():
    ai._reset_for_tests()
    yield
    ai._reset_for_tests()


class _Sched:
    def add_job(self, *a, **k):
        pass


@pytest.fixture
async def wired(tmp_path):
    await ai.wire(_Sched(), str(tmp_path / "agent-inbox"), role="assistant")
    return ai.get_inbox("assistant")


def _path_scope(resolved):
    """The real path_scope callback, as the SDK would run it."""
    for m in resolved["PreToolUse"]:
        if m.matcher and "Read" in m.matcher:
            return m.hooks[0]
    raise AssertionError("no path_scope matcher resolved")


async def _read(cb, path) -> bool:
    out = await cb({"tool_name": "Read", "tool_input": {"file_path": path}},
                   None, {})
    return not out      # {} / None means allowed


async def _write(cb, path) -> bool:
    out = await cb({"tool_name": "Write", "tool_input": {"file_path": path,
                                                         "content": "x"}},
                   None, {})
    return not out


# ---------------------------------------------------------------------------
# The read grant
# ---------------------------------------------------------------------------


async def test_the_grant_admits_ready_and_nothing_else(wired, tmp_path):
    receipt = await wired.publish(PDF, ".pdf", "invoice.pdf")
    cb = _path_scope(resolve_hooks(HooksConfig(), default_cwd="",
                                   extra_readable=ai.readable_prefixes("assistant")))
    assert await _read(cb, os.path.join(wired.ready_dir, receipt.name))
    for denied in (
        "/data/options.json",
        "/data/webhook_secrets/trigger.key",
        os.path.join(wired.staging_dir, ".part-x"),
        os.path.join(wired.meta_dir, f"{receipt.name}.json"),
        os.path.join(wired.ready_dir, "..", "staging", ".part-x"),
        wired.base,
        "/config/agent-home/assistant/.claude/settings.json",
    ):
        assert not await _read(cb, denied), denied


async def test_the_grant_never_makes_anything_writable(wired):
    cb = _path_scope(resolve_hooks(HooksConfig(), default_cwd="",
                                   extra_readable=ai.readable_prefixes("assistant")))
    assert not await _write(cb, os.path.join(wired.ready_dir, "x.pdf"))


async def test_an_explicit_hooks_file_keeps_the_grant(wired):
    explicit = HooksConfig(pre_tool_use=[
        {"policy": "path_scope", "readable": ["/config/agents/x"],
         "writable": []}])
    receipt = await wired.publish(PDF, ".pdf", "x.pdf")
    cb = _path_scope(resolve_hooks(explicit, default_cwd="",
                                   extra_readable=ai.readable_prefixes("assistant")))
    assert await _read(cb, os.path.join(wired.ready_dir, receipt.name))
    assert await _read(cb, "/config/agents/x/file")     # its own grant survives
    assert explicit.pre_tool_use[0]["readable"] == ["/config/agents/x"]  # not mutated


async def test_without_the_grant_nothing_is_readable(wired):
    receipt = await wired.publish(PDF, ".pdf", "x.pdf")
    cb = _path_scope(resolve_hooks(HooksConfig(), default_cwd=""))
    assert not await _read(cb, os.path.join(wired.ready_dir, receipt.name))


async def test_tina_cannot_read_a_file_sent_to_ellen(wired, tmp_path):
    """Only the agent a file was sent to can read it. The butler resident (the
    voice agent, persona Tina) has no inbox and so no read grant. Real Agents,
    real resolved hooks: the assistant's admits the file, the butler's denies
    it — including when handed the exact path."""
    from test_agent_plugin_binding import _make_agent

    receipt = await wired.publish(PDF, ".pdf", "invoice.pdf")
    path = os.path.join(wired.ready_dir, receipt.name)
    ellen = _make_agent(tmp_path / "e", role="assistant")
    tina = _make_agent(tmp_path / "t", role="butler")
    assert await _read(_path_scope(ellen._resolved_hooks), path)
    assert not await _read(_path_scope(tina._resolved_hooks), path)


# ---------------------------------------------------------------------------
# The Telegram handler
#
# The suite runs against conftest's `telegram` stub, so these use plain fakes
# with the attribute names PTB uses. That those names ARE PTB's is established
# separately, against the real library, in `test_real_ptb_...` below.
# ---------------------------------------------------------------------------

_KINDS = ("document", "photo", "sticker", "video", "video_note", "voice",
          "audio", "animation", "contact", "location", "venue", "poll",
          "dice", "game", "story", "web_app_data", "invoice", "pinned_message",
          "new_chat_title", "new_chat_photo", "delete_chat_photo",
          "message_auto_delete_timer_changed", "new_chat_members",
          "left_chat_member", "write_access_allowed")


def _msg(*, chat_id=OPERATOR, user_id=OPERATOR, thread=None, is_bot=False,
         **attach):
    fields = {k: None for k in _KINDS}
    fields.update(attach)
    return NS(message_id=7, chat=NS(id=chat_id),
              from_user=NS(id=user_id, first_name="Op", is_bot=is_bot),
              message_thread_id=thread, **fields)


def _doc(name="statement-q3.pdf", size=1234):
    return NS(file_id="fid", file_name=name, file_size=size)


def _update(msg):
    return NS(message=msg)


def _channel(fake_telegram_bot, chat_id=OPERATOR):
    from channels.telegram import TelegramChannel
    return TelegramChannel(bot=fake_telegram_bot, chat_id=chat_id,
                           engagement_supergroup_id=SUPERGROUP,
                           default_agent="assistant")


def _replies(bot):
    return [t for _, t in bot.messages]


async def test_a_document_is_stored_and_acknowledged(wired, fake_telegram_bot, monkeypatch):
    async def fetch(bot, file_id, cap):
        assert file_id == "fid"
        return PDF
    monkeypatch.setattr(ai, "fetch_telegram_file", fetch)
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(document=_doc())))
    [reply] = _replies(fake_telegram_bot)
    assert reply.startswith("Got statement-q3.pdf")
    assert "7 days" in reply
    [stored] = wired.list_files()
    assert stored.display_name == "statement-q3.pdf"


async def test_the_largest_photo_size_is_the_one_fetched(wired, fake_telegram_bot, monkeypatch):
    fetched = []

    async def fetch(bot, file_id, cap):
        fetched.append(file_id)
        return b"\xff\xd8\xff" + b"\x00" * 64
    monkeypatch.setattr(ai, "fetch_telegram_file", fetch)
    sizes = (NS(file_id="small", width=90, height=60, file_size=10),
             NS(file_id="large", width=1280, height=853, file_size=90),
             NS(file_id="medium", width=320, height=213, file_size=30))
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(photo=sizes)))
    assert fetched == ["large"]
    assert _replies(fake_telegram_bot)[0].startswith("Got your photo")


@pytest.mark.parametrize("attach,expect", [
    ({"document": _doc("archive.zip")}, "I can't open a .zip"),
    ({"document": _doc("noext")}, "I can't open a file like that"),
    ({"sticker": NS(file_id="s")}, "I can't read a sticker"),
    ({"voice": NS(file_id="v")}, "I can't read a voice message"),
    ({"web_app_data": NS(data="payload")}, "I can't read a Web App result"),
    ({"new_chat_title": "Renamed"}, "I can't do anything with that"),
    ({}, "I can't read that kind of message"),
])
async def test_every_refused_kind_is_answered(fake_telegram_bot, attach, expect, wired):
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(**attach)))
    [reply] = _replies(fake_telegram_bot)
    assert reply.startswith(expect), reply
    assert wired.list_files() == []


async def test_a_pin_is_answered_not_silent(fake_telegram_bot, wired):
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(pinned_message=_msg(document=_doc()))))
    assert _replies(fake_telegram_bot) == [
        "I can't do anything with that — I only take PDFs, images, and text or CSV files."]


async def test_an_engagement_topic_file_is_refused_in_the_topic(fake_telegram_bot, wired):
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(
        chat_id=SUPERGROUP, thread=555, document=_doc())))
    sg = fake_telegram_bot._supergroups[SUPERGROUP]
    assert sg.messages_by_thread[555] == [
        "I can't take files in an engagement — send it in our direct chat."]
    assert wired.list_files() == []


async def test_a_non_operator_is_refused_and_nothing_is_fetched(fake_telegram_bot, wired, monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(ai, "fetch_telegram_file", fetch)
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(user_id=999, document=_doc())))
    assert _replies(fake_telegram_bot)[0].startswith("Files are only accepted")
    fetch.assert_not_awaited()


async def test_no_inbox_means_an_honest_refusal(fake_telegram_bot):
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(document=_doc())))
    assert _replies(fake_telegram_bot) == [
        "I couldn't save that one. Nothing's been kept — try sending it again."]


async def test_a_notice_in_the_engagement_group_draws_nothing(fake_telegram_bot, wired):
    """Topic lifecycle notices — many caused by Casa's own topic actions — are
    not something anyone sent; a refusal to each would be noise in the topic."""
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(
        chat_id=SUPERGROUP, thread=555, forum_topic_closed=NS())))
    await ch._on_non_text_message(_update(_msg(
        chat_id=SUPERGROUP, thread=555, pinned_message=_msg(document=_doc()))))
    sg = fake_telegram_bot._supergroups.get(SUPERGROUP)
    assert sg is None or not any(sg.messages_by_thread.values())
    assert _replies(fake_telegram_bot) == []


async def test_a_bot_file_in_the_engagement_group_draws_nothing(fake_telegram_bot, wired):
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(
        chat_id=SUPERGROUP, thread=555, is_bot=True, document=_doc())))
    sg = fake_telegram_bot._supergroups.get(SUPERGROUP)
    assert sg is None or not any(sg.messages_by_thread.values())


async def test_a_file_from_another_chat_is_dropped_like_text(fake_telegram_bot, wired, monkeypatch):
    """The text path logs and drops an update from any chat other than the
    configured ones; a file from such a chat gets exactly the same treatment."""
    fetch = AsyncMock()
    monkeypatch.setattr(ai, "fetch_telegram_file", fetch)
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(chat_id=555, user_id=555,
                                               document=_doc())))
    await ch._on_non_text_message(_update(_msg(chat_id=-4242, user_id=7,
                                               new_chat_members=[NS()])))
    assert _replies(fake_telegram_bot) == []
    fetch.assert_not_awaited()


async def test_accept_all_mode_refuses_files_with_a_reply(fake_telegram_bot, wired):
    """With no chat id configured there is no operator, so no one's files can be
    accepted — but, as text is answered in that mode, the file is too."""
    ch = _channel(fake_telegram_bot, chat_id="")
    await ch._on_non_text_message(_update(_msg(chat_id=555, user_id=555,
                                               document=_doc())))
    assert _replies(fake_telegram_bot)[0].startswith("Files are only accepted")
    assert wired.list_files() == []


async def test_an_unexpected_failure_is_reported_as_uncertain(fake_telegram_bot, wired, monkeypatch):
    """An exception the handler did not anticipate may have struck after
    publication, so the reply claims neither that the file was kept nor that it
    was not."""
    async def boom(*a, **k):
        raise RuntimeError("unanticipated")
    monkeypatch.setattr(wired, "receive", boom)
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(document=_doc())))
    assert _replies(fake_telegram_bot) == [
        "I couldn't confirm that file was saved — it may or may not still be "
        "there. Send it again to be sure."]


def test_every_outcome_has_its_own_honest_line():
    from channels.telegram import _InboundClass, _inbound_reply
    cls = _InboundClass(True, display_name="invoice.pdf", ext=".pdf")
    lines = {o: _inbound_reply(ai.Receipt(o, name="n", size=2048), cls)
             for o in ai.Outcome}
    assert len(set(lines.values())) == len(lines)          # no two alike
    assert "Nothing's been kept" in lines[ai.Outcome.STORAGE_FAILED]
    assert "Nothing's been kept" not in lines[ai.Outcome.UNCERTAIN]
    assert "couldn't confirm" in lines[ai.Outcome.UNCERTAIN]
    assert lines[ai.Outcome.STORED].startswith("Got invoice.pdf")
    days = ai.RETENTION_S // 86400
    assert f"{days} days" in lines[ai.Outcome.STORED]
    assert f"{days} days" in lines[ai.Outcome.FULL]
    # Full is capacity, not a count: it can be reached by bytes alone.
    assert f"{ai.MAX_FILES} files" in lines[ai.Outcome.FULL]
    assert f"{ai.MAX_BYTES // (1024 * 1024)} MB" in lines[ai.Outcome.FULL]


async def test_flood_control_is_honoured_once(fake_telegram_bot, wired):
    """Telegram's RetryAfter is an instruction to try again after a stated wait.
    The refusal is delivered on the second attempt rather than lost."""
    from telegram.error import RetryAfter
    real_send = fake_telegram_bot.send_message
    calls = []

    async def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RetryAfter(0)
        return await real_send(*a, **k)
    fake_telegram_bot.send_message = flaky
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(document=_doc("archive.zip"))))
    assert len(calls) == 2
    assert _replies(fake_telegram_bot)[0].startswith("I can't open a .zip")


async def test_flood_control_twice_gives_up_without_hanging(fake_telegram_bot, wired):
    from telegram.error import RetryAfter
    calls = []

    async def always(*a, **k):
        calls.append(1)
        raise RetryAfter(0)
    fake_telegram_bot.send_message = always
    ch = _channel(fake_telegram_bot)
    await ch._on_non_text_message(_update(_msg(document=_doc("archive.zip"))))
    assert len(calls) == 2


async def test_the_non_text_handler_is_registered(monkeypatch):
    """Registration is what makes every other handler test matter: without it
    every non-text message is dropped silently. Handlers are registered before
    the Application initializes, so initialize fails on purpose here and the
    registrations are read back."""
    from unittest.mock import MagicMock
    import channels.telegram as tg_mod
    from channels.telegram import TelegramChannel

    sentinel = object()
    monkeypatch.setattr(tg_mod, "_non_text_filter", lambda: sentinel)
    monkeypatch.setattr(tg_mod, "MessageHandler",
                        lambda f, cb, block=True: ("MessageHandler", f, cb, block))
    registered = []
    app = MagicMock()
    app.add_handler = registered.append
    app.initialize = AsyncMock(side_effect=RuntimeError("stop after registration"))
    app.shutdown = AsyncMock()
    app.stop = AsyncMock()
    chain = MagicMock()
    chain.token.return_value = chain
    chain.base_url.return_value = chain
    chain.base_file_url.return_value = chain
    chain.build.return_value = app
    monkeypatch.setattr(tg_mod.Application, "builder", lambda: chain)
    ch = TelegramChannel(bot_token="t", chat_id=OPERATOR, default_agent="assistant")
    with pytest.raises(RuntimeError, match="stop after registration"):
        await ch._rebuild()
    assert ("MessageHandler", sentinel, ch._on_non_text_message, False) in registered


# ---------------------------------------------------------------------------
# Against the REAL python-telegram-bot, in a subprocess
#
# conftest replaces `telegram` for the whole session, so the real library can
# only be exercised out of process. These are the facts the fakes above assume.
# ---------------------------------------------------------------------------

_REAL_PTB = textwrap.dedent(r"""
    import datetime as dt, json, sys
    sys.path.insert(0, sys.argv[1])
    try:
        from telegram import (Chat, Document, Message, PhotoSize, Sticker,
                              Update, User, WebAppData)
    except ImportError:
        print(json.dumps({"skip": True})); raise SystemExit(0)
    from channels.telegram import _classify_inbound, _non_text_filter

    now = dt.datetime(2026, 9, 21, tzinfo=dt.timezone.utc)
    chat = Chat(id=100, type="private")
    user = User(id=100, first_name="Op", is_bot=False)
    def msg(**kw):
        return Message(message_id=1, date=now, chat=chat, from_user=user, **kw)
    doc = Document(file_id="fid", file_unique_id="u", file_name="Invoice.PDF",
                   file_size=1234)
    f = _non_text_filter()        # the expression production registers
    out = {
      "doc": [bool(f.check_update(Update(1, message=msg(document=doc)))),
              _classify_inbound(msg(document=doc)).ext],
      "photo": _classify_inbound(msg(photo=(
          PhotoSize("small", "u1", 90, 60), PhotoSize("large", "u2", 1280, 853)))
      ).file_id,
      "sticker": _classify_inbound(msg(sticker=Sticker(
          "s", "su", 512, 512, False, False, "regular"))).refusal,
      "web_app": [bool(f.check_update(Update(1, message=msg(
                     web_app_data=WebAppData("d", "b"))))),
                  _classify_inbound(msg(web_app_data=WebAppData("d", "b"))).refusal],
      "pin": [bool(f.check_update(Update(1, message=msg(pinned_message=msg(text="x"))))),
              _classify_inbound(msg(pinned_message=msg(text="x"))).refusal],
      "text": bool(f.check_update(Update(1, message=msg(text="hi")))),
      "edited": bool(f.check_update(Update(1, edited_message=msg(document=doc)))),
      "channel_post": bool(f.check_update(Update(1, channel_post=Message(
          message_id=1, date=now, chat=Chat(id=-5, type="channel"), document=doc)))),
    }
    print(json.dumps(out))
""")


def test_real_ptb_filter_and_attribute_names():
    import json
    root = os.path.join(os.path.dirname(__file__), "..", "casa", "rootfs", "opt", "casa")
    r = subprocess.run([sys.executable, "-c", _REAL_PTB, os.path.abspath(root)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if out.get("skip"):
        pytest.skip("python-telegram-bot is not installed in this lane")
    assert out["doc"] == [True, ".pdf"]
    assert out["photo"] == "large"
    assert out["sticker"].startswith("I can't read a sticker")
    assert out["web_app"][0] is True
    assert out["web_app"][1].startswith("I can't read a Web App result")
    assert out["pin"][0] is True
    assert out["pin"][1].startswith("I can't do anything with that")
    assert out["text"] is False            # disjoint from the TEXT handler
    assert out["edited"] is False          # an edited caption draws no reply
    assert out["channel_post"] is False


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


async def _call_list(role):
    import agent as agent_mod
    import tools
    token = agent_mod.origin_var.set({"role": role})
    try:
        out = await tools.list_inbound_files.handler({})
    finally:
        agent_mod.origin_var.reset(token)
    return out["content"][0]["text"]


async def test_the_tool_lists_paths_and_says_listing_is_not_reading(wired):
    receipt = await wired.publish(PDF, ".pdf", "statement-q3.pdf")
    text = await _call_list("assistant")
    assert '"statement-q3.pdf" — PDF' in text
    assert os.path.join(wired.ready_dir, receipt.name) in text
    assert "Listing a file is not reading it" in text


async def test_the_tool_gives_other_roles_nothing(wired):
    await wired.publish(PDF, ".pdf", "x.pdf")
    text = await _call_list("butler")
    assert "no inbound files" in text
    assert wired.ready_dir not in text
