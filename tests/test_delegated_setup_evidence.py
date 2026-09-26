"""#1052: a delegated session hands its successful plugin-tool results to the
setup-obligation store, with the binding of the resolution its own session was
built from, and refreshes plugin health when that clears a failed obligation.

Observed on v0.328.0 (#1051 round 1): the operator ran a specialist's setup by
hand through the assistant, it succeeded, and plugin health still announced
"could not finish setting up" — the specialist's session reported nothing.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock,
    UserMessage,
)

pytestmark = pytest.mark.asyncio

_SETUP = "mcp__plugin_bank-feed_bank-feed__setup_bank_feed"


def _result():
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id="s")


def _client(messages):
    class _FakeSDKClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt):
            return None

        async def receive_response(self):
            for m in messages:
                yield m
    return _FakeSDKClient


@pytest.fixture
def run(monkeypatch):
    import plugin_setup_episodes
    import tools as tm

    calls: list[dict] = []
    regens: list[list] = []
    state = {"cleared": True}

    def settle(**kw):
        calls.append(kw)
        return state["cleared"]

    monkeypatch.setattr(plugin_setup_episodes, "settle_from_tool_evidence",
                        settle)
    monkeypatch.setattr(tm, "_regenerate_plugin_health", regens.append)
    monkeypatch.setattr(tm, "_agent_role_map", {"finance": MagicMock()})
    monkeypatch.setattr(
        tm, "_delegated_resolution", lambda cfg: SimpleNamespace(plugins=[
            SimpleNamespace(name="bank-feed", artifact_id="art-7")]))
    built: list = []

    def builder(cfg, *, resolution=None, output_format=None):
        built.append(resolution)
        return MagicMock()

    monkeypatch.setattr(tm, "_build_specialist_options", builder)
    cfg = MagicMock()
    cfg.role = "finance"
    cfg.memory.token_budget = 0

    async def go(messages, resolution=None):
        monkeypatch.setattr(tm, "ClaudeSDKClient", _client(messages))
        return await tm._run_delegated_agent(cfg, "run setup", "",
                                             resolution=resolution)

    return SimpleNamespace(go=go, calls=calls, regens=regens, state=state,
                           built=built)


def _use(tool_id, name):
    return AssistantMessage(content=[ToolUseBlock(id=tool_id, name=name,
                                                  input={})], model="m")


def _res(tool_id, is_error=None):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool_id,
                                                content="ok",
                                                is_error=is_error)])


async def test_a_successful_setup_result_is_handed_over_with_the_binding(run):
    await run.go([_use("t1", _SETUP), _res("t1"), _result()])
    assert len(run.calls) == 1
    call = run.calls[0]
    assert call["role"] == "finance"
    assert call["tool"] == _SETUP
    assert call["binding"] == {"bank-feed": "art-7"}
    assert call["delegated"] is True
    assert isinstance(call["invoked_at"], float)
    # the options were built from the very resolution the binding came from
    assert [p.artifact_id for p in run.built[0].plugins] == ["art-7"]
    assert run.regens == [[]]


async def test_a_caller_supplied_resolution_is_the_binding(run):
    res = SimpleNamespace(plugins=[
        SimpleNamespace(name="bank-feed", artifact_id="art-3")])
    await run.go([_use("t1", _SETUP), _res("t1"), _result()], resolution=res)
    assert run.calls[0]["binding"] == {"bank-feed": "art-3"}
    assert run.built == [res]


async def test_no_refresh_when_nothing_was_cleared(run):
    run.state["cleared"] = False
    await run.go([_use("t1", _SETUP), _res("t1"), _result()])
    assert len(run.calls) == 1
    assert run.regens == []


async def test_errored_and_non_plugin_results_are_not_evidence(run):
    await run.go([
        _use("t1", _SETUP), _res("t1", is_error=True),
        _use("t2", "Read"), _res("t2"),
        _res("t-unknown"),
        _result()])
    assert run.calls == []
    assert run.regens == []


# --- the refresh is serialized with every other regeneration -----------------

async def test_the_refresh_waits_for_the_plugin_lock(monkeypatch):
    """Diff round 1 (Astra S2): the report lock orders only the WRITE, so a
    regeneration that computed from the still-failed row could land after
    ours and restore the notice. The refresh runs under the same guard every
    live regeneration holds — and a turn waits for it only boundedly."""
    import asyncio

    import tools as tm
    lock = asyncio.Lock()
    monkeypatch.setattr(tm, "_PLUGIN_TOOLS_LOCK", lock)
    monkeypatch.setattr(tm, "_PLUGIN_TOOLS_LOCK_OWNER", None)
    monkeypatch.setattr(tm, "_SETUP_CLEARED_REFRESH_WAIT_S", 0.05)
    regens: list = []
    monkeypatch.setattr(tm, "_regenerate_plugin_health", regens.append)
    await lock.acquire()                # another regeneration is in flight
    try:
        await tm._refresh_health_after_setup_cleared()   # returns, bounded
        assert regens == []             # did not run beside the holder
    finally:
        lock.release()
    for _ in range(50):
        if regens:
            break
        await asyncio.sleep(0.01)
    assert regens == [[]]               # ...and ran once the lock was free
    assert not tm._SETUP_CLEARED_REFRESHES


async def test_an_uncontended_refresh_completes_before_returning(monkeypatch):
    import asyncio

    import tools as tm
    monkeypatch.setattr(tm, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    monkeypatch.setattr(tm, "_PLUGIN_TOOLS_LOCK_OWNER", None)
    regens: list = []
    monkeypatch.setattr(tm, "_regenerate_plugin_health", regens.append)
    await tm._refresh_health_after_setup_cleared()
    assert regens == [[]]
