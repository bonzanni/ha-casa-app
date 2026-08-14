"""INV-TRIG-0xx — a resident's scheduled Telegram turn can deliver media (#485).

A turn fired by a scheduled trigger carries a session-keying LABEL in
``chat_id`` (``f"{type}-{name}"``), not a chat id, so ``send_media`` refused it:
no numeric, nonzero chat. The lift is deliberately narrow:

* eligibility is a MARKER (``_scheduled_delivery``), never an inference from
  ``message_type == "scheduled"`` — the authenticated webhook route dispatches
  ``MessageType.SCHEDULED`` too (``casa_core.py``), and an inference would have
  admitted third-party webhook content to the operator's DM;
* the marker is RESERVED, so no external ingress can set it;
* the delivery identity is resolved at the POINT OF USE from
  ``operator_user_id()`` — the single, already fail-closed home of operator
  identity — never stamped into the message and never written over the label;
* the marker is read only by ``send_media`` here, not by ``turn_provenance()``,
  so the protected-action gate (``authz_grants``) cannot widen with it.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio

OPERATOR = 55501234
LABEL = "cron-weekly-invoice"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _scheduled_origin(**over):
    """The origin agent.py builds for a resident's own scheduled trigger turn."""
    origin = {
        "role": "assistant",
        "execution_role": "assistant",
        "channel": "telegram",
        "chat_id": LABEL,
        "user_id": None,
        "message_type": "scheduled",
        "source": "scheduler",
        "_scheduled_delivery": True,
    }
    origin.update(over)
    return origin


def _channel(operator_id=OPERATOR):
    ch = MagicMock()
    ch.operator_user_id = MagicMock(return_value=operator_id)
    ch.send_media = AsyncMock()
    return ch


def _install_channel(monkeypatch, ch):
    import tools as tools_mod

    mgr = MagicMock()
    mgr.get = MagicMock(return_value=ch)
    monkeypatch.setattr(tools_mod, "_channel_manager", mgr)
    return mgr


# ---------------------------------------------------------------------------
# The marker is unspoofable
# ---------------------------------------------------------------------------


def test_marker_is_reserved_and_stripped_from_external_context():
    """An external caller must never be able to set it: /invoke bodies, webhook
    payloads and voice frames all pass through this sanitizer."""
    from provenance import RESERVED_CONTEXT_KEYS, sanitize_external_context

    assert "_scheduled_delivery" in RESERVED_CONTEXT_KEYS
    cleaned = sanitize_external_context(
        {"_scheduled_delivery": True, "harmless": 1},
    )
    assert cleaned == {"harmless": 1}


def test_marker_is_copied_through_into_the_turn_origin():
    """agent._process copies a FIXED set of reserved markers off the bus
    context into origin_var. A marker the set does not name never reaches the
    origin, so the tool never sees it and the feature is simply dead."""
    import agent as agent_mod

    assert "_scheduled_delivery" in agent_mod.COPIED_CONTEXT_MARKERS


# ---------------------------------------------------------------------------
# Both scheduled dispatch sites stamp it — and only for telegram
# ---------------------------------------------------------------------------


class TestDispatchSitesStamp:
    async def test_scheduler_dispatch_stamps_the_marker(self):
        from aiohttp import web
        from config import TriggerSpec
        from trigger_registry import TriggerRegistry

        sched = MagicMock()
        bus = MagicMock()
        bus.send = AsyncMock()
        reg = TriggerRegistry(scheduler=sched, app=web.Application(), bus=bus)
        reg.register_agent(
            "assistant",
            [TriggerSpec(name="weekly-invoice", type="cron",
                         schedule="0 7 * * 1", channel="telegram",
                         prompt="invoice pass")],
            channels=["telegram"],
        )

        await sched.add_job.call_args.args[0]()

        msg = bus.send.call_args.args[0]
        assert msg.context["_scheduled_delivery"] is True
        # The label is untouched — it is the SDK session key and the voice-job
        # scope; overwriting it would merge unrelated conversations.
        assert msg.context["chat_id"] == "cron-weekly-invoice"

    async def test_scheduler_dispatch_does_not_stamp_a_non_telegram_trigger(self):
        from aiohttp import web
        from config import TriggerSpec
        from trigger_registry import TriggerRegistry

        sched = MagicMock()
        bus = MagicMock()
        bus.send = AsyncMock()
        reg = TriggerRegistry(scheduler=sched, app=web.Application(), bus=bus)
        reg.register_agent(
            "assistant",
            [TriggerSpec(name="hb", type="interval", minutes=60,
                         channel="voice", prompt="tick")],
            channels=["voice"],
        )

        await sched.add_job.call_args.args[0]()

        msg = bus.send.call_args.args[0]
        assert "_scheduled_delivery" not in msg.context

    async def test_reminder_sweep_stamps_the_marker(self, tmp_path):
        """The sweep is the SECOND dispatch site — it delivers what the
        scheduler could not (a past-dated reminder after a restart). If only
        one site stamps, a reminder's media works or not depending on whether
        Casa happened to be up when it came due."""
        from datetime import datetime, timedelta, timezone

        import reminders

        agents_dir = tmp_path / "agents"
        (agents_dir / "assistant").mkdir(parents=True)
        path = agents_dir / "assistant" / "triggers.yaml"
        path.write_text("schema_version: 1\ntriggers: []\n", encoding="utf-8")

        now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        reminders.add_entry(str(path), {
            "name": "reminder-overdue1", "type": "date",
            "at": "2026-08-15T08:00:00+02:00", "one_shot": True,
            "channel": "telegram", "managed_by": "agent",
            "prompt": "send the invoice",
        })

        bus = MagicMock()
        bus.send = AsyncMock()
        runtime = types.SimpleNamespace(
            agents_dir=str(agents_dir), bus=bus, trigger_registry=None,
            role_configs={"assistant": object()},
        )

        delivered = await reminders.sweep_reminders(runtime, now)

        assert delivered == 1
        msg = bus.send.await_args.args[0]
        assert msg.context["_scheduled_delivery"] is True
        assert msg.context["chat_id"] == "date-reminder-overdue1"


# ---------------------------------------------------------------------------
# The eligibility helper — every arm fail-closed
# ---------------------------------------------------------------------------


class TestOperatorTarget:
    def test_scheduled_telegram_turn_resolves_the_operator(self, monkeypatch):
        import tools as tools_mod

        _install_channel(monkeypatch, _channel())
        assert tools_mod._scheduled_operator_target(_scheduled_origin()) == (
            OPERATOR, OPERATOR,
        )

    def test_no_marker_no_target(self, monkeypatch):
        """A plain scheduled turn from before this feature, or any turn Casa
        did not stamp, stays text-only."""
        import tools as tools_mod

        _install_channel(monkeypatch, _channel())
        origin = _scheduled_origin()
        del origin["_scheduled_delivery"]
        assert tools_mod._scheduled_operator_target(origin) is None

    @pytest.mark.parametrize("marker", [False, "true", 1, None])
    def test_marker_must_be_exactly_true(self, monkeypatch, marker):
        import tools as tools_mod

        _install_channel(monkeypatch, _channel())
        assert tools_mod._scheduled_operator_target(
            _scheduled_origin(_scheduled_delivery=marker),
        ) is None

    def test_non_telegram_channel_no_target(self, monkeypatch):
        import tools as tools_mod

        _install_channel(monkeypatch, _channel())
        assert tools_mod._scheduled_operator_target(
            _scheduled_origin(channel="voice"),
        ) is None

    def test_delegated_execution_no_target(self, monkeypatch):
        """A delegated specialist inherits the whole parent origin, marker
        included — it must not inherit the delivery target with it."""
        import tools as tools_mod

        _install_channel(monkeypatch, _channel())
        assert tools_mod._scheduled_operator_target(
            _scheduled_origin(execution_role="finance-specialist"),
        ) is None

    def test_engagement_context_no_target(self, monkeypatch):
        import tools as tools_mod

        _install_channel(monkeypatch, _channel())
        token = tools_mod.engagement_var.set(
            types.SimpleNamespace(id="e1", kind="specialist", status="active"),
        )
        try:
            assert tools_mod._scheduled_operator_target(
                _scheduled_origin(),
            ) is None
        finally:
            tools_mod.engagement_var.reset(token)

    @pytest.mark.parametrize("configured", [None, 0, -1002233445566])
    def test_no_configured_operator_no_target(self, monkeypatch, configured):
        """Identity v0.136: with no operator configured NOBODY is the operator.
        A group id is negative and must never become a delivery target."""
        import tools as tools_mod

        _install_channel(monkeypatch, _channel(operator_id=configured))
        assert tools_mod._scheduled_operator_target(_scheduled_origin()) is None

    def test_no_channel_no_target(self, monkeypatch):
        import tools as tools_mod

        monkeypatch.setattr(tools_mod, "_channel_manager", None)
        assert tools_mod._scheduled_operator_target(_scheduled_origin()) is None


# ---------------------------------------------------------------------------
# send_media end to end
# ---------------------------------------------------------------------------


PDF = b"%PDF-1.4 fake invoice"


class TestSendMediaFromScheduledTurn:
    """Real outbox + real tool handler — the harness of test_send_media_tool.py."""

    @pytest.fixture
    def wired(self, tmp_path):
        import json
        import os

        import plugin_outbox
        import tools as tools_mod

        ob = plugin_outbox.init_outbox(str(tmp_path / "plugin-outbox"))
        ch = _channel()
        cm = MagicMock()
        cm.get.return_value = ch
        tools_mod.init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        )
        path = os.path.join(ob._root_realpath, "invoice-2026-08.pdf")
        with open(path, "wb") as fh:
            fh.write(PDF)
        try:
            yield types.SimpleNamespace(
                ch=ch, path=path, decode=lambda r: json.loads(
                    r["content"][0]["text"]),
            )
        finally:
            ob.close()
            plugin_outbox._OUTBOX = None

    async def _call(self, origin, path):
        import agent as agent_mod
        import tools as tools_mod

        token = agent_mod.origin_var.set(origin)
        try:
            return await tools_mod.send_media.handler(
                {"path": path, "kind": "document"},
            )
        finally:
            agent_mod.origin_var.reset(token)

    async def test_delivers_to_the_operator_dm(self, wired):
        res = wired.decode(await self._call(_scheduled_origin(), wired.path))

        assert res["status"] == "ok", res
        wired.ch.send_media.assert_awaited_once()
        # The DELIVERY chat is the operator's — resolved at use, never written
        # into the turn's own session label.
        assert wired.ch.send_media.await_args.kwargs["context"]["chat_id"] == (
            OPERATOR
        )

    async def test_refused_when_no_operator_is_configured(self, wired):
        wired.ch.operator_user_id.return_value = None

        res = wired.decode(await self._call(_scheduled_origin(), wired.path))

        assert res["status"] == "error"
        assert res["kind_error"] == "invalid_origin"
        wired.ch.send_media.assert_not_awaited()

    async def test_unmarked_scheduled_turn_still_refused(self, wired):
        """The webhook route also dispatches MessageType.SCHEDULED. It is never
        marked, and it must stay unable to reach the operator's DM."""
        origin = _scheduled_origin(source="webhook")
        del origin["_scheduled_delivery"]

        res = wired.decode(await self._call(origin, wired.path))

        assert res["status"] == "error"
        assert res["kind_error"] == "invalid_origin"
        wired.ch.send_media.assert_not_awaited()

    async def test_ordinary_dm_turn_is_unaffected(self, wired):
        origin = {
            "role": "assistant", "execution_role": "assistant",
            "channel": "telegram", "chat_id": 987654, "user_id": 987654,
            "message_type": "channel_in", "source": "telegram",
        }

        res = wired.decode(await self._call(origin, wired.path))

        assert res["status"] == "ok", res
        assert wired.ch.send_media.await_args.kwargs["context"]["chat_id"] == (
            987654
        )
        # The operator lookup is not even consulted on the ordinary path.
        wired.ch.operator_user_id.assert_not_called()


# ---------------------------------------------------------------------------
# The marker must survive an async delegation
# ---------------------------------------------------------------------------


# The behavioural counterpart of this — scheduled -> delegate -> completion
# resumes the resident WITH the marker — lives beside its harness in
# tests/test_notification_handling.py::TestSynthesisPreservesOriginMarkers.
