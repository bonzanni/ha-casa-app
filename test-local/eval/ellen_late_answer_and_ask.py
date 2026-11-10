#!/usr/bin/env python3
"""Ellen late-answer and ask eval (#1348): a late answer and its holding
line, and the silence after ``ask_user``.

1. A sync delegation that outlives its 60 s wait returns ``pending``, and the
   answer comes back later as a NOTIFICATION that Casa turns into a fresh turn
   (``agent.Agent._synthesize_delegation_turn``). The late answer is told the
   way a sync one is — by the resident, in her own words, never under the
   delegate's name, every fact and question kept (judged as in
   ``ellen_own_words.py``) — and the reply to the ``pending`` result may be at
   most one short line.
2. After ``ask_user`` has posted a question, the person already sees it with
   its buttons: nothing the resident writes after the call may narrate it.

Live-model gate, the shape of ``ellen_own_words.py``, whose mechanical checks,
judge and doctrine edits it imports (keep the two files side by side): the resident's REAL
compiled text projection (``binding_commit=False``, nothing under /config or
/data written) plus a ``<delegates>`` block, with side-effect-free FAKES of
``delegate_to_agent`` and ``ask_user`` carrying the production descriptions
and schemas and answering in the production result shapes. A late-answer
case runs TWO turns in one session, as production does: the person's ask
(answered ``pending``), then the notice body the installed
``_synthesize_delegation_turn`` builds. Nothing is started, asked or posted.

``--handoff-shown`` (slice 5f) serves the pending result an operator turn gets
once Casa showed its hand-off line: ``tools.PENDING_HANDOFF_SHOWN_NOTE`` after
the note, and the reply to it must then be silence (no holding line).

``--variant baseline`` serves the projection as compiled. ``--variant
doctrine`` applies the own-words doctrine edits of ``ellen_own_words.py``,
after which the projection carries ``DOCTRINE_PARAGRAPH``, so a deployed image
need not carry the change to measure it.

Run inside the deployed container with both files copied under /tmp, or from a
checkout against its default prompt and a logged-in Claude Code CLI:

    venv_test/bin/python test-local/eval/ellen_late_answer_and_ask.py --variant doctrine --runs 5

Exit 0 = every run of every case passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ellen_own_words as own_words  # noqa: E402  (also puts Casa's code on sys.path)

ANCHOR = "When a delegate's result answers the person"

# Kept identical to the paragraph the change adds to the role doctrine; the
# prompt test pins the shipped text, this copy only feeds the doctrine variant.
DOCTRINE_PARAGRAPH = (
    "A delegate's answer that comes back later, in a notification, is passed "
    "on the same way. Casa's lines about a delegate's posts cover only those "
    "posts: an answer it wrote above them is not among them, and is passed on. "
    "A line above them that only announces those posts, points to them or says "
    "what they hold or propose, such as \"I'm posting its card now\" or \"it's "
    "on the card I just posted\", is not an answer: the posts have already "
    "reached the person and speak for themselves, so that line is not passed on. "
    "When a delegation comes back pending, the person would "
    "otherwise wait in silence: say only that it is still running, in one "
    "short line such as \"Alex is still on it.\", with nothing added: not "
    "the task again, not what you have done, not what will happen next. When "
    "`ask_user` has posted a question, the "
    "person already sees it with its buttons: write nothing about it, and "
    "when you have nothing else for them, stay silent exactly as Casa's note "
    "in its result says."
)

# The production result shapes (tools.py: the degraded sync wait and a DM
# turn's ask_user post); a prompt test pins both notes against tools.py, so a
# deployed image that predates the ask_user note still serves the shipped one.
PENDING_NOTE = ("Delegation continues in background; you will receive a "
                "NOTIFICATION when complete.")
ASK_NOTE = (
    "(Casa: the person now sees this question with its buttons in their chat. "
    "Do not announce or retell it. If you have nothing else for them, reply "
    "with exactly <silent/>.)")
AWAITING = {"status": "awaiting_user", "request_id": "5d1e0c7aeval",
            "note": ASK_NOTE}

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
    "text, ask Alex with a sync delegation; Alex starts it itself and runs it)\n"
    "</jobs>"
)

Q1 = ("Q1 2026 isn't in the books yet: they start 1 Apr 2026. Ask me to do Q1 and "
      "I'll start the books from 1 Jan.")
DATA = "September groceries came to €412.30 over 9 payments, most of it at Esselunga."
# The live #1348 case: a slow answer that is a question for the person.
GMAIL = ("The Gmail connection returned no messages for Q3. Is the invoices label "
         "still where your supplier invoices land, or did they move?")
# The live #1348 run on casa-test (2026-10-08): a late answer carrying a
# connection report and a question, with a card the desk posted meanwhile —
# Casa's echo line and silence note follow the answer, as
# ``tools._with_post_echo`` appends them.
LIVE_ASK = ("Ask Alex what he needs me to decide before he can close Q3 — I want "
            "his question, not a summary.")
LIVE_REASON = (
    "The decision is the bank side, not a figure: every sync on the three linked "
    "accounts has returned a 404 since 7 October, and the vault flags this install "
    "as restored from a backup, so those cached balances and transactions can no "
    "longer be trusted as current.")
LIVE_QUESTION = (
    "My question for you — do you want me to re-link the three accounts now before "
    "I close Q3, or close it on the cached data as-is and note the sync gap in the "
    "package?")
LIVE_ANSWER = f"{LIVE_REASON} {LIVE_QUESTION}"
CASES = [
    # The reasoning sentence carries a status code and an internal name, which
    # the resident's other reply rules may put in plain words; the judge counts
    # only whether each fact, conclusion and question keeps its meaning.
    {"id": "late-live", "user": LIVE_ASK, "late_echo": True, "late": LIVE_ANSWER},
    {"id": "late-answer", "user": "do Q1 2026", "late": Q1},
    {"id": "late-data", "user": "how much did we spend on groceries in September?",
     "late": DATA},
    {"id": "late-question", "user": "check Q3", "late": GMAIL},
    # A plain ask: the person asks for buttons, no delegate involved; the
    # first is the live #1348 ask.
    {"id": "ask-live", "ask": True,
     "user": "Quick one: ask me whether I'd like tea or coffee this afternoon. "
             "Just ask, nothing else."},
    {"id": "ask-buttons", "ask": True,
     "user": ("ask me with buttons whether the bins reminder should come "
              "tonight or tomorrow morning")},
]

# A holding line says only that the delegation is still running: one of a
# bounded set of renderings of the doctrine's own example, so a short line
# that claims anything else (a completion, a failure, an unrelated figure)
# fails (#1348 x2). Every live run's line matched the example exactly.
HOLDING_LINE = re.compile(
    r"Alex(?: is|'s) still (?:on it|working on it|looking into it)\.?")


def _system_prompt(variant: str) -> tuple[str, str]:
    from agent_loader import load_agent_from_dir
    from policies import load_policies
    from prompt_compiler import projection_for
    agent_dir, policy = own_words._paths()
    cfg = load_agent_from_dir(agent_dir, policies=load_policies(policy),
                              binding_commit=False)
    bundle = cfg.compiled_prompt_bundle
    if bundle is None:
        raise SystemExit("no compiled bundle: the eval measures the bundle-bound prompt")
    base = projection_for(bundle, channel="telegram", origin_route="telegram").system_prompt
    if variant == "doctrine":
        base = own_words.apply_doctrine(base)
    if _norm(DOCTRINE_PARAGRAPH) not in _norm(base):
        raise SystemExit("the prompt served does not carry DOCTRINE_PARAGRAPH")
    if own_words._RETIRED_TOOL.search(base):
        raise SystemExit("the prompt served still names the retired relay tool")
    channel = "\n<channel_context>\nchannel: telegram\ntrust: authenticated (operator)\n</channel_context>"
    return base + channel + SURFACE, own_words._model(cfg)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'")).strip()


def _silent(text: str) -> bool:
    from output_boundary import strips_to_silence
    return strips_to_silence(text)


HANDOFF_SHOWN = False   # --handoff-shown: only silence passes


def judge_holding(reply: str) -> tuple[bool, str]:
    """The reply to a ``pending`` result: silence, or the one holding line."""
    if _silent(reply):
        return True, "silent"
    if HANDOFF_SHOWN:
        return False, "a line under the hand-off line"
    if HOLDING_LINE.fullmatch(_norm(reply)):
        return True, "one short line"
    return False, "not the holding line"


def judge_after_ask(after: str) -> tuple[bool, str]:
    """What the resident wrote after its ask_user call must be silence."""
    ok = _silent(after)
    return ok, "nothing after the question" if ok else "narrated the question"


def _fake(production, captured: list, reply):
    from claude_agent_sdk import tool

    @tool(production.name, production.description, production.input_schema)
    async def fake(args: dict) -> dict:
        captured.append((production.name, args))
        return {"content": [{"type": "text", "text": reply(args)}]}
    return fake


def _notice_body(case: dict) -> str:
    """The turn text Casa builds from the late completion, by the installed
    code — the eval measures the shipped template, never a copy."""
    from agent import Agent
    from bus import BusMessage, MessageType
    from specialist_registry import DelegationComplete
    import tools as casa_tools
    text = case["late"]
    if case.get("late_echo"):
        text += ("\n\n📊 Alex posted a proposal to your chat (3 buttons).\n"
                 + casa_tools.POST_ECHO_SILENCE_NOTE)
    complete = DelegationComplete(
        delegation_id="0f3c9a2e-eval", agent="finance", status="ok",
        text=text, origin={"user_text": case["user"]}, elapsed_s=74.0)
    msg = BusMessage(type=MessageType.NOTIFICATION, source="finance",
                     target="assistant", content=complete, channel="telegram",
                     context={"chat_id": 1})
    return str(Agent._synthesize_delegation_turn(None, msg).content)


async def _turn(client, text: str) -> tuple[str, str, str]:
    """One turn: (all text, text after an ask_user call, text after a
    delegate_to_agent call)."""
    from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
    from datetime import datetime
    from timekeeping import compose_time_envelope, resolve_tz
    await client.query(compose_time_envelope(datetime.now(resolve_tz())) + text)
    texts: list[str] = []
    after: list[str] = []
    held: list[str] = []
    asked = delegated = False
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    asked = asked or b.name.endswith("__ask_user")
                    delegated = delegated or b.name.endswith("__delegate_to_agent")
                elif isinstance(b, TextBlock):
                    texts.append(b.text)
                    if asked:
                        after.append(b.text)
                    if delegated:
                        held.append(b.text)
    return "\n".join(texts), "\n".join(after), "\n".join(held)


async def _one(case: dict, system_prompt: str, model: str,
               cli: str | None) -> tuple[bool, str, list, str]:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server
    import tools as casa_tools
    captured: list = []
    note = PENDING_NOTE + (" " + casa_tools.PENDING_HANDOFF_SHOWN_NOTE.format(name="Alex")
                           if HANDOFF_SHOWN else "")
    pending = json.dumps({"status": "pending", "delegation_id": "0f3c9a2e-eval",
                          "agent": "finance", "timeout_s": 60, "note": note})
    fakes = [
        _fake(casa_tools.delegate_to_agent, captured, lambda _a: pending),
        _fake(casa_tools.ask_user, captured, lambda _a: json.dumps(AWAITING)),
    ]
    server = create_sdk_mcp_server(name="casa-framework", tools=fakes)
    kw = own_words._sdk_kw(model, cli)
    kw.update(system_prompt=system_prompt,
              mcp_servers={"casa-framework": server}, tools=[],
              allowed_tools=[f"mcp__casa-framework__{f.name}" for f in fakes],
              strict_mcp_config=True, setting_sources=[], skills=[],
              cwd=tempfile.mkdtemp(), max_turns=4)
    async with ClaudeSDKClient(ClaudeAgentOptions(**kw)) as client:
        first, after, holding = await _turn(client, case["user"])
        names = [n for n, _ in captured]
        if case.get("ask"):
            if "ask_user" not in names:
                return False, "never asked", captured, first
            ok, why = judge_after_ask(after)
            return ok, why, captured, first
        if "delegate_to_agent" not in names:
            return False, "never delegated", captured, first
        # Both turns are judged, each on its own, so a run reports both. The
        # holding line is what follows the delegation call: a lead-in before
        # the call is #1332's accepted residual, reported, not judged.
        held, held_why = judge_holding(holding)
        if _norm(first) != _norm(holding):
            held_why += " (+lead-in)"
        late, _after, _held = await _turn(client, _notice_body(case))
    transcript = f"{first!r} || {late!r}"
    # The late answer is told as a sync one is: nothing under the delegate's
    # name or Casa's post header, and every fact, conclusion and question kept.
    ok, why = own_words.mechanical("Alex", f"{first}\n{late}")
    if ok:
        asked = [a for n, a in captured[len(names):] if n == "ask_user"]
        ok, why = await own_words.judge(case["late"], late, asked, cli)
    return (held and ok, f"holding: {held_why}; late: {why}",
            captured, transcript)


async def _run(variant: str, runs: int, cli: str | None) -> bool:
    if own_words.IN_CONTAINER:
        own_words._seed_container_env("CLAUDE_CODE_OAUTH_TOKEN")
    else:
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT",
                  "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(k, None)
    system_prompt, model = _system_prompt(variant)
    print(f"variant={variant} model={model!r} runs={runs}")
    all_ok = True
    for case in CASES:
        passed = 0
        for i in range(runs):
            ok, why, captured, reply = await _one(case, system_prompt, model, cli)
            passed += ok
            print(f"  [{case['id']} #{i + 1}] {'PASS' if ok else 'FAIL'} ({why}) "
                  f"calls={[n for n, _ in captured]} reply={reply!r}")
        print(f"{case['id']}: {passed}/{runs}")
        all_ok = all_ok and passed == runs
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=("baseline", "doctrine"), required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--handoff-shown", action="store_true")
    parser.add_argument("--cli", default=None, help="Claude Code CLI to drive the model with")
    args = parser.parse_args()
    global HANDOFF_SHOWN
    HANDOFF_SHOWN = args.handoff_shown
    try:
        ok = asyncio.run(_run(args.variant, args.runs, args.cli))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        ok = False
    print("VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
