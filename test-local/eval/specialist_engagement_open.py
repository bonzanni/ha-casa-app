#!/usr/bin/env python3
"""Ongoing-engagement eval (#1411): asked for an ongoing conversation with a
specialist, does the engagement stay open after the specialist's first turn,
and does the resident avoid telling the person that a closed topic is open?

Seen live (casa-test, v0.344.79): the resident DID open an interactive
engagement each time, but the finance specialist called ``emit_completion``
on its very first turn, so Casa closed the topic at once; the second time its
summary said "the topic is open and will stay open", and the resident relayed
that.

Three stages, each a live model with side-effect-free FAKES of the
``casa-framework`` tools carrying the PRODUCTION descriptions and schemas:

1. ``compose`` — the resident's real compiled text projection (plus the
   ``<delegates>`` surface) gets the person's request; its
   ``delegate_to_agent`` call is captured (mode, task, context).
2. ``launch`` — the finance specialist's real compiled prompt (its role and
   persona from a ``casa-specialist-finance`` checkout, compiled by
   ``prompt_compiler.compile_projection_set``), its model and effort, gets the
   production launch prompt built from the captured task. A fresh session, as
   live: every engagement starts one. FAIL when it calls ``emit_completion``
   on that turn. A second turn then sends the person's "that's all, we're
   done": the control, PASS only when it now calls ``emit_completion``.
3. ``relay`` (``--history FILE``, a long resumed session like Casa's DM
   session; ``--history`` of ``ellen_ask_buttons.py`` builds one) — the
   resident gets the live request, then the engagement's completion notice as
   ``Agent._synthesize_delegation_turn`` builds it, carrying the live false
   summary. A judge model reads the reply: FAIL when it tells the person the
   topic is open or will stay open.

``--root`` selects the Casa code to measure (default: this checkout), so a
before/after pair runs the same script against two trees. A tree without
``tools.engagement_launch_prompt`` (before #1411) gets the launch prompt that
tree builds inline, copied below verbatim.

    venv_test/bin/python test-local/eval/specialist_engagement_open.py --runs 3 \\
        --finance ~/Projects/casa-specialist-finance \\
        --cli ~/.local/share/claude/versions/2.1.293 [--history FILE] [--root DIR]

Exit 0 = every stage of every run passed.
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
DEFAULT_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "casa", "rootfs", "opt", "casa"))

# The two live requests (casa-test s72, v0.344.79) verbatim, plus a third.
REQUESTS = [
    "Thanks. Now I'd like an ongoing conversation with Alex in an Engagements "
    "topic that stays open, so I can chat with Alex there over the next few "
    "minutes. Topic: 'Quick budgeting chat'. Don't have Alex run any checks or "
    "tools yet; I'll write in the topic myself.",
    "No need to change anything. Then please open a conversation with Alex in "
    "its own topic instead. My question, for Alex only and with no checks or "
    "tools: in one line, what kinds of things can you help me with? Nothing "
    "else, just that.",
    "Can you set me up with Alex for a chat about our grocery spending? I want "
    "to go back and forth with him on it for a while.",
]
DONE = "Thanks Alex, that's all I needed for now. We're done here."
# The live summary of the second sighting, verbatim.
LIVE_SUMMARY = ("The 'Quick budgeting chat' topic is open and will stay open. "
                "I ran no checks or tools. Alex is ready for your messages there.")

# main's inline launch prompt (tools._launch_specialist_engagement), verbatim.
OLD_LAUNCH = (
    "You are engaged with the user in a Telegram forum topic.\n"
    "Task: {task}\n\n"
    "Context from {engager}:\n"
    "{context}\n\n"
    "When the task is complete, call emit_completion(text=..., "
    "artifacts=..., next_steps=..., status='ok')."
)

RESIDENT_TOOLS = [
    "cancel_engagement", "cleanup_engagement_topics", "start_job",
    "delegate_to_agent", "engage_executor", "ask_user", "get_schedule",
    "list_inbound_files", "share_inbound_file", "vault_drop_off",
    "plugin_status", "set_reminder", "cancel_reminder", "ack_event",
    "consent_reprompt", "recall_memory", "send_message", "send_media",
]
SPECIALIST_TOOLS = ["emit_completion", "ask_user", "query_engager",
                    "send_media", "recall_memory", "get_schedule"]
SURFACE = (
    "\n<delegates>\n"
    "- butler (Tina) — house devices — lights, climate, locks, media, sensors\n"
    "  Delegate when: user asks to control or check the state of anything in the house\n"
    "- finance (Alex) — household financial records — bank transactions, balances, categorization\n"
    "  Delegate when: user asks about spending, balances, transactions, or budgeting\n"
    "</delegates>\n"
)
ENG_ID = "afe54c329b904aab9e2fcdb030e9da61"


def _setup(root: str) -> None:
    sys.path.insert(0, root)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT",
              "CLAUDE_CODE_SESSION_ID"):
        os.environ.pop(k, None)


def _resident_prompt(root: str) -> tuple[str, str]:
    from agent_loader import load_agent_from_dir
    from config import resolve_model
    from policies import load_policies
    from prompt_compiler import projection_for
    d = os.path.join(root, "defaults")
    cfg = load_agent_from_dir(os.path.join(d, "agents", "assistant"),
                              policies=load_policies(os.path.join(d, "policies", "disclosure.yaml")),
                              binding_commit=False)
    base = projection_for(cfg.compiled_prompt_bundle, channel="telegram",
                          origin_route="telegram").system_prompt
    channel = ("\n<channel_context>\nchannel: telegram\ntrust: authenticated "
               "(operator)\n</channel_context>")
    return base + channel + SURFACE, resolve_model(cfg.model)


def _specialist_prompt(root: str, finance: str) -> tuple[str, str]:
    from pathlib import Path
    from persona_pack import load_persona_pack
    from prompt_compiler import compile_projection_set
    from role_artifact import load_role_artifact
    from role_slot import materialize_role
    role = materialize_role(source=load_role_artifact(Path(finance) / "role"), options={})
    persona = load_persona_pack(Path(finance) / "persona" / "pack",
                                Path(finance) / "persona" / "manifest.json")
    p = Path(root) / "defaults" / "personality"
    proj = compile_projection_set(
        role=role, persona=persona,
        platform_frame=(p / "platform-frame.md").read_text(encoding="utf-8"),
        safety_kernel=(p / "safety-kernel.md").read_text(encoding="utf-8"))
    return proj["text"].system_prompt, role.resolved_model.sdk_model


def _launch_prompt(task: str, context: str) -> str:
    import tools
    fn = getattr(tools, "engagement_launch_prompt", None)
    if fn is not None:
        return fn(task, context, "Ellen")
    return OLD_LAUNCH.format(task=task, engager="Ellen", context=context or "(none)")


def _fakes(names: list, captured: list, answers: dict) -> list:
    from claude_agent_sdk import tool
    import tools as casa_tools
    out = []
    for name in names:
        prod = getattr(casa_tools, name)

        def make(prod=prod):
            @tool(prod.name, prod.description, prod.input_schema)
            async def fake(args: dict) -> dict:
                captured.append((prod.name, dict(args)))
                text = answers.get(prod.name, json.dumps({"status": "ok"}))
                return {"content": [{"type": "text", "text": text}]}
            return fake
        out.append(make())
    return out


def _options(prompt, model, cli, names, captured, answers, cwd, **extra):
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server
    from config import effort_for
    server = create_sdk_mcp_server(name="casa-framework",
                                   tools=_fakes(names, captured, answers))
    kw = dict(model=model, system_prompt=prompt,
              mcp_servers={"casa-framework": server},
              tools=["Read"], allowed_tools=["Read"]
              + [f"mcp__casa-framework__{n}" for n in names],
              strict_mcp_config=True, setting_sources=[], skills=[],
              cwd=cwd, cli_path=cli, **{"max_turns": 6, **extra})
    effort = effort_for(model)
    if effort:
        kw["effort"] = effort
    return ClaudeAgentOptions(**kw)


async def _turn(client, text: str, envelope: bool = True) -> str:
    from claude_agent_sdk import AssistantMessage, TextBlock
    from datetime import datetime
    from timekeeping import compose_time_envelope, resolve_tz
    head = compose_time_envelope(datetime.now(resolve_tz())) if envelope else ""
    await client.query(head + text)
    out: list[str] = []
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    out.append(b.text)
    return "\n".join(out)


PENDING = json.dumps({"status": "pending", "engagement_id": ENG_ID,
                      "agent": "finance", "mode": "interactive", "topic_id": 2773})


async def _compose(req, rprompt, rmodel, cli):
    from claude_agent_sdk import ClaudeSDKClient
    captured: list = []
    opts = _options(rprompt, rmodel, cli, RESIDENT_TOOLS, captured,
                    {"delegate_to_agent": PENDING}, tempfile.mkdtemp())
    async with ClaudeSDKClient(opts) as client:
        reply = await _turn(client, req)
    calls = [a for n, a in captured if n == "delegate_to_agent"]
    return (calls[0] if calls else None), reply


async def _launch(call, sprompt, smodel, cli):
    from claude_agent_sdk import ClaudeSDKClient
    captured: list = []
    opts = _options(sprompt, smodel, cli, SPECIALIST_TOOLS, captured, {},
                    tempfile.mkdtemp(), max_turns=8)
    prompt = _launch_prompt(call.get("task", ""), call.get("context", ""))
    async with ClaudeSDKClient(opts) as client:
        first = await _turn(client, prompt, envelope=False)
        closed_first = any(n == "emit_completion" for n, _ in captured)
        second = None
        closed_done = closed_first
        if not closed_first:
            second = await _turn(client, DONE, envelope=False)
            closed_done = any(n == "emit_completion" for n, _ in captured)
    summary = next((a.get("text") for n, a in captured if n == "emit_completion"), None)
    return closed_first, closed_done, first, second, summary


def _notice(req: str):
    import agent as agent_mod
    from bus import BusMessage, MessageType
    from specialist_registry import DelegationComplete
    complete = DelegationComplete(
        delegation_id=ENG_ID, agent="finance", status="ok", text=LIVE_SUMMARY,
        message=LIVE_SUMMARY,
        origin={"role": "assistant", "channel": "telegram", "chat_id": "1",
                "cid": "c1", "user_text": req},
        elapsed_s=5.0)
    msg = BusMessage(type=MessageType.NOTIFICATION, source="finance",
                     target="assistant", content=complete, channel="telegram",
                     context={"cid": "c1", "chat_id": "1",
                              "engagement_id": ENG_ID, "next_steps": []})
    return agent_mod.Agent._synthesize_delegation_turn(None, msg).content


JUDGE = (
    "Below is a message an assistant sent to a person after a chat topic with "
    "a specialist called Alex had been CLOSED by the system. Does the message "
    "tell the person, without taking it back, that the topic or conversation "
    "with Alex is still open, will stay open, or that they can keep writing "
    "there? Answer with exactly one word: OPEN if it does, OK if it does "
    "not.\n\nMESSAGE:\n{reply}"
)


async def _judge(reply: str, cli: str) -> str:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    opts = ClaudeAgentOptions(model="claude-haiku-5-5", tools=[], allowed_tools=[],
                              setting_sources=[], cli_path=cli, max_turns=1,
                              cwd=tempfile.mkdtemp())
    async with ClaudeSDKClient(opts) as client:
        out = await _turn(client, JUDGE.format(reply=reply), envelope=False)
    return "OPEN" if "OPEN" in out.upper() else "OK"


async def _relay(req, rprompt, rmodel, cli, history):
    from claude_agent_sdk import ClaudeSDKClient
    captured: list = []
    opts = _options(rprompt, rmodel, cli, RESIDENT_TOOLS, captured,
                    {"delegate_to_agent": PENDING}, history["cwd"],
                    resume=history["session_id"], fork_session=True)
    body = _notice(req)
    async with ClaudeSDKClient(opts) as client:
        first = await _turn(client, req)
        reply = await _turn(client, body)
    return first, reply, body


async def _run(args) -> bool:
    _setup(args.root)
    rprompt, rmodel = _resident_prompt(args.root)
    sprompt, smodel = _specialist_prompt(args.root, args.finance)
    history = None
    if args.history:
        with open(args.history) as fh:
            history = json.load(fh)
    print(f"root={args.root} resident={rmodel} specialist={smodel} runs={args.runs} "
          f"history={args.history!r} new_launch={_has_new()}")
    ok_all = True
    tally = {"interactive": 0, "open": 0, "closes_on_done": 0, "relay_ok": 0, "n": 0, "rn": 0}
    for ri, req in enumerate(REQUESTS):
        for i in range(args.runs):
            tag = f"req{ri} #{i + 1}"
            if args.stage in ("all", "launch"):
                call, reply = await _compose(req, rprompt, rmodel, args.cli)
                tally["n"] += 1
                if call is None:
                    print(f"  {tag} NO-DELEGATION reply={reply!r}")
                    ok_all = False
                    continue
                mode = call.get("mode")
                tally["interactive"] += mode == "interactive"
                closed_first, closed_done, first, second, summary = await _launch(
                    call, sprompt, smodel, args.cli)
                tally["open"] += not closed_first
                tally["closes_on_done"] += closed_done and not closed_first
                ok = mode == "interactive" and not closed_first and closed_done
                ok_all &= ok
                print(f"  {tag} {'PASS' if ok else 'FAIL'} mode={mode} "
                      f"closed_on_launch={closed_first} closed_on_done={closed_done}\n"
                      f"    task={call.get('task')!r}\n    context={call.get('context')!r}\n"
                      f"    first={first!r}\n    second={second!r}\n    summary={summary!r}")
            if args.stage in ("all", "relay") and history is not None and ri == 0:
                first, reply, body = await _relay(req, rprompt, rmodel, args.cli, history)
                verdict = await _judge(reply, args.cli)
                tally["rn"] += 1
                tally["relay_ok"] += verdict == "OK"
                ok_all &= verdict == "OK"
                print(f"  {tag} RELAY {verdict}\n    turn1={first!r}\n    reply={reply!r}")
                if i == 0:
                    print(f"    notice={body!r}")
    print(f"INTERACTIVE {tally['interactive']}/{tally['n']} "
          f"STAYS-OPEN {tally['open']}/{tally['n']} "
          f"CLOSES-WHEN-DONE {tally['closes_on_done']}/{tally['open']} "
          f"RELAY-OK {tally['relay_ok']}/{tally['rn']}")
    return ok_all


def _has_new() -> bool:
    import tools
    return hasattr(tools, "engagement_launch_prompt")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--finance", required=True)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--history", default=None)
    parser.add_argument("--stage", choices=["all", "launch", "relay"], default="all")
    args = parser.parse_args()
    args.root = os.path.abspath(os.path.expanduser(args.root))
    args.finance = os.path.abspath(os.path.expanduser(args.finance))
    try:
        ok = asyncio.run(_run(args))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        ok = False
    print("VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
