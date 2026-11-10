"""The engagement launch owner's generic launch behaviour.

These cases were written against ``engage_executor``, the configurator's
engagement launcher. The configurator is a delegated run now and that tool is
gone; what they pin about an engagement LAUNCH — that it needs no forum
group, the engager's ``engagement_var`` scope, and the
compensation of a launch cancelled after its record exists — belongs to the one
launcher left, ``tools._launch_interactive_engagement`` (a background job's
engagement), and is pinned here through it. The configurator-only cases (types,
the brief, the duplicate-task guard, the executor plugin gate, the topic title,
the hand-off lines) went with the tool. Class and test names are kept so their
node ids stay stable.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from tests.test_delegate_to_agent_interactive import (
        _make_alex_cfg, _make_assistant_cfg, launch_job)
except ImportError:
    from test_delegate_to_agent_interactive import (
        _make_alex_cfg, _make_assistant_cfg, launch_job)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

_ORIGIN = {"role": "assistant", "channel": "telegram",
           "chat_id": "c1", "cid": "x", "user_text": "hi"}


def _channel(*, supergroup=-100123):
    channel = MagicMock()
    channel.engagement_supergroup_id = supergroup
    channel.bot = MagicMock()
    return channel


def _wire(channel, registry):
    from tools import init_tools
    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)
    specialist_reg = MagicMock()
    specialist_reg.get.return_value = _make_alex_cfg()
    init_tools(
        channel_manager=cm, bus=MagicMock(),
        specialist_registry=specialist_reg, mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=registry,
        agent_role_map={"assistant": _make_assistant_cfg()},
    )


def _registry(tmp_path):
    from engagement_registry import EngagementRegistry
    return EngagementRegistry(tombstone_path=str(tmp_path / "engagements.json"))


def _payload(result):
    return json.loads(result["content"][0]["text"])


class TestEngageExecutorReal:
    async def test_no_log_group_still_launches(self, tmp_path, monkeypatch):
        """No forum group: the launch neither refuses nor sets anything up
        in the group; the job starts."""
        import agent as agent_mod
        channel = _channel(supergroup=0)
        _wire(channel, _registry(tmp_path))
        monkeypatch.setattr(agent_mod, "active_engagement_driver",
                            MagicMock(open=AsyncMock()), raising=False)
        payload = _payload(await launch_job("t", origin=dict(_ORIGIN)))
        assert payload["status"] == "pending", payload

    async def test_non_telegram_origin_gets_accurate_error(self, tmp_path):
        """R-2 (v0.69.7): a non-Telegram origin is told jobs originate from
        Telegram."""
        _wire(_channel(supergroup=0), _registry(tmp_path))
        payload = _payload(await launch_job(
            "t", origin={**_ORIGIN, "channel": "webhook"}))
        assert payload["kind"] == "engagement_wrong_origin"
        assert "Telegram" in payload["message"]
        assert "supergroup" not in payload["message"].lower()

    async def test_does_not_leak_engagement_var_to_caller(
        self, tmp_path, monkeypatch,
    ):
        """The launch must not bind engagement_var in the engager's scope: the
        driver sets it only inside the engagement's own turn. The engager's
        task observes engagement_var == None both before and after the call."""
        import agent as agent_mod
        from tools import engagement_var
        _wire(_channel(), _registry(tmp_path))
        monkeypatch.setattr(agent_mod, "active_engagement_driver",
                            MagicMock(open=AsyncMock()), raising=False)

        assert engagement_var.get(None) is None  # pre-state
        r = await launch_job("t", origin=dict(_ORIGIN))
        assert engagement_var.get(None) is None  # post-state
        assert _payload(r)["status"] == "pending"


class TestCancelledLaunchAfterRecordCompensates:
    """Sol r1 (#363 family): a cancellation AFTER create() but BEFORE the
    driver started used to leave a durably-active record with nothing
    driving it — the record must be marked errored, in the background."""

    async def test_cancel_during_driver_start_marks_error(
            self, tmp_path, monkeypatch):
        import agent as agent_mod
        import tools as tools_mod

        registry = _registry(tmp_path)
        channel = _channel()
        _wire(channel, registry)

        started = asyncio.Event()

        async def _hanging_open(rec, *, options):
            started.set()
            await asyncio.Event().wait()

        # The launcher awaits the session's open inline, so the cancellation
        # lands inside it.
        fake_driver = MagicMock(
            open=AsyncMock(side_effect=_hanging_open),
            cancel=AsyncMock(),
        )
        monkeypatch.setattr(
            agent_mod, "active_engagement_driver", fake_driver,
            raising=False,
        )

        task = asyncio.ensure_future(launch_job("do the thing",
                                                origin=dict(_ORIGIN)))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await tools_mod.drain_launch_death_reports()
        pending = list(tools_mod._CANCELLED_LAUNCH_TASKS)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # The record is terminally errored — not left active with no driver.
        recs = list(registry._records.values())
        assert len(recs) == 1
        assert recs[0].status == "error"
        # Terra/Sol r2: the driver may have gone LIVE before the cancellation
        # — the compensation runs its idempotent terminal teardown.
        fake_driver.cancel.assert_awaited_once()
        assert fake_driver.cancel.await_args.args[0].id == recs[0].id
        # the brief was never queued
        assert fake_driver.enqueue_brief.call_count == 0
