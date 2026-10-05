"""#1282 controls: only a stored-call turn loses its recall and its context.
Every other caller of ``tools._run_delegated_agent`` — a desk reply (a desk
origin with no ``stored_call``) and an ordinary delegation — still recalls
once and sends the prompt it sent before, byte for byte; and the desk turn's
context composer is unchanged for the reply and the file turn (N1-2, N1-3).
"""
from __future__ import annotations

import time

import pytest

import agent as agent_mod
import specialist_desk as sd
import tools as tools_mod
from recall_renderer import READABLE_SLICE_PROMPT_LINE
from test_delegate_to_agent import _FakeSpecialistClient, _specialist_cfg, _with_origin

MEMORY = "MEMORY-DIGEST-SENTINEL"


@pytest.fixture
def recalled(monkeypatch):
    calls: list[dict] = []

    async def fake_recall(sem, **kw):
        calls.append(kw)
        return MEMORY

    async def fake_retain(sem, **kw):
        return None
    monkeypatch.setattr(tools_mod, "delegated_recall", fake_recall)
    monkeypatch.setattr(tools_mod, "retain_delegated", fake_retain)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", object(), raising=False)
    monkeypatch.setattr(tools_mod, "_agent_role_map", {}, raising=False)
    monkeypatch.setattr(tools_mod, "ClaudeSDKClient", _FakeSpecialistClient)
    _FakeSpecialistClient.reset(response="answer")
    return calls


def _expected(caller_role, caller_name, task, context):
    return ("<delegation_context>\n"
            f"caller_role: {caller_role}\ncaller_name: {caller_name}\n"
            "originating_channel: telegram\nsuggested_register: text\n"
            "</delegation_context>\n\n"
            '<memory_context agent="finance">\n'
            f"{READABLE_SLICE_PROMPT_LINE}\n{MEMORY}\n</memory_context>\n\n"
            f"Task: {task}\n\nContext from {caller_name}:\n{context}")


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["desk_reply", "delegation"])
async def test_a_turn_without_a_stored_call_recalls_and_keeps_its_prompt(recalled, caller):
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
    assert len(recalled) == 1 and recalled[0]["query"] == "the second one is wrong"
    assert _FakeSpecialistClient.captured_prompt == _expected(
        "assistant", tools_mod._display_name_for_role("assistant"),
        "the second one is wrong", "CTX-SENTINEL")


def test_the_desk_context_composer_is_unchanged_for_the_reply_and_the_file_turn():
    block = "<desk>\nBLOCK\n</desk>"
    now = time.time()
    assert sd._compose_context(block, None, None, now, resident_name="Ellen") == (
        sd.turn_frame("Ellen") + "\n\n" + block)
    assert sd._compose_context(block, None, None, now, resident_name="Ellen", file=True) == (
        sd.turn_frame("Ellen", file=True) + "\n\n" + block)
    assert "is a message from the operator" in sd.turn_frame("Ellen")
