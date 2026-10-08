#!/usr/bin/env python3
"""Ellen hand-off line eval (#1350): after an engagement whose ``pending``
result says the person already sees Casa's hand-off line, the resident adds
nothing about the hand-off.

Live-model gate, the shape of ``ellen_late_answer_and_ask.py``: the resident's
REAL compiled text projection plus an ``<executors>`` block rendered by
``agent._render_executors_block`` from the shipped executors.yaml, through ONE
turn per run. ``engage_executor`` is a side-effect-free FAKE carrying the
PRODUCTION description and input schema; it answers in the production
``pending`` shape, with ``tools.HANDOFF_SHOWN_NOTE`` (``--variant note``) or
without it (``--variant baseline``). Nothing is started or posted.

A run passes when the resident engaged the configurator and everything it
wrote after that call strips to silence. Text BEFORE the call is reported but
not judged (it is not this change's).

Run inside the deployed container (as the sibling evals), or locally against
the repository's defaults with ``--local`` (compiles the prompt from
casa/rootfs/opt/casa/defaults; needs a logged-in Claude Code CLI, named with
``--cli`` since the image's ``claude_runtime.CLAUDE_CLI_PATH`` is absent there):

    venv_test/bin/python test-local/eval/ellen_handoff_line.py --local --variant note --runs 5 \\
        --cli "$(command -v claude)"

Exit 0 = every run passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "casa", "rootfs", "opt", "casa"))

CASES = [
    {"id": "plugin-update",
     "user": "update the gmail plugin to the latest version please"},
    {"id": "new-trigger",
     "user": "add a reminder trigger that pings me every Sunday at 18:00 to put the bins out"},
]


def _paths(local: bool) -> tuple[str, str, str]:
    if local:
        d = os.path.join(LOCAL_ROOT, "defaults")
        return (os.path.join(d, "agents", "assistant"),
                os.path.join(d, "policies", "disclosure.yaml"),
                os.path.join(d, "agents", "assistant", "executors.yaml"))
    return ("/config/agents/assistant", "/config/policies/disclosure.yaml",
            "/config/agents/assistant/executors.yaml")


def _system_prompt(local: bool) -> tuple[str, str]:
    from agent import _render_executors_block
    from agent_loader import load_agent_from_dir
    from policies import load_policies
    from prompt_compiler import projection_for
    agent_dir, policy, _executors = _paths(local)
    cfg = load_agent_from_dir(agent_dir, policies=load_policies(policy),
                              binding_commit=False)
    bundle = cfg.compiled_prompt_bundle
    if bundle is None:
        raise SystemExit("no compiled bundle")
    base = projection_for(bundle, channel="telegram", origin_route="telegram").system_prompt
    channel = ("\n<channel_context>\nchannel: telegram\ntrust: authenticated "
               "(operator)\n</channel_context>")
    block = _render_executors_block(getattr(cfg, "executors", None))
    if not block:
        raise SystemExit("no <executors> block")
    from config import resolve_model
    return base + channel + "\n" + block, resolve_model(cfg.model)


def _pending(note: bool) -> str:
    import tools as casa_tools
    payload = {"status": "pending", "engagement_id": "0f3c9a2e" + "0" * 24,
               "executor_type": "configurator", "topic_id": 4242}
    if note:
        payload["note"] = casa_tools.HANDOFF_SHOWN_NOTE.format(name="the configurator")
    return json.dumps(payload)


async def _one(case: dict, system_prompt: str, model: str, note: bool,
               cli: str | None) -> tuple[bool, str, str, str]:
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock,
        ToolUseBlock, create_sdk_mcp_server, tool,
    )
    from output_boundary import strips_to_silence
    import tools as casa_tools
    from claude_runtime import CLAUDE_CLI_PATH
    from config import effort_for

    prod = casa_tools.engage_executor
    result = _pending(note)

    @tool(prod.name, prod.description, prod.input_schema)
    async def fake(args: dict) -> dict:
        return {"content": [{"type": "text", "text": result}]}

    server = create_sdk_mcp_server(name="casa-framework", tools=[fake])
    kw = dict(model=model, system_prompt=system_prompt,
              mcp_servers={"casa-framework": server}, tools=[],
              allowed_tools=["mcp__casa-framework__engage_executor"],
              strict_mcp_config=True, setting_sources=[], skills=[],
              cwd=tempfile.mkdtemp(), max_turns=4)
    effort = effort_for(model)
    if effort:
        kw["effort"] = effort
    kw["cli_path"] = cli or CLAUDE_CLI_PATH
    before: list[str] = []
    after: list[str] = []
    engaged = False
    from timekeeping import compose_time_envelope, resolve_tz
    from datetime import datetime
    async with ClaudeSDKClient(ClaudeAgentOptions(**kw)) as client:
        await client.query(compose_time_envelope(datetime.now(resolve_tz())) + case["user"])
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, ToolUseBlock) and b.name.endswith("__engage_executor"):
                        engaged = True
                    elif isinstance(b, TextBlock):
                        (after if engaged else before).append(b.text)
    said = "\n".join(after)
    if not engaged:
        return False, "never engaged", "\n".join(before), said
    ok = strips_to_silence(said)
    return ok, "silent after the hand-off" if ok else "announced the hand-off", "\n".join(before), said


async def _run(args) -> bool:
    if args.local:
        sys.path.insert(0, LOCAL_ROOT)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT",
                  "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(k, None)
    else:
        sys.path.insert(0, "/opt/casa")
    system_prompt, model = _system_prompt(args.local)
    note = args.variant == "note"
    print(f"variant={args.variant} model={model!r} runs={args.runs}")
    all_ok = True
    for case in CASES:
        passed = 0
        for i in range(args.runs):
            ok, why, before, after = await _one(case, system_prompt, model, note, args.cli)
            passed += ok
            print(f"  [{case['id']} #{i + 1}] {'PASS' if ok else 'FAIL'} ({why}) "
                  f"before={before!r} after={after!r}")
        print(f"{case['id']}: {passed}/{args.runs}")
        all_ok = all_ok and passed == args.runs
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=("baseline", "note"), required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--cli", default=None)
    args = parser.parse_args()
    try:
        ok = asyncio.run(_run(args))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        ok = False
    print("VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
