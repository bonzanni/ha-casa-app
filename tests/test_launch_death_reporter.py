"""#678 — the launch-death reporter's remaining count and mutation pins.

Separate from the frozen red case (``test_in_casa_launch_terminal_artifact.py``,
accepted and not to be edited) and from its H2 guard. Everything here exists to
make one specific mutation fail; the mapping is named on each test.

No ``asyncio.sleep`` is patched anywhere: a wedged counterparty is modelled as
an ``asyncio.Event`` that is never set, and the production bound is shortened
instead. Patching ``<module>.asyncio.sleep`` is the shared module attribute and
is forbidden in this repo (it once OOM-killed the whole VM).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from tests.test_in_casa_launch_terminal_artifact import (
        ScriptedCompleteClient, ScriptedCutoffClient, _Probe, _build, _launch,
        _mock_executor_def,
    )
except ImportError:  # pragma: no cover — direct-path collection
    from test_in_casa_launch_terminal_artifact import (
        ScriptedCompleteClient, ScriptedCutoffClient, _Probe, _build, _launch,
        _mock_executor_def,
    )

pytestmark = [pytest.mark.asyncio]


class TextlessCompleteClient(ScriptedCutoffClient):
    """A launch turn that RAN TO ITS END — one ResultMessage — but streamed no
    assistant text and called no emit_completion. Nothing about the engagement
    is visible anywhere: the `no_visible_output` arm."""

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage, ResultMessage, ToolUseBlock,
        )
        use = ToolUseBlock(id="t1", name="Read", input={"file_path": "/x"})
        type(self).frames_yielded += 1
        yield AssistantMessage(content=[use], model="claude-sonnet-4-6")
        rm = ResultMessage.__new__(ResultMessage)
        for k, v in dict(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="s", total_cost_usd=0.0,
            usage={}, result="", stop_reason=None, parent_tool_use_id=None,
        ).items():
            setattr(rm, k, v)
        type(self).result_messages_yielded += 1
        type(self).frames_yielded += 1
        yield rm


def _record(rec_id: str, **overrides):
    from engagement_registry import EngagementRecord
    base = dict(
        id=rec_id, kind="executor", role_or_type="configurator",
        driver="in_casa", status="active", topic_id=42, started_at=0.0,
        last_user_turn_ts=0.0, last_idle_reminder_ts=0.0, completed_at=None,
        sdk_session_id=None, origin={}, task="t",
    )
    base.update(overrides)
    return EngagementRecord(**base)


async def _payload(envelope):
    return json.loads(envelope["content"][0]["text"])


class TestTheOtherArm:
    async def test_a_complete_but_mute_launch_turn_is_reported(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: drop the `no_visible_output` arm → this fails.

        A ResultMessage arrived, so the turn was not cut off; but nothing was
        posted and nothing was completed, so the operator has a topic with no
        evidence anything happened.
        """
        probe = _Probe()
        engage_executor, registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, TextlessCompleteClient,
        )
        envelope = await _launch(engage_executor)
        payload = await _payload(envelope)

        assert TextlessCompleteClient.result_messages_yielded == 1
        assert probe.finalize_count == 0
        assert payload["status"] == "error", payload
        assert payload["kind"] == "launch_turn_incomplete"
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"
        assert channel._post_engagement_notice.await_count == 1
        assert channel.close_topic.await_count == 1


class TestTheDriverDoesNotAdjudicate:
    async def test_the_observation_ignores_the_records_own_status(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: read the record's status in the driver → this fails.

        A lock-free status read there could observe the uncommitted window of a
        strict terminal transition that a persist failure then ROLLS BACK
        (engagement_registry.py commits the terminal fields in memory before
        awaiting the tombstone write). The driver must therefore observe the
        turn's own facts and nothing else; the registry adjudicates.
        """
        from claude_agent_sdk import ClaudeAgentOptions
        from drivers.in_casa_driver import (
            InCasaDriver, LAUNCH_MISSING_RESULT,
        )
        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", ScriptedCutoffClient)

        class _Handle:
            async def emit(self, text):
                pass

            async def finalize(self, text):
                pass

        drv = InCasaDriver(topic_stream_factory=lambda t: _Handle())
        rec = _record("a" * 32)
        # The record LOOKS terminal to a lock-free reader — exactly the
        # transient state a rollback erases.
        rec.status = "completed"

        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="s"))

        assert drv.launch_turn_incomplete(rec.id) == LAUNCH_MISSING_RESULT
        # And popping is a pop: a second read cannot report the same death.
        assert drv.launch_turn_incomplete(rec.id) == ""

    async def test_a_ticketed_follow_up_records_no_observation(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: record the observation on ticketed turns too → this fails.

        A follow-up turn's failure owner is the Telegram delivery task's
        "Turn failed" notice (#649); this cluster is scoped to the launch turn,
        and a ticketed write could clobber an unread launch observation.
        """
        from claude_agent_sdk import ClaudeAgentOptions
        from drivers.in_casa_driver import InCasaDriver
        monkeypatch.setattr(
            "drivers.in_casa_driver.ClaudeSDKClient", ScriptedCompleteClient)

        class _Handle:
            async def emit(self, text):
                pass

            async def finalize(self, text):
                pass

        drv = InCasaDriver(topic_stream_factory=lambda t: _Handle())
        rec = _record("b" * 32)
        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="s"))
        assert drv.launch_turn_incomplete(rec.id) == ""

        # Now a FOLLOW-UP turn on the same client that ends without a
        # ResultMessage. It must leave the launch slot untouched.
        async def _truncated(self):
            from claude_agent_sdk import AssistantMessage, TextBlock
            yield AssistantMessage(
                content=[TextBlock(text="working")], model="m")

        monkeypatch.setattr(
            type(drv._clients[rec.id]), "receive_response", _truncated)
        await drv.send_user_turn(rec, "and now this")
        assert drv.launch_turn_incomplete(rec.id) == ""


class TestEveryBoundIsRealAndEveryCountIsOne:
    """MUTATIONS: remove the notice timeout / the paint bound / the close bound
    / the driver.cancel bound → the matching case below fails. Each wedged
    counterparty is an Event that is never set; no asyncio.sleep is patched."""

    async def test_a_notice_that_never_returns_still_reaches_the_close(
        self, tmp_path, monkeypatch,
    ):
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_TOPIC_OP_TIMEOUT_S", 0.05)
        probe = _Probe()
        engage_executor, registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(rec, text):
            probe.events.append("notice")
            await wedged.wait()

        channel._post_engagement_notice = AsyncMock(side_effect=_hang)

        envelope = await asyncio.wait_for(_launch(engage_executor), 15)

        assert (await _payload(envelope))["status"] == "error"
        assert channel._post_engagement_notice.await_count == 1
        assert probe.driver_cancel_calls == 1
        assert channel.close_topic.await_count == 1
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"

    async def test_a_paint_that_never_returns_still_reaches_the_close(
        self, tmp_path, monkeypatch,
    ):
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_TOPIC_OP_TIMEOUT_S", 0.05)
        probe = _Probe()
        engage_executor, _registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(*, engagement_id, new_state):
            await wedged.wait()

        channel.update_topic_state = AsyncMock(side_effect=_hang)

        envelope = await asyncio.wait_for(_launch(engage_executor), 15)

        assert (await _payload(envelope))["status"] == "error"
        assert channel.update_topic_state.await_count == 1
        assert channel.close_topic.await_count == 1

    async def test_a_close_that_never_returns_still_retires_the_reporter(
        self, tmp_path, monkeypatch,
    ):
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_TOPIC_OP_TIMEOUT_S", 0.05)
        probe = _Probe()
        engage_executor, _registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(*, thread_id):
            await wedged.wait()

        channel.close_topic = AsyncMock(side_effect=_hang)

        envelope = await asyncio.wait_for(_launch(engage_executor), 15)

        assert (await _payload(envelope))["status"] == "error"
        assert channel.close_topic.await_count == 1
        assert not tools_mod._LAUNCH_DEATH_TASKS

    async def test_a_driver_cancel_that_never_returns_still_tells_the_operator(
        self, tmp_path, monkeypatch,
    ):
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_DRIVER_CANCEL_TIMEOUT_S", 0.05)
        probe = _Probe()
        engage_executor, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(engagement):
            probe.driver_cancel_calls += 1
            await wedged.wait()

        monkeypatch.setattr(driver, "cancel", _hang)

        envelope = await asyncio.wait_for(_launch(engage_executor), 15)

        assert (await _payload(envelope))["status"] == "error"
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"
        assert channel._post_engagement_notice.await_count == 1
        assert probe.driver_cancel_calls == 1
        assert channel.close_topic.await_count == 1


class TestTheStrictTransitionIsTheAuthority:
    async def test_a_persist_failure_reports_nothing_and_retires_the_client(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: use `mark_error` instead of the strict primitive, or drop
        the PERSIST_FAILED branch → this fails.

        `mark_error` persists BEST-EFFORT: it swallows a tombstone-write
        failure and still returns True, which would authorize an irreversible
        topic close over a record disk still calls `active`. The strict
        primitive rolls the record back and re-raises, and the reporter then
        posts nothing, paints nothing and closes nothing — an open topic over a
        live record is recoverable, a closed one is not. It DOES retire the
        driver client: with no exception out of the driver, start()'s rollback
        never ran, so `is_alive` would hand the next operator message to an
        ended client instead of resuming from the persisted session id.
        """
        probe = _Probe()
        engage_executor, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )

        real_write = registry._write_tombstone

        def _boom_on_terminal(snapshot):
            # Fail ONLY the terminal write: create() persists strictly too, and
            # failing that would abort the launch before it ever ran a turn.
            if any(row.get("status") == "error" for row in snapshot):
                raise OSError("tombstone write refused")
            return real_write(snapshot)

        monkeypatch.setattr(registry, "_write_tombstone", _boom_on_terminal)

        envelope = await _launch(engage_executor)
        payload = await _payload(envelope)

        assert payload["status"] == "error", payload
        assert payload["kind"] == "launch_turn_incomplete"
        created_id = next(iter(registry._records))
        rec = registry.get(created_id)
        assert rec.status == "active", rec.status      # rolled fully back
        assert rec.completed_at is None
        assert channel._post_engagement_notice.await_count == 0
        assert channel.update_topic_state.await_count == 0
        assert channel.close_topic.await_count == 0
        assert probe.driver_cancel_calls == 1
        assert driver.is_alive(rec) is False

    async def test_a_lost_terminal_race_reports_nothing_and_stays_pending(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: ignore the strict transition's winner bool → this fails.

        The engagement reported itself between the turn's end and the owner's
        read (its own emit_completion, or a concurrent /complete or /cancel).
        That writer owns every side effect; this path must perform ZERO and
        tell the engager exactly what a successful launch is told.
        """
        probe = _Probe()
        engage_executor, registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        real_create = registry.create

        async def create_then_terminalize(*a, **kw):
            rec = await real_create(*a, **kw)
            # Stand in for emit_completion winning the flip durably.
            await registry.try_transition_terminal(
                rec.id, "completed", strict=True)
            return rec

        monkeypatch.setattr(registry, "create", create_then_terminalize)

        envelope = await _launch(engage_executor)
        payload = await _payload(envelope)

        assert payload["status"] == "pending", payload
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "completed"
        assert channel._post_engagement_notice.await_count == 0
        assert channel.update_topic_state.await_count == 0
        assert channel.close_topic.await_count == 0


class TestCancellationHasAnOwner:
    async def test_the_reporter_survives_its_launchers_cancellation(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: await the reporter unshielded → this fails.

        The registry deliberately KEEPS a durable terminal state and re-raises
        when a cancellation lands after its write committed, so an inline owner
        could commit the flip and then never post, paint or close — and a
        second, compensating reporter would correctly LOSE the transition and
        do nothing, leaving the durable error with no side-effect owner at all.
        """
        import tools as tools_mod
        probe = _Probe()
        engage_executor, registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        entered = asyncio.Event()
        real_notice = channel._post_engagement_notice

        async def _slow_notice(rec, text):
            entered.set()
            await asyncio.sleep(0)
            await real_notice(rec, text)

        channel._post_engagement_notice = AsyncMock(side_effect=_slow_notice)

        task = asyncio.ensure_future(_launch(engage_executor))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The launcher is gone; the reporter finished the job anyway.
        for _ in range(200):
            if not tools_mod._LAUNCH_DEATH_TASKS:
                break
            await asyncio.sleep(0)
        assert not tools_mod._LAUNCH_DEATH_TASKS
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"
        assert channel._post_engagement_notice.await_count == 1
        assert channel.close_topic.await_count == 1

    async def test_a_cancelled_launch_now_posts_before_it_closes(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: drop the reporter from the cancellation compensator →
        this fails.

        Before #678 a cancelled launch flipped the topic to failed and closed
        it having posted NOTHING: the operator saw a ❌ title over an empty
        topic and could not tell whether the task had mutated anything.
        """
        import tools as tools_mod
        probe = _Probe()
        engage_executor, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        entered = asyncio.Event()
        wedged = asyncio.Event()

        async def _hang(engagement, prompt, options, expected_generation=None):
            entered.set()
            await wedged.wait()

        monkeypatch.setattr(driver, "start", _hang)

        task = asyncio.ensure_future(_launch(engage_executor))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(500):
            if not tools_mod._LAUNCH_DEATH_TASKS and channel.close_topic.await_count:
                break
            await asyncio.sleep(0)

        created_id = next(iter(registry._records))
        rec = registry.get(created_id)
        assert rec.status == "error", rec.status
        assert rec.origin["error_kind"] == "launch_cancelled"
        assert channel._post_engagement_notice.await_count == 1
        assert channel.close_topic.await_count == 1
        assert probe.events.index("notice") < probe.events.index("topic_close")


class TestTheBoundaries:
    async def test_a_healthy_claude_code_launch_is_untouched(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: call `launch_turn_incomplete` outside the in_casa branches
        → this fails.

        Neither `DriverProtocol` nor `ClaudeCodeDriver` defines the method, so
        a bare attribute access would raise AttributeError and the launcher's
        generic `except Exception` would mark a HEALTHY engagement errored and
        abort its topic.
        """
        import agent as agent_mod

        class _ClaudeCodeStub:
            """No `launch_turn_incomplete` — deliberately not a MagicMock,
            which would fabricate the attribute and hide the boundary."""

            def __init__(self):
                self.starts = 0

            async def start(self, rec, prompt=None, options=None,
                            expected_generation=None):
                self.starts += 1

            async def cancel(self, rec):
                pass

        probe = _Probe()
        engage_executor, registry, channel, _d = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
            driver_kind="claude_code",
        )
        stub = _ClaudeCodeStub()
        monkeypatch.setattr(
            agent_mod, "active_claude_code_driver", stub, raising=False)

        envelope = await _launch(engage_executor)
        payload = await _payload(envelope)

        assert stub.starts == 1
        assert payload["status"] == "pending", payload
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "active"
        assert probe.transition_calls == []
        assert channel._post_engagement_notice.await_count == 0
        assert channel.close_topic.await_count == 0

    async def test_inbound_admitted_during_a_dying_launch_is_disclosed(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: drop the terminal hook's inbound snapshot → this fails.

        Telegram admits an inbound ticket SYNCHRONOUSLY before its first await,
        and an expected-non-delivery path discharges it with no telling — so a
        message admitted while the launch turn was ending would vanish with the
        topic, unmentioned. The funnel discloses this shape; a new
        out-of-funnel terminal writer inherits the obligation.
        """
        probe = _Probe()
        engage_executor, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        real_start = driver.start

        async def start_then_admit(rec, prompt=None, options=None,
                                   expected_generation=None):
            await real_start(rec, prompt=prompt, options=options,
                             expected_generation=expected_generation)
            # The operator typed while the turn was ending.
            driver.admit_inbound(rec.id, "wait, also remove the old config")

        monkeypatch.setattr(driver, "start", start_then_admit)

        envelope = await _launch(engage_executor)
        assert (await _payload(envelope))["status"] == "error"

        assert len(probe.notice_texts) == 1
        notice = probe.notice_texts[0]
        assert "had no turn start recorded" in notice, notice
        assert "wait, also remove the old config" in notice, notice
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"


class TextBearingCutoffClient(ScriptedCutoffClient):
    """The production shape in full: the executor narrates, calls tools, gets
    results — and THEN the stream dies. Text WAS posted, so `no_visible_output`
    cannot catch it; only the missing ResultMessage can.

    Found by mutation-checking: the frozen red case's stream carries no text,
    so BOTH arms satisfy it and removing the missing-ResultMessage arm alone
    left it green. This test isolates that arm.
    """

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock,
            UserMessage,
        )
        type(self).frames_yielded += 1
        yield AssistantMessage(
            content=[TextBlock(text="Removing the weather plugin now.")],
            model="claude-sonnet-4-6")
        use = ToolUseBlock(id="t1", name="plugin_remove", input={"name": "w"})
        type(self).frames_yielded += 1
        yield AssistantMessage(content=[use], model="claude-sonnet-4-6")
        res = ToolResultBlock(tool_use_id="t1", content="removed", is_error=False)
        type(self).frames_yielded += 1
        yield UserMessage(content=[res])
        # EOF mid-tool-loop. Text was posted; no ResultMessage ever arrives.


class TestTheMissingResultArmOnItsOwn:
    async def test_a_cutoff_that_already_posted_text_is_still_reported(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: drop the `missing_result_message` arm → this fails, and
        it is the ONLY test that does.

        This is the reported production shape: the executor said what it was
        doing, ran its tool, and then its turn died. The posted text is not a
        report — the work stopped mid-flight and nobody owns the outcome — so
        the record must not stay `active` behind a dead transport.
        """
        probe = _Probe()
        engage_executor, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, TextBearingCutoffClient,
        )
        envelope = await _launch(engage_executor)
        payload = await _payload(envelope)

        # Text really did reach the topic, so `no_visible_output` cannot fire.
        assert TextBearingCutoffClient.frames_yielded == 3
        assert TextBearingCutoffClient.result_messages_yielded == 0
        assert probe.finalize_count == 1
        assert probe.emit_count >= 1

        assert payload["status"] == "error", payload
        assert payload["kind"] == "launch_turn_incomplete"
        assert "without ResultMessage" in payload["message"]
        created_id = next(iter(registry._records))
        rec = registry.get(created_id)
        assert rec.status == "error"
        assert rec.origin["error_kind"] == "launch_turn_incomplete"
        assert channel._post_engagement_notice.await_count == 1
        assert channel.close_topic.await_count == 1
        assert probe.events.index("notice") < probe.events.index("topic_close")
        assert driver.is_alive(rec) is False
