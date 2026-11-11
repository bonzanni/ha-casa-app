"""A delegate's answer reaches the resident, who tells it to the person herself.

Casa posts nothing under a delegate's name on the resident's behalf: no tool
for it, no held answer, no instruction to call one in a result or a notice.
What the resident reads is the delegate's whole turn, bounded at the result
cap as ever; when that cap cut the end away, the message the delegate closed
its turn with travels beside it whole (``closing_message``), on the sync
result, the late notice and the durable row a boot replay announces from. A
closing ``<silent/>`` is told to the resident as silence.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import specialist_limits
import tools as tools_mod

CASA_ROOT = Path(tools_mod.__file__).resolve().parent
DID = "0f3c9a2e-1111-4222-8333-944445555666"
CHAT = 42

ANSWER = ("October groceries: €420.18 on 3 November; Albert Heijn €301.40. "
          "Shall I split it by week?")
LONG = "narration line. " * 1600          # ~25,600 chars, over the 20,000 cap
CARD = "I'm posting its card now."


def _origin(**over):
    base = {"role": "assistant", "channel": "telegram", "_tg_bot": 111, "chat_id": CHAT,
            "user_id": CHAT, "cid": "c1"}
    base.update(over)
    return base


def _silent_note(name="Alex"):
    return tools_mod.SILENT_CLOSE_NOTE.format(name=name)


# --- drivers ---------------------------------------------------------------------

def _script_turn(monkeypatch, steps):
    """The fake specialist answers in several text-bearing messages, a tool call
    and its result between them, as a real delegate's turn does."""
    import test_delegate_to_agent as tdta
    from claude_agent_sdk import (AssistantMessage, TextBlock, ToolResultBlock,
                                  ToolUseBlock, UserMessage)
    real = tdta._FakeSpecialistClient.receive_response

    async def receive_response(self):
        async for msg in real(self):
            if not isinstance(msg, AssistantMessage):
                yield msg
                continue
            for i, (words, tool) in enumerate(steps):
                content = [TextBlock(text=words)]
                if tool is not None:
                    content.append(ToolUseBlock(id=f"tu{i}", name=f"mcp__browser__{tool}",
                                                input={}))
                yield AssistantMessage(content=content, model="m")
                if tool is not None:
                    yield UserMessage(content=[ToolResultBlock(tool_use_id=f"tu{i}",
                                                               content="ok")])
    monkeypatch.setattr(tdta._FakeSpecialistClient, "receive_response", receive_response)


async def _delegate(tmp_path, monkeypatch, steps, mode="sync", origin=None):
    """``delegate_to_agent`` from a resident's Telegram turn, the real handler
    over a scripted delegate (Alex, the finance specialist)."""
    from test_delegate_to_agent import _DelegateHarness, _origin as d_origin
    import test_delegate_to_agent as tdta
    import plugin_registry
    _script_turn(monkeypatch, steps)
    if origin is None:
        origin = {**d_origin(chat_id=CHAT), "user_id": CHAT, "_operator_turn": True}
    # an async run outlives the harness's own patches: keep the fake in place
    monkeypatch.setattr(tools_mod, "ClaudeSDKClient", tdta._FakeSpecialistClient)
    monkeypatch.setattr(plugin_registry, "resolve_for",
                        lambda *a, **kw: tdta.ResolutionResult(registry_valid=True))
    payload, _ = await _DelegateHarness()._run(tmp_path, monkeypatch, mode=mode,
                                               origin=origin)
    await asyncio.gather(*list(tools_mod._delegation_callback_tasks))
    for _ in range(5):                  # an async answer settles in a task of its own
        await asyncio.sleep(0)
    await asyncio.gather(*list(tools_mod._delegation_settle_tasks))
    if mode == "async":
        assert payload["status"] == "pending"
    return payload


def _make_job(**changes):
    from job_registry import DeliveryState, ExecutionState, VoiceJob
    from personality_types import SpeakerProvenance
    from task_model import Requester
    system = SpeakerProvenance(speaker_kind="system")
    base = VoiceJob(
        id=DID, parent_job_id=None,
        creating_speaker=system, executing_speaker=system,
        creating_role="assistant", specialist_role="finance",
        specialist_display_name="Alex",
        creator_peer="telegram", creator_user_id=None,
        route={"_tg_bot": 111},
        scope_id=str(CHAT), origin_route_id="c1", origin_device_id=None,
        task="what did I spend on groceries", context="",
        created_at=100.0, started_at=101.0, terminal_at=None,
        expires_at=None, execution_state=ExecutionState.RUNNING,
        delivery_state=DeliveryState.NONE,
        result=None, failure=None, awaiting_input=False,
        continuable_until=None, delivery_sequence=0,
        delivery_attempt_id=None, lease_until=None, cancel_pending=False,
        # a Telegram delegation is a task: who runs it, who asked, where it
        # posts (nowhere: finance has no chat of its own here)
        executor="finance", requester=Requester("assistant", {"_tg_bot": 111}),
        delivery=None,
    )
    return replace(base, **changes)


async def _late(tmp_path, monkeypatch, output):
    """The late answer of an async delegation: the real completion callback,
    its terminal written to a real job ledger. Returns the live notice's
    content and the ledger."""
    from job_registry import JobRegistry
    from specialist_registry import DelegationRecord

    jobs = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0)
    await jobs.load()
    await jobs.create(_make_job())
    published = []

    class _Bus:
        async def notify(self, msg):
            published.append(msg)

    class _Registry:
        job_registry = jobs

        async def complete_delegation(self, did, result="", *,
                                      announce_creator=False, closing_message=""):
            return await jobs.finish_compat(
                did, result, announce_creator=announce_creator,
                closing_message=closing_message)

    monkeypatch.setattr(tools_mod, "_bus", _Bus())
    monkeypatch.setattr(tools_mod, "_specialist_registry", _Registry())
    record = DelegationRecord(id=DID, agent="finance", started_at=0.0, origin=_origin())

    async def _run():
        return output
    task = asyncio.ensure_future(_run())
    tools_mod._attach_completion_callback(task, record)
    await task
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.gather(*list(tools_mod._delegation_settle_tasks))
    (msg,) = published
    return msg, jobs


async def _replayed(tmp_path, jobs):
    """The boot replay of the ledger's owed row: reloaded from disk, recovered
    and announced by the real replay producer."""
    from casa_core import _notify_recovered_delegations
    from job_registry import JobRegistry

    class _BusProbe:
        queues = {"assistant": object()}

        def __init__(self):
            self.sent = []

        async def notify(self, message):
            self.sent.append(message)

    reloaded = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await reloaded.load()
    owed = await reloaded.recover_after_restart()
    bus = _BusProbe()
    await _notify_recovered_delegations(owed, reloaded, bus, assistant_role="assistant")
    (msg,) = bus.sent
    assert msg.content.replayed_after_restart is True
    return msg


def _body(msg) -> str:
    from agent import Agent
    return str(Agent._synthesize_delegation_turn(Mock(), msg).content)


def _output(text, final=None):
    return tools_mod.DelegatedOutput(text=text, run_subtype="success",
                                     final_message=final)


# --- no relay tool, no hold --------------------------------------------------------

def test_relay_answer_is_not_a_casa_tool():
    names = {t.name for t in tools_mod.CASA_TOOLS}
    assert "relay_answer" not in names
    assert not hasattr(tools_mod, "_RELAYABLE") and not hasattr(tools_mod, "_hold_answer")


def test_ellens_grants_name_no_relay_tool():
    for path in ("defaults/agents/assistant/runtime.yaml",
                 "defaults/roles/resident/assistant/role.yaml"):
        assert "relay_answer" not in (CASA_ROOT / path).read_text(encoding="utf-8"), path


@pytest.mark.asyncio
async def test_a_sync_result_carries_the_answer_and_no_relay_key(tmp_path, monkeypatch):
    payload = await _delegate(tmp_path, monkeypatch,
                              (("€420, mostly Albert Heijn.", None),))
    assert payload["status"] == "ok"
    assert "relay" not in payload
    assert "€420, mostly Albert Heijn." in payload["text"]


@pytest.mark.asyncio
async def test_a_late_answer_notice_names_no_relay(tmp_path, monkeypatch):
    msg, _ = await _late(tmp_path, monkeypatch, _output(ANSWER))
    body = _body(msg)
    assert "relay_answer" not in body and "Result text from" in body
    assert ANSWER in body


@pytest.mark.asyncio
async def test_a_replayed_late_answer_names_no_relay(tmp_path, monkeypatch):
    _, jobs = await _late(tmp_path, monkeypatch, _output(ANSWER))
    body = _body(await _replayed(tmp_path, jobs))
    assert "relay_answer" not in body and "Result text from" in body
    assert ANSWER in body


# --- the closing message a long turn would lose ------------------------------------

LONG_THEN_ANSWER = ((LONG, "observe"), (ANSWER, None))


@pytest.mark.asyncio
async def test_a_long_narration_keeps_the_closing_answer_sync(tmp_path, monkeypatch):
    payload = await _delegate(tmp_path, monkeypatch, LONG_THEN_ANSWER)
    assert payload["output_truncated"] is True
    assert ANSWER not in payload["text"]                  # the cap cut it from the text
    assert ANSWER in payload["closing_message"]
    assert len(payload["text"]) <= specialist_limits._MAX_OUTPUT_CHARS


@pytest.mark.asyncio
async def test_a_long_narration_keeps_the_closing_answer_late(tmp_path, monkeypatch):
    msg, jobs = await _late(tmp_path, monkeypatch,
                            _output(LONG + "\n\n" + ANSWER, final=ANSWER))
    body = _body(msg)
    assert ANSWER in body and tools_mod.CLOSING_MARKER in body
    assert body.index(tools_mod.CLOSING_MARKER) < body.index(ANSWER)
    assert len(msg.content.text) <= specialist_limits._MAX_OUTPUT_CHARS
    assert ANSWER in jobs.get(DID).closing_message


@pytest.mark.asyncio
async def test_a_long_narration_keeps_the_closing_answer_after_replay(tmp_path, monkeypatch):
    _, jobs = await _late(tmp_path, monkeypatch,
                          _output(LONG + "\n\n" + ANSWER, final=ANSWER))
    body = _body(await _replayed(tmp_path, jobs))
    assert ANSWER in body and tools_mod.CLOSING_MARKER in body


@pytest.mark.asyncio
async def test_a_failed_terminal_write_retries_with_the_closing_message(
        tmp_path, monkeypatch):
    """The registry-owned retry after a failed terminal write lands the
    closing message beside the answer, so the boot replay still renders it."""
    from job_registry import ExecutionState, JobRegistry
    from specialist_registry import DelegationRecord

    jobs = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0,
                       reconciliation_retry_interval=0.01)
    await jobs.load()
    await jobs.create(_make_job())

    class _Registry:
        job_registry = jobs

        async def complete_delegation(self, did, result="", **kw):
            raise OSError("terminal snapshot write failed")

    class _Bus:
        async def notify(self, msg):
            pass
    monkeypatch.setattr(tools_mod, "_bus", _Bus())
    monkeypatch.setattr(tools_mod, "_specialist_registry", _Registry())
    record = DelegationRecord(id=DID, agent="finance", started_at=0.0, origin=_origin())

    async def _run():
        return _output(LONG + "\n\n" + ANSWER, final=ANSWER)
    task = asyncio.ensure_future(_run())
    tools_mod._attach_completion_callback(task, record)
    await task
    for _ in range(200):
        await asyncio.sleep(0.01)
        if jobs.get(DID).execution_state is ExecutionState.SUCCEEDED:
            break
    await asyncio.gather(*list(tools_mod._delegation_settle_tasks))
    assert jobs.get(DID).closing_message == ANSWER
    await jobs.close()
    body = _body(await _replayed(tmp_path, jobs))
    assert ANSWER in body and tools_mod.CLOSING_MARKER in body


@pytest.mark.asyncio
async def test_an_answer_inside_todays_prefix_survives_a_long_tail(tmp_path, monkeypatch):
    payload = await _delegate(tmp_path, monkeypatch, (
        ("n" * 18_000, "observe"), (ANSWER, "observe"),
        ("t" * 5_000, "observe"), ("c" * 3_000, None)))
    assert payload["output_truncated"] is True
    assert ANSWER in payload["text"]                      # in today's prefix: still there
    assert payload["closing_message"] == "c" * 3_000


@pytest.mark.asyncio
async def test_a_long_closing_message_is_kept_to_the_full_cap(tmp_path, monkeypatch):
    closing = "d" * (11_000 - len(ANSWER)) + ANSWER
    payload = await _delegate(tmp_path, monkeypatch,
                              (("n" * 11_000, "observe"), (closing, None)))
    assert payload["output_truncated"] is True
    assert ANSWER not in payload["text"]
    assert ANSWER in payload["closing_message"]
    assert payload["closing_message"] == closing


@pytest.mark.asyncio
async def test_an_answer_followed_by_a_card_line_reaches_the_resident_whole(
        tmp_path, monkeypatch):
    payload = await _delegate(tmp_path, monkeypatch,
                              ((ANSWER, "post_card"), (CARD, None)))
    assert ANSWER in payload["text"] and CARD in payload["text"]
    assert "closing_message" not in payload                       # under the cap: today's result

    msg, jobs = await _late(tmp_path, monkeypatch,
                            _output(ANSWER + "\n\n" + CARD, final=CARD))
    for body in (_body(msg), _body(await _replayed(tmp_path, jobs))):
        assert ANSWER in body and CARD in body
        assert tools_mod.CLOSING_MARKER not in body
    assert jobs.get(DID).closing_message == ""


@pytest.mark.asyncio
async def test_a_scheduled_delegation_keeps_the_answer_before_a_card_line(
        tmp_path, monkeypatch):
    from test_delegate_to_agent import _origin as d_origin
    origin = {**d_origin(chat_id=CHAT), "source": "scheduler",
              "message_type": "scheduled"}
    payload = await _delegate(tmp_path, monkeypatch,
                              ((ANSWER, "post_card"), (CARD, None)), origin=origin)
    assert payload["status"] == "ok"
    assert ANSWER in payload["text"]


def test_nothing_rides_beside_a_turn_under_the_cap():
    assert tools_mod._closing_beyond_cut(_output("a\n\nb", final="b"), "a\n\nb", False) == ""


@pytest.mark.asyncio
async def test_a_single_message_over_the_cap_still_honours_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(specialist_limits, "_MAX_OUTPUT_CHARS", 10)
    payload = await _delegate(tmp_path, monkeypatch, (("x" * 50, None),))
    assert payload["output_truncated"] is True
    assert len(payload["text"]) == 10
    assert "closing_message" not in payload


@pytest.mark.asyncio
async def test_a_closing_silence_appends_nothing(tmp_path, monkeypatch):
    payload = await _delegate(tmp_path, monkeypatch,
                              ((LONG, "observe"), ("<silent/>", None)))
    assert payload["output_truncated"] is True
    assert "closing_message" not in payload
    assert payload["text"].endswith(_silent_note())


# --- the closing message is the delegate's last message, never its narration -------

NARRATION_THEN_SILENCE = (
    ("The page has a time field. Handing over now.", "request_live_view"),
    ("<silent/>", None),
)
FINAL = ("The time field is empty after the live view, so I can't report a time.\n\n"
         "Source: 1. Special Format Inputs")
NARRATION_THEN_ANSWER = (
    ("Live view is done; observe the page.", "observe"),
    ("Time field is empty. Finish as a mismatch.", "finish"),
    (FINAL, None),
)


def _spy_closing_words(monkeypatch):
    seen = []
    real = tools_mod._closing_words

    def spy(output):
        words = real(output)
        seen.append(words)
        return words
    monkeypatch.setattr(tools_mod, "_closing_words", spy)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_narration_then_a_closing_silence_holds_nothing(tmp_path, monkeypatch, mode):
    seen = _spy_closing_words(monkeypatch)
    payload = await _delegate(tmp_path, monkeypatch, NARRATION_THEN_SILENCE, mode)
    assert seen and set(seen) == {"<silent/>"}       # the closing message, not the narration
    assert "relay" not in payload and "closing_message" not in payload
    if mode == "sync":
        # the assistant still reads the whole turn, then the silent-close note
        assert payload["text"].startswith(
            "The page has a time field. Handing over now.\n\n<silent/>")
        assert payload["text"].endswith(_silent_note())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_only_the_closing_message_is_held_never_the_narration(
        tmp_path, monkeypatch, mode):
    seen = _spy_closing_words(monkeypatch)
    payload = await _delegate(tmp_path, monkeypatch, NARRATION_THEN_ANSWER, mode)
    assert seen and set(seen) == {FINAL}
    assert "relay" not in payload and "closing_message" not in payload
    if mode == "sync":
        # what the assistant reads is the whole turn, unchanged
        assert payload["text"].startswith(
            "Live view is done; observe the page.\n\nTime field is empty. Finish as a "
            "mismatch.\n\n" + FINAL)


# --- a closing silence is told to the resident as silence ---------------------------

@pytest.mark.asyncio
async def test_a_sync_closing_silence_is_told_as_silence(tmp_path, monkeypatch):
    payload = await _delegate(tmp_path, monkeypatch, NARRATION_THEN_SILENCE)
    note = _silent_note()
    assert payload["text"].endswith("\n\n" + note)
    assert payload["text"].count(note) == 1


@pytest.mark.asyncio
async def test_a_sync_answer_carries_no_silence_note(tmp_path, monkeypatch):
    payload = await _delegate(tmp_path, monkeypatch, NARRATION_THEN_ANSWER)
    assert "closed its turn with" not in payload["text"]


async def _late_notice(monkeypatch, output):
    from specialist_registry import DelegationRecord
    published = []

    class _Bus:
        async def notify(self, msg):
            published.append(msg)
    reg = MagicMock()
    reg.complete_delegation = AsyncMock(return_value=None)
    monkeypatch.setattr(tools_mod, "_bus", _Bus())
    monkeypatch.setattr(tools_mod, "_specialist_registry", reg)
    record = DelegationRecord(id=DID, agent="finance", started_at=0.0, origin=_origin())

    async def _run():
        return output
    task = asyncio.ensure_future(_run())
    tools_mod._attach_completion_callback(task, record)
    await task
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.gather(*list(tools_mod._delegation_settle_tasks))
    (msg,) = published
    return msg.content


@pytest.mark.asyncio
@pytest.mark.parametrize("text, final, noted", [
    ("Waiting for the run to get ready.\n\n<silent/>", "<silent/>", True),
    ("<silent/>", None, True),
    ("Waiting for the run.\n\nYou're signed in.", "You're signed in.", False),
    ("Done.\n\n<silent/>", "Done.\n\n<silent/>", False),   # words beside the sentinel
    ("", None, False),                                      # no answer is not a silence
])
async def test_a_late_closing_silence_is_told_as_silence(monkeypatch, text, final, noted):
    content = await _late_notice(monkeypatch, _output(text, final=final))
    note = _silent_note(tools_mod._display_name_for_role("finance"))
    assert (note in content.text) is noted
    assert content.closing_message == ""


# --- a plugin's setup turn ----------------------------------------------------------

def test_the_setup_turn_asks_for_the_outcome_in_one_plain_line():
    """The setup turn once posted "The setup tool returned: Paired with Casa
    Browser 0.1.8." Both branches ask for the outcome alone, in one plain line."""
    import plugin_setup_episodes as pse
    entry = {"granted_tools": ["mcp__plugin_x_srv"], "targets": ["specialist:browser"]}
    _, text = pse._compose({"id": "ep1", "plugin": "x"}, entry, "setup")
    assert ("and to answer with its outcome alone, in one short plain line, without naming "
            "the tool") in text
    entry["targets"] = ["resident:assistant"]
    _, text = pse._compose({"id": "ep1", "plugin": "x"}, entry, "setup")
    assert ("then tell the operator its outcome in one short plain line, in its own terms, "
            "without naming the tool or saying what it returned.") in text
    prompt = " ".join((CASA_ROOT / "defaults/agents/assistant/prompts/system.md")
                      .read_text(encoding="utf-8").split())
    assert ("tell the operator its outcome in one short plain line, in its terms (never "
            "\"the setup tool returned …\")") in prompt


@pytest.mark.asyncio
async def test_a_row_without_the_closing_message_loads_as_empty(tmp_path, monkeypatch):
    from job_registry import JobRegistry
    _, jobs = await _late(tmp_path, monkeypatch,
                          _output(LONG + "\n\n" + ANSWER, final=ANSWER))
    path = tmp_path / "jobs.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot[0]["closing_message"] == ANSWER
    snapshot[0].pop("closing_message")
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    reloaded = JobRegistry(path)
    await reloaded.load()
    assert reloaded.get(DID).closing_message == ""
    await jobs.ack_terminal_notification(DID)
    assert jobs.get(DID).closing_message == ""        # dropped with the answer on delivery
