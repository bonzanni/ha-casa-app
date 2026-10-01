"""#1141 regressions — what a turn-limit stop on an ``in_casa`` engagement must
NOT change. Every test here is green at the base (3f6f752b) and exists to make
a named mutation of the change fail; the red case is
``tests/test_pin_1141_engagement_turn_limit.py`` (accepted, not to be edited).

Same rigs as the red case: the real driver, the real owners, the production
follow-up seam; spies that record and never answer. No ``asyncio.sleep`` is
patched.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

try:
    from tests.test_pin_1141_engagement_turn_limit import (
        LINE, _WATCHDOG_S, _Probe, _build, _drain_owner, _followup_rig,
        _frames, _launch, _launch_client, _operator_turn, _spy_launch,
        _system_turn, _warnings,
    )
except ImportError:  # pragma: no cover — direct-path collection
    from test_pin_1141_engagement_turn_limit import (
        LINE, _WATCHDOG_S, _Probe, _build, _drain_owner, _followup_rig,
        _frames, _launch, _launch_client, _operator_turn, _spy_launch,
        _system_turn, _warnings,
    )

pytestmark = [pytest.mark.asyncio]

_NOT_DELIVERED_NOTICE = (
    "This turn finished, but Casa could not deliver its response to this "
    "topic."
)


@pytest.mark.parametrize("shape", ["tool", "text"])
async def test_a_batch_turn_is_unchanged(tmp_path, fake_telegram_bot,
                                         monkeypatch, caplog, shape):
    """ruling-1141: "background job batches are unchanged". A system turn
    into a job engagement that stops at its limit posts nothing, logs no
    WARNING, is not cut off, and the job goes on.

    MUTATIONS: the batch exclusion removed (a line is posted); keyed on the
    system marker alone or on the job alone (likewise, or the red case's
    RC7/RC8 fail); the limit falling through to the existing incomplete-turn
    WARNINGs (a WARNING is logged)."""
    f = await _followup_rig(tmp_path, fake_telegram_bot, monkeypatch,
                            _frames(shape), job=True)
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    await _system_turn(f, text="Batch 1")
    assert len(f.client.query_prompts) == 1
    assert f.notices() == [], f.events
    assert f.results == [False]
    assert _warnings(caplog) == []
    assert f.after.await_count == 1
    assert f.after.await_args.kwargs == {"turn_cut_off": False}
    assert f.reg.get(f.rec.id).status == "active"


async def test_a_batch_turn_whose_text_was_refused_still_ends_the_job(
        tmp_path, fake_telegram_bot, monkeypatch):
    """C13-5(c): a batch turn that stopped at its limit AND whose text the
    topic refused keeps today's handling — the not-delivered notice, cut off,
    and the job is finalized by ``job_after_turn``.

    MUTATION: the limit winning on a batch turn (no notice, not cut off)."""
    f = await _followup_rig(tmp_path, fake_telegram_bot, monkeypatch,
                            _frames("text"), job=True, refused=True)
    await _system_turn(f, text="Batch 1")
    assert f.notices() == [_NOT_DELIVERED_NOTICE], f.events
    assert LINE not in f.notices()
    assert f.results == [True]
    assert f.after.await_args.kwargs == {"turn_cut_off": True}


@pytest.mark.parametrize("via", ["operator", "system"])
async def test_another_error_subtype_is_not_a_limit_stop(
        tmp_path, fake_telegram_bot, monkeypatch, caplog, via):
    """Only ``error_max_turns`` is a limit stop. Another aborted result with
    the same ``is_error`` and ``stop_reason`` — and no text — stays what it is
    today: a quiet follow-up, no line, no WARNING.

    MUTATIONS: the predicate widened to ``is_error``, or keyed on
    ``stop_reason == "tool_use"`` (a line is posted)."""
    f = await _followup_rig(
        tmp_path, fake_telegram_bot, monkeypatch,
        _frames("tool", subtype="error_during_execution"))
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    if via == "operator":
        await _operator_turn(f)
    else:
        await _system_turn(f)
    assert f.notices() == [], f.events
    assert f.results == [False]
    assert _warnings(caplog) == []


async def test_a_mute_launch_that_did_not_stop_at_its_limit_still_dies(
        tmp_path, monkeypatch):
    """INV-ENG-011 is unchanged for every launch that did NOT stop at its
    limit: a tool-only launch whose result is another abort is still reported
    dead as ``no_visible_output``, and no limit line is posted.

    MUTATIONS: the limit exemption widened to every mute launch, or to every
    ``is_error`` / ``stop_reason == "tool_use"`` result (the record stays
    live)."""
    probe = _Probe()
    client_cls = _launch_client("tool", subtype="error_during_execution")
    engage_executor, registry, channel, driver = _build(
        tmp_path, monkeypatch, probe, client_cls)
    seen, _inner = _spy_launch(monkeypatch, driver, probe)
    await _launch(engage_executor)
    await asyncio.wait_for(_drain_owner(), _WATCHDOG_S)
    rec = registry.get(next(iter(registry._records)))
    assert rec.status == "error", rec.status
    assert rec.origin.get("error_kind") == "launch_turn_incomplete"
    assert seen.obs == ["no_visible_output"]
    assert LINE not in probe.notice_texts
    assert len(probe.notice_texts) == 1


@pytest.mark.parametrize("told", [True, False])
async def test_a_limit_stop_over_a_terminal_record_posts_nothing(
        tmp_path, fake_telegram_bot, monkeypatch, told):
    """C13-6: the engagement ended during the turn (a self-emit completion,
    or the operator's /cancel), then the turn stopped at its limit with its
    text delivered. Nothing is posted into its topic — no line, no
    unconfirmed-outcome notice — and it is not cut off.

    MUTATION: the terminal suppression removed (the line is posted over a
    terminal record)."""
    f = await _followup_rig(tmp_path, fake_telegram_bot, monkeypatch,
                            _frames("text"))
    real_settled = f.reg.settled_terminal_state

    async def _settled(engagement_id):
        status, _told = await real_settled(engagement_id)
        return status, told
    f.reg.settled_terminal_state = _settled
    real_report = f.ch._report_incomplete_turn

    async def _report(r, token, **kw):
        await f.reg.try_transition_terminal(r.id, "completed", strict=True)
        return await real_report(r, token, **kw)
    f.ch._report_incomplete_turn = _report
    await _operator_turn(f)
    assert f.notices() == [], f.events
    assert f.results == [False]


async def test_a_refusal_that_also_carries_the_limit_subtype_is_a_refusal(
        tmp_path, fake_telegram_bot, monkeypatch):
    """INV-TURN-007: a refusal keeps ``ApiErrorTurn`` — the failure owner's
    "Turn failed" notice — even on a result whose subtype is the limit's. No
    limit line, and no observation is left behind.

    MUTATION: limit detection moved ahead of the refusal raise, returning
    instead of raising (the line is posted and the refusal is lost)."""
    f = await _followup_rig(tmp_path, fake_telegram_bot, monkeypatch,
                            _frames("tool", stop_reason="refusal"))
    await _operator_turn(f)
    notices = f.notices()
    assert len(notices) == 1 and notices[0].startswith("Turn failed:"), notices
    assert LINE not in notices
    assert f.drv._followup_incomplete == {}
