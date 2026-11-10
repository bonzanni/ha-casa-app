"""The agent-log sink, its writer, registry and retention (``agent_logs``).

Everything runs against a fake bot and a fake clock: no network, no real
sleeping. The fake bot records every request with the fake time it was made
and can be told to raise ``telegram.error`` classes. In the unit session those
are the suite's stand-ins (``conftest.py``), so the error texts below are
written the way python-telegram-bot hands them over: prefix stripped and
capitalised. The last test repeats the error handling in a subprocess against
the real library, over a mock HTTP transport.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import json
import logging
import os
import stat
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter

import agent_logs
from agent_logs import AgentLogs, LogKey

CASA = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"
CHAT = -100123
HOUR = 3600.0

FINANCE = LogKey("agent", "finance")
FIDO = LogKey("agent", "fido")
ELLEN_PLUGINS = LogKey("plugins", "assistant")

NAMES = {"finance": "Finance", "fido": "Fido", "assistant": "Ellen"}


def _retry_after(seconds: int) -> RetryAfter:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return RetryAfter(seconds)


# As python-telegram-bot presents them: "Bad Request: " stripped, capitalised.
THREAD_GONE = "Message thread not found"
NOT_MODIFIED = ("Message is not modified: specified new message content "
                "and reply markup are exactly the same as a current content and reply "
                "markup of the message")
EDIT_GONE = "Message to edit not found"
CANT_DELETE = "Message can't be deleted"
DELETE_GONE = "Message to delete not found"
KICKED = "Forbidden: bot was kicked from the supergroup chat"


class Clock:
    def __init__(self) -> None:
        self.mono = 1_000.0
        self.wall = 1_800_000_000.0

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(max(0.0, seconds))
        await asyncio.sleep(0)


class FakeBot:
    """Records ``(method, kwargs, monotonic time)``; ``fail[method]`` is a list
    of exceptions raised by that method's next calls, in order."""

    id = 4242

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.fail: dict[str, list[BaseException]] = {}
        self.next_message = 100
        self.next_thread = 500
        self.member: Any = _admin(can_manage_topics=True)

    def _call(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs, self.clock.mono))
        queue = self.fail.get(method)
        if queue:
            raise queue.pop(0)

    async def send_message(self, **kw: Any) -> Any:
        self._call("send_message", kw)
        self.next_message += 1
        return SimpleNamespace(message_id=self.next_message)

    async def edit_message_text(self, **kw: Any) -> Any:
        self._call("edit_message_text", kw)
        return True

    async def create_forum_topic(self, **kw: Any) -> Any:
        self._call("create_forum_topic", kw)
        self.next_thread += 1
        return SimpleNamespace(message_thread_id=self.next_thread)

    async def delete_messages(self, **kw: Any) -> bool:
        self._call("delete_messages", kw)
        return True

    async def delete_message(self, **kw: Any) -> bool:
        self._call("delete_message", kw)
        return True

    async def get_chat_member(self, **kw: Any) -> Any:
        self._call("get_chat_member", kw)
        return self.member

    def named(self, method: str) -> list[dict[str, Any]]:
        return [kw for m, kw, _ in self.calls if m == method]

    def methods(self) -> list[str]:
        return [m for m, _, _ in self.calls if m != "get_chat_member"]


def _admin(*, can_manage_topics: bool | None) -> Any:
    # The real ChatMemberAdministrator is exercised in the subprocess test.
    return SimpleNamespace(status="administrator", can_delete_messages=False,
                           can_manage_topics=can_manage_topics)


def _member() -> Any:
    return SimpleNamespace(status="member")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def bot(clock: Clock) -> FakeBot:
    fake = FakeBot(clock)
    yield fake
    # A log post never goes to the General topic.
    for kw in fake.named("send_message"):
        assert kw.get("message_thread_id") is not None, kw
        assert "parse_mode" not in kw


def _make(clock: Clock, tmp_path: Path, name: str = "log-topics.json") -> AgentLogs:
    return AgentLogs(display_name=lambda role: NAMES.get(role, ""),
                     path=str(tmp_path / name), monotonic=clock.monotonic,
                     wall=clock.time, sleep=clock.sleep)


@pytest.fixture
async def logs(clock: Clock, bot: FakeBot, tmp_path: Path) -> AgentLogs:
    sink = _make(clock, tmp_path)
    assert await sink.check_enabled(bot, CHAT)
    bot.calls.clear()
    return sink


async def _drain(sink: AgentLogs, limit: int = 500) -> int:
    steps = 0
    while await sink.step():
        steps += 1
        assert steps < limit
    return steps


def _texts(bot: FakeBot) -> list[str]:
    return [kw["text"] for m, kw, _ in bot.calls
            if m in ("send_message", "edit_message_text")]


# ---------------------------------------------------------------------------
# The sink
# ---------------------------------------------------------------------------


def test_log_is_synchronous() -> None:
    assert not inspect.iscoroutinefunction(AgentLogs.log)


async def test_log_is_a_no_op_until_enabled(clock, bot, tmp_path) -> None:
    sink = _make(clock, tmp_path)
    assert sink.log(FINANCE, "Q3", "hello") is None
    assert await sink.step() is False

    bot.member = _admin(can_manage_topics=False)
    assert await sink.check_enabled(bot, CHAT) is False
    sink.log(FINANCE, "Q3", "hello")
    assert await sink.step() is False
    assert bot.methods() == []


async def test_log_never_raises_and_warns_at_most_once_a_minute(
    logs, clock, caplog,
) -> None:
    logs._keys = None  # any internal failure
    with caplog.at_level(logging.WARNING, logger="agent_logs"):
        assert logs.log(FINANCE, "Q3", "a") is None
        logs.log(FINANCE, "Q3", "b")
        assert len(caplog.records) == 1
        clock.advance(61)
        logs.log(FINANCE, "Q3", "c")
        assert len(caplog.records) == 2


def test_run_name_and_topic_name() -> None:
    assert agent_logs.run_name("Q3 accounting\nwith details") == "Q3 accounting"
    long = agent_logs.run_name("x" * 60)
    assert len(long) == 40 and long.endswith("…")
    assert agent_logs.run_name("y" * 40) == "y" * 40

    def names(role: str) -> str:
        return NAMES.get(role, "")

    assert agent_logs.topic_name(FINANCE, names) == "Finance"
    assert agent_logs.topic_name(ELLEN_PLUGINS, names) == "Ellen · plugins"
    assert agent_logs.topic_name(LogKey("agent", "unknown"), names) == "unknown"
    cut = agent_logs.topic_name(LogKey("agent", "z" * 200), names)
    assert len(cut) == 128 and cut.endswith("…")
    assert FINANCE.state_key == "agent:finance"
    assert ELLEN_PLUGINS.state_key == "plugins:assistant"


async def test_first_post_creates_the_topic_then_sends_into_it(logs, bot) -> None:
    logs.log(FINANCE, "Q3 accounting\nmore", "▶ started")
    await _drain(logs)
    assert bot.methods() == ["create_forum_topic", "send_message"]
    assert bot.named("create_forum_topic")[0] == {"chat_id": CHAT, "name": "Finance"}
    sent = bot.named("send_message")[0]
    assert sent["chat_id"] == CHAT
    assert sent["message_thread_id"] == 501
    assert sent["text"] == "Q3 accounting · ▶ started"


async def test_send_refuses_without_a_thread_id(logs, bot) -> None:
    """The last line of defence: even if a caller reached the send with no
    topic, nothing goes to General."""
    with pytest.raises(ValueError):
        await logs._send(None, "x")
    assert bot.named("send_message") == []


# ---------------------------------------------------------------------------
# Budget, flood waits, fairness
# ---------------------------------------------------------------------------


def _max_in_any_window(times: list[float], window: float = 60.0) -> int:
    times = sorted(times)
    best = 0
    for i, start in enumerate(times):
        best = max(best, sum(1 for t in times[i:] if t < start + window))
    return best


async def test_budget_caps_every_request_at_18_per_minute(logs, bot, clock) -> None:
    """Creates, sends, edits and deletes all draw on one budget."""
    for i in range(40):  # retention work: two batches due
        logs._posts.append([10_000 + i, clock.wall - 45 * HOUR])
    keys = [LogKey("agent", f"a{i}") for i in range(8)]

    done = False

    async def producer() -> None:
        # A new round once the last one is posted, so rolling messages are
        # both sent and edited.
        nonlocal done
        for round_ in range(12):
            for key in keys:
                logs.log(key, "run", f"line {round_} " + "x" * 700)
            while logs._order:
                await asyncio.sleep(0)
        done = True

    async def writer() -> None:
        while not done or logs._order:
            if not await logs.step():
                await asyncio.sleep(0)

    await asyncio.gather(producer(), writer(), logs.retention_pass())
    methods = set(bot.methods())
    assert {"create_forum_topic", "send_message", "edit_message_text",
            "delete_messages"} <= methods
    times = [t for m, _, t in bot.calls if m != "get_chat_member"]
    assert len(times) >= 100
    assert _max_in_any_window(times) == 18


async def test_retry_after_pauses_the_whole_writer(logs, bot, clock) -> None:
    logs.log(FINANCE, "Q3", "one")
    logs.log(FIDO, "Shop", "two")
    await logs.step()  # finance topic
    await logs.step()  # fido topic
    bot.fail["send_message"] = [_retry_after(30)]
    t0 = clock.mono
    await logs.step()  # finance send: flood wait
    logs._posts.append([999, clock.wall - 45 * HOUR])
    await logs.step()  # fido send waits out the pause
    await logs.retention_pass()
    await _drain(logs)

    after = [(m, t) for m, _, t in bot.calls if t > t0 or m == "send_message"]
    first_send_t = after[0][1]
    assert first_send_t == t0  # the refused one
    later = [t for m, t in after[1:]]
    assert later and all(t >= t0 + 30 for t in later)
    # The refused lines were not lost.
    assert sorted(_texts(bot)) == ["Q3 · one", "Q3 · one", "Shop · two"]
    assert bot.named("delete_messages") == [{"chat_id": CHAT, "message_ids": [999]}]


def test_retry_after_is_read_as_a_number_or_a_timedelta() -> None:
    assert agent_logs.retry_after_seconds(_retry_after(7)) == 7.0
    assert agent_logs.retry_after_seconds(
        SimpleNamespace(retry_after=dt.timedelta(seconds=2.5))) == 2.5


async def test_keys_are_served_round_robin(logs, bot) -> None:
    """A busy key cannot starve another: each turn is one operation."""
    big = "x" * 3000  # one line per message
    for i in range(5):
        logs.log(FINANCE, "Q3", f"{i} {big}")
    logs.log(FIDO, "Shop", "small")
    await _drain(logs)
    order = [(m, kw.get("name") or kw.get("message_thread_id"))
             for m, kw, _ in bot.calls]
    assert order[:4] == [
        ("create_forum_topic", "Finance"),
        ("create_forum_topic", "Fido"),
        ("send_message", 501),
        ("send_message", 502),
    ]
    assert [t for m, t in order[4:]] == [501] * 4


async def test_lines_that_arrive_while_a_key_waits_coalesce(logs, bot) -> None:
    logs.log(FINANCE, "Q3", "a")
    await logs.step()  # topic
    logs.log(FINANCE, "Q3", "b")
    logs.log(FINANCE, "Q3", "c")
    await _drain(logs)
    assert _texts(bot) == ["Q3 · a\nQ3 · b\nQ3 · c"]


# ---------------------------------------------------------------------------
# The rolling message
# ---------------------------------------------------------------------------


async def _post(sink: AgentLogs, key: LogKey, text: str) -> None:
    sink.log(key, "Q3", text)
    await _drain(sink)


async def test_rolling_message_is_edited_with_its_whole_text(logs, bot) -> None:
    await _post(logs, FINANCE, "a")
    await _post(logs, FINANCE, "b")
    assert bot.methods() == ["create_forum_topic", "send_message", "edit_message_text"]
    edit = bot.named("edit_message_text")[0]
    assert edit == {"text": "Q3 · a\nQ3 · b", "chat_id": CHAT, "message_id": 101}


async def test_a_new_message_when_the_text_would_exceed_4096(logs, bot) -> None:
    await _post(logs, FINANCE, "a" * 3000)
    await _post(logs, FINANCE, "b" * 1100)
    assert bot.methods() == ["create_forum_topic", "send_message", "send_message"]
    assert all(len(t) <= 4096 for t in _texts(bot))


async def test_a_new_message_after_five_idle_minutes(logs, bot, clock) -> None:
    await _post(logs, FINANCE, "a")
    clock.advance(299)
    await _post(logs, FINANCE, "b")
    clock.advance(301)
    await _post(logs, FINANCE, "c")
    assert bot.methods() == ["create_forum_topic", "send_message",
                             "edit_message_text", "send_message"]
    assert _texts(bot)[-1] == "Q3 · c"


async def test_a_new_message_after_an_hour_even_when_busy(logs, bot, clock) -> None:
    await _post(logs, FINANCE, "start")
    sent_at = logs._posts[0][1]
    for i in range(14):  # an edit every four minutes: never idle
        clock.advance(240)
        await _post(logs, FINANCE, f"e{i}")
    assert bot.methods().count("send_message") == 1
    clock.advance(250)  # 60 min 10 s since the first send, 250 s idle
    await _post(logs, FINANCE, "late")
    assert bot.methods()[-1] == "send_message"
    # A post's age counts from its first send: edits never moved it.
    assert logs._posts[0][1] == sent_at


async def test_one_entry_longer_than_a_message_is_split(logs, bot) -> None:
    body = "".join(chr(ord("a") + i % 26) for i in range(9000))
    await _post(logs, FINANCE, body)
    texts = _texts(bot)
    assert bot.methods() == ["create_forum_topic"] + ["send_message"] * 3
    assert all(len(t) <= 4096 for t in texts)
    assert "".join(texts) == f"Q3 · {body}"


async def test_utf16_length_counts_toward_the_limit(logs, bot) -> None:
    await _post(logs, FINANCE, "🔧" * 3000)  # 6000 UTF-16 units
    assert all(agent_logs._tg_len(t) <= 4096 for t in _texts(bot))
    assert len(_texts(bot)) == 2


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


async def test_not_modified_counts_as_done(logs, bot) -> None:
    await _post(logs, FINANCE, "a")
    bot.fail["edit_message_text"] = [BadRequest(NOT_MODIFIED)]
    await _post(logs, FINANCE, "b")
    await _post(logs, FINANCE, "c")
    assert bot.methods() == ["create_forum_topic", "send_message",
                             "edit_message_text", "edit_message_text"]
    assert _texts(bot)[-1] == "Q3 · a\nQ3 · b\nQ3 · c"


async def test_a_lost_message_to_edit_starts_a_new_one(logs, bot) -> None:
    await _post(logs, FINANCE, "a")
    bot.fail["edit_message_text"] = [BadRequest(EDIT_GONE)]
    await _post(logs, FINANCE, "b")
    assert bot.methods() == ["create_forum_topic", "send_message",
                             "edit_message_text", "send_message"]
    assert _texts(bot)[-1] == "Q3 · b"


@pytest.mark.parametrize("raw", [THREAD_GONE, "Topic_closed", "Topic_deleted"])
async def test_a_lost_topic_is_recreated_once_then_lines_drop(
    logs, bot, caplog, raw,
) -> None:
    bot.fail["send_message"] = [BadRequest(raw), BadRequest(raw)]
    with caplog.at_level(logging.WARNING, logger="agent_logs"):
        await _post(logs, FINANCE, "a")
    assert bot.methods() == ["create_forum_topic", "send_message",
                             "create_forum_topic", "send_message"]
    assert len([r for r in caplog.records if "dropped" in r.getMessage()]) == 1
    assert "agent:finance" not in logs._topics
    bot.calls.clear()
    await _post(logs, FINANCE, "b")
    assert bot.methods() == ["create_forum_topic", "send_message"]
    assert _texts(bot) == ["Q3 · b"]


async def test_a_lost_topic_recreated_once_delivers_the_lines(logs, bot) -> None:
    await _post(logs, FINANCE, "a")
    bot.fail["edit_message_text"] = [BadRequest(THREAD_GONE)]
    await _post(logs, FINANCE, "b")
    assert bot.methods() == ["create_forum_topic", "send_message",
                             "edit_message_text", "create_forum_topic",
                             "send_message"]
    assert bot.named("send_message")[-1]["message_thread_id"] == 502
    assert _texts(bot)[-1] == "Q3 · b"
    assert logs._topics["agent:finance"] == 502


async def test_losing_the_group_disables_until_the_next_check(
    logs, bot, caplog,
) -> None:
    logs.log(FIDO, "Shop", "queued")
    bot.fail["create_forum_topic"] = [Forbidden(KICKED)]
    with caplog.at_level(logging.WARNING, logger="agent_logs"):
        await _drain(logs)
    assert logs.enabled is False
    assert len(caplog.records) == 1
    logs.log(FINANCE, "Q3", "ignored")
    assert await logs.step() is False
    await logs.retention_pass()
    assert bot.methods() == ["create_forum_topic"]

    assert await logs.check_enabled(bot, CHAT) is True
    await _post(logs, FINANCE, "back")
    assert _texts(bot) == ["Q3 · back"]


async def test_any_other_error_drops_that_operations_lines(logs, bot, caplog) -> None:
    await _post(logs, FINANCE, "a")
    bot.fail["edit_message_text"] = [BadRequest("Something new")]
    with caplog.at_level(logging.WARNING, logger="agent_logs"):
        await _post(logs, FINANCE, "b")
    assert len(caplog.records) == 1
    await _post(logs, FINANCE, "c")
    assert _texts(bot)[-1] == "Q3 · a\nQ3 · c"


# ---------------------------------------------------------------------------
# Queue overflow
# ---------------------------------------------------------------------------


async def test_overflow_drops_the_same_keys_oldest_and_says_so(logs, bot) -> None:
    for i in range(agent_logs.QUEUE_LIMIT + 5):
        logs.log(FINANCE, "Q3", f"n{i}")
    await _drain(logs)
    texts = _texts(bot)
    assert texts[0].startswith("… 5 lines skipped\nQ3 · n5\n")
    joined = "\n".join(texts)
    assert "Q3 · n4\n" not in joined
    assert joined.endswith(f"Q3 · n{agent_logs.QUEUE_LIMIT + 4}")


async def test_overflow_takes_room_from_the_logging_key_not_the_busiest(
    logs, bot,
) -> None:
    for i in range(1500):
        logs.log(FINANCE, "Q3", f"n{i}")
    for i in range(agent_logs.QUEUE_LIMIT - 1500):
        logs.log(FIDO, "Shop", f"s{i}")
    logs.log(FIDO, "Shop", "newest")
    await _drain(logs)
    # Edits repeat a message's earlier lines; each distinct line counts once.
    finance_lines = "\n".join(t for t in _texts(bot) if "Q3" in t).split("\n")
    assert [f"Q3 · n{i}" for i in range(1500)] == [
        line for line in dict.fromkeys(finance_lines) if line.startswith("Q3")]
    assert not any(line.startswith("…") for line in finance_lines)
    joined = "\n".join(_texts(bot))
    assert "… 1 line skipped\nShop · s1\n" in joined
    assert "Shop · s0\n" not in joined


async def test_overflow_from_a_key_with_nothing_queued_takes_from_the_largest(
    logs, bot,
) -> None:
    for i in range(agent_logs.QUEUE_LIMIT):
        logs.log(FINANCE, "Q3", f"n{i}")
    logs.log(FIDO, "Shop", "still heard")
    await _drain(logs)
    texts = _texts(bot)
    assert "Shop · still heard" in texts
    assert any(t.startswith("… 1 line skipped\nQ3 · n1\n") for t in texts)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def test_retention_deletes_only_posts_older_than_44_hours(logs, bot, clock) -> None:
    now = clock.wall
    logs._posts = [[1, now - 44 * HOUR + 1], [2, now - 44 * HOUR - 1],
                   [3, now - 10]]
    await logs.retention_pass()
    assert bot.named("delete_messages") == [{"chat_id": CHAT, "message_ids": [2]}]
    assert [p[0] for p in logs._posts] == [1, 3]


async def test_retention_batches_at_most_100_ids(logs, bot, clock) -> None:
    logs._posts = [[i, clock.wall - 45 * HOUR] for i in range(250)]
    await logs.retention_pass()
    batches = [kw["message_ids"] for kw in bot.named("delete_messages")]
    assert [len(b) for b in batches] == [100, 100, 50]
    assert sorted(i for b in batches for i in b) == list(range(250))
    assert logs._posts == []


async def test_a_failed_batch_falls_back_to_one_id_at_a_time(logs, bot, clock) -> None:
    logs._posts = [[i, clock.wall - 45 * HOUR] for i in (1, 2, 3, 4)]
    bot.fail["delete_messages"] = [BadRequest(CANT_DELETE)]
    bot.fail["delete_message"] = [
        BadRequest(CANT_DELETE),          # 1: gone for good
        BadRequest(DELETE_GONE),          # 2: already gone
        BadRequest("Hmm"),                # 3: kept for the next pass
    ]                                     # 4: deleted
    await logs.retention_pass()
    assert bot.methods() == ["delete_messages"] + ["delete_message"] * 4
    assert [kw["message_id"] for kw in bot.named("delete_message")] == [1, 2, 3, 4]
    assert [p[0] for p in logs._posts] == [3]


async def test_posts_past_47_5_hours_are_forgotten_without_a_call(
    logs, bot, clock, caplog,
) -> None:
    now = clock.wall
    logs._posts = [[1, now - 47.5 * HOUR - 1], [2, now - 48 * HOUR],
                   [3, now - 47.5 * HOUR + 60]]
    with caplog.at_level(logging.WARNING, logger="agent_logs"):
        await logs.retention_pass()
    assert bot.named("delete_messages") == [{"chat_id": CHAT, "message_ids": [3]}]
    assert logs._posts == []
    warned = [r.getMessage() for r in caplog.records]
    assert len(warned) == 1 and "2 posts" in warned[0]


async def test_retention_runs_without_the_topics_right(clock, bot, tmp_path) -> None:
    """Deleting the bot's own posts needs no admin right."""
    sink = _make(clock, tmp_path)
    bot.member = _member()
    assert await sink.check_enabled(bot, CHAT) is False
    sink._posts = [[7, clock.wall - 45 * HOUR]]
    await sink.retention_pass()
    assert bot.named("delete_messages") == [{"chat_id": CHAT, "message_ids": [7]}]


# ---------------------------------------------------------------------------
# Enablement
# ---------------------------------------------------------------------------


async def test_enabled_only_for_an_admin_that_manages_topics(
    clock, bot, tmp_path, caplog,
) -> None:
    sink = _make(clock, tmp_path)
    assert await sink.check_enabled(bot, CHAT) is True
    assert bot.named("get_chat_member") == [{"chat_id": CHAT, "user_id": FakeBot.id}]

    with caplog.at_level(logging.WARNING, logger="agent_logs"):
        for member in (_admin(can_manage_topics=False),
                       _admin(can_manage_topics=None),
                       SimpleNamespace(status="administrator")):
            bot.member = member
            assert await sink.check_enabled(bot, CHAT) is False
        assert len(caplog.records) == 1  # the same reason warns once

        bot.member = _member()
        assert await sink.check_enabled(bot, CHAT) is False
        assert len(caplog.records) == 2

        bot.fail["get_chat_member"] = [BadRequest("Chat not found")]
        assert await sink.check_enabled(bot, CHAT) is False
        assert len(caplog.records) == 3

        caplog.clear()
        assert await sink.check_enabled(bot, 0) is False  # no group: silent
        assert caplog.records == []


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


async def test_registry_is_written_privately_and_reloads(clock, bot, tmp_path) -> None:
    sink = _make(clock, tmp_path)
    await sink.check_enabled(bot, CHAT)
    await _post(sink, FINANCE, "a")
    await sink._save()
    path = tmp_path / "log-topics.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["topics"] == {"agent:finance": 501}
    assert data["posts"] == [[101, clock.wall]]

    again = _make(clock, tmp_path)
    assert await again.check_enabled(bot, CHAT)
    bot.calls.clear()
    await _post(again, FINANCE, "b")
    assert bot.methods() == ["send_message"]  # topic remembered, new message
    assert [p[0] for p in again._posts] == [101, 102]


async def test_registry_writes_through_atomic_write_json(
    logs, monkeypatch, tmp_path,
) -> None:
    written: list[tuple[str, dict, int | None]] = []
    monkeypatch.setattr(agent_logs, "atomic_write_json",
                        lambda path, data, mode=None: written.append((path, data, mode)))
    await _post(logs, FINANCE, "a")
    await logs._save()
    assert written and written[-1][0] == str(tmp_path / "log-topics.json")
    assert written[-1][2] == 0o600


async def test_registry_writes_are_coalesced_to_one_per_ten_seconds(
    clock, bot, tmp_path, monkeypatch,
) -> None:
    writes: list[float] = []
    real = agent_logs.atomic_write_json

    def counting(path, data, mode=None):
        writes.append(clock.mono)
        real(path, data, mode=mode)

    monkeypatch.setattr(agent_logs, "atomic_write_json", counting)
    sink = _make(clock, tmp_path)
    await sink.check_enabled(bot, CHAT)
    sink.start()
    try:
        for i in range(6):
            sink.log(LogKey("agent", f"k{i}"), "r", "x")
            clock.advance(1)
            for _ in range(20):
                await asyncio.sleep(0.005)
    finally:
        await sink.stop()
    assert len(bot.named("send_message")) == 6
    assert writes, "nothing was written"
    gaps = [b - a for a, b in zip(writes, writes[1:-1])]
    assert all(g >= 10 for g in gaps), writes
    data = json.loads((tmp_path / "log-topics.json").read_text())
    assert len(data["posts"]) == 6 and len(data["topics"]) == 6


async def test_a_corrupt_registry_is_set_aside(clock, tmp_path) -> None:
    (tmp_path / "log-topics.json").write_text("{not json")
    sink = _make(clock, tmp_path)
    assert sink._topics == {} and sink._posts == []
    assert (tmp_path / "log-topics.json.casabak").exists()


async def test_a_different_group_forgets_the_old_topics(clock, bot, tmp_path) -> None:
    sink = _make(clock, tmp_path)
    await sink.check_enabled(bot, CHAT)
    await _post(sink, FINANCE, "a")
    assert await sink.check_enabled(bot, CHAT - 1)
    assert sink._topics == {} and sink._posts == []


# ---------------------------------------------------------------------------
# The writer task
# ---------------------------------------------------------------------------


async def test_the_writer_task_drains_and_stop_writes_once(
    clock, bot, tmp_path,
) -> None:
    sink = _make(clock, tmp_path)
    await sink.check_enabled(bot, CHAT)
    sink.start()
    sink.start()  # idempotent
    sink.log(FINANCE, "Q3", "a")
    sink.log(FIDO, "Shop", "b")
    for _ in range(200):
        if len(bot.named("send_message")) == 2:
            break
        await asyncio.sleep(0.005)
    await sink.stop()
    assert sorted(_texts(bot)) == ["Q3 · a", "Shop · b"]
    data = json.loads((tmp_path / "log-topics.json").read_text())
    assert len(data["posts"]) == 2


# ---------------------------------------------------------------------------
# Against the real library
# ---------------------------------------------------------------------------

_REAL_PTB_SCRIPT = r'''
import asyncio, json, os, sys, warnings
from urllib.parse import parse_qs

import httpx
import telegram
from telegram.request import HTTPXRequest

sys.path.insert(0, sys.argv[1])
import agent_logs
from agent_logs import AgentLogs, LogKey

CHAT = -100123
now = {"mono": 1000.0, "wall": 1_800_000_000.0}

async def sleep(seconds):
    now["mono"] += max(0.0, seconds)
    now["wall"] += max(0.0, seconds)
    await asyncio.sleep(0)

def fail(code, description, **params):
    body = {"ok": False, "error_code": code, "description": description}
    if params:
        body["parameters"] = params
    return httpx.Response(code, json=body)

MEMBER = {"status": "administrator",
          "user": {"id": 1, "is_bot": True, "first_name": "t"},
          "can_be_edited": False, "is_anonymous": False, "can_manage_chat": True,
          "can_delete_messages": False, "can_manage_video_chats": False,
          "can_restrict_members": False, "can_promote_members": False,
          "can_change_info": False, "can_invite_users": True,
          "can_post_stories": False, "can_edit_stories": False,
          "can_delete_stories": False, "can_manage_topics": True}

script = {
    "sendMessage": [fail(400, "Bad Request: message thread not found"), None,
                    fail(403, "Forbidden: bot was kicked from the supergroup chat")],
    "editMessageText": [
        fail(400, "Bad Request: message is not modified: specified new message "
                  "content and reply markup are exactly the same as a current "
                  "content and reply markup of the message"),
        fail(429, "Too Many Requests: retry after 3", retry_after=3),
    ],
    "deleteMessages": [fail(400, "Bad Request: message can't be deleted")],
    "deleteMessage": [fail(400, "Bad Request: message to delete not found")],
}
threads = iter([77, 78])
seen = []

def handler(request):
    name = request.url.path.rsplit("/", 1)[-1]
    form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8"),
                                         keep_blank_values=True).items()}
    seen.append((name, form, now["mono"]))
    queued = script.get(name)
    if queued:
        response = queued.pop(0)
        if response is not None:
            return response
    if name == "getMe":
        result = {"id": 1, "is_bot": True, "first_name": "t", "username": "t"}
    elif name == "getChatMember":
        result = MEMBER
    elif name == "createForumTopic":
        result = {"message_thread_id": next(threads), "name": form["name"],
                  "icon_color": 7322096}
    elif name in ("sendMessage", "editMessageText"):
        result = {"message_id": 5, "date": 0, "text": form["text"],
                  "chat": {"id": CHAT, "type": "supergroup", "is_forum": True},
                  "message_thread_id": int(form.get("message_thread_id") or 78)}
    else:
        result = True
    return httpx.Response(200, json={"ok": True, "result": result})

def endpoints():
    return [n for n, _, _ in seen if n != "getMe"]

async def drain(sink):
    while await sink.step():
        pass

async def main():
    bot = telegram.Bot("1:AA", request=HTTPXRequest(
        httpx_kwargs={"transport": httpx.MockTransport(handler)}))
    sink = AgentLogs(display_name=lambda role: "Finance", path=sys.argv[2],
                     monotonic=lambda: now["mono"], wall=lambda: now["wall"],
                     sleep=sleep)
    key = LogKey("agent", "finance")
    async with bot:
        assert await sink.check_enabled(bot, CHAT), "admin with topics right"
        assert seen[-1][1]["user_id"] == "1", seen[-1]

        sink.log(key, "Q3", "a")
        await drain(sink)
        assert endpoints() == ["getChatMember", "createForumTopic", "sendMessage",
                               "createForumTopic", "sendMessage"], endpoints()
        sends = [f for n, f, _ in seen if n == "sendMessage"]
        assert [f["message_thread_id"] for f in sends] == ["77", "78"], sends
        assert all("parse_mode" not in f for f in sends), sends
        assert sends[-1]["text"] == "Q3 · a"

        sink.log(key, "Q3", "b")
        await drain(sink)
        assert endpoints()[-1] == "editMessageText"
        n = len(seen)

        sink.log(key, "Q3", "c")
        t0 = now["mono"]
        await drain(sink)
        await drain(sink)
        edits = [(f["text"], t) for nm, f, t in seen[n:] if nm == "editMessageText"]
        assert len(edits) == 2, edits
        assert edits[1][1] >= t0 + 3, edits
        assert edits[1][0] == "Q3 · a\nQ3 · b\nQ3 · c", edits

        sink._posts = [[5, now["wall"] - 45 * 3600], [6, now["wall"] - 45 * 3600]]
        n = len(seen)
        await sink.retention_pass()
        assert endpoints()[-3:] == ["deleteMessages", "deleteMessage", "deleteMessage"]
        assert json.loads(seen[n][1]["message_ids"]) == [5, 6]
        assert sink._posts == [], sink._posts

        # The deleted post was the rolling message: the next line starts a new
        # one, and the bot has been removed from the group.
        sink.log(key, "Q3", "d")
        await drain(sink)
        assert endpoints()[-1] == "sendMessage"
        assert sink.enabled is False

asyncio.run(main())

# The flood wait's delay is read without the library's deprecation warning,
# both as the number it returns today and as the timedelta it can switch to.
flood = telegram.error.RetryAfter(3)
for switch in ("", "true"):
    os.environ["PTB_TIMEDELTA"] = switch
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        seconds = agent_logs.retry_after_seconds(flood)
    assert seconds == 3.0, (switch, seconds)
    assert not caught, [str(w.message) for w in caught]
print("OK")
'''


def test_the_real_library_errors_are_classified(tmp_path: Path) -> None:
    """In a fresh interpreter with python-telegram-bot itself: a lost topic is
    recreated, "not modified" is done, a flood wait pauses and is read without
    the library's deprecation warning, a failed bulk delete falls back to one id
    at a time, a kicked bot disables the log."""
    proc = subprocess.run(
        [sys.executable, "-c", _REAL_PTB_SCRIPT, str(CASA),
         str(tmp_path / "log-topics.json")],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip().endswith("OK"), proc.stdout
