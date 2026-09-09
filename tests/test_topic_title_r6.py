"""W-R6 (v0.81.0) — persisted short topic title, durably shared.

The engager-supplied ``topic_title`` is normalized ONCE at ingest, PERSISTED on
the EngagementRecord (additive + absent-tolerant load), and read by BOTH the
topic-name STATE-EDIT site (telegram.update_topic_state) and the live-SUMMARY
title source (claude_code_driver._summary_goal_line) — a single durable source.
Legacy rows with no persisted title fall back to the Casa-derived concise_task
label with no crash.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Persistence: round-trip + legacy-absent load.
# ---------------------------------------------------------------------------


async def test_topic_title_persists_and_round_trips(tmp_path):
    from engagement_registry import EngagementRegistry

    path = str(tmp_path / "e.json")
    reg = EngagementRegistry(tombstone_path=path, bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "wire up the Gmail plugin",
        {}, topic_id=7, topic_title="Gmail plugin",
    )
    assert rec.topic_title == "Gmail plugin"

    # The field is in the on-disk tombstone.
    on_disk = json.loads((tmp_path / "e.json").read_text())
    assert on_disk[0]["topic_title"] == "Gmail plugin"

    # A fresh registry reloading the tombstone recovers it.
    reg2 = EngagementRegistry(tombstone_path=path, bus=None)
    await reg2.load()
    assert reg2.get(rec.id).topic_title == "Gmail plugin"


async def test_legacy_row_without_topic_title_loads_as_empty(tmp_path):
    from engagement_registry import EngagementRegistry

    tombstone = tmp_path / "e.json"
    # A pre-v0.81 row: NO topic_title key at all.
    tombstone.write_text(json.dumps([
        {
            "id": "legacy1",
            "kind": "executor",
            "role_or_type": "configurator",
            "driver": "claude_code",
            "status": "idle",
            "topic_id": 42,
            "started_at": 1000.0,
            "last_user_turn_ts": 1000.0,
            "last_idle_reminder_ts": 0.0,
            "completed_at": None,
            "sdk_session_id": None,
            "origin": {"role": "assistant"},
            "task": "old task",
        }
    ]))
    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await reg.load()  # absent-tolerant: must not raise
    rec = reg.get("legacy1")
    assert rec is not None
    assert rec.topic_title == ""


# ---------------------------------------------------------------------------
# Reader 1: the live-summary title source (_summary_goal_line).
# ---------------------------------------------------------------------------


def _record(**over):
    from engagement_registry import EngagementRecord

    base = dict(
        id="e1", kind="executor", role_or_type="configurator",
        driver="claude_code", status="active", topic_id=1,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={},
        task="wire up the Gmail plugin",
    )
    base.update(over)
    return EngagementRecord(**base)


def test_summary_goal_line_reads_persisted_title():
    from drivers.claude_code_driver import ClaudeCodeDriver

    rec = _record(topic_title="Gmail plugin")
    assert ClaudeCodeDriver._summary_goal_line(rec) == "Gmail plugin"


def test_summary_goal_line_falls_back_to_derived_label():
    from drivers.claude_code_driver import ClaudeCodeDriver
    from channels.state_emoji import concise_task

    # No persisted title → the Casa-derived concise_task label (legacy parity).
    rec = _record(topic_title="")
    assert ClaudeCodeDriver._summary_goal_line(rec) == concise_task(
        "wire up the Gmail plugin")


# ---------------------------------------------------------------------------
# Reader 2: the topic-name STATE-EDIT site (telegram.update_topic_state).
# ---------------------------------------------------------------------------


async def _make_channel_with_topic(fake_telegram_bot, reg, *, topic_title, task):
    from channels.telegram import TelegramChannel

    ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                         engagement_supergroup_id=-1001)
    ch._engagement_registry = reg
    msg = await fake_telegram_bot.create_forum_topic(-1001, name="seed")
    tid = msg.message_thread_id
    rec = await reg.create(
        "executor", "configurator", "claude_code", task, {}, topic_id=tid,
        topic_title=topic_title,
    )
    return ch, rec, tid


async def test_state_edit_uses_persisted_title(fake_telegram_bot, tmp_path):
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    ch, rec, tid = await _make_channel_with_topic(
        fake_telegram_bot, reg,
        topic_title="Gmail plugin", task="wire up the Gmail plugin")

    await ch.update_topic_state(engagement_id=rec.id, new_state="completed")

    topic = fake_telegram_bot._supergroups[-1001].topics[tid]
    # ✅ (completed) + the PERSISTED short title — not a concise_task(task).
    assert topic.name == "✅ Gmail plugin"


async def test_state_edit_falls_back_to_derived_label_for_legacy(
    fake_telegram_bot, tmp_path,
):
    from engagement_registry import EngagementRegistry
    from channels.state_emoji import concise_task

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    ch, rec, tid = await _make_channel_with_topic(
        fake_telegram_bot, reg,
        topic_title="", task="wire up the Gmail plugin")

    await ch.update_topic_state(engagement_id=rec.id, new_state="completed")

    topic = fake_telegram_bot._supergroups[-1001].topics[tid]
    assert topic.name == f"✅ {concise_task('wire up the Gmail plugin')}"
