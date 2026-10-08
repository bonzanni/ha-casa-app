#!/usr/bin/env python3
"""Ellen late-answer and ask eval (#1348): the two paths #1332's relay eval
does not reach.

1. A sync delegation that outlives its 60 s wait returns ``pending``, and the
   answer comes back later as a NOTIFICATION that Casa turns into a fresh turn
   (``agent.Agent._synthesize_delegation_turn``). The late answer must go out
   exactly as a sync one does — "Alex: <its exact words>" — and the reply to
   the ``pending`` result may be at most one short line.
2. After ``ask_user`` has posted a question, the person already sees it with
   its buttons: nothing the resident writes after the call may narrate it.

Live-model gate, the shape of ``ellen_relay_fidelity.py``: the resident's REAL
compiled text projection (``binding_commit=False``, nothing under /config or
/data written) plus a ``<delegates>`` block, with side-effect-free FAKES of
``delegate_to_agent`` and ``ask_user`` carrying the production descriptions
and schemas and answering in the production result shapes. A late-answer
case runs TWO turns in one session, as production does: the person's ask
(answered ``pending``), then the notice body the installed
``_synthesize_delegation_turn`` builds. Nothing is started, asked or posted.

``--variant baseline`` serves the projection as compiled. ``--variant
doctrine`` inserts ``DOCTRINE_PARAGRAPH`` after the #1332 paragraph, where the
change puts it, so a deployed image need not carry the change to measure it.

Run inside the deployed container, one-shot, writing only under /tmp:

    cat test-local/eval/ellen_late_answer_and_ask.py \\
        | ssh <host> -- sudo -n docker exec -i <container> python3 -u - --variant doctrine --runs 5

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

sys.path.insert(0, "/opt/casa")

ANCHOR = "When a delegate's result answers the person"

# Kept identical to the paragraph the change adds to the role doctrine; the
# prompt test pins the shipped text, this copy only feeds the doctrine variant.
DOCTRINE_PARAGRAPH = (
    "A delegate's answer that comes back later, in a notification, is passed "
    "on the same way. Passed on under the delegate's name, what it says about "
    "a connection stays its own report: add no note of yours on whether it "
    "has been checked. Casa's lines about a delegate's posts cover only those "
    "posts: an answer it wrote above them is not among them, and is passed on. "
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
    "text, ask Alex with a sync delegation; Alex starts it itself and it runs in "
    "Alex's topic)\n"
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
    # the resident's other reply rules may translate (#1332 keeps them applying
    # to what is passed on): it is judged by ``judge_carried``, the question
    # verbatim.
    {"id": "late-live", "user": LIVE_ASK, "late_echo": True, "late": LIVE_ANSWER,
     "carried": (LIVE_REASON, LIVE_QUESTION)},
    {"id": "late-answer", "user": "do Q1 2026", "late": Q1,
     "expect": [f"Alex: {Q1}", f'Alex: "{Q1}"']},
    {"id": "late-data", "user": "how much did we spend on groceries in September?",
     "late": DATA, "expect": [f"Alex: {DATA}", f'Alex: "{DATA}"']},
    {"id": "late-question", "user": "check Q3", "late": GMAIL,
     "expect": [f"Alex: {GMAIL}", f'Alex: "{GMAIL}"']},
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


def _system_prompt(variant: str) -> tuple[str, str]:
    from agent_loader import load_agent_from_dir
    from policies import load_policies
    from prompt_compiler import projection_for
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


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'")).strip()


def _silent(text: str) -> bool:
    from output_boundary import strips_to_silence
    return strips_to_silence(text)


def judge_holding(reply: str) -> tuple[bool, str]:
    """The reply to a ``pending`` result: silence, or the one holding line."""
    if _silent(reply):
        return True, "silent"
    if HOLDING_LINE.fullmatch(_norm(reply)):
        return True, "one short line"
    return False, "not the holding line"


def judge_late(case: dict, reply: str) -> tuple[bool, str]:
    ok = _norm(reply) in {_norm(e) for e in case["expect"]}
    return ok, "exact" if ok else "not the attributed answer"


# The reasoning sentence as it may be carried: verbatim, or with the status
# code and the internal name put in plain words, as the resident's other reply
# rules may do (#1332 keeps them applying). Every other word is the
# specialist's, so a changed date, a reversed conclusion or a hedge of the
# resident's own fails the match (#1348 x1). The renderings are the ones the
# live runs produced.
SYNC_FAILED = ("returned a 404", "failed", "been failing", "returned an error")
FLAGGED = ("the vault flags this install", "this install is flagged",
           "this setup is flagged", "the records flag this install")
CARRIED_REASON = re.compile(
    "The decision is the bank side, not a figure: every sync on the three "
    "linked accounts has (?:" + "|".join(map(re.escape, SYNC_FAILED)) + ") "
    "since 7 October, and (?:" + "|".join(map(re.escape, FLAGGED)) + ") as "
    "restored from a backup, so those cached balances and transactions can no "
    "longer be trusted as current\\.")


def judge_carried(case: dict, reply: str) -> tuple[bool, str]:
    """The attributed answer as ONE message: "Alex: ", the reasoning sentence
    in one of its allowed renderings, then the question verbatim, and nothing
    after."""
    reason, question = case["carried"]
    assert CARRIED_REASON.fullmatch(_norm(reason)), "the verbatim reason must match"
    r = _norm(reply)
    for quote in ('"', ""):
        head, tail = f"Alex: {quote}", f"{_norm(question)}{quote}"
        if r.startswith(head) and r.endswith(tail):
            break
    else:
        return False, "not the attributed answer ending in its question"
    middle = r[len(head):len(r) - len(tail)].strip()
    if not CARRIED_REASON.fullmatch(middle):
        return False, "the reasoning was not carried as written"
    return True, "carried"


def judge_late_turn(case: dict, late: str, asked: bool) -> tuple[bool, str]:
    """The late turn: the attributed answer as text. Putting it to the person
    as a button question instead is not passing it on, and silence after such
    a question would hide the answer, so an ask fails (#1348 x1)."""
    if asked:
        return False, "asked instead of passing the answer on"
    if "carried" in case:
        return judge_carried(case, late)
    return judge_late(case, late)


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


async def _one(case: dict, system_prompt: str, model: str) -> tuple[bool, str, list, str]:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server
    from claude_runtime import CLAUDE_CLI_PATH
    from config import effort_for
    import tools as casa_tools
    captured: list = []
    pending = json.dumps({"status": "pending", "delegation_id": "0f3c9a2e-eval",
                          "agent": "finance", "timeout_s": 60, "note": PENDING_NOTE})
    fakes = [
        _fake(casa_tools.delegate_to_agent, captured, lambda _a: pending),
        _fake(casa_tools.ask_user, captured, lambda _a: json.dumps(AWAITING)),
    ]
    server = create_sdk_mcp_server(name="casa-framework", tools=fakes)
    opts = ClaudeAgentOptions(
        model=model, effort=effort_for(model), cli_path=CLAUDE_CLI_PATH,
        system_prompt=system_prompt,
        mcp_servers={"casa-framework": server}, tools=[],
        allowed_tools=[f"mcp__casa-framework__{f.name}" for f in fakes],
        strict_mcp_config=True, setting_sources=[], skills=[],
        cwd=tempfile.mkdtemp(), max_turns=4,
    )
    async with ClaudeSDKClient(opts) as client:
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
        ok, why = judge_late_turn(
            case, late, "ask_user" in [n for n, _ in captured[len(names):]])
        return (held and ok, f"holding: {held_why}; late: {why}",
                captured, transcript)


async def _run(variant: str, runs: int) -> bool:
    _seed_container_env("CLAUDE_CODE_OAUTH_TOKEN")
    system_prompt, model = _system_prompt(variant)
    print(f"variant={variant} model={model!r} runs={runs}")
    all_ok = True
    for case in CASES:
        passed = 0
        for i in range(runs):
            ok, why, captured, reply = await _one(case, system_prompt, model)
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
