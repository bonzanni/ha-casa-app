"""An own-chat task's answer keeps its memory line.

When a task of an agent that keeps no memory settles in the agent's own chat,
memory keeps "<name> posted his answer to your chat." once for the answer —
however many pages it takes, however often it is delivered — beside the
neutral line for its start line (the two are separate posts since their slots
differ); every other post of the task keeps "<name> posted to your chat."."""
from __future__ import annotations

import pytest

import result_broker as rb
import tools
from test_memory_capture import captured_events
from test_memory_capture import mem  # noqa: F401 — fixture
from test_own_voice_tasks import (  # noqa: F401 — the shared fixture
    NAME, START, _alex_texts, _delegate, _long_answer, _settled, world,
)
from test_own_voice_tasks import _FakeSpecialistClient  # the client the shared world runs

pytestmark = pytest.mark.asyncio

ANSWER = f"{NAME} posted his answer to your chat."
POSTED = f"{NAME} posted to your chat."


async def test_the_answer_is_one_answer_line_beside_the_start_lines_neutral_one(world, mem):
    tools._agent_role_map["finance"].memory.token_budget = 0
    _FakeSpecialistClient.reset(response="The total is 42.\n\n" + _long_answer())
    payload = await _delegate("sync")
    assert payload["status"] == "ok", payload
    texts = _alex_texts(world)
    assert texts[0] == START and len(texts) >= 3          # start line, then a paged answer
    # the same answer delivered again files under the same answer post
    job = world.reg.job_registry.get(payload["delegation_id"])
    birth = tools._record_birth(job)
    await tools._settle_task(birth, answer="The total is 42.\n\n" + _long_answer())
    await _settled()
    rows = await captured_events(mem)
    assert sorted(e["text"] for e in rows) == [ANSWER, POSTED]
    # a plain post of the same task is neutral
    await tools._post_task_text(birth, "a plugin's card", intent=tools.IntentKind.DISCRETE,
                                slot="plugin-card")
    rows = await captured_events(mem)
    assert sorted(e["text"] for e in rows) == [ANSWER, POSTED, POSTED]


async def test_the_answer_is_posted_with_the_task_answer_kind(world):
    tools._agent_role_map["finance"].memory.token_budget = 0
    _FakeSpecialistClient.reset(response="The total is 42.")
    await _delegate("sync")
    kinds = {post.slot: post.kind for _chat, _mid, post in world.alex.filed}
    assert kinds == {"task-start": "", "task-answer": rb.TASK_ANSWER}
