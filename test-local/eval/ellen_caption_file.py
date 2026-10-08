#!/usr/bin/env python3
"""Ellen caption-file eval (#1378): a file sent with a caption runs one turn
whose message is the caption and whose Casa note names the file; for an
invoice PDF captioned "add it to the invoices" the resident passes the file to
Finance — a copy shared for a plugin and a delegation to Finance — without
asking first.

Live-model gate, the shape of ``ellen_delegation_fidelity.py`` and
``ellen_handoff_line.py``: the resident's REAL compiled text projection plus a
``<delegates>`` block of the shape ``agent._render_prompt_surface`` renders,
through ONE turn per run. The query is composed by the production
``timekeeping.compose_turn_preamble`` with the production
``channels.telegram.caption_file_note``. ``list_inbound_files``,
``share_inbound_file``, ``delegate_to_agent`` and ``ask_user`` are
side-effect-free FAKES carrying the PRODUCTION descriptions and input schemas;
nothing is read, shared, started or asked.

A run passes when the resident called ``delegate_to_agent`` for Finance, shared
the file (or named its inbox path in the brief), and asked nothing.

Run inside the deployed container (as the sibling evals), or locally with
``--local`` and a logged-in Claude Code CLI named with ``--cli``:

    venv_test/bin/python test-local/eval/ellen_caption_file.py --local --runs 3 \\
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

INBOX = "/data/agent-inbox/assistant/ready/1791497912714-788468856613de39.pdf"
HANDOFF = "/data/handoff/casa/1791497913000-0123456789abcdef/invoice-0912.pdf"

SURFACE = (
    "\n<delegates>\n"
    "- butler (Tina) — house devices — lights, climate, locks, media, sensors\n"
    "  Delegate when: user asks to control or check the state of anything in the house\n"
    "- finance (Alex) — household financial records — bank transactions, balances, categorization\n"
    "  Delegate when: user asks about spending, balances, transactions, or budgeting\n"
    "</delegates>\n"
)

CASES = [
    {"id": "invoice", "caption": "add it to the invoices",
     "name": "invoice-0912.pdf", "kind": "PDF", "size": "212 KB"},
]


def _paths(local: bool) -> tuple[str, str]:
    if local:
        d = os.path.join(LOCAL_ROOT, "defaults")
        return (os.path.join(d, "agents", "assistant"),
                os.path.join(d, "policies", "disclosure.yaml"))
    return "/config/agents/assistant", "/config/policies/disclosure.yaml"


def _system_prompt(local: bool) -> tuple[str, str]:
    from agent_loader import load_agent_from_dir
    from config import resolve_model
    from policies import load_policies
    from prompt_compiler import projection_for
    agent_dir, policy = _paths(local)
    cfg = load_agent_from_dir(agent_dir, policies=load_policies(policy),
                              binding_commit=False)
    bundle = cfg.compiled_prompt_bundle
    if bundle is None:
        raise SystemExit("no compiled bundle")
    base = projection_for(bundle, channel="telegram", origin_route="telegram").system_prompt
    channel = ("\n<channel_context>\nchannel: telegram\ntrust: authenticated "
               "(operator)\n</channel_context>")
    model = cfg.model
    if "${" in model:
        from config import _substitute_env
        try:
            with open("/data/options.json", encoding="utf-8") as fh:
                os.environ.setdefault("PRIMARY_AGENT_MODEL",
                                      str(json.load(fh).get("primary_agent_model", "")))
        except OSError:
            pass
        model = _substitute_env(model)
    return base + channel + SURFACE, resolve_model(model)


def _fake(production, captured: list, reply: str):
    from claude_agent_sdk import tool

    @tool(production.name, production.description, production.input_schema)
    async def fake(args: dict) -> dict:
        captured.append((production.name, args))
        return {"content": [{"type": "text", "text": reply}]}
    return fake


async def _one(case: dict, system_prompt: str, model: str, cli: str | None) -> tuple[bool, list, str]:
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock,
        create_sdk_mcp_server,
    )
    from channels.telegram import caption_file_note
    from claude_runtime import CLAUDE_CLI_PATH
    from config import effort_for
    from datetime import datetime
    from timekeeping import compose_turn_preamble, resolve_tz
    import tools as casa_tools

    captured: list = []
    listing = ("Files the operator sent you, newest first. Listing a file is not "
               "reading it: open one with the Read tool before describing it.\n"
               f'1. "{case["name"]}" — {case["kind"]}, {case["size"]}, received just now\n'
               f"   path: {INBOX}")
    fakes = [
        _fake(casa_tools.list_inbound_files, captured, listing),
        _fake(casa_tools.share_inbound_file, captured,
              f'Shared "{case["name"]}". Pass this path to the plugin tool:\n'
              f"{HANDOFF}\nThe shared copy is kept 7 days."),
        _fake(casa_tools.delegate_to_agent, captured,
              '{"status": "ok", "text": "[test stub: Alex filed the invoice]"}'),
        _fake(casa_tools.ask_user, captured, '{"status": "pending"}'),
    ]
    server = create_sdk_mcp_server(name="casa-framework", tools=fakes)
    kw = dict(model=model, system_prompt=system_prompt,
              mcp_servers={"casa-framework": server}, tools=[],
              allowed_tools=[f"mcp__casa-framework__{f.name}" for f in fakes],
              strict_mcp_config=True, setting_sources=[], skills=[],
              cwd=tempfile.mkdtemp(), max_turns=6)
    effort = effort_for(model)
    if effort:
        kw["effort"] = effort
    kw["cli_path"] = cli or CLAUDE_CLI_PATH
    note = caption_file_note(case["name"], case["kind"], case["size"], INBOX, album=False)
    query = compose_turn_preamble(datetime.now(resolve_tz()), [note]) + case["caption"]
    said: list[str] = []
    async with ClaudeSDKClient(ClaudeAgentOptions(**kw)) as client:
        await client.query(query)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                said.extend(b.text for b in msg.content if isinstance(b, TextBlock))
    names = [n for n, _ in captured]
    delegations = [a for n, a in captured if n == "delegate_to_agent"
                   and a.get("agent") in ("finance", "Alex")]
    shared = any(n == "share_inbound_file" and INBOX in json.dumps(a) for n, a in captured)
    brief = " ".join(json.dumps(a, ensure_ascii=False) for a in delegations)
    ok = (bool(delegations) and (shared or INBOX in brief)
          and "ask_user" not in names)
    return ok, captured, "\n".join(said)


async def _run(args) -> bool:
    if args.local:
        sys.path.insert(0, LOCAL_ROOT)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT",
                  "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(k, None)
    else:
        sys.path.insert(0, "/opt/casa")
    system_prompt, model = _system_prompt(args.local)
    print(f"model={model!r} runs={args.runs}")
    all_ok = True
    for case in CASES:
        passed = 0
        for i in range(args.runs):
            ok, captured, said = await _one(case, system_prompt, model, args.cli)
            passed += ok
            print(f"  [{case['id']} #{i + 1}] {'PASS' if ok else 'FAIL'} "
                  + json.dumps(captured, ensure_ascii=False)[:800] + f" said={said[:300]!r}")
        print(f"{case['id']}: {passed}/{args.runs}")
        all_ok = all_ok and passed == args.runs
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser()
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
