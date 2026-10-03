#!/usr/bin/env python3
"""The real-CLI gate (S5 design §5.3): what the bundled Claude CLI does to an
MCP tool call's arguments between the model, the hooks and the server.

Runs the REAL ``claude_agent_sdk`` and its bundled CLI — never the e2e mocks —
against the fixture server beside this file, with Casa-shaped hooks that
record every input they see. The credential is read from ONE environment
variable at run time (``CLAUDE_CODE_OAUTH_TOKEN``, as Casa's own service
exports it; the operator supplies it from outside, e.g. ``op run`` or an
inline ``op`` read); nothing about where it lives is in this tree.

Four cases, each its own session (model: Haiku, ``max_turns`` 3):

  1. normalisation — a PreToolUse hook REWRITES the call to ``{"n": "7"}``
     (a string where the schema says integer) with the schema-defaulted
     ``mode`` omitted: what does the server receive? The hook, not the model,
     fixes the value, so the case measures the CLI's passthrough between the
     hooks and the server — type coercion and default filling — directly.
     (The design refuses at deposit anything a CLI transformation would
     change; this case shows whether there is one.)
  2. denied — a PreToolUse hook denies the call: neither PostToolUse nor
     PostToolUseFailure may fire, and the server must receive nothing.
  3a. ordinary rewrite — a PreToolUse hook returns ``updatedInput`` changing
     ``n``: PostToolUse's ``tool_input`` must equal what the server received
     and differ from the original.
  3b. nonce-only rewrite — the hook adds ``__consentNonce``: PostToolUse's
     ``tool_input`` carries it, the server must NOT receive it.
  4. two matchers — Casa's pinned-turn hook set as the builder installs it
     (the ``matcher=None`` catch-all pin FIRST, then the REAL plugin admission
     hook carrying the same one-shot pin, then the real result hook) on a
     server whose tool is plugin-named; the model is told to call it twice.
     Exactly ONE execution must reach the server and PostToolUse must fire
     once; the catch-all must not consume the pin the admission hook judges
     (diff round 1, Terra: both matchers match a plugin tool and run
     concurrently).

Writes ``evidence/gate-<timestamp>.json`` (ignored by git — the evidence file
belongs in the private round record, never in this tree) and exits non-zero
when any case's assertions fail. Nothing here is a pytest; it is run by hand
before the build and before the release.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "fixture_server.py"
TOOL = "mcp__fixture__probe"
CASA = HERE.parents[1] / "casa" / "rootfs" / "opt" / "casa"
# case 4: the server key makes the CLI name the tool like a Casa plugin's
TOOL4 = "mcp__plugin_gate_fixture__probe"
MODEL = os.environ.get("GATE_MODEL", "claude-haiku-4-5-20251001")


def _require_sdk():
    try:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        sys.exit(f"the real claude_agent_sdk is required (not the e2e mock): {exc}")
    import claude_agent_sdk
    if "mock" in (getattr(claude_agent_sdk, "__file__", "") or "").lower():
        sys.exit("refusing to run against the mock SDK")


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


async def _run_case(name: str, prompt: str, pre_decision, workdir: Path) -> dict:
    """One session. ``pre_decision(tool_input) -> dict`` is the PreToolUse
    hook's output for the fixture tool ({} = allow unchanged)."""
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query

    evidence_path = workdir / f"{name}.server.jsonl"
    seen: dict = {"pre": [], "post": [], "post_failure": [], "messages": [], "result": None}

    async def pre(input_data, tool_use_id, context):
        if input_data.get("tool_name") != TOOL:
            return {}
        seen["pre"].append({"tool_use_id": tool_use_id,
                            "tool_input": input_data.get("tool_input")})
        return pre_decision(input_data.get("tool_input"))

    async def post(input_data, tool_use_id, context):
        if input_data.get("tool_name") != TOOL:
            return {}
        seen["post"].append({"tool_use_id": tool_use_id,
                             "tool_input": input_data.get("tool_input"),
                             "tool_response": input_data.get("tool_response")})
        return {}

    async def post_failure(input_data, tool_use_id, context):
        if input_data.get("tool_name") != TOOL:
            return {}
        seen["post_failure"].append({"tool_use_id": tool_use_id,
                                     "tool_input": input_data.get("tool_input"),
                                     "error": str(input_data.get("error"))[:300]})
        return {}

    options = ClaudeAgentOptions(
        model=MODEL,
        max_turns=3,
        cwd=str(workdir),
        allowed_tools=[TOOL],
        permission_mode="default",
        mcp_servers={"fixture": {"type": "stdio", "command": sys.executable,
                                 "args": [str(SERVER)],
                                 "env": {"GATE_EVIDENCE": str(evidence_path)}}},
        strict_mcp_config=True,
        setting_sources=[],
        hooks={
            "PreToolUse": [HookMatcher(matcher=None, hooks=[pre])],
            "PostToolUse": [HookMatcher(matcher=None, hooks=[post])],
            "PostToolUseFailure": [HookMatcher(matcher=None, hooks=[post_failure])],
        },
        system_prompt=("You are a test fixture. Follow the instruction literally: make the "
                       "one tool call described, with exactly the arguments described, then "
                       "reply with the single word done."),
    )
    async for message in query(prompt=prompt, options=options):
        kind = type(message).__name__
        if kind == "ResultMessage":
            seen["result"] = {"subtype": getattr(message, "subtype", None),
                              "is_error": getattr(message, "is_error", None),
                              "num_turns": getattr(message, "num_turns", None)}
        else:
            seen["messages"].append(kind)
    seen["server_received"] = [r["received"] for r in _read_lines(evidence_path)]
    return seen


def _allow_unchanged(_tool_input):
    return {}


def _deny(_tool_input):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": "gate: denied on purpose"}}


def _rewrite_n(tool_input):
    updated = dict(tool_input or {})
    updated["n"] = 2
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}


def _force_string_n(_tool_input):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {"n": "7"}}}


def _add_nonce(tool_input):
    updated = dict(tool_input or {})
    updated["__consentNonce"] = "fixture"
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}


async def _run_two_matchers(workdir: Path) -> dict:
    """Case 4: the pinned turn's REAL hook set on a plugin-named tool."""
    sys.path.insert(0, str(CASA))
    import pinned_run as pr
    import result_broker as rb
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query
    from plugin_grants import PluginContract, ResultContractMap, ToolContract

    art = "a" * 64
    cmap = ResultContractMap(
        tools={TOOL4: ToolContract(art, "gate", "safe", (), {}, delivers={}, servers=("fixture",),
                                   wire_name="probe", transport="stdio")},
        plugins={"gate": PluginContract(art, True, frozenset(), name="gate")})
    owner = pr.PinnedRun(run_id="gate", runtime_name=TOOL4, canonical='{"n":1}', label="Yes",
                         build_input=pr.BuildInput(cfg=None, resolution=None, withheld=(), protected={},
                                                   contract_map=cmap, plan=None, target="specialist:gate"))
    store = rb.ReferenceStore(now=time.time)
    admission = rb.make_plugin_admission_hook("gate", cmap, client_id="gate-c1", store=store, owner=owner)
    result = rb.make_result_hook(cmap, client_id="gate-c1", store=store, owner=owner)
    evidence_path = workdir / "4_two_matchers.server.jsonl"
    seen: dict = {"pre": [], "post": [], "post_failure": [], "messages": [], "result": None}

    async def record_pre(input_data, tool_use_id, context):
        if input_data.get("tool_name") == TOOL4:
            seen["pre"].append({"tool_use_id": tool_use_id, "tool_input": input_data.get("tool_input")})
        return {}

    async def record_post(input_data, tool_use_id, context):
        if input_data.get("tool_name") == TOOL4:
            seen["post"].append({"tool_use_id": tool_use_id, "tool_input": input_data.get("tool_input"),
                                 "tool_response": input_data.get("tool_response")})
        return {}

    async def record_failure(input_data, tool_use_id, context):
        if input_data.get("tool_name") == TOOL4:
            seen["post_failure"].append({"tool_use_id": tool_use_id, "error": str(input_data.get("error"))[:300]})
        return {}

    options = ClaudeAgentOptions(
        model=MODEL, max_turns=4, cwd=str(workdir), allowed_tools=[TOOL4], permission_mode="default",
        mcp_servers={"plugin_gate_fixture": {"type": "stdio", "command": sys.executable, "args": [str(SERVER)],
                                             "env": {"GATE_EVIDENCE": str(evidence_path)}}},
        strict_mcp_config=True, setting_sources=[],
        hooks={
            "PreToolUse": [HookMatcher(matcher=None, hooks=[owner.pin_hook]),
                           HookMatcher(matcher=rb.PLUGIN_TOOL_MATCHER, timeout=rb.HOOK_TIMEOUT_S, hooks=[admission]),
                           HookMatcher(matcher=None, hooks=[record_pre])],
            "PostToolUse": [HookMatcher(matcher=rb.PLUGIN_TOOL_MATCHER, timeout=rb.HOOK_TIMEOUT_S, hooks=[result]),
                            HookMatcher(matcher=None, hooks=[record_post])],
            "PostToolUseFailure": [HookMatcher(matcher=None, hooks=[record_failure])],
        },
        system_prompt=("You are a test fixture. Follow the instruction literally: make the tool calls "
                       "described, with exactly the arguments described, then reply with the single word done."),
    )
    prompt = (f"Call the tool `{TOOL4}` exactly twice, each time with arguments {{\"n\": 1}}: once, and "
              "then once more. If a call is refused, do not retry it a third time. Then reply done.")
    async for message in query(prompt=prompt, options=options):
        kind = type(message).__name__
        if kind == "ResultMessage":
            seen["result"] = {"subtype": getattr(message, "subtype", None),
                              "is_error": getattr(message, "is_error", None),
                              "num_turns": getattr(message, "num_turns", None)}
        else:
            seen["messages"].append(kind)
    seen["server_received"] = [r["received"] for r in _read_lines(evidence_path)]
    seen["owner"] = {"fired": owner.fired, "captured": (owner.captured.kind if owner.captured else None),
                     "rewritten": owner.rewritten}
    return seen


CASES = [
    ("1_normalisation",
     f"Call the tool `{TOOL}` exactly once with arguments {{\"n\": 1}}. Then reply done.",
     _force_string_n),
    ("2_denied",
     f"Call the tool `{TOOL}` exactly once with arguments {{\"n\": 1}}. If the call is refused, "
     "do not retry; reply done.",
     _deny),
    ("3a_rewrite",
     f"Call the tool `{TOOL}` exactly once with arguments {{\"n\": 1}}. Then reply done.",
     _rewrite_n),
    ("3b_nonce",
     f"Call the tool `{TOOL}` exactly once with arguments {{\"n\": 1}}. Then reply done.",
     _add_nonce),
    ("4_two_matchers", "(Casa's pinned hook set; see _run_two_matchers)", None),
]


def _judge(name: str, seen: dict) -> list[str]:
    """Return the list of failed assertions (empty = pass)."""
    fails: list[str] = []
    pre = seen["pre"]; post = seen["post"]; pf = seen["post_failure"]; recv = seen["server_received"]
    if name == "1_normalisation":
        if len(pre) != 1: fails.append(f"expected exactly one PreToolUse, saw {len(pre)}")
        if len(recv) != 1: fails.append(f"expected exactly one server call, saw {len(recv)}")
        if recv and recv[0] != {"n": "7"}:
            fails.append(f"the hook's exact object {{'n': '7'}} did not reach the server unchanged: {recv[0]!r}")
        if recv and "mode" in recv[0]:
            fails.append(f"the server received a schema default nobody sent: {recv[0]!r}")
        if post and recv and post[0]["tool_input"] != recv[0]:
            fails.append(f"PostToolUse input {post[0]['tool_input']!r} != server received {recv[0]!r}")
    elif name == "2_denied":
        if len(pre) < 1: fails.append("the deny hook never saw the call")
        if post: fails.append(f"PostToolUse fired for a denied call: {post!r}")
        if pf: fails.append(f"PostToolUseFailure fired for a denied call: {pf!r}")
        if recv: fails.append(f"the server received a denied call: {recv!r}")
    elif name == "3a_rewrite":
        if len(recv) != 1: fails.append(f"expected exactly one server call, saw {len(recv)}")
        if recv and recv[0].get("n") != 2: fails.append(f"the rewrite did not reach the server: {recv[0]!r}")
        if len(post) != 1: fails.append(f"expected exactly one PostToolUse, saw {len(post)}")
        if post and recv and post[0]["tool_input"] != recv[0]:
            fails.append(f"PostToolUse input {post[0]['tool_input']!r} != server received {recv[0]!r}")
        if pre and post and pre[0]["tool_input"] == post[0]["tool_input"]:
            fails.append("PostToolUse shows the pre-rewrite input")
    elif name == "3b_nonce":
        if len(recv) != 1: fails.append(f"expected exactly one server call, saw {len(recv)}")
        if recv and "__consentNonce" in recv[0]: fails.append(f"the nonce reached the server: {recv[0]!r}")
        if len(post) != 1: fails.append(f"expected exactly one PostToolUse, saw {len(post)}")
        if post and "__consentNonce" not in (post[0]["tool_input"] or {}):
            fails.append(f"PostToolUse does not report the nonce: {post[0]['tool_input']!r}")
        if post and recv and {k: v for k, v in post[0]["tool_input"].items() if k != "__consentNonce"} != recv[0]:
            fails.append(f"PostToolUse minus the nonce {post[0]['tool_input']!r} != server received {recv[0]!r}")
    elif name == "4_two_matchers":
        owner = seen.get("owner") or {}
        if len(pre) < 1: fails.append("the pinned tool was never attempted")
        if recv != [{"n": 1}]: fails.append(f"expected exactly one execution of the stored call, saw {recv!r}")
        if len(post) != 1: fails.append(f"expected exactly one PostToolUse, saw {len(post)}")
        if owner.get("fired") is not True: fails.append("the pin never allowed the one call")
        if owner.get("captured") != "receipt": fails.append(f"the capture is {owner.get('captured')!r}, not a receipt")
        if pf: fails.append(f"PostToolUseFailure fired: {pf!r}")
    return fails


async def main() -> int:
    _require_sdk()
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("no credential in the environment (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY)")
    import claude_agent_sdk
    from claude_agent_sdk import _cli_version
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = HERE / "evidence"
    out_dir.mkdir(exist_ok=True)
    report = {"stamp": stamp, "sdk": getattr(claude_agent_sdk, "__version__", "?"),
              "cli": getattr(_cli_version, "__cli_version__", "?"), "model": MODEL, "cases": {}}
    overall_ok = True
    only = {c for c in os.environ.get("GATE_CASES", "").split(",") if c}
    with tempfile.TemporaryDirectory(prefix="gate-") as tmp:
        for name, prompt, decision in CASES:
            if only and name not in only:
                continue
            workdir = Path(tmp) / name
            workdir.mkdir()
            try:
                if decision is None:
                    seen = await asyncio.wait_for(_run_two_matchers(workdir), 240)
                else:
                    seen = await asyncio.wait_for(_run_case(name, prompt, decision, workdir), 240)
            except Exception as exc:  # noqa: BLE001 — a failed session is a failed case
                seen = {"error": f"{type(exc).__name__}: {exc}"[:500], "pre": [], "post": [],
                        "post_failure": [], "server_received": [], "messages": [], "result": None}
            fails = _judge(name, seen) if "error" not in seen else [seen["error"]]
            report["cases"][name] = {"prompt": prompt, **seen, "failed_assertions": fails,
                                     "pass": not fails}
            overall_ok = overall_ok and not fails
            print(f"{name:18s} {'PASS' if not fails else 'FAIL'}", flush=True)
            for f in fails:
                print(f"    - {f}", flush=True)
    report["pass"] = overall_ok
    path = out_dir / f"gate-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"evidence: {path}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
