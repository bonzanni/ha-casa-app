"""A specialist session's extras — the long-run compaction window, its pad, its
run in the agent's log — decided once by ``specialist_session`` and applied as
the delegated runner applied them inline: the same env, the same pad under the
same key, the same log lines, for a long-run role and an ordinary one."""
from __future__ import annotations

import dataclasses

import pytest
from claude_agent_sdk import ClaudeAgentOptions

import agent as agent_mod
import plugin_bindings
import scratchpad
import specialist_session
import tools as tools_mod
from agent_log_double import RecordingLogs
from agent_logs import LogKey
from authz_grants import GrantIdentity
from scratchpad import GRANT, PROMPT_LINE, SERVER
from test_memory_capture import mem  # noqa: F401 — fixture
from test_plugin_continuations import ORIGIN, announce, conv, lanes, roles  # noqa: F401
from test_scratchpad import pads  # noqa: F401

LONG = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"


def _reference(options, cfg, origin, session_id, *, task=None, words=None, conv=None):
    """The delegated runner's inline code before the extraction, verbatim."""
    from plugin_bindings import BINDINGS
    _long_conversation = (
        getattr(getattr(cfg, "session", None), "long_runs", False) is True)
    if _long_conversation:
        options = dataclasses.replace(options, env={**options.env,
                                                    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"})
    if not origin.get("stored_call"):
        options = scratchpad.attach(
            options, session_id, eager=_long_conversation,
            **({"task": task, "words": words} if conv is None else {}))
        scratchpad.PADS.expire(BINDINGS.holds_session)
    return options


def _shape(options):
    servers = options.mcp_servers
    return (dict(options.env), sorted(servers), servers.get(SERVER, {}).get("alwaysLoad"),
            list(options.allowed_tools), options.system_prompt)


@pytest.fixture
def logs(monkeypatch):
    sink = RecordingLogs()
    monkeypatch.setattr(tools_mod, "_agent_logs", sink)
    return sink


@pytest.fixture
def bindings(monkeypatch, tmp_path):
    store = plugin_bindings.BindingStore(str(tmp_path / "b.json"))
    monkeypatch.setattr(plugin_bindings, "BINDINGS", store)
    return store


def _cfg(long_runs, kind="specialist"):
    from types import SimpleNamespace
    return SimpleNamespace(role="browser", kind=kind,
                           session=SimpleNamespace(long_runs=long_runs))


# --- the helper equals the inline code it replaced ----------------------------------------------

@pytest.mark.parametrize("long_runs", [False, True])
@pytest.mark.parametrize("stored_call", [False, True])
@pytest.mark.parametrize("first", [True, False])
def test_the_extras_equal_the_inline_code_they_replaced(pads, bindings, logs, monkeypatch,  # noqa: F811
                                                        long_runs, stored_call, first):
    cfg = _cfg(long_runs)
    origin = {"stored_call": {"run_id": "r"}} if stored_call else {}
    base = ClaudeAgentOptions(system_prompt="x", env={"A": "1"})
    brief = {"task": "find the receipt", "words": "his words"} if first else {}
    extras = specialist_session.session_env_and_pad(cfg, origin, run_id="S1")
    got = extras.apply(base, **brief)
    got_pads = {k: pads.lines(k) for k in pads.keys()}
    pads_ref = scratchpad.PadStore()
    monkeypatch.setattr(scratchpad, "PADS", pads_ref)
    want = _reference(base, cfg, origin, "S1", conv=None if first else object(),
                      **({"task": brief["task"], "words": brief["words"]} if first else {}))
    assert _shape(got) == _shape(want)
    assert got_pads == {k: pads_ref.lines(k) for k in pads_ref.keys()}       # the same pad, the same brief
    assert (LONG in got.env) is long_runs
    assert base.env == {"A": "1"}                                            # the options are copied, never changed


def test_the_run_starts_in_the_agents_log_and_cleanup_lets_an_unheld_pad_go(pads, bindings, logs):  # noqa: F811
    extras = specialist_session.session_env_and_pad(_cfg(False), {}, run_id="S1")
    assert logs.texts(LogKey("agent", "browser")) == ["▶ started"]
    extras.apply(ClaudeAgentOptions(system_prompt="x"), task="t")
    assert pads.keys() == ["S1"]
    extras.cleanup()
    assert pads.keys() == []
    # a pad a binding can still resume stays
    held = specialist_session.session_env_and_pad(_cfg(False), {}, run_id="S2")
    held.apply(ClaudeAgentOptions(system_prompt="x"), task="t")
    ident = GrantIdentity(operator_id=100, bot_id=111, chat_id=42, enforcement_role="browser",
                          artifact_id="1" * 64, engagement_id="")
    ref = bindings.for_turn(plugin="browser", turn_key="d-1", identity=ident,
                            chain=("assistant", "browser"), origin={"_delegated_session_id": "S2"})
    bindings.adopt(ref.ref, "S2")
    held.cleanup()
    assert pads.keys() == ["S2"]


def test_a_conversation_session_is_keyed_by_its_own_key(pads, bindings, logs):  # noqa: F811
    """An agent's own conversation takes the same extras, its pad keyed by its session."""
    extras = specialist_session.session_env_and_pad(_cfg(True), {}, run_id="conversation:browser")
    assert extras.env == {LONG: "200000"} and extras.pad.key == "conversation:browser"
    assert extras.pad.eager is True
    opts = extras.apply(ClaudeAgentOptions(system_prompt="x"))
    assert opts.env[LONG] == "200000" and opts.mcp_servers[SERVER]["alwaysLoad"] is True
    assert pads.keys() == []                  # no brief given: the pad opens on its first write


# --- the delegated runner, through the real code --------------------------------------------------

async def _delegate(conv, origin=None):  # noqa: F811
    tok = agent_mod.origin_var.set(dict(origin or ORIGIN))
    try:
        return await tools_mod._run_delegated_agent(conv.roles["browser"], "go", "")
    finally:
        agent_mod.origin_var.reset(tok)


@pytest.mark.parametrize("long_runs", [False, True])
async def test_a_delegated_run_has_the_env_the_pad_and_the_log_lines(conv, pads, logs,  # noqa: F811
                                                                       long_runs):
    conv.roles["browser"].session.long_runs = long_runs
    seen: list = []

    async def _during():
        seen.append(pads.keys())
    conv.h.on_receive = _during
    await _delegate(conv)
    (opts,) = conv.h.launches
    long = long_runs
    assert (opts.env.get(LONG) == "200000") is long and (LONG in opts.env) is long
    assert GRANT in opts.allowed_tools and SERVER in opts.mcp_servers
    assert opts.mcp_servers[SERVER].get("alwaysLoad") is (True if long else None)
    assert opts.system_prompt == f"x\n\n{PROMPT_LINE}"
    assert seen == [[opts.session_id]]                 # the pad is the run's session's
    assert pads.keys() == []                           # and goes with the run
    lines = logs.texts(LogKey("agent", "browser"))
    assert lines[0] == "▶ started" and lines[-1] == "✓ answered"


async def test_a_stored_call_run_has_no_pad_but_keeps_its_env(conv, pads, logs):  # noqa: F811
    conv.roles["browser"].session.long_runs = True
    origin = {**ORIGIN, "stored_call": {"run_id": "r", "runtime_name": "x", "canonical": "{}",
                                        "label": "go"}}
    await _delegate(conv, origin)
    (opts,) = conv.h.launches
    assert SERVER not in opts.mcp_servers and opts.env.get(LONG) == "200000"
    assert logs.texts(LogKey("agent", "browser"))[0] == "▶ started"
