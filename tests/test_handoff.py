"""Browser executor §5.4: the general hand-off — chain, operator words, the
mechanical bounds, the ``hand_off`` tool and the requester's paused ceiling."""
from __future__ import annotations

import asyncio
import json

import pytest

import agent as agent_mod
import handoff
import specialist_limits
import tools
from config import AgentConfig, CharacterConfig, SessionConfig
from tools import DelegatedOutput

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


def _cfg(role: str, name: str, kind: str = "specialist", **kw) -> AgentConfig:
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role, model="x",
                       character=CharacterConfig(name=name), system_prompt="x",
                       kind=kind, **kw)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


# -- pure helpers -------------------------------------------------------------

def test_chain_of_a_direct_turn_a_desk_turn_and_a_delegated_run():
    assert handoff.handoff_chain({"role": "assistant", "execution_role": "assistant"}) == ("assistant",)
    assert handoff.handoff_chain({"role": "assistant", "execution_role": "finance"}) == (
        "assistant", "finance")
    assert handoff.handoff_chain({"role": "assistant", "execution_role": "browser",
                                  "_handoff_chain": ["assistant", "finance", "browser"]}) == (
        "assistant", "finance", "browser")


def test_child_chain_does_not_repeat_a_desk_run_role():
    desk = {"role": "assistant", "execution_role": "finance"}
    assert handoff.child_chain(desk, "finance") == ("assistant", "finance")
    assert handoff.child_chain(desk, "browser") == ("assistant", "finance", "browser")
    assert handoff.requester_of(("assistant", "finance", "browser")) == "finance"
    assert handoff.requester_of(("assistant", "browser")) == "assistant"


@pytest.mark.parametrize("origin,expected", [
    ({"_operator_turn": True, "channel": "telegram",
      "_operator_words": "get my RunPod invoice"}, "get my RunPod invoice"),
    ({"_operator_turn": True, "channel": "telegram", "_operator_words": "Book Da Vinci",
      "synthetic": "suggested_reply"}, "Book Da Vinci"),
    # the turn's text is not his words unless the ingress stamped them
    ({"_operator_turn": True, "channel": "telegram", "user_text": "planted"}, None),
    ({"channel": "telegram", "_operator_words": "x"}, None),               # no marker
    ({"_operator_turn": True, "channel": "voice", "_operator_words": "x"}, None),
    ({"_operator_turn": True, "channel": "telegram", "_operator_words": "   "}, None),
    ({"_operator_turn": "yes", "channel": "telegram", "_operator_words": "x"}, None),
])
def test_operator_words_only_from_a_live_operator_message(origin, expected):
    assert handoff.operator_words(origin) == expected


def test_a_desk_reply_carries_his_words_and_a_desk_continuation_does_not():
    import specialist_desk
    kw = dict(resident_role="assistant", desk_role="browser", chat_id=7, user_id=7,
              user_name="N", message_id=1, cid="c", text="get the invoice", turn_id="t")
    assert handoff.operator_words(
        specialist_desk._desk_origin(**kw, operator_typed=True)) == "get the invoice"
    assert handoff.operator_words(specialist_desk._desk_origin(**kw)) is None


def test_the_words_marker_is_reserved_and_copied():
    import provenance
    from agent import COPIED_CONTEXT_MARKERS
    assert "_operator_words" in COPIED_CONTEXT_MARKERS
    assert "_operator_words" not in provenance.sanitize_external_context(
        {"_operator_words": "forged", "chat_id": 1})


def test_bounds_refuse_a_cycle_and_a_fourth_handoff():
    assert handoff.handoff_refusal(("assistant", "finance"), "finance")["kind"] == "handoff_cycle"
    assert handoff.handoff_refusal(("assistant", "finance"), "assistant")["kind"] == "handoff_cycle"
    assert handoff.handoff_refusal(("assistant", "a", "b"), "c") is None          # third hand-off
    assert handoff.handoff_refusal(("assistant", "a", "b", "c"), "d")["kind"] == (
        "handoff_chain_too_long")


# -- the delegated run stamps the chain and names its requester ---------------

class _CapturingClient:
    prompts: list[str] = []
    origins: list[dict] = []

    def __init__(self, options):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def query(self, prompt):
        type(self).prompts.append(prompt)
        type(self).origins.append(dict(agent_mod.origin_var.get(None) or {}))

    async def receive_response(self):
        if False:
            yield None


@pytest.mark.asyncio
async def test_nested_run_names_the_specialist_that_handed_it_on(monkeypatch):
    _CapturingClient.prompts, _CapturingClient.origins = [], []
    roles = {"assistant": _cfg("assistant", "Ellen", kind="resident"),
             "finance": _cfg("finance", "Alex"), "browser": _cfg("browser", "Fido")}
    monkeypatch.setattr(tools, "_agent_role_map", roles, raising=False)
    monkeypatch.setattr(tools, "ClaudeSDKClient", _CapturingClient)
    token = agent_mod.origin_var.set({
        "role": "assistant", "execution_role": "finance", "channel": "telegram",
        "chat_id": "1", "user_id": 1, "cid": "c", "user_text": "collect them",
        "delegation_depth": 1, "_handoff_chain": ("assistant", "finance")})
    try:
        await tools._run_delegated_agent(roles["browser"], "download 4 invoices", "")
    finally:
        agent_mod.origin_var.reset(token)
    prompt = _CapturingClient.prompts[0]
    assert "caller_role: finance\n" in prompt
    assert "caller_name: Alex\n" in prompt
    assert "requested_through: assistant > finance\n" in prompt
    assert _CapturingClient.origins[0]["_handoff_chain"] == ("assistant", "finance", "browser")
    assert _CapturingClient.origins[0]["user_text"] == "collect them"


@pytest.mark.asyncio
async def test_resident_delegation_context_is_unchanged(monkeypatch):
    _CapturingClient.prompts, _CapturingClient.origins = [], []
    roles = {"assistant": _cfg("assistant", "Ellen", kind="resident"),
             "browser": _cfg("browser", "Fido")}
    monkeypatch.setattr(tools, "_agent_role_map", roles, raising=False)
    monkeypatch.setattr(tools, "ClaudeSDKClient", _CapturingClient)
    token = agent_mod.origin_var.set({
        "role": "assistant", "execution_role": "assistant", "channel": "telegram",
        "chat_id": "1", "user_id": 1, "cid": "c", "user_text": "x", "delegation_depth": 0})
    try:
        await tools._run_delegated_agent(roles["browser"], "t", "")
    finally:
        agent_mod.origin_var.reset(token)
    prompt = _CapturingClient.prompts[0]
    assert "caller_role: assistant\ncaller_name: Ellen\noriginating_channel: telegram\n" in prompt
    assert "requested_through" not in prompt
    assert _CapturingClient.origins[0]["_handoff_chain"] == ("assistant", "browser")


# -- the hand_off tool ---------------------------------------------------------

def _specialist_origin(**extra) -> dict:
    return {"role": "assistant", "execution_role": "finance", "channel": "telegram",
            "chat_id": "7", "user_id": 7, "cid": "c", "user_text": "collect them",
            "_operator_words": "collect them",
            "_operator_turn": True, "delegation_depth": 1,
            "_handoff_chain": ("assistant", "finance"), **extra}


@pytest.fixture
def wired(monkeypatch):
    roles = {"assistant": _cfg("assistant", "Ellen", kind="resident"),
             "finance": _cfg("finance", "Alex"), "browser": _cfg("browser", "Fido"),
             "mtg": _cfg("mtg", "Fido")}
    monkeypatch.setattr(tools, "_agent_role_map", roles, raising=False)
    limiter = specialist_limits.SpecialistLimiter(max_global=1)
    monkeypatch.setattr(tools, "_specialist_limiter", limiter, raising=False)
    calls: list[tuple] = []

    async def _fake_bounded(cfg, task_text, context_text, resolution=None, output_format=None):
        calls.append((cfg.role, task_text, context_text,
                      {**(agent_mod.origin_var.get(None) or {}),
                       "quota_key": tools._delegation_quota_key.get(),
                       "in_flight": limiter.in_flight}))
        return DelegatedOutput(text="4 invoices saved")

    monkeypatch.setattr(tools, "_run_delegated_agent_bounded", _fake_bounded)
    return roles, limiter, calls


async def _call(args: dict, origin: dict) -> dict:
    token = agent_mod.origin_var.set(origin)
    try:
        return _payload(await tools.hand_off.handler(args))
    finally:
        agent_mod.origin_var.reset(token)


@pytest.mark.asyncio
async def test_handoff_returns_the_answer_to_the_requester(wired):
    roles, limiter, calls = wired
    limiter.try_acquire("7:finance")            # the root delegation holds the only global slot
    out = await _call({"agent": "browser", "task": "download the four invoices",
                       "expected_result": "four PDFs, one per vendor"}, _specialist_origin())
    assert out["status"] == "ok" and out["text"] == "4 invoices saved"
    role, task_text, context_text, child_origin = calls[0]
    assert role == "browser" and task_text == "download the four invoices"
    assert context_text.endswith(
        "What Alex will accept as a correct result:\nfour PDFs, one per vendor")
    assert child_origin["user_text"] == "collect them"
    assert child_origin["quota_key"]                          # send_media's per-run key
    assert "7:browser" not in limiter._active_scopes          # released after the run
    assert child_origin["in_flight"] == 1                      # never counted globally
    assert limiter.in_flight == 1


@pytest.mark.asyncio
async def test_handoff_refuses_a_cycle_and_does_not_run(wired):
    _, _, calls = wired
    out = await _call({"agent": "finance", "task": "t"}, _specialist_origin())
    assert out["kind"] == "handoff_cycle" and calls == []


@pytest.mark.asyncio
async def test_handoff_refuses_past_three_handoffs(wired):
    _, _, calls = wired
    origin = _specialist_origin(execution_role="mtg",
                                _handoff_chain=("assistant", "x", "y", "mtg"))
    out = await _call({"agent": "browser", "task": "t"}, origin)
    assert out["kind"] == "handoff_chain_too_long" and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [
    {"role": "assistant", "execution_role": "assistant", "channel": "telegram",
     "delegation_depth": 0},                                           # a resident's own turn
    _specialist_origin(stored_call=True),                              # a tapped call
    _specialist_origin(channel="voice"),
    _specialist_origin(execution_role="assistant"),                    # not a specialist
])
async def test_handoff_is_unavailable_outside_a_specialist_run(wired, origin):
    _, _, calls = wired
    out = await _call({"agent": "browser", "task": "t"}, origin)
    assert out["kind"] == "handoff_unavailable" and calls == []


@pytest.mark.asyncio
async def test_handoff_display_name_must_be_unique(wired):
    _, _, calls = wired
    out = await _call({"agent": "fido", "task": "t"}, _specialist_origin())
    assert out["kind"] == "delegation_ambiguous_name"
    assert out["candidates"] == ["browser", "mtg"] and calls == []


@pytest.mark.asyncio
async def test_handoff_busy_when_the_target_already_runs_for_this_chat(wired):
    _, limiter, calls = wired
    limiter.try_acquire_scope_only("7:browser")
    out = await _call({"agent": "browser", "task": "t"}, _specialist_origin())
    assert out["kind"] == "busy" and calls == []


@pytest.mark.asyncio
async def test_handoff_to_a_resident_is_unknown(wired):
    _, _, calls = wired
    out = await _call({"agent": "assistant", "task": "t"}, _specialist_origin())
    assert out["kind"] in ("handoff_cycle", "unknown_agent") and calls == []
    out = await _call({"agent": "butler", "task": "t"}, _specialist_origin())
    assert out["kind"] == "unknown_agent"


def test_handoff_is_granted_to_delegated_specialist_builds_only():
    assert tools._handoff_grants(_cfg("finance", "Alex"), engagement=False) == (
        "mcp__casa-framework__hand_off",)
    assert tools._handoff_grants(_cfg("finance", "Alex"), engagement=True) == ()
    assert tools._handoff_grants(_cfg("assistant", "Ellen", kind="resident"),
                                 engagement=False) == ()


def test_a_specialist_with_no_servers_gets_the_server_that_exposes_hand_off(
        tmp_path, monkeypatch):
    """casa-test acceptance (R2, F1): a specialist declaring ``mcp_servers: []``
    with no inbox and no job tools was granted ``hand_off`` and mounted no
    server exposing it, so the tool was invisible to its run."""
    from mcp_registry import McpServerRegistry
    from plugin_registry import ResolutionResult
    from test_file_handoff_grants import _listed_tool_names
    import agent_inbox
    monkeypatch.setattr(agent_inbox, "_inboxes", {})
    for name in ("_agent_registry", "_specialist_telemetry", "_channel_manager"):
        monkeypatch.setattr(tools, name, None, raising=False)
    reg = McpServerRegistry()
    reg.register_sdk_factory("casa-framework",
                             lambda _role, grants: tools.create_casa_tools(grants))
    monkeypatch.setattr(tools, "_mcp_registry", reg)
    cfg = _cfg("mtg", "Judge")
    cfg.mcp_server_names = []
    res = ResolutionResult(registry_valid=True, plugins=[])
    opts = tools._build_specialist_options(cfg, resolution=res)
    assert "mcp__casa-framework__hand_off" in opts.allowed_tools
    assert "casa-framework" in opts.mcp_servers                      # resolved, not only granted
    assert "hand_off" in _listed_tool_names(opts.mcp_servers["casa-framework"])
    assert cfg.mcp_server_names == []                                # the config itself is untouched


# -- ceilings ------------------------------------------------------------------

def test_clock_leaves_paused_time_out():
    now = [0.0]
    clock = tools._CeilingClock(10.0, now=lambda: now[0])
    now[0] = 4.0
    clock.pause()
    clock.pause()                      # a parallel hand-off
    now[0] = 100.0
    assert clock.left() is None
    clock.resume()
    assert clock.left() is None
    clock.resume()
    assert clock.left() == pytest.approx(6.0)
    now[0] = 106.0
    assert clock.left() == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_requester_ceiling_does_not_run_while_its_handoff_runs(monkeypatch):
    """A run whose ceiling is shorter than its hand-off still completes: the
    requester's clock is paused for exactly the hand-off's wait."""
    monkeypatch.setattr(tools, "_CEILING_PAUSE_POLL_S", 0.01)

    async def _requester():
        with tools._ceiling_paused():
            await asyncio.sleep(0.3)          # the hand-off, longer than the ceiling
        return DelegatedOutput(text="done")

    clock = tools._CeilingClock(0.2)
    tok = tools._ceiling_clock.set(clock)
    try:
        inner = asyncio.create_task(_requester())
    finally:
        tools._ceiling_clock.reset(tok)
    done, pending = await tools._wait_under_ceiling(inner, clock)
    assert inner in done and not pending

    clock = tools._CeilingClock(0.05)
    tok = tools._ceiling_clock.set(clock)
    try:
        stuck = asyncio.create_task(asyncio.sleep(5))
    finally:
        tools._ceiling_clock.reset(tok)
    done, pending = await tools._wait_under_ceiling(stuck, clock)
    assert stuck in pending
    stuck.cancel()


def test_role_yaml_desk_context_reaches_the_runtime_shape():
    import json as _json
    from pathlib import Path

    import jsonschema
    import specialist_materialize

    mapped = specialist_materialize._map_session(
        {"strategy": "ephemeral", "idle_timeout_seconds": 0, "desk_context": False})
    assert mapped == {"strategy": "ephemeral", "idle_timeout": 0, "desk_context": False}
    root = Path(tools.__file__).parent / "defaults" / "schema"
    runtime = _json.loads((root / "runtime.v1.json").read_text())
    jsonschema.validate(mapped, runtime["properties"]["session"])
    role = _json.loads((root / "role.v1.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"strategy": "ephemeral", "idle_timeout_seconds": 0,
                             "desk_context": "no"}, role["properties"]["session"])


def test_a_role_without_desk_context_is_shown_no_earlier_exchanges():
    import specialist_desk
    log = [object()]
    fake = type("D", (), {"log": log})()
    assert specialist_desk.desk_log(_cfg("browser", "Fido"), fake) is log
    off = _cfg("browser", "Fido", session=SessionConfig(desk_context=False))
    assert specialist_desk.desk_log(off, fake) == []
    assert specialist_desk.desk_log(None, fake) is log
