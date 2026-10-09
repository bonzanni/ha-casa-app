#!/usr/bin/env python3
"""Ellen ask-buttons eval (#1390): when the person asks to choose with buttons
between naturally long choices, do the buttons show words, or does the whole
keyboard fall back to "Option 1/2/3"?

Live-model check, in the shape of the sibling ``ellen_*`` evals: the
resident's REAL compiled text projection plus its ``<executors>`` block, and
side-effect-free FAKES of every ``casa-framework`` tool the resident is
granted, carrying the PRODUCTION descriptions and input schemas (a lone
``ask_user`` reads differently from one tool among eighteen). The model,
effort and CLI are the ones Casa runs with (``config.effort_for``,
``claude_runtime.CLAUDE_CLI_PATH``; ``--cli`` overrides the CLI for a local
run). The captions are computed by the production resolver
(``channels.telegram.short_option_labels``), so the verdict is what the
keyboard would actually show.

``--warm N`` first runs N ordinary turns in the same session.

``--history FILE`` is the live condition: Casa resumes ONE long-lived DM
session across restarts and releases, so a question is asked with a long
history behind it, written under earlier tool descriptions. The first run
builds that history once (pasted statements, then two button questions asked
under the pre-#1390 ``ask_user`` description, with their taps) and records the
session id in FILE; every run then resumes a FORK of it with this tree's
description, so a before/after pair shares the same history.

    venv_test/bin/python test-local/eval/ellen_ask_buttons.py --local --runs 3 \\
        --cli "$(command -v claude)"

Prints one line per run and a ``WORDS x/y`` total. Exit 0 = every run's
buttons showed words (no "Option n" floor).
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
sys.path.insert(0, HERE)
import ellen_topic_purge as base  # noqa: E402 — the system-prompt builder

# The two live-check wordings (casa-test, v0.344.74) verbatim, plus a third.
REQUESTS = [
    "The boiler is losing pressure again. Let me choose with buttons between: "
    "calling the plumber at the emergency rate today, topping it up myself "
    "until the November service, or replacing it now with the heat-pump "
    "subsidy.",
    "For the bathroom tap that keeps dripping I could have the handyman fix "
    "the washer next week, buy a new mixer tap and fit it myself this weekend, "
    "or leave it until the bathroom renovation in spring. Give me buttons to "
    "pick one.",
    "My sister's birthday is on Sunday. I'm torn between booking a table at "
    "the Italian place she likes for Saturday night, ordering the cake from "
    "the bakery and hosting lunch at ours on Sunday, or just sending flowers "
    "and calling her since she might be away. Buttons please.",
]
WARM = [
    "What's a good temperature to set the fridge to?",
    "Remind me what the difference is between a combi boiler and a system boiler, briefly.",
    "Thanks. Can you suggest a quick dinner with eggs and spinach?",
    "How long should I let a cast-iron pan cool before washing it?",
]
CASA_TOOLS = [
    "cancel_engagement", "cleanup_engagement_topics", "start_job",
    "delegate_to_agent", "engage_executor", "ask_user", "get_schedule",
    "list_inbound_files", "share_inbound_file", "vault_drop_off",
    "plugin_status", "set_reminder", "cancel_reminder", "ack_event",
    "consent_reprompt", "recall_memory", "send_message", "send_media",
]


def _old_description(desc: str) -> str:
    """The ``ask_user`` description before #1390: the button-wording steer
    (everything between "their DM. " and "Two-turn:") removed."""
    import re
    return re.sub(r"their DM\. .*?Two-turn:", "their DM. Two-turn:", desc,
                  flags=re.S)


# History turns (``--history``): bulk like a day of plugin output, then two
# button questions with naturally long choices, each answered by a tap.
def _statement(n: int) -> str:
    import random
    rnd = random.Random(n)
    shops = ["Albert Heijn", "Jumbo", "Shell", "Gamma", "Praxis", "HEMA",
             "Bol.com", "Eneco", "Vitens", "Ziggo", "NS", "Kruidvat"]
    rows = [f"2026-0{1 + i % 9}-{1 + i % 28:02d}  {rnd.choice(shops):<12} "
            f"EUR {rnd.randint(2, 400)}.{rnd.randint(0, 99):02d}  "
            f"ref NL{rnd.randint(10**9, 10**10)}" for i in range(n)]
    return "\n".join(rows)


HISTORY = [
    "Here is the bank export for the first half of the year, keep it handy "
    "for the quarterly check, no need to summarise:\n" + _statement(900),
    "And the second half, same thing:\n" + _statement(900),
    "Ask me with buttons whether to renew the car insurance with the current "
    "provider at the quoted price, switch to the cheaper online insurer with "
    "the higher excess, or call the current one first to negotiate.",
    "@ANSWER",
    "Give me buttons for the gutters: pay the roofer to clean them before "
    "the storms next week, borrow the neighbour's ladder and do it myself "
    "on Saturday, or wait until the spring maintenance visit.",
    "@ANSWER",
]


def _fakes(captured: list, ask_description: str | None = None) -> list:
    from claude_agent_sdk import tool
    import tools as casa_tools

    awaiting = json.dumps({"status": "awaiting_user", "request_id": "r1",
                           "note": casa_tools.ASK_USER_SILENCE_NOTE})
    out = []
    for name in CASA_TOOLS:
        prod = getattr(casa_tools, name)
        desc = (ask_description if name == "ask_user" and ask_description
                else prod.description)

        def make(prod=prod, desc=desc):
            @tool(prod.name, desc, prod.input_schema)
            async def fake(args: dict) -> dict:
                if prod.name == "ask_user":
                    # The production validation, so a refused question is
                    # refused here too and the model can answer the refusal.
                    err = casa_tools._ask_user_validate(args)[-1]
                    if err is not None:
                        captured.append(("ask_user_refused", dict(args)))
                        return {"content": [{"type": "text", "text": json.dumps(
                            {"status": "error", "kind": "invalid_arguments",
                             "message": err})}], "is_error": True}
                captured.append((prod.name, dict(args)))
                text = (awaiting if prod.name == "ask_user"
                        else json.dumps({"status": "ok"}))
                return {"content": [{"type": "text", "text": text}]}
            return fake
        out.append(make())
    return out


def _captions(options: list) -> tuple[list, list]:
    from channels.telegram import short_option_labels
    labels = [o.get("label") if isinstance(o, dict) else o for o in options]
    shorts = [o.get("short") if isinstance(o, dict) else None for o in options]
    return labels, short_option_labels([str(x) for x in labels], shorts)


def _options(prompt: str, model: str, cli: str | None, captured: list,
             cwd: str, ask_description: str | None = None, **extra):
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server
    from claude_runtime import CLAUDE_CLI_PATH
    from config import effort_for

    server = create_sdk_mcp_server(
        name="casa-framework", tools=_fakes(captured, ask_description))
    kw = dict(model=model, system_prompt=prompt,
              mcp_servers={"casa-framework": server},
              tools=["Read", "Write", "Edit"],
              allowed_tools=["Read", "Write", "Edit"]
              + [f"mcp__casa-framework__{n}" for n in CASA_TOOLS],
              strict_mcp_config=True, setting_sources=[], skills=[],
              cwd=cwd, max_turns=6, cli_path=cli or CLAUDE_CLI_PATH, **extra)
    effort = effort_for(model)
    if effort:
        kw["effort"] = effort
    return ClaudeAgentOptions(**kw)


async def _session_turn(client, text: str) -> str | None:
    """One turn; returns the session id the result reports."""
    from claude_agent_sdk import ResultMessage
    from datetime import datetime
    from timekeeping import compose_time_envelope, resolve_tz
    await client.query(compose_time_envelope(datetime.now(resolve_tz())) + text)
    sid = None
    async for msg in client.receive_response():
        if isinstance(msg, ResultMessage):
            sid = msg.session_id
    return sid


async def _build_history(path: str, prompt: str, model: str,
                         cli: str | None) -> dict:
    """Build the long history once (pre-#1390 description) and record it."""
    from claude_agent_sdk import ClaudeSDKClient
    import tools as casa_tools

    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    cwd = tempfile.mkdtemp(prefix="ask-history-")
    captured: list = []
    old = _old_description(casa_tools.ask_user.description)
    sid = None
    async with ClaudeSDKClient(
            _options(prompt, model, cli, captured, cwd, old)) as client:
        for text in HISTORY:
            if text == "@ANSWER":
                asks = [a for n, a in captured if n == "ask_user"]
                opts = asks[-1].get("options") if asks else None
                first = opts[0] if opts else "the first one"
                if isinstance(first, dict):
                    first = first.get("label")
                text = f"[button answer to r1]: {first}"
            sid = await _session_turn(client, text) or sid
    asks = [a.get("options") for n, a in captured if n == "ask_user"]
    rec = {"session_id": sid, "cwd": cwd, "history_asks": asks}
    with open(path, "w") as fh:
        json.dump(rec, fh)
    print(f"history built: {rec}")
    return rec


async def _one(req: str, prompt: str, model: str, cli: str | None,
               warm: int, history: dict | None = None) -> list:
    from claude_agent_sdk import ClaudeSDKClient

    captured: list = []
    if history is not None:
        opts = _options(prompt, model, cli, captured, history["cwd"],
                        resume=history["session_id"], fork_session=True)
    else:
        opts = _options(prompt, model, cli, captured, tempfile.mkdtemp())
    async with ClaudeSDKClient(opts) as client:
        for w in WARM[:warm]:
            await base._turn(client, w)
        captured.clear()
        await base._turn(client, req)
    refused = sum(1 for n, _a in captured if n == "ask_user_refused")
    return [a for n, a in captured if n == "ask_user"], refused


async def _run(args) -> bool:
    if args.local:
        sys.path.insert(0, base.LOCAL_ROOT)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT",
                  "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(k, None)
    else:
        sys.path.insert(0, "/opt/casa")
    prompt, model = base._system_prompt(args.local)
    print(f"model={model!r} runs={args.runs} warm={args.warm} "
          f"history={args.history!r}")
    history = (await _build_history(args.history, prompt, model, args.cli)
               if args.history else None)
    words = total = 0
    for ri, req in enumerate(REQUESTS):
        for i in range(args.runs):
            asks, refused = await _one(req, prompt, model, args.cli,
                                       args.warm, history)
            total += 1
            if not asks:
                print(f"  req{ri} #{i + 1} NO-ASK refused={refused}")
                continue
            opts = asks[0].get("options") or []
            labels, caps = _captions(opts)
            ok = bool(caps) and caps[0] != "Option 1"
            words += ok
            print(f"  req{ri} #{i + 1} {'WORDS' if ok else 'FLOOR'} "
                  f"refused={refused} "
                  f"buttons={caps!r} labels={[len(str(x)) for x in labels]} "
                  f"q={asks[0].get('question')!r} options={opts!r}")
    print(f"WORDS {words}/{total}")
    return words == total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warm", type=int, default=0)
    parser.add_argument("--history", default=None)
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
