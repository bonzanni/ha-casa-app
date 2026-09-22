"""The evidence hook (#1038 §6.2): Casa's own PostToolUse / PostToolUseFailure
matchers on ``Read`` record into the running turn's scope whether an inbox file
was opened — the runtime's own call, before the tool result reaches the model.

Measured 2026-09-22 (rounds-2026-09-22-output-boundary/measurement-posttooluse.md):
the SDK delivers both events for the built-in ``Read`` to a Python matcher, with
``tool_input.file_path`` verbatim, and a ``path_scope``-denied read produces neither.
"""
from __future__ import annotations

import os

import pytest

import agent as agent_mod
import agent_inbox
import hooks
import output_boundary as ob
from output_boundary import IntentKind as K

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _inbox(tmp_path):
    """A provisioned AND registered inbox — ``wire()`` installs it last at boot;
    ``open_inbox`` alone grants nothing (``readable_prefixes`` reads the registry)."""
    agent_inbox._reset_for_tests()
    ib = agent_inbox.open_inbox("assistant", str(tmp_path))
    agent_inbox._inboxes["assistant"] = ib
    yield ib
    agent_inbox._reset_for_tests()


@pytest.fixture
def scope():
    s = ob.TurnScope(id="t1", cid="c", role="assistant", display_name="Ellen",
                     channel="telegram", message_type="channel_in")
    token = agent_mod.origin_var.set({"role": "assistant", "turn_scope": s})
    yield s
    agent_mod.origin_var.reset(token)


def _matchers():
    m = hooks.read_evidence_matchers("assistant")
    post = m["PostToolUse"][0]
    fail = m["PostToolUseFailure"][0]
    assert post.matcher == "Read" and fail.matcher == "Read"
    return post.hooks[0], fail.hooks[0]


def _inbox_file(ib, name="1758500000000-a1b2.pdf"):
    path = os.path.join(ib.ready_dir, name)
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n")
    return path


async def test_a_successful_read_of_a_listed_inbox_file_is_evidence(_inbox, scope):
    path = _inbox_file(_inbox)
    scope.arm(ob.ReadBeforeDescribe(files=((path, "invoice.pdf"),)))
    post, _ = _matchers()
    out = await post({"hook_event_name": "PostToolUse", "tool_name": "Read",
                      "tool_input": {"file_path": path},
                      "tool_response": {"type": "text", "file": {"filePath": path}}},
                     "toolu_1", {})
    assert out == {}
    assert scope.admit(K.FINAL_REPLY, "€412").annotations == ()


async def test_a_failed_read_is_recorded_as_failed_not_ok(_inbox, scope):
    path = _inbox_file(_inbox)
    scope.arm(ob.ReadBeforeDescribe(files=((path, "invoice.pdf"),)))
    _, fail = _matchers()
    await fail({"hook_event_name": "PostToolUseFailure", "tool_name": "Read",
                "tool_input": {"file_path": path}, "error": "File does not exist"},
               "toolu_2", {})
    assert scope.admit(K.FINAL_REPLY, "€412").annotations == (
        "Casa: Ellen answered without opening “invoice.pdf” in this turn.",)


async def test_a_read_outside_the_inbox_records_nothing_and_arms_nothing(_inbox, scope, tmp_path):
    outside = str(tmp_path / "elsewhere.txt")
    post, _ = _matchers()
    await post({"hook_event_name": "PostToolUse", "tool_name": "Read",
                "tool_input": {"file_path": outside}, "tool_response": {}},
               "toolu_3", {})
    assert scope.obligations == []
    assert scope.admit(K.FINAL_REPLY, "hi").annotations == ()


async def test_a_read_of_an_unlisted_inbox_file_arms_the_obligation_with_its_name(_inbox, scope):
    path = _inbox_file(_inbox)
    _, fail = _matchers()
    await fail({"hook_event_name": "PostToolUseFailure", "tool_name": "Read",
                "tool_input": {"file_path": path}, "error": "boom"}, "toolu_4", {})
    assert scope.admit(K.FINAL_REPLY, "€412").annotations == (
        "Casa: Ellen answered without opening “1758500000000-a1b2.pdf” in this turn.",)


async def test_the_hook_is_inert_when_no_scope_is_bound(_inbox):
    path = _inbox_file(_inbox)
    post, _ = _matchers()
    token = agent_mod.origin_var.set(None)
    try:
        assert await post({"hook_event_name": "PostToolUse", "tool_name": "Read",
                           "tool_input": {"file_path": path}, "tool_response": {}},
                          "toolu_5", {}) == {}
    finally:
        agent_mod.origin_var.reset(token)


async def test_a_role_without_an_inbox_records_nothing(scope):
    m = hooks.read_evidence_matchers("butler")
    post = m["PostToolUse"][0].hooks[0]
    await post({"hook_event_name": "PostToolUse", "tool_name": "Read",
                "tool_input": {"file_path": "/data/agent-inbox/assistant/ready/x.pdf"},
                "tool_response": {}}, "toolu_6", {})
    assert scope.obligations == []
