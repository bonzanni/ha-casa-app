"""#780 red case — an evicted inbound message whose eviction notice never sends
is quoted at the terminal, by whichever of the two terminal renderers runs.

Specified by **terra** in the drive red-case round (MODE: SPECIFY) against
``b126e6f38b391bdedc7da850e91ed5c1b37cbf01``; accepted by **sol**.

The invariants this module pins are the WIDENED clauses (declared under D34 —
the base bytes do not support them, which is the defect) of two existing ids
owned by ``docs/architecture/engagement-completion-gate.md``:

  * INV-ENG-016 — the evicted envelopes still awaiting their eviction notice
    stay visible to every terminal disclosure (they disclose, never veto);
  * INV-ENG-017 — the reservation-exclusion set names the printable spool
    envelopes: queued, in flight, OR evicted and awaiting its notice.

Pre-fix terminus, read at ``b126e6f3``:

  - ``drivers/claude_code_driver.py:592-597`` (``_lane_members``) and
    ``:676-679`` (``_in_flight``) — both quoted populations require
    ``notice == "none"``; ``:709-715`` derives the exclusion set from exactly
    those two.
  - ``:770-775`` — a redirect at ordinary cap sets the newest ordinary
    victim's ``notice = "pending"``; ``:614-616`` retains such a row for its
    notice. It is durable on disk and in neither population.
  - ``channels/telegram.py:2288-2302`` — the ingress reservation is released
    the moment the message's own enqueue resolved, so no carrier is left.
  - ``tools.py:8326`` / ``:9035`` — both renderers snapshot only those two
    populations; ``:8411`` / ``:9260`` gate their whole inbound paragraph on
    a non-empty population. Both predicates evaluate False and the operator
    is told nothing about a message still on disk.

Every arrangement here is produced by REAL ``enqueue`` calls on a REAL
``_InboundSpool`` at the production spool path, attached to a REAL
``ClaudeCodeDriver``; no envelope field is ever set by hand. The terminal
renderers are the REAL ``_finalize_engagement`` and ``_report_launch_death``.
The driver handed to them is a deliberately NARROW forwarding object: it
forwards only the real driver's actual ``inbound_*`` attributes and raises
``AttributeError`` for anything else, so a fix gated on an accessor's
truthy default cannot pass through a mock, and a NEW accessor is visible to
the renderer only if the real driver actually grew it. Every assertion is on
the WHOLE payload that reached the channel.

Pre-fix, the first whole-payload assertion of each test fails because the
inbound paragraph is absent from the post entirely.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio]


# The exact paragraph the renderers must emit for ONE evicted message and
# nothing else: one bullet, an exact (not "up to") count, no empty bullet from
# the notice-only capacity-drop row that shares the spool.
PARAGRAPH = (
    "\n\n⚠️ 1 inbound message(s) had no turn start recorded before "
    "this engagement ended — they may never have been read:\n• m9"
)

LAUNCH_DEATH_BASE = (
    "⚠️ This engagement stopped before reporting a result "
    "(cutoff). Its task may be incomplete — inspect any partial changes "
    "before retrying."
)


class _InboundOnly:
    """Forwards ONLY the real driver's actual ``inbound_*`` attributes.

    Everything else — ``finalize_completion_post``, ``finalize_summary``,
    ``settle_all_open_questions``, ``drain_inbound_spool``, ``cancel`` — raises
    ``AttributeError``, exactly as an absent method on a duck driver would, so
    the renderers take their fallback paths and the assertions read the post
    the mocked channel received. ``hasattr(driver, "inbound_<new>")`` is True
    here if and only if it is True of the real driver.
    """

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        if name.startswith("inbound_"):
            return getattr(self._real, name)
        raise AttributeError(name)


class _NarrowChannel:
    """The launch-death reporter's channel: a topic sender and a closer, and
    nothing else — no ``_post_engagement_notice`` seam, no rich sender — so
    the notice goes through ``send_to_topic`` and is captured whole."""

    def __init__(self):
        self.send_to_topic = AsyncMock(return_value=7)
        self.close_topic = AsyncMock()


async def _registry(tmp_path):
    """A real registry wired into ``tools``, the way the completion-gate tests
    wire it; the channel manager hands back a MagicMock topic channel whose
    senders are AsyncMocks returning a message id (a confirmed post)."""
    from engagement_registry import EngagementRegistry
    from tools import init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    tch = MagicMock()
    tch.send_to_topic = AsyncMock(return_value=11)
    tch.send_response_to_topic = AsyncMock(return_value=12)
    tch.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = tch
    bus = MagicMock()
    bus.notify = AsyncMock()
    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    return reg, tch


async def _record(reg, topic_id):
    return await reg.create(
        kind="executor", role_or_type="probe-exec", driver="claude_code",
        task="t", origin={"role": "assistant", "channel": "telegram"},
        topic_id=topic_id,
    )


async def _evict_with_a_failing_notice(tmp_path, eid, *, hold_victim=False):
    """The real sequence from the issue, driven end to end.

    Reader unarmed: ten ordinary messages ``m0..m9`` (ids 100..109), each
    reserved, durably enqueued and released as the Telegram handler does;
    then ``STOP`` (id 110) evicts the newest ordinary, ``m9``; then ``m10``
    (id 111) refills the ordinary lane to its cap and ``m11`` (id 112) is
    dropped, which plants the notice-only ``consumed``/``pending``/``text=""``
    row in the SAME spool. Every notice send fails forever. Then the reader
    is armed and every remaining lane member is delivered and consumed by
    real ``on_spawn`` + ``on_turn_start`` cycles — eleven of them, because a
    successful delivery disarms the reader (one message per FIFO EOF).

    ``hold_victim`` keeps ``m9``'s reservation held (the alias arrangement);
    it is not used by the red case itself.
    """
    import drivers.claude_code_driver as ccd
    from drivers.workspace import control_dir, inbound_spool_path

    d = ccd.ClaudeCodeDriver(
        engagements_root=str(tmp_path / "eng" / eid),
        send_to_topic=AsyncMock(), casa_framework_mcp_url="http://x")
    os.makedirs(control_dir(eid), exist_ok=True)
    send_notice = AsyncMock(return_value=False)
    spool = ccd._InboundSpool(
        engagement_id=eid, spool_path=inbound_spool_path(eid),
        write_fifo=AsyncMock(return_value=True),
        send_notice=send_notice, current_epoch=lambda: 1)
    d._inbound[eid] = spool

    async def _through(text, mid, *, release=True):
        d.reserve_inbound(eid, text=text, message_id=mid)
        disposition = await spool.enqueue(text, tg_message_id=mid)
        if release:
            d.release_inbound_reservation(eid, message_id=mid)
        return disposition

    for i in range(10):
        assert await _through(f"m{i}", 100 + i,
                              release=not (hold_victim and i == 9)) == "queued"
    assert await _through("STOP", 110) == "evicted_other(109)"
    assert await _through("m10", 111) == "queued"
    assert await _through("m11", 112) == "dropped_full"

    for _ in range(11):
        await spool.on_spawn()
        await spool.on_turn_start()
    assert spool._lane_members() == []

    # The durable terminal state, as counts.
    rows = [(e.text, e.state, e.notice, e.tg_message_id) for e in spool._envelopes]
    assert len(rows) == 2, rows
    assert ("m9", "queued", "pending", 109) in rows, rows
    assert ("", "consumed", "pending", 112) in rows, rows
    assert spool.unread_depth() == 0
    assert spool.in_flight_texts() == []
    assert spool.has_pending() is True
    assert send_notice.await_count >= 1
    with open(inbound_spool_path(eid), encoding="utf-8") as fh:
        on_disk = [json.loads(line) for line in fh if line.strip()]
    assert [(r["text"], r["state"], r["notice"], r["tg_message_id"])
            for r in on_disk if r["text"] == "m9"] == [
        ("m9", "queued", "pending", 109)], on_disk
    return d, spool, send_notice


class TestAnEvictedMessageWhoseNoticeNeverSendsIsQuotedAtTheTerminal:

    async def test_the_finalization_funnel_quotes_it_on_cancel(self, tmp_path):
        """Renderer 1, the shared funnel, on a cancellation (ungated)."""
        from tools import FinalizeResult, _finalize_engagement

        reg, tch = await _registry(tmp_path)
        rec = await _record(reg, 42)
        real, _spool, _notice = await _evict_with_a_failing_notice(tmp_path, rec.id)

        result = await _finalize_engagement(
            rec, outcome="cancelled", text="", artifacts=[], next_steps=[],
            driver=_InboundOnly(real))

        assert result is FinalizeResult.FINALIZED
        assert tch.send_response_to_topic.await_count == 1
        (_topic, payload), _kw = tch.send_response_to_topic.await_args
        assert payload == "Engagement cancelled." + PARAGRAPH, payload
        assert tch.send_to_topic.await_count == 0

    async def test_the_launch_death_reporter_quotes_it(self, tmp_path):
        """Renderer 2, the launch-death notice — the whole notice, the inbound
        paragraph after the base sentence, no retention sentence (nothing
        recorded a stop cause)."""
        from tools import LaunchDeathResult, _report_launch_death

        reg, _tch = await _registry(tmp_path)
        rec = await _record(reg, 43)
        real, _spool, _notice = await _evict_with_a_failing_notice(tmp_path, rec.id)
        channel = _NarrowChannel()

        result = await _report_launch_death(
            channel, rec, 43, kind="cutoff", detail="cutoff",
            driver=_InboundOnly(real))

        assert result is LaunchDeathResult.REPORTED
        assert channel.send_to_topic.await_count == 1
        (_topic, payload), _kw = channel.send_to_topic.await_args
        assert payload == LAUNCH_DEATH_BASE + PARAGRAPH, payload

    async def test_a_gated_completion_is_not_vetoed_and_still_quotes_it(
            self, tmp_path):
        """The population discloses and never vetoes: with only the evicted
        row left, the completion TOOL's gated path finalizes AND quotes it.
        At base it finalizes and says nothing — the no-veto half already
        holds, the disclosure half is the defect."""
        from tools import FinalizeResult, _finalize_engagement

        reg, tch = await _registry(tmp_path)
        rec = await _record(reg, 44)
        real, _spool, _notice = await _evict_with_a_failing_notice(tmp_path, rec.id)

        result = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=_InboundOnly(real), inbound_gate=True)

        assert result is FinalizeResult.FINALIZED
        assert reg.get(rec.id).status == "completed"
        assert tch.send_response_to_topic.await_count == 1
        (_topic, payload), _kw = tch.send_response_to_topic.await_args
        assert payload == "Engagement completed. Summary:\ndone" + PARAGRAPH, payload
