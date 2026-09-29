"""#1075 — the pins around the buffered closing-silence rule.

``tests/test_buffered_closing_silence.py`` holds the accepted red cases (the
ruling's rows 1-3 and a synchronous delegate's refusal). This module pins the
rest of the ruling and everything it must leave alone, through the same real
``handle_message`` → ``_process`` → fold harness where the behaviour is a
delivery, and on ``TurnScope`` directly where it is a record:

* row 4 (an unrecorded delivery → note → ``<silent/>``), a consumed retry, a
  trailing run of several sentinels, a mid-turn recant, the disclosure line
  applied after the strip;
* what does NOT change: a single message with its sentinel, prose after a
  sentinel in the last message, a trailing whitespace message, a patched
  ``_process`` with no per-message fact, a streaming turn, the #1079
  discharge on a streaming announcement turn;
* the record: ``closing_silence_earned`` is never vacuous, an in-flight or
  failed attempt blocks it, a synchronous child shares the attempts and an
  async child or an engagement does not, the attempt count gates rule 1, and
  rule 1 is not a CHOSEN silence (INV-JOB-010 unchanged).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import output_boundary as ob
from bus import BusMessage, MessageType
from channels import DeliveryOutcome
from output_boundary import IntentKind as K
from output_boundary_testing import scope as _scope

try:
    from tests.test_buffered_closing_silence import (
        TURNS, _Factory, _Hook, _mk_assistant, _resident, _run, _send,
        _assert_buffered, SENT)
except ImportError:  # pragma: no cover — run from inside tests/
    from test_buffered_closing_silence import (
        TURNS, _Factory, _Hook, _mk_assistant, _resident, _run, _send,
        _assert_buffered, SENT)




async def _deliveries(tmp_path, monkeypatch, make_msg, script, *,
                      send_outcome=DeliveryOutcome.DELIVERED, scripts=None):
    agent, stub = await _resident(tmp_path, send_outcome)
    factory = _Factory(scripts if scripts is not None else [script])
    try:
        await _run(agent, make_msg(), factory, monkeypatch)
    finally:
        await agent.aclose()
    return stub, factory


# ---------------------------------------------------------------------------
# The rest of the ruling, through the real turn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_msg", TURNS)
async def test_row4_an_unrecorded_delivery_keeps_the_note(
    tmp_path, monkeypatch, make_msg,
):
    """A Home Assistant notification or a plugin delivery opens no record, so
    rule 2 applies: the note arrives without the tag."""
    async def _ha_notify():  # a delivery Casa's send record never sees
        return None
    stub, factory = await _deliveries(tmp_path, monkeypatch, make_msg, [
        _Hook(_ha_notify), _mk_assistant("Notified the kitchen speaker."),
        _mk_assistant("<silent/>")])
    _assert_buffered(stub, factory)
    assert stub.final_texts() == ["Notified the kitchen speaker."]


@pytest.mark.parametrize("make_msg", TURNS)
async def test_a_consumed_retry_keeps_the_text(tmp_path, monkeypatch, make_msg):
    """A confirmed send on attempt 1, a retryable fault, then a winning
    attempt ending in narration and the sentinel: the send record spans two
    attempts, so rule 1 is not available — the winner's text, untagged."""
    retryable = type("CLIConnectionError", (RuntimeError,), {})
    results: list = []
    stub, factory = await _deliveries(tmp_path, monkeypatch, make_msg, None,
                                      scripts=[
        [_send(results), retryable("upstream reset")],
        [_mk_assistant("Done."), _mk_assistant("<silent/>")]])
    assert len(factory.clients) == 2
    assert len(stub.tool_sends()) == 1
    assert stub.final_texts() == ["Done."]


@pytest.mark.parametrize("make_msg", TURNS)
async def test_several_trailing_sentinels_are_one_closing_run(
    tmp_path, monkeypatch, make_msg,
):
    stub, _ = await _deliveries(tmp_path, monkeypatch, make_msg, [
        _mk_assistant("Done."), _mk_assistant("<silent/>"),
        _mk_assistant("  <silent/>\n")])
    assert stub.final_texts() == ["Done."]
    results: list = []
    stub, _ = await _deliveries(tmp_path, monkeypatch, make_msg, [
        _send(results), _mk_assistant("Done."), _mk_assistant("<silent/>"),
        _mk_assistant("<silent/>")])
    assert stub.final_texts() == []


@pytest.mark.parametrize("make_msg", TURNS)
async def test_a_mid_turn_recant_is_kept_verbatim(tmp_path, monkeypatch, make_msg):
    stub, _ = await _deliveries(tmp_path, monkeypatch, make_msg, [
        _mk_assistant("<silent/>\nActually: 10."), _mk_assistant("<silent/>")])
    assert stub.final_texts() == ["<silent/>\nActually: 10."]


@pytest.mark.parametrize("make_msg", TURNS)
async def test_the_disclosure_is_applied_after_the_strip(
    tmp_path, monkeypatch, make_msg,
):
    """INV-OUT-002: silence and the strip are judged on the UNANNOTATED text.
    Rule 2 keeps the text and the file turn's disclosure line; rule 1 stays
    silent — the line never makes a silent turn visible."""
    import tools

    async def _arm_unread_file():
        tools._current_scope(tools._snapshot_origin()).arm(
            ob.ReadBeforeDescribe(files=(("/ready/x.pdf", "x.pdf"),)))

    stub, _ = await _deliveries(tmp_path, monkeypatch, make_msg, [
        _Hook(_arm_unread_file), _mk_assistant("Done."),
        _mk_assistant("<silent/>")])
    (text,) = stub.final_texts()
    assert text.endswith("\n\nDone.") and "x.pdf" in text
    assert "<silent/>" not in text
    results: list = []
    stub, _ = await _deliveries(tmp_path, monkeypatch, make_msg, [
        _Hook(_arm_unread_file), _send(results), _mk_assistant("Done."),
        _mk_assistant("<silent/>")])
    assert stub.final_texts() == []


# ---------------------------------------------------------------------------
# What does not change
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_msg", TURNS)
@pytest.mark.parametrize("script_texts, delivered", [
    # One message: no message boundary, so nothing for the rule to read.
    (["Done.\n\n<silent/>"], "Done.\n\n<silent/>"),
    # Prose after a sentinel in the LAST message: the G-3 recant contract.
    (["Done.", "<silent/>\nActually: 10."], "Done.\n\n<silent/>\nActually: 10."),
    # A trailing whitespace message is not a closing `<silent/>`.
    (["Done.", "   "], "Done.\n\n   "),
])
async def test_endings_the_rule_does_not_read_are_delivered_as_before(
    tmp_path, monkeypatch, make_msg, script_texts, delivered,
):
    results: list = []
    stub, _ = await _deliveries(tmp_path, monkeypatch, make_msg,
                                [_send(results)]
                                + [_mk_assistant(t) for t in script_texts])
    assert len(stub.tool_sends()) == 1
    assert stub.final_texts() == [delivered]


@pytest.mark.parametrize("make_msg", TURNS)
async def test_whole_output_silence_stays_silent(tmp_path, monkeypatch, make_msg):
    stub, _ = await _deliveries(tmp_path, monkeypatch, make_msg, [
        _mk_assistant("<silent/>"), _mk_assistant("<silent/>")])
    assert stub.final_texts() == []


@pytest.mark.parametrize("make_msg", TURNS)
async def test_a_patched_process_keeps_the_whole_text_rule(
    tmp_path, monkeypatch, make_msg,
):
    """No per-message fact (a ``_process`` that returns a bare string and
    writes no report): today's whole-text rule — delivered as it is."""
    agent, stub = await _resident(tmp_path)
    try:
        with patch.object(agent, "_process",
                          AsyncMock(return_value="Done.\n\n<silent/>")):
            await agent.handle_message(make_msg())
    finally:
        await agent.aclose()
    assert stub.final_texts() == ["Done.\n\n<silent/>"]


async def test_a_streaming_turn_is_unchanged(tmp_path, monkeypatch):
    """A direct chat streams: its closing `<silent/>` after a confirmed send
    and earlier text is delivered exactly as before the ruling."""
    results: list = []
    msg = BusMessage(type=MessageType.REQUEST, source="telegram", target="assistant",
                     content="hi", channel="telegram",
                     context={"chat_id": "123", "cid": "dm-1"})
    stub, _ = await _deliveries(tmp_path, monkeypatch, lambda: msg, [
        _send(results), _mk_assistant("Done."), _mk_assistant("<silent/>")])
    assert stub.on_token_created == 1
    assert stub.final_texts() == ["Done.\n\n<silent/>"]


async def test_a_synthesized_announcement_turn_streams_and_still_discharges(
    tmp_path, monkeypatch,
):
    """A durable announcement is synthesized into a REQUEST turn, which
    streams, so the ruling never reaches it; and a send refused before
    admission on that turn leaves the #1079 discharge exactly as it was: a
    bare `<silent/>` after it acknowledges once."""
    try:
        from tests.test_chosen_silence_announcements import _Counted, _live_notice
    except ImportError:  # pragma: no cover
        from test_chosen_silence_announcements import _Counted, _live_notice
    agent, stub = await _resident(tmp_path)
    counted = _Counted()
    results: list = []
    scopes: list = []
    real_admit = ob.TurnScope.admit

    def _spy(self, kind, text, **kw):
        scopes.append(self)
        return real_admit(self, kind, text, **kw)

    factory = _Factory([[_send(results, channel="nosuch"),
                         _mk_assistant("<silent/>")]])
    try:
        with patch.object(ob.TurnScope, "admit", _spy):
            await _run(agent, _live_notice(counted), factory, monkeypatch)
    finally:
        await agent.aclose()
    assert [bool(r.get("is_error")) for r in results] == [True]
    assert all(s.streaming_allowed for s in scopes) and scopes
    assert [a.state for a in scopes[-1].send_attempts] == ["failed"]
    assert scopes[-1].operator_sends == []
    assert counted.count == 1
    assert stub.final_texts() == []


# ---------------------------------------------------------------------------
# The record the rule reads
# ---------------------------------------------------------------------------


def _buffered_scope():
    s = _scope()
    s.arm(ob.NoStream("scheduled"))
    return s


def _report(**kw):
    return {"reply_messages": ("Done.", "<silent/>"), "retries": [],
            "attempts": 1, **kw}


def _confirmed(s, intent=K.DISCRETE):
    s.open_attempt("send_message").state = "ok"
    s.admit(intent, "Sent.").mark_delivered()



@pytest.mark.parametrize("intent", [K.DISCRETE, K.KEYBOARD, K.CAPTION])
def test_rule1_on_each_admitted_send_kind(intent):
    s = _buffered_scope()
    _confirmed(s, intent)
    out = s.admit(K.FINAL_REPLY, "Done.\n\n<silent/>", report=_report())
    assert out.suppressed is True and out == ""
    # Not a CHOSEN silence: INV-JOB-010 stays "a final text of nothing but
    # sentinels", and the #1079 discharge keeps reading exactly that.
    assert out.chosen_silence is False


def test_rule1_on_a_caption_less_media_send():
    s = _buffered_scope()
    s.open_attempt("send_media").state = "ok"
    s.open_send("media").delivered = True
    assert s.admit(K.FINAL_REPLY, "Done.\n\n<silent/>",
                   report=_report()).suppressed is True


def test_no_send_has_earned_nothing():
    s = _buffered_scope()
    assert s.operator_sends_delivered is True       # vacuous, as #1079 reads it
    assert s.closing_silence_earned is False        # never vacuous
    assert s.admit(K.FINAL_REPLY, "Done.\n\n<silent/>",
                   report=_report()) == "Done."


@pytest.mark.parametrize("state", ["open", "failed"])
def test_an_unresolved_or_failed_attempt_selects_rule2(state):
    """An attempt still ``open`` at admission — a synchronous delegate's send
    in flight — or ``failed`` keeps the text."""
    s = _buffered_scope()
    _confirmed(s)
    s.open_attempt("send_media").state = state
    assert s.admit(K.FINAL_REPLY, "Done.\n\n<silent/>",
                   report=_report()) == "Done."


def test_an_undelivered_commitment_selects_rule2():
    s = _buffered_scope()
    s.open_attempt("send_message").state = "ok"
    s.admit(K.DISCRETE, "Sent.")                    # UNKNOWN: never promoted
    assert s.admit(K.FINAL_REPLY, "Done.\n\n<silent/>",
                   report=_report()) == "Done."


@pytest.mark.parametrize("report", [
    _report(retries=["sdk_error"]),
    _report(attempts=2),        # a stale-resume re-run records no retry
    {"reply_messages": ("Done.", "<silent/>"), "attempts": 1},   # no retries key
    {"reply_messages": ("Done.", "<silent/>"), "retries": []},  # no attempts key
])
def test_rule1_needs_one_attempt_and_no_retries(report):
    s = _buffered_scope()
    _confirmed(s)
    assert s.admit(K.FINAL_REPLY, "Done.\n\n<silent/>", report=report) == "Done."


def test_a_fact_that_is_not_this_texts_is_ignored():
    s = _buffered_scope()
    _confirmed(s)
    out = s.admit(K.FINAL_REPLY, "Other.\n\n<silent/>", report=_report())
    assert out.suppressed is False and out == "Other.\n\n<silent/>"


def test_a_streaming_scope_ignores_the_report():
    s = _scope()
    _confirmed(s)
    out = s.admit(K.FINAL_REPLY, "Done.\n\n<silent/>", report=_report())
    assert out.suppressed is False and out == "Done.\n\n<silent/>"


def test_the_verdict_is_taken_at_admission():
    """A delegate that timed out and keeps running shares the record; what it
    tries AFTER the launcher's final admission belongs to its own completion
    notice, and cannot revise a verdict already taken."""
    s = _buffered_scope()
    child = ob.TurnScope.for_child(s, "", synchronous=True)
    _confirmed(s)
    out = s.admit(K.FINAL_REPLY, "Done.\n\n<silent/>", report=_report())
    child.open_attempt("send_message").state = "failed"
    assert out.suppressed is True
    assert [a.state for a in s.send_attempts] == ["ok", "failed"]


def test_attempts_are_shared_exactly_like_the_send_record():
    s = _buffered_scope()
    sync_child = ob.TurnScope.for_child(s, "", synchronous=True)
    async_child = ob.TurnScope.for_child(s, "", synchronous=False)
    assert sync_child.send_attempts is s.send_attempts
    assert async_child.send_attempts is not s.send_attempts
    async_child.open_attempt("send_message").state = "failed"
    eng = ob.TurnScope.for_engagement(
        SimpleNamespace(id="e1", origin={"role": "assistant"}), display_name="X")
    eng.open_attempt("send_message").state = "failed"
    _confirmed(s)
    assert s.closing_silence_earned is True
    sync_child.open_attempt("send_message").state = "failed"
    assert s.closing_silence_earned is False


async def test_the_report_carries_the_winning_attempt_and_the_attempt_count(
    tmp_path, monkeypatch,
):
    """The per-message fact is the WINNING attempt's, never an accumulation:
    a failed attempt whose text differs is not in it, and the report counts
    both attempts — the fact rule 1's one-attempt condition reads."""
    try:
        from tests.test_buffered_closing_silence import _scheduled
    except ImportError:  # pragma: no cover
        from test_buffered_closing_silence import _scheduled
    retryable = type("CLIConnectionError", (RuntimeError,), {})
    agent, stub = await _resident(tmp_path)
    reports: list = []
    real_process = agent._process

    async def _observed(msg, on_token=None, turn_report=None):
        text = await real_process(msg, on_token=on_token, turn_report=turn_report)
        reports.append(dict(turn_report))
        return text

    factory = _Factory([
        [_mk_assistant("Stale narration."), _mk_assistant("<silent/>"),
         retryable("upstream reset")],
        [_mk_assistant("Done."), _mk_assistant("<silent/>")]])
    try:
        with patch.object(agent, "_process", _observed):
            await _run(agent, _scheduled(), factory, monkeypatch)
    finally:
        await agent.aclose()
    (report,) = reports
    assert report["reply_messages"] == ("Done.", "<silent/>")
    assert report["attempts"] == 2
    assert len(report["retries"]) == 1
    assert stub.final_texts() == ["Done."]
