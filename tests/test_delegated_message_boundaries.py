"""#1221 arm 2: a delegated specialist's text keeps its message boundaries.

The runner (`tools._run_delegated_agent`) joins the text of successive
text-bearing assistant messages with one blank line, as the resident turn
does (agent.py); TextBlocks inside one message join as before, a message with
no text (a tool call, an empty block, a sub-agent's API-error message) adds
nothing, and the caller's 20,000-character bound applies to the joined text.
Every case drives the REAL runner with a scripted SDK client — never a
hand-built `DelegatedOutput` — on the sync `delegate_to_agent` payload and on
the desk file turn's labelled reply.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolUseBlock

import result_broker as rb
import specialist_desk as sd
import specialist_limits
import tools as tools_mod
from bus import MessageBus
from channels import ChannelManager
from specialist_registry import SpecialistRegistry
from test_delegate_to_agent import (
    _FakeSpecialistClient, _caller_cfg, _origin, _seed_specialist_dir, _specialist_cfg,
    _use_synthetic_roles_dir, _with_origin,
)
from test_desk_turn import _REAL_BOUNDED, LABEL, OPERATOR, _reply, env  # noqa: F401 — the real desk harness

pytestmark = pytest.mark.asyncio

A = "Obtaining the handoff path."
B = "Filed the operator's file."
FILE = "invoice.pdf"


def _text(t):
    return TextBlock(text=t)


def _tool(i="tu-1"):
    return ToolUseBlock(id=i, name="mcp__casa-framework__share_inbound_file", input={"path": "/x"})


def _msg(*blocks, error=None, parent=None):
    m = AssistantMessage(content=list(blocks), model="sonnet")
    object.__setattr__(m, "error", error)
    object.__setattr__(m, "parent_tool_use_id", parent)
    return m


def _subagent_error():
    """A sub-agent-scoped API fault (#568): skipped, and the run continues."""
    return _msg(_text("ERROR"), error="rate_limit", parent="sub")


class _Scripted(_FakeSpecialistClient):
    """The test module's fake specialist client, yielding a SCRIPT of assistant
    messages (then a successful result) instead of its one canned message."""

    script: list = []
    yielded = 0
    launches = 0
    on_message = None

    @classmethod
    def load(cls, *messages, on_message=None):
        _FakeSpecialistClient.reset()
        cls.script = list(messages)
        cls.yielded = 0
        cls.launches = 0
        cls.on_message = on_message

    def __init__(self, options):
        super().__init__(options)
        type(self).launches += 1

    async def receive_response(self):
        cls = type(self)
        yield SystemMessage(subtype="init", data={"session_id": "exec-sid"})
        for m in cls.script:
            cls.yielded += 1
            if cls.on_message is not None:
                cls.on_message(m)
            yield m
        result = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                               is_error=False, num_turns=2, session_id="exec-sid")
        cls.yielded += 1
        yield result


def _ran_once(n_messages):
    assert _Scripted.launches == 1
    assert _Scripted.yielded == n_messages + 1           # every message, then the result


async def _sync(tmp_path, monkeypatch, *messages):
    specialists = tmp_path / "ex"
    specialists.mkdir()
    _seed_specialist_dir(specialists, "finance", enabled=True)
    _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
    reg = SpecialistRegistry(str(specialists), tombstone_path=str(tmp_path / "del.json"))
    reg.load()
    tools_mod.init_tools(ChannelManager(), MessageBus(), reg,
                         agent_role_map={"assistant": _caller_cfg(delegates=("finance",))})
    _Scripted.load(*messages)
    with patch("tools.ClaudeSDKClient", _Scripted):
        result = await _with_origin(
            tools_mod.delegate_to_agent.handler({
                "agent": "finance", "task": "file it", "context": "", "mode": "sync"}),
            _origin())
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "ok", payload
    _ran_once(len(messages))
    return payload["text"]


@pytest.fixture
def desk(env, monkeypatch):
    """The real desk harness with the REAL bounded runner and the REAL
    `_run_delegated_agent` underneath, on a real specialist config."""
    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", _REAL_BOUNDED)
    cfg = _specialist_cfg("finance")
    cfg.kind = "specialist"
    tools_mod._agent_role_map["finance"] = cfg
    with patch("tools.ClaudeSDKClient", _Scripted):
        yield env


async def _desk_file_turn(desk, *messages, on_message=None):
    _Scripted.load(*messages, on_message=on_message)
    await _reply(desk, text="[casa file] The operator sent you a file: invoice.pdf", file_name=FILE)
    _ran_once(len(messages))


# --- the defect: the boundary between two messages ------------------------------

async def test_sync_message_boundary(tmp_path, monkeypatch):
    text = await _sync(tmp_path, monkeypatch, _msg(_text(A), _tool()), _msg(_text(B)))
    assert text == A + "\n\n" + B


async def test_file_desk_message_boundary(desk):
    await _desk_file_turn(desk, _msg(_text(A), _tool()), _msg(_text(B)))
    assert desk.channel.notices == []
    (message, _), = desk.channel.replies
    assert str(message) == LABEL + "\n" + A + "\n\n" + B


async def test_subagent_error_adds_nothing(tmp_path, monkeypatch):
    text = await _sync(tmp_path, monkeypatch, _msg(_text("A")), _subagent_error(), _msg(_text("B")))
    assert text == "A\n\nB"


async def test_empty_messages_add_nothing(tmp_path, monkeypatch):
    text = await _sync(tmp_path, monkeypatch,
                       _msg(_tool("t1")), _msg(_text("")), _msg(_text("A")), _msg(_tool("t2")),
                       _msg(_text("B")), _msg(_text("")), _msg(_tool("t3")))
    assert text == "A\n\nB"


# --- the bound applies to the joined text -----------------------------------------

async def test_sync_bound_after_join(tmp_path, monkeypatch):
    assert specialist_limits._MAX_OUTPUT_CHARS == 20_000
    text = await _sync(tmp_path, monkeypatch, _msg(_text("A" * 19_999)), _msg(_text("B" * 10)))
    assert text == "A" * 19_999 + "\n"


async def test_file_desk_bound_after_join(desk):
    await _desk_file_turn(desk, _msg(_text("A" * 19_999)), _msg(_text("B" * 10)))
    body = "".join(str(m) for m, _ in desk.channel.replies)
    assert body == LABEL + "\n" + "A" * 19_999 + "\n"


# --- controls: unchanged at base ----------------------------------------------------

async def test_one_message_unchanged(tmp_path, monkeypatch):
    text = await _sync(tmp_path, monkeypatch, _msg(_text(" A\nB ")))
    assert text == " A\nB "


async def test_blocks_within_message_unchanged(tmp_path, monkeypatch):
    text = await _sync(tmp_path, monkeypatch, _msg(_text("A"), _text("B")))
    assert text == "AB"


async def test_two_sentinels_keep_silent_branch(desk):
    def post(m):
        if m is first:          # a proven post, recorded under the running delegation
            rb.POSTS.record(tools_mod._delegation_quota_key.get(),
                            rb.PostEvent("c", "probe", "report", LABEL, 1, None))
    first = _msg(_text("<silent/>"), _tool())
    await _desk_file_turn(desk, first, _msg(_text("<silent/>")), on_message=post)
    assert desk.channel.replies == []
    assert desk.channel.notices == []
    assert desk.desk.log[-1].text == sd.POSTED_VIEW
