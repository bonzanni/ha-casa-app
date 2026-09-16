"""#1003, the agent half: an ordinary turn hands each successful plugin-tool
result to ``plugin_setup_episodes.settle_from_tool_evidence`` the moment it is
observed (under the session gate and the client lock — before the turn's
reply, before the gate releases), and a Casa-dispatched setup turn whose
obligation was settled meanwhile does not run at all.
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from test_agent_process import (  # noqa: F401
    ScriptedToolClient, _SETUP_NS, _capture_reports, _make_agent, _mk_init,
    _mk_result, _mk_tool_result, _mk_tool_use, _msg, _setup_msg, _mk_assistant,
)

pytestmark = pytest.mark.asyncio


@contextmanager
def _capture_evidence():
    import plugin_setup_episodes as pse
    calls: list[dict] = []

    def _settle(**kw):
        calls.append(kw)

    with patch.object(pse, "settle_from_tool_evidence", _settle):
        yield calls


async def test_ordinary_turn_settles_from_a_successful_plugin_tool(tmp_path):
    agent = _make_agent(tmp_path)
    ScriptedToolClient.reset([
        _mk_init("sid-a", ["Read", _SETUP_NS]),
        _mk_tool_use("t1", _SETUP_NS),
        _mk_tool_result("t1", is_error=False, text="auth url"),
        _mk_assistant("Here is the link."),
        _mk_result("sid-a"),
    ])
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient), \
            _capture_evidence() as calls, _capture_reports() as reports:
        await agent._process(_msg("telegram", "op-1", text="install gmail"))
    assert reports == []                      # not a dispatched turn
    assert len(calls) == 1
    call = calls[0]
    assert call["role"] == "assistant"
    assert call["tool"] == _SETUP_NS
    assert isinstance(call["invoked_at"], float)
    assert math.isfinite(call["invoked_at"])
    assert call["binding"] == agent.active_plugin_binding


async def test_evidence_is_handed_over_before_the_turn_ends(tmp_path):
    """The settlement fires when the result is observed, not in the finally —
    a turn that raises after the tool ran still settled."""
    agent = _make_agent(tmp_path)
    ScriptedToolClient.reset([
        _mk_init("sid-b", [_SETUP_NS]),
        _mk_tool_use("t1", _SETUP_NS),
        _mk_tool_result("t1", is_error=False, text="ok"),
        RuntimeError("stream died"),
    ])
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient), \
            _capture_evidence() as calls:
        with pytest.raises(Exception):
            await agent._process(_msg("telegram", "op-2"))
    assert [c["tool"] for c in calls] == [_SETUP_NS]


async def test_an_errored_result_is_not_evidence(tmp_path):
    agent = _make_agent(tmp_path)
    ScriptedToolClient.reset([
        _mk_init("sid-c", [_SETUP_NS]),
        _mk_tool_use("t1", _SETUP_NS),
        _mk_tool_result("t1", is_error=True, text="denied"),
        _mk_assistant("It was denied."),
        _mk_result("sid-c"),
    ])
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient), \
            _capture_evidence() as calls:
        await agent._process(_msg("telegram", "op-3"))
    assert calls == []


async def test_a_non_plugin_tool_is_not_evidence(tmp_path):
    agent = _make_agent(tmp_path)
    ScriptedToolClient.reset([
        _mk_init("sid-d", ["Read"]),
        _mk_tool_use("t1", "Read"),
        _mk_tool_result("t1", is_error=False, text="file"),
        _mk_assistant("read it"),
        _mk_result("sid-d"),
    ])
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient), \
            _capture_evidence() as calls:
        await agent._process(_msg("telegram", "op-4"))
    assert calls == []


async def test_the_dispatched_turn_reports_and_does_not_evidence_settle(tmp_path):
    agent = _make_agent(tmp_path)
    ScriptedToolClient.reset([
        _mk_init("sid-e", [_SETUP_NS]),
        _mk_tool_use("t1", _SETUP_NS),
        _mk_tool_result("t1", is_error=False, text="wired"),
        _mk_assistant("Setup done."),
        _mk_result("sid-e"),
    ])
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient), \
            _capture_evidence() as calls, _capture_reports() as reports:
        await agent._process(_setup_msg("op-5"))
    assert calls == []
    assert [r[0] for r in reports] == ["ep-521"]


async def test_a_settled_setup_turn_does_not_run(tmp_path):
    """The queued Casa-dispatched turn finds its obligation settled once it
    holds the session gate: no client, no prompt, no reply."""
    import plugin_setup_episodes as pse
    agent = _make_agent(tmp_path)
    ScriptedToolClient.reset([
        _mk_init("sid-f", [_SETUP_NS]),
        _mk_assistant("should never run"),
        _mk_result("sid-f"),
    ])
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient), \
            patch.object(pse, "dispatch_still_owed", lambda _id: False), \
            _capture_evidence():
        out = await agent._process(_setup_msg("op-6", episode="ep-x"))
    assert out is None
    assert ScriptedToolClient.instances == []
    assert ScriptedToolClient.scripts != []     # the script was never consumed


async def test_an_owed_setup_turn_runs_as_before(tmp_path):
    import plugin_setup_episodes as pse
    agent = _make_agent(tmp_path)
    ScriptedToolClient.reset([
        _mk_init("sid-g", [_SETUP_NS]),
        _mk_tool_use("t1", _SETUP_NS),
        _mk_tool_result("t1", is_error=False, text="wired"),
        _mk_assistant("Setup done."),
        _mk_result("sid-g"),
    ])
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient), \
            patch.object(pse, "dispatch_still_owed", lambda _id: True), \
            _capture_reports() as reports:
        out = await agent._process(_setup_msg("op-7", episode="ep-y"))
    assert out == "Setup done."
    assert reports[0][1]["tools_used_ok"] == {_SETUP_NS}
