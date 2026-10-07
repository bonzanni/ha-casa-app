#!/usr/bin/env python3
"""Ellen delegation-fidelity eval (#1315): an operator message meant for a
specialist reaches it in the operator's own words, delegated before any
clarifying question.

Live-model gate, the shape of ``ellen_brief_fidelity.py``. It drives the
resident's REAL compiled text projection — the prompt a bundle-bound resident
is actually served (``prompt_compiler.projection_for``), loaded with
``binding_commit=False`` so nothing under /config or /data is written — plus a
``<delegates>``/``<jobs>`` block of the shape ``agent._render_prompt_surface``
renders, through ONE turn per case. ``delegate_to_agent``, ``ask_user`` and
``start_job`` are side-effect-free FAKES carrying the PRODUCTION descriptions
and input schemas from ``tools.py``; nothing is started, asked or posted.

``--variant baseline`` serves the projection as compiled. ``--variant doctrine``
serves the same projection with ``DOCTRINE_PARAGRAPH`` inserted after the
"When a request falls within …" paragraph, where the change puts it (the
compiler places role doctrine verbatim), so a deployed image need not carry
the change to measure it.

Run inside the deployed container, one-shot, writing only under /tmp:

    cat test-local/eval/ellen_delegation_fidelity.py \\
        | ssh <host> -- sudo -n docker exec -i <container> python3 - --variant doctrine --runs 3

Exit 0 = every run of every case passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, "/opt/casa")

from agent_loader import load_agent_from_dir  # noqa: E402
from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)
from policies import load_policies  # noqa: E402
from prompt_compiler import projection_for  # noqa: E402
import tools as casa_tools  # noqa: E402

ANCHOR = "When a request falls within what one of your delegates owns"

# Kept identical to the paragraph the change adds to the role doctrine; the
# prompt test pins the shipped text, this copy only feeds the doctrine variant.
DOCTRINE_PARAGRAPH = (
    "When a message is meant for something a delegate owns — an answer to a "
    "question its plugin posted, a phrase its plugin asked the person to "
    "send, or a short request in its area — delegate it with the person's own "
    "words quoted exactly in `task=`, and put your reading of them, if any, in "
    "`context=`. Do not turn their words into a different action. When you "
    "cannot tell which of a delegate's actions they mean, delegate their words "
    "and let the delegate settle it; ask the person to choose only when the "
    "delegate's result asks, or when no delegate owns the request. A sign-in "
    "link or one-time code still never goes into a brief."
)

SURFACE = (
    "\n<delegates>\n"
    "- butler (Tina) — house devices — lights, climate, locks, media, sensors\n"
    "  Delegate when: user asks to control or check the state of anything in the house\n"
    "- finance (Alex) — household financial records — bank transactions, balances, categorization\n"
    "  Delegate when: user asks about spending, balances, transactions, or budgeting\n"
    "</delegates>\n"
    "\n<jobs>\n"
    "- quarterly-accounting:work — Accounting check: Checks the bank and Gmail, "
    "matches invoices, keeps the bank ledger's notes current (Alex's own job: in "
    "text, ask Alex with a sync delegation; Alex starts it itself and it runs in "
    "Alex's topic)\n"
    "</jobs>"
)

REPLY_NOTE = (
    "<casa_notes>\n"
    "The person sent this as a reply to a message Casa posted for 📊 Alex, "
    "posted 2026-10-06 21:40, which read:\n"
    "«📊 Alex\nWhich quarter should the books start from? Reply with the "
    "quarter, for example: start from Q2 2026»\n"
    "Their words answer that message, which may not be your latest one.\n"
    "</casa_notes>\n\n"
)

CASES = [
    {"id": "typed-phrase", "user": "start from Q2 2026", "prefix": "",
     "must": "start from Q2 2026"},
    {"id": "short-request", "user": "check Q2", "prefix": "", "must": "check Q2"},
    {"id": "reply-note", "user": "start from Q2 2026", "prefix": REPLY_NOTE,
     "must": "start from Q2 2026"},
]


def _seed_container_env(*names: str) -> None:
    for name in names:
        if os.environ.get(name):
            continue
        try:
            with open(f"/run/s6/container_environment/{name}", encoding="utf-8") as fh:
                os.environ[name] = fh.read().strip("\n\x00")
        except OSError:
            pass


def _model(cfg) -> str:
    model = cfg.model
    if "${" in model:
        from config import _substitute_env, resolve_model
        try:
            with open("/data/options.json", encoding="utf-8") as fh:
                opts = json.load(fh)
            os.environ.setdefault(
                "PRIMARY_AGENT_MODEL", str(opts.get("primary_agent_model", "")))
        except OSError:
            pass
        model = resolve_model(_substitute_env(model))
    return model


def _fake(production, captured: list, reply: str):
    @tool(production.name, production.description, production.input_schema)
    async def fake(args: dict) -> dict:
        captured.append((production.name, args))
        return {"content": [{"type": "text", "text": reply}]}
    return fake


def _system_prompt(variant: str) -> tuple[str, str]:
    policies = load_policies("/config/policies/disclosure.yaml")
    cfg = load_agent_from_dir("/config/agents/assistant", policies=policies,
                              binding_commit=False)
    bundle = cfg.compiled_prompt_bundle
    if bundle is None:
        raise SystemExit("no compiled bundle: the eval measures the bundle-bound prompt")
    base = projection_for(bundle, channel="telegram", origin_route="telegram").system_prompt
    if variant == "doctrine":
        start = base.find(ANCHOR)
        if start < 0:
            raise SystemExit(f"anchor not found in the projection: {ANCHOR!r}")
        end = base.find("\n\n", start)
        end = len(base) if end < 0 else end
        base = base[:end] + "\n\n" + DOCTRINE_PARAGRAPH + base[end:]
    channel = "\n<channel_context>\nchannel: telegram\ntrust: authenticated (operator)\n</channel_context>"
    return base + channel + SURFACE, _model(cfg)


async def _one(case: dict, system_prompt: str, model: str) -> tuple[bool, list]:
    captured: list = []
    fakes = [
        _fake(casa_tools.delegate_to_agent, captured,
              '{"status": "ok", "text": "[test stub: delegated]"}'),
        _fake(casa_tools.ask_user, captured, '{"status": "pending"}'),
        _fake(casa_tools.start_job, captured, '{"status": "pending"}'),
    ]
    server = create_sdk_mcp_server(name="casa-framework", tools=fakes)
    opts = ClaudeAgentOptions(
        model=model, system_prompt=system_prompt,
        mcp_servers={"casa-framework": server}, tools=[],
        allowed_tools=[f"mcp__casa-framework__{f.name}" for f in fakes],
        strict_mcp_config=True, setting_sources=[], skills=[],
        cwd=tempfile.mkdtemp(), max_turns=3,
    )
    from timekeeping import compose_time_envelope, resolve_tz
    from datetime import datetime
    query = compose_time_envelope(datetime.now(resolve_tz())) + case["prefix"] + case["user"]
    async with ClaudeSDKClient(opts) as client:
        await client.query(query)
        async for _msg in client.receive_response():
            pass
    first = captured[0] if captured else None
    ok = (first is not None and first[0] == "delegate_to_agent"
          and first[1].get("agent") in ("finance", "Alex")
          and case["must"] in str(first[1].get("task", ""))
          and not any(name == "ask_user" for name, _ in captured))
    return ok, captured


async def _run(variant: str, runs: int) -> bool:
    _seed_container_env("CLAUDE_CODE_OAUTH_TOKEN")
    system_prompt, model = _system_prompt(variant)
    print(f"variant={variant} model={model!r} runs={runs}")
    all_ok = True
    for case in CASES:
        passed = 0
        for i in range(runs):
            ok, captured = await _one(case, system_prompt, model)
            passed += ok
            print(f"  [{case['id']} #{i + 1}] {'PASS' if ok else 'FAIL'} "
                  + json.dumps(captured, ensure_ascii=False)[:600])
        print(f"{case['id']}: {passed}/{runs}")
        all_ok = all_ok and passed == runs
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=("baseline", "doctrine"), required=True)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    try:
        ok = asyncio.run(_run(args.variant, args.runs))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        ok = False
    print("VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
