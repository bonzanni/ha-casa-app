"""#678 — the launch-death reporter's count and mutation pins.

A launch that ends after its record exists without a running job — cancelled
while its session opens, or caught by Casa's stop — is reported by ONE owner,
``tools._report_launch_death``: one strict terminal transition, then one line
in the job's log (synchronous: it never waits on Telegram), and the bounded
driver teardown. Everything here exists to make one specific mutation of that
owner fail; the mapping is named on each test. (A launch turn that is cut off
once the session runs is a session loss, owned by session recovery, and is
pinned with the job session.)

No ``asyncio.sleep`` is patched anywhere: a wedged counterparty is modelled as
an ``asyncio.Event`` that is never set, and the production bound is shortened
instead. Patching ``<module>.asyncio.sleep`` is the shared module attribute and
is forbidden in this repo (it once OOM-killed the whole VM).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from channels.tg_address import TgAddress

try:
    from tests.test_in_casa_launch_terminal_artifact import (
        ScriptedCompleteClient, ScriptedCutoffClient, _Probe, _build, _launch,
    )
except ImportError:  # pragma: no cover — direct-path collection
    from test_in_casa_launch_terminal_artifact import (
        ScriptedCompleteClient, ScriptedCutoffClient, _Probe, _build, _launch,
    )

pytestmark = [pytest.mark.asyncio]

#: The operator's direct chat message 7, received by Casa's bot (111): the
#: message the job is handed and the one its lost-message notice answers.
_MESSAGE_7 = {
    "tg_message": TgAddress(111, 100, 7),
    "notice": {"address": [111, 100, 7], "answer": False},
}

_COUNTERPARTY_LATENCY_S = 0.1
"""#695: the modelled round-trip of the Telegram counterparty the launch-death
reporter posts through.

NOT a wait and not an allowance: no test waits for this to elapse. It is part of
the ARRANGEMENT — a real driver teardown is tens of milliseconds, and
pinning it at zero is what let ``TestCancellationHasAnOwner`` observe complete
effects from an incomplete reporter on an idle box. With the counterparty this
slow, the reporter provably has NOT finished when each readiness wait below
begins, so "the wait observes the reporter's completion" is falsifiable on any
machine instead of only on a loaded CI runner."""


async def _drain_owner():
    """Await every anchored launch-death reporter (and its telling) before
    asserting what it left behind."""
    import tools as tools_mod
    await tools_mod.drain_launch_death_reports()
    await asyncio.sleep(0)


async def _drain_reporters():
    """Await the reporters themselves, not a yield count (#695): snapshot the
    anchored set (its own done-callback mutates it) and gather it. Plain
    ``gather``: ``_report_launch_death`` promises never to raise, so a raise
    must red the test rather than become a successful drain."""
    import tools as tools_mod
    pending = list(tools_mod._LAUNCH_DEATH_TASKS)
    if pending:
        await asyncio.gather(*pending)
        # ``gather`` returns WITHOUT yielding when every child is already done
        # (CPython's bpo-46672 fast path), so a reporter's own done callback
        # may still be queued; one yield flushes what is ALREADY queued.
        await asyncio.sleep(0)


async def _cancel_while_opening(launcher, driver, monkeypatch, *, during=None):
    """Run one launch whose session open never returns, and cancel the
    launcher there — after its record exists. ``during(rec)`` runs
    while the open hangs. Returns once the cancelled launcher re-raised."""
    entered = asyncio.Event()
    wedged = asyncio.Event()
    seen: list = []

    async def _hang(engagement, options=None):
        seen.append(engagement)
        entered.set()
        await wedged.wait()

    monkeypatch.setattr(driver, "open", _hang)
    task = asyncio.ensure_future(_launch(launcher))
    await asyncio.wait_for(entered.wait(), 5)
    if during is not None:
        during(seen[0])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return seen[0]


class TestEveryBoundIsRealAndEveryCountIsOne:
    """MUTATIONS: drop the death line / remove the driver.cancel bound → the
    matching case below fails. A wedged counterparty is an Event that is never
    set; no asyncio.sleep is patched."""

    async def test_the_death_line_is_logged_once_and_the_teardown_runs(
        self, tmp_path, monkeypatch,
    ):
        import tools as tools_mod
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )

        await _cancel_while_opening(launcher, driver, monkeypatch)
        await asyncio.wait_for(_drain_owner(), 15)

        assert probe.notice_texts == ["⚠️ stopped before reporting a result"]
        assert probe.driver_cancel_calls == 1
        assert not tools_mod._LAUNCH_DEATH_TASKS
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"

    async def test_a_driver_cancel_that_never_returns_still_tells_the_operator(
        self, tmp_path, monkeypatch,
    ):
        import tools as tools_mod
        monkeypatch.setattr(tools_mod, "_DRIVER_CANCEL_TIMEOUT_S", 0.05)
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        wedged = asyncio.Event()

        async def _hang(engagement):
            probe.driver_cancel_calls += 1
            await wedged.wait()

        monkeypatch.setattr(driver, "cancel", _hang)

        await _cancel_while_opening(launcher, driver, monkeypatch)
        await asyncio.wait_for(_drain_owner(), 15)

        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"
        assert probe.notice_texts == ["⚠️ stopped before reporting a result"]
        assert probe.driver_cancel_calls == 1


class TestTheStrictTransitionIsTheAuthority:
    async def test_a_persist_failure_reports_nothing_and_retires_the_client(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: use `mark_error` instead of the strict primitive, or drop
        the PERSIST_FAILED branch → this fails.

        `mark_error` persists BEST-EFFORT: it swallows a tombstone-write
        failure and still returns True, which would report a death over a
        record disk still calls `active`. The strict
        primitive rolls the record back and re-raises, and the reporter then
        logs no death over a live record. It DOES retire the driver's
        session, so `is_alive` never hands the next message to a client the
        launch abandoned.
        """
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )

        real_write = registry._write_tombstone

        def _boom_on_terminal(snapshot):
            # Fail ONLY the terminal write: create() persists strictly too, and
            # failing that would abort the launch before its session opened.
            if any(row.get("status") == "error" for row in snapshot):
                raise OSError("tombstone write refused")
            return real_write(snapshot)

        monkeypatch.setattr(registry, "_write_tombstone", _boom_on_terminal)

        await _cancel_while_opening(launcher, driver, monkeypatch)
        await _drain_owner()

        created_id = next(iter(registry._records))
        rec = registry.get(created_id)
        assert rec.status == "active", rec.status      # rolled fully back
        assert rec.completed_at is None
        # The reporter logs no death over a live record; its owner logs one
        # line saying the outcome could not be recorded — that is the one.
        assert probe.notice_texts == ["⚠️ could not be recorded"]
        assert probe.driver_cancel_calls == 1
        assert driver.is_alive(rec) is False

    async def test_a_lost_terminal_race_reports_nothing_and_stays_pending(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: ignore the strict transition's winner bool → this fails.

        The engagement reported itself before the reporter's transition (its
        own emit_completion, or a concurrent /complete or /cancel). That writer
        owns every side effect; the reporter performs ZERO and leaves the
        winner's outcome standing.
        """
        probe = _Probe()
        launcher, registry, channel, driver = _build(
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

        await _cancel_while_opening(launcher, driver, monkeypatch)
        await _drain_owner()

        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "completed"
        assert len(probe.transition_calls) == 2     # the winner's, then the loser's
        assert probe.notice_texts == []


class TestCancellationHasAnOwner:
    async def test_the_reporter_survives_its_launchers_cancellation(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: await the reporter unshielded → this fails.

        Casa's stop latched while the session opened, so the launcher reports
        the death inline and then returns its envelope. The registry
        deliberately KEEPS a durable terminal state and re-raises when a
        cancellation lands after its write committed, so a launcher cancelled
        inside that report could commit the flip and then never finish its
        teardown — and a second, compensating reporter would correctly LOSE
        the transition and do nothing, leaving the durable error with no
        side-effect owner at all.
        """
        import tools as tools_mod
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCompleteClient,
        )
        entered = asyncio.Event()
        settled: list = []
        real_settle = tools_mod._settle_job_journal

        async def _slow_settle(rec, drv):
            entered.set()
            await asyncio.sleep(0)
            await asyncio.sleep(_COUNTERPARTY_LATENCY_S)
            await real_settle(rec, drv)
            settled.append(rec.id)

        monkeypatch.setattr(tools_mod, "_settle_job_journal", _slow_settle)
        real_open = driver.open

        async def _open_then_the_stop_latches(engagement, options=None):
            await real_open(engagement, options=options)
            monkeypatch.setattr(registry, "launch_shutdown_active", lambda: True)

        monkeypatch.setattr(driver, "open", _open_then_the_stop_latches)

        task = asyncio.ensure_future(_launch(launcher))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The launcher is gone; the reporter finished the job anyway.
        await _drain_reporters()
        assert not tools_mod._LAUNCH_DEATH_TASKS
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"
        assert registry.get(created_id).origin["error_kind"] == "launch_cancelled"
        assert probe.notice_texts == ["⚠️ stopped before reporting a result"]
        assert settled == [created_id]

    async def test_a_cancelled_launch_now_posts_before_it_closes(
        self, tmp_path, monkeypatch,
    ):
        """MUTATION: drop the reporter from the cancellation compensator →
        this fails.

        Before #678 a cancelled launch ended having said NOTHING: the operator
        could not tell whether the task had mutated anything. Its log says so
        before the session is torn down.
        """
        import tools as tools_mod
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        real_cancel = driver.cancel

        async def _slow_cancel(engagement):
            probe.events.append("teardown")
            await asyncio.sleep(_COUNTERPARTY_LATENCY_S)
            await real_cancel(engagement)

        monkeypatch.setattr(driver, "cancel", _slow_cancel)

        # The launcher awaits `open()` inline; a launch cancelled while its
        # session is opening is cancelled there.
        await _cancel_while_opening(launcher, driver, monkeypatch)

        await _drain_reporters()
        assert not tools_mod._LAUNCH_DEATH_TASKS

        created_id = next(iter(registry._records))
        rec = registry.get(created_id)
        assert rec.status == "error", rec.status
        assert rec.origin["error_kind"] == "launch_cancelled"
        assert probe.notice_texts == ["⚠️ stopped before reporting a result"]
        assert probe.events.index("notice") < probe.events.index("teardown")


class TestTheBoundaries:
    async def test_inbound_admitted_during_a_dying_launch_is_not_listed(
        self, tmp_path, monkeypatch,
    ):
        """A message the operator sent while the launch was dying is not
        listed in the death notice: the notice names the launch's fault only.
        What the message is owed is the journal's to tell."""
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )

        await _cancel_while_opening(
            launcher, driver, monkeypatch,
            during=lambda rec: driver.submit_operator(
                rec, "wait, also remove the old config", **_MESSAGE_7))
        await _drain_owner()

        assert len(probe.notice_texts) == 1
        notice = probe.notice_texts[0]
        assert "wait, also remove the old config" not in notice, notice
        created_id = next(iter(registry._records))
        assert registry.get(created_id).status == "error"

    async def test_a_message_accepted_during_a_dying_launch_is_told_lost_once(
        self, tmp_path, monkeypatch,
    ):
        import job_inbox
        probe = _Probe()
        launcher, registry, channel, driver = _build(
            tmp_path, monkeypatch, probe, ScriptedCutoffClient,
        )
        channel.tell_journal_lost = AsyncMock()
        # The transport hands out the endpoint of the bot that received the
        # message: the same double here.
        channel.endpoint.return_value = channel

        rec = await _cancel_while_opening(
            launcher, driver, monkeypatch,
            during=lambda rec: driver.submit_operator(
                rec, "wait, also remove the old config", **_MESSAGE_7))
        await _drain_owner()

        assert registry.get(rec.id).status == "error"
        assert channel.tell_journal_lost.await_count == 1
        assert job_inbox.load(rec.id) == []
