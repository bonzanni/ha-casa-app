"""#1282 controls: only a stored-call turn loses its memory and its context.
An ordinary delegation through ``tools._run_delegated_agent`` (an origin with
no ``stored_call``) still gets its memory view and sends the prompt it sent
before, byte for byte.
"""
from __future__ import annotations

import pytest

import agent as agent_mod
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
async def test_a_turn_without_a_stored_call_reads_memory_and_keeps_its_prompt(served):
    cfg = _specialist_cfg()
    cfg.memory.token_budget = 4000
    origin = {"role": "assistant", "channel": "telegram", "chat_id": "42", "cid": "c1"}
    out = await _with_origin(
        tools_mod._run_delegated_agent(cfg, "the second one is wrong", "CTX-SENTINEL"), origin)
    assert out.text == "answer"
    assert MEMORY in served
    assert _FakeSpecialistClient.captured_prompt == _expected(
        "assistant", tools_mod._display_name_for_role("assistant"),
        "the second one is wrong", "CTX-SENTINEL", served)

