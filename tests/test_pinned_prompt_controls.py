"""#1282 controls: only a stored-call turn loses its memory and its context.
Every other caller of ``tools._run_delegated_agent`` — a desk reply (a desk
origin with no ``stored_call``) and an ordinary delegation — still gets its
memory view and sends the prompt it sent before, byte for byte; and the desk
turn's context composer is unchanged for the reply and the file turn (N1-2,
N1-3).
"""
from __future__ import annotations

import time

import pytest

import agent as agent_mod
import specialist_desk as sd
import tools as tools_mod
from memory import view as memory_view
from test_delegate_to_agent import _FakeSpecialistClient, _specialist_cfg, _with_origin

MEMORY = "MEMORY-DIGEST-SENTINEL"


@pytest.fixture
def served(monkeypatch, tmp_path):
    from memory_serving_support import fact, ledger_store, wire
    store = ledger_store(tmp_path, [fact("sentinel", MEMORY)])
    wire(monkeypatch, store)
    monkeypatch.setattr(tools_mod, "_agent_role_map", {}, raising=False)
    monkeypatch.setattr(tools_mod, "ClaudeSDKClient", _FakeSpecialistClient)
    _FakeSpecialistClient.reset(response="answer")
    with store.transaction() as conn:
        return memory_view.render(conn, "private", 4000).text


def _expected(caller_role, caller_name, task, context, view):
    return ("<delegation_context>\n"
            f"caller_role: {caller_role}\ncaller_name: {caller_name}\n"
            "originating_channel: telegram\nsuggested_register: text\n"
            "</delegation_context>\n\n"
            + (f"{view}\n\n" if view else "")
            + f"Task: {task}\n\nContext from {caller_name}:\n{context}")


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["desk_reply", "delegation"])
async def test_a_turn_without_a_stored_call_reads_memory_and_keeps_its_prompt(
        served, caller):
    cfg = _specialist_cfg()
    cfg.memory.token_budget = 4000
    if caller == "desk_reply":
        origin = sd._desk_origin(resident_role="assistant", desk_role="finance", chat_id=42,
                                 user_id=42, user_name="Nicola", message_id=70, cid="c1",
                                 text="the second one is wrong", turn_id="t1")
    else:
        origin = {"role": "assistant", "channel": "telegram", "chat_id": "42", "cid": "c1"}
    out = await _with_origin(
        tools_mod._run_delegated_agent(cfg, "the second one is wrong", "CTX-SENTINEL"), origin)
    assert out.text == "answer"
    assert MEMORY in served
    # The desk is the operator's own, so a desk reply reads the private view
    # as a delegation from his private chat does. Red case: a desk origin
    # with no sender clearance reads at ``public``, where no ledger is
    # maintained, and gets no view.
    assert _FakeSpecialistClient.captured_prompt == _expected(
        "assistant", tools_mod._display_name_for_role("assistant"),
        "the second one is wrong", "CTX-SENTINEL", served)


def test_the_desk_context_composer_is_unchanged_for_the_reply_and_the_file_turn():
    block = "<desk>\nBLOCK\n</desk>"
    now = time.time()
    assert sd._compose_context(block, None, None, now, resident_name="Ellen") == (
        sd.turn_frame("Ellen") + "\n\n" + block)
    assert sd._compose_context(block, None, None, now, resident_name="Ellen", file=True) == (
        sd.turn_frame("Ellen", file=True) + "\n\n" + block)
    assert "is a message from the operator" in sd.turn_frame("Ellen")
