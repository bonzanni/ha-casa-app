#!/usr/bin/env python3
"""Ellen own-words eval: a delegate's answer reaches the person told by the
resident herself, in her own words, with every fact, amount, date, conclusion
and question the delegate gave kept exactly.

Live-model gate. It drives the resident's REAL compiled text projection
(``prompt_compiler.projection_for``, loaded with ``binding_commit=False`` so
nothing under /config or /data is written) plus a ``<delegates>``/``<jobs>``
block of the shape ``agent._render_prompt_surface`` renders. ``delegate_to_agent``,
``ask_user`` and ``start_job`` are side-effect-free FAKES carrying the
PRODUCTION descriptions and input schemas from ``tools.py``; the fake
``delegate_to_agent`` answers in the production result shapes. A late answer
runs TWO turns in one session, as production does: the person's ask (answered
``pending``), then the notice body the installed
``Agent._synthesize_delegation_turn`` builds. Nothing is started, asked or
posted.

Cases: (a) a sync answer with two amounts, a date and a conclusion; (b) the
same kind of answer coming back later, by notification; (c) the configurator's
late answer that asks the person a question; (d) a delegate that closed its
turn with ``<silent/>``.

A run passes when the resident delegated, and
- nothing she wrote has a line opening with the delegate's name and a colon,
  and no ``📊`` (Casa's header for a delegate's own post);
- for (d), her reply strips to silence;
- otherwise a JUDGE model (``JUDGE_MODEL``) reads the delegate's answer and her
  reply (with the question and buttons of an ``ask_user`` call, if she made
  one) and finds that every fact keeps its association (amount with payee,
  amount with date), every conclusion keeps its meaning and every question
  the delegate asks the person reaches them. Fidelity is judged, not grepped:
  a reply that holds every token but swaps two amounts is still wrong.

``--judge-check`` runs the judge alone on hand-made replies (swapped amounts,
a reversed conclusion, an omitted question: each must be rejected; a faithful
paraphrase must be accepted) and exits.

``--variant doctrine`` (the default) serves the projection with the
own-words rule in place of the rule it replaces, so an image that does not yet
carry the change can measure it; ``--variant shipped`` serves the projection
as compiled. Either way the prompt served must not name the retired relay
tool.

Run inside the deployed container, one-shot, writing only under /tmp:

    cat test-local/eval/ellen_own_words.py \\
        | ssh <host> -- sudo -n docker exec -i <container> python3 -u - --reps 5

or from a checkout, against its default prompt and a logged-in Claude Code CLI
(``--cli``, default: ``claude`` on PATH):

    venv_test/bin/python test-local/eval/ellen_own_words.py --reps 5

Exit 0 = every run of every case passed (or, with ``--judge-check``, the judge
gave every hand-made reply its expected verdict).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else ""
LOCAL_ROOT = (os.path.normpath(os.path.join(HERE, "..", "..", "casa", "rootfs", "opt", "casa"))
              if HERE else "")
IN_CONTAINER = os.path.exists("/opt/casa/agent.py")
sys.path.insert(0, "/opt/casa" if IN_CONTAINER else LOCAL_ROOT)

JUDGE_MODEL = "claude-haiku-5-5"

# The rule as the resident's prompt states it; a prompt test pins this copy to
# the shipped text, and the doctrine variant serves it.
DOCTRINE_PARAGRAPH = (
    "When a delegate's result answers the person, tell them that answer "
    "yourself, in your own words and as yourself: the delegate's \"I\" becomes "
    "its name (\"Alex found €420 in October…\"). Keep every fact, amount, date, "
    "name and question it gives them exactly as it gave them; leave out only "
    "its account of how it worked. Never write it under the delegate's name or "
    "as a quote introduced by its name, and add no question, offer or next "
    "step it did not make, and no note on what you will do. Say it in as many "
    "words as the answer needs and no more. A result meant for you rather than "
    "the person is not passed on, except that a question or request it asks "
    "you to put to them is put to them."
)

ANCHOR = "When a delegate's result answers the person"


def _loose(text: str) -> re.Pattern:
    """*text* matched across any run of whitespace, as the prompt wraps it;
    ``{tool}`` stands for any one tool name."""
    return re.compile(r"\s+".join(re.escape(w).replace(re.escape("{tool}"), r"\w+")
                                  for w in text.split()))


# The tool the own-words rule retires, as the prompt named it.
_RETIRED_TOOL = re.compile(r"\brelay_\w+")


# The doctrine variant's other edits, the same ones the change makes to the
# role doctrine: (old, new). ``None`` as old marks nothing to do.
DOCTRINE_EDITS = [
    # the late-answer paragraph loses its sentence about a delegate's report
    # passed on under its name: the resident now speaks herself, and the
    # connection-verdict rule covers what she says
    ("Passed on under the delegate's name, what it says about a connection "
     "stays its own report: add no note of yours on whether it has been "
     "checked.", ""),
    ("pass its answer on with {tool}; in Telegram they carry on with it "
     "directly by replying to its message, and if they ask how to go on with "
     "it, tell them that.",
     "tell them its answer in your own words; they carry on by writing to "
     "you, and you delegate their next words the same way."),
]

# delegate_to_agent's ``mode`` description, as the change words it.
MODE_DESCRIPTION = (
    "'sync' waits for the answer; 'async' returns at once and the answer "
    "arrives later. 'interactive' is retired and always refused: to let the "
    "person talk with a specialist, delegate their words in 'sync' mode; they "
    "carry on by writing to you.")

CONFIGURATOR_WHEN = (
    "The person wants to CHANGE Casa's configuration, or to install an existing "
    "component from a repository (owner/repo@ref). It runs by itself and its "
    "answer comes back to you: tell the person what it says, in your own "
    "words. When it asks something, put it to the person, then delegate their "
    "answer to it with what it asked.")

SURFACE = (
    "\n<delegates>\n"
    "- butler (Tina) — house devices — lights, climate, locks, media, sensors\n"
    "  Delegate when: user asks to control or check the state of anything in the house\n"
    "- configurator — Casa's own configuration: agents, triggers, delegates, prompts "
    "and voice files, and the components installed from repositories\n"
    f"  Delegate when: {CONFIGURATOR_WHEN}\n"
    "- finance (Alex) — household financial records — bank transactions, balances, categorization\n"
    "  Delegate when: user asks about spending, balances, transactions, or budgeting\n"
    "</delegates>\n"
    "\n<jobs>\n"
    "- quarterly-accounting:work — Accounting check: Checks the bank and Gmail, "
    "matches invoices, keeps the bank ledger's notes current (Alex's own job: in "
    "text, ask Alex with a sync delegation; Alex starts it itself and runs it)\n"
    "</jobs>"
)

# The production result shapes (tools.py): the degraded sync wait, and an
# async delegation's immediate return.
PENDING_NOTE = ("Delegation continues in background; you will receive a "
                "NOTIFICATION when complete.")
DELEGATION_ID = "0f3c9a2e-eval"

GROCERIES = ("You spent €301.75 at Lidl and €84.45 at Albert Heijn on groceries "
             "in October; the last payment was on 28 October. That is less than "
             "in September.")
ENERGY = ("Q3 energy came to €612.80: €418.30 for electricity and €194.50 for "
          "gas. The September bill, paid on 2 October, was the highest of the "
          "three.")
WEATHER = ("The weather plugin v1.2.0 is installed. Before it posts the daily "
           "forecast at 07:30 it needs one choice from the person: temperatures "
           "in Celsius or in Fahrenheit?")
SILENT_DESK = "Starting the Q3 accounting check; the card is on its way.\n\n<silent/>"

CASES = [
    {"id": "a-sync-answer", "user": "how much did we spend on groceries in October?",
     "agent": "finance", "name": "Alex", "answer": GROCERIES},
    {"id": "b-late-answer", "user": "what did energy cost us in Q3?",
     "agent": "finance", "name": "Alex", "answer": ENERGY, "late": True},
    {"id": "c-configurator-asks",
     "user": "install the weather plugin from owner/casa-plugin-weather@v1.2.0",
     "agent": "configurator", "name": "configurator", "answer": WEATHER, "late": True},
    {"id": "d-silent-close", "user": "start the Q3 accounting",
     "agent": "finance", "name": "Alex", "answer": SILENT_DESK, "silent": True},
]

# The judge's own check: three replies that are wrong in the way a retelling
# can be wrong, and one that is right. Each must get its expected verdict.
JUDGE_CHECK = [
    ("a-sync-answer", "swapped amounts",
     "Alex found you spent €84.45 at Lidl and €301.75 at Albert Heijn on "
     "groceries in October, with the last payment on 28 October. That's less "
     "than in September.", False),
    ("a-sync-answer", "reversed conclusion",
     "Alex found you spent €301.75 at Lidl and €84.45 at Albert Heijn on "
     "groceries in October, with the last payment on 28 October. That's more "
     "than in September.", False),
    ("c-configurator-asks", "omitted question",
     "The weather plugin v1.2.0 is installed. It will post the daily forecast "
     "at 07:30.", False),
    ("a-sync-answer", "faithful paraphrase",
     "Alex says October groceries were €301.75 at Lidl and €84.45 at Albert "
     "Heijn, last paid on 28 October — less than September.", True),
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


def _paths() -> tuple[str, str]:
    if IN_CONTAINER:
        return "/config/agents/assistant", "/config/policies/disclosure.yaml"
    d = os.path.join(LOCAL_ROOT, "defaults")
    return (os.path.join(d, "agents", "assistant"),
            os.path.join(d, "policies", "disclosure.yaml"))


def _model(cfg) -> str:
    from config import resolve_model
    model = cfg.model
    if "${" in model:
        from config import _substitute_env
        try:
            with open("/data/options.json", encoding="utf-8") as fh:
                os.environ.setdefault(
                    "PRIMARY_AGENT_MODEL", str(json.load(fh).get("primary_agent_model", "")))
        except OSError:
            pass
        model = _substitute_env(model)
    return resolve_model(model)


def apply_doctrine(base: str) -> str:
    """*base* with the own-words rule in place of the paragraph it replaces,
    and the doctrine's other edits applied."""
    start = base.find(ANCHOR)
    if start < 0:
        raise SystemExit(f"anchor not found in the projection: {ANCHOR!r}")
    end = base.find("\n\n", start)
    end = len(base) if end < 0 else end
    base = base[:start] + DOCTRINE_PARAGRAPH + base[end:]
    for old, new in DOCTRINE_EDITS:
        pattern = _loose(old) if new else re.compile(_loose(old).pattern + r"\s+")
        base, n = pattern.subn(new, base)
        if n != 1:
            raise SystemExit(f"doctrine edit applied {n} times: {old[:50]!r}… "
                             "(an image that carries the change: use --variant shipped)")
    return base


def _system_prompt(variant: str) -> tuple[str, str]:
    from agent_loader import load_agent_from_dir
    from policies import load_policies
    from prompt_compiler import projection_for
    agent_dir, policy = _paths()
    cfg = load_agent_from_dir(agent_dir, policies=load_policies(policy),
                              binding_commit=False)
    bundle = cfg.compiled_prompt_bundle
    if bundle is None:
        raise SystemExit("no compiled bundle: the eval measures the bundle-bound prompt")
    base = projection_for(bundle, channel="telegram", origin_route="telegram").system_prompt
    if variant == "doctrine":
        base = apply_doctrine(base)
    if _RETIRED_TOOL.search(base):
        raise SystemExit("the prompt served still names the retired relay tool")
    channel = ("\n<channel_context>\nchannel: telegram\ntrust: authenticated "
               "(operator)\n</channel_context>")
    return base + channel + SURFACE, _model(cfg)


def _fake(production, captured: list, reply, schema=None):
    from claude_agent_sdk import tool

    @tool(production.name, production.description, schema or production.input_schema)
    async def fake(args: dict) -> dict:
        captured.append((production.name, args))
        return {"content": [{"type": "text", "text": reply(args)}]}
    return fake


def _sdk_kw(model: str, cli: str | None) -> dict:
    from config import effort_for
    kw: dict = {"model": model}
    effort = effort_for(model)
    if effort:
        kw["effort"] = effort
    if IN_CONTAINER:
        from claude_runtime import CLAUDE_CLI_PATH
        kw["cli_path"] = cli or CLAUDE_CLI_PATH
    else:
        kw["cli_path"] = cli or shutil.which("claude")
    return kw


# ---------------------------------------------------------------------------
# mechanical checks and the judge
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'")).strip()


def mechanical(name: str, said: str) -> tuple[bool, str]:
    """Nothing the resident wrote is put under the delegate's name or Casa's
    post header."""
    if "📊" in said:
        return False, "📊 in the reply"
    head = re.compile(rf"^[\s>*_\"“]*{re.escape(name)}[\s*_]*:", re.IGNORECASE)
    if any(head.match(line) for line in said.splitlines()):
        return False, f"a line opens with '{name}:'"
    return True, "ok"


JUDGE_SYSTEM = (
    "You check whether an assistant passed a colleague's answer on to a person "
    "faithfully. You get the colleague's ANSWER and the assistant's REPLY to "
    "the person (with any question it put to them with buttons). The reply is "
    "a retelling in the assistant's own words, so wording, order, tense and "
    "person may change, and the colleague may be named in the third person. "
    "List every item of the answer meant for the person: each fact (each "
    "amount with what it belongs to, each date with what it dates, each name, "
    "version and time), each conclusion or comparison, and each question the "
    "colleague asks the person. For each, decide whether the reply states it "
    "with the SAME association and meaning: an amount moved to another payee, "
    "a changed figure or date, a comparison reversed, or a question dropped "
    "or changed into a different question is NOT kept. Rewording is kept: "
    "\"you spent\" told as \"Alex found\", or \"less than\" told as \"lower "
    "than\" or \"under\", keeps the meaning. A question put with buttons is "
    "kept when its question and buttons ask the same thing. Think it through "
    "first if you need to, then end your reply with one JSON object on its "
    "own, and nothing after it: {\"items\": [{\"item\": str, \"kept\": "
    "bool}], \"faithful\": bool}, where faithful is true only when every "
    "item is kept."
)


def _last_verdict(raw: str) -> dict | None:
    """The last JSON object in *raw* that carries a verdict."""
    decoder = json.JSONDecoder()
    found = None
    for i, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(raw, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("faithful"), bool):
            found = obj
    return found


def _desk_words(answer: str) -> str:
    return answer.replace("<silent/>", "").strip()


async def judge(answer: str, reply: str, asked: list, cli: str | None) -> tuple[bool, str]:
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock,
    )
    shown = reply.strip()
    for a in asked:
        shown += (f"\n[question put with buttons] {a.get('question', '')} "
                  f"buttons: {json.dumps(a.get('options') or [], ensure_ascii=False)}")
    prompt = f"ANSWER:\n{_desk_words(answer)}\n\nREPLY:\n{shown}\n"
    kw = _sdk_kw(JUDGE_MODEL, cli)
    kw.update(system_prompt=JUDGE_SYSTEM, tools=[], allowed_tools=[],
              strict_mcp_config=True, setting_sources=[], skills=[],
              cwd=tempfile.mkdtemp(), max_turns=1)
    texts: list[str] = []
    async with ClaudeSDKClient(ClaudeAgentOptions(**kw)) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
    raw = "\n".join(texts)
    verdict = _last_verdict(raw)
    if verdict is None:
        return False, f"judge gave no verdict: {raw!r}"
    lost = [i.get("item") for i in verdict.get("items") or []
            if isinstance(i, dict) and not i.get("kept")]
    ok = verdict["faithful"] and not lost
    return ok, "faithful" if ok else f"not kept: {lost}"


# ---------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------

async def _turn(client, text: str) -> tuple[str, bool]:
    from claude_agent_sdk import AssistantMessage, TextBlock
    from datetime import datetime
    from timekeeping import compose_time_envelope, resolve_tz
    await client.query(compose_time_envelope(datetime.now(resolve_tz())) + text)
    texts: list[str] = []
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
    return "\n".join(texts), True


def _notice_body(case: dict) -> str:
    """The turn text Casa builds from the late completion, by the installed
    code — the eval measures the shipped template, never a copy."""
    from agent import Agent
    from bus import BusMessage, MessageType
    from specialist_registry import DelegationComplete
    complete = DelegationComplete(
        delegation_id=DELEGATION_ID, agent=case["agent"], status="ok",
        text=case["answer"], origin={"user_text": case["user"]}, elapsed_s=74.0)
    msg = BusMessage(type=MessageType.NOTIFICATION, source=case["agent"],
                     target="assistant", content=complete, channel="telegram",
                     context={"chat_id": 1})
    return str(Agent._synthesize_delegation_turn(None, msg).content)


def _delegate_reply(case: dict):
    import tools as casa_tools

    def reply(args: dict) -> str:
        if case.get("late"):
            if args.get("mode") == "async":
                return json.dumps({"status": "pending", "delegation_id": DELEGATION_ID,
                                   "agent": case["agent"], "mode": "async"})
            return json.dumps({"status": "pending", "delegation_id": DELEGATION_ID,
                               "agent": case["agent"], "timeout_s": 60,
                               "note": PENDING_NOTE})
        text = case["answer"]
        if case.get("silent"):
            text += "\n\n" + casa_tools.SILENT_CLOSE_NOTE.format(name=case["name"])
        return json.dumps({"status": "ok", "delegation_id": DELEGATION_ID,
                           "agent": case["agent"], "elapsed_s": 6.2, "text": text,
                           "output_truncated": False}, ensure_ascii=False)
    return reply


async def _one(case: dict, system_prompt: str, model: str, variant: str,
               cli: str | None) -> tuple[bool, str, list, str]:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server
    from output_boundary import strips_to_silence
    import tools as casa_tools
    captured: list = []
    schema = None
    if variant == "doctrine":
        schema = copy.deepcopy(casa_tools.delegate_to_agent.input_schema)
        schema["properties"]["mode"]["description"] = MODE_DESCRIPTION
    fakes = [
        _fake(casa_tools.delegate_to_agent, captured, _delegate_reply(case), schema),
        _fake(casa_tools.ask_user, captured, lambda _a: json.dumps({
            "status": "awaiting_user", "request_id": "5d1e0c7aeval",
            "note": casa_tools.ASK_USER_SILENCE_NOTE})),
        _fake(casa_tools.start_job, captured, lambda _a: '{"status": "pending"}'),
    ]
    server = create_sdk_mcp_server(name="casa-framework", tools=fakes)
    kw = _sdk_kw(model, cli)
    kw.update(system_prompt=system_prompt, mcp_servers={"casa-framework": server},
              tools=[], allowed_tools=[f"mcp__casa-framework__{f.name}" for f in fakes],
              strict_mcp_config=True, setting_sources=[], skills=[],
              cwd=tempfile.mkdtemp(), max_turns=4)
    async with ClaudeSDKClient(ClaudeAgentOptions(**kw)) as client:
        first, _ = await _turn(client, case["user"])
        if not any(n == "delegate_to_agent" for n, _ in captured):
            return False, "never delegated", captured, first
        said, answer_turn, start = first, first, len(captured)
        if case.get("late"):
            answer_turn, _ = await _turn(client, _notice_body(case))
            said = f"{first}\n{answer_turn}"
    ok, why = mechanical(case["name"], said)
    transcript = f"{first!r}" + (f" || {answer_turn!r}" if case.get("late") else "")
    if not ok:
        return False, why, captured, transcript
    if case.get("silent"):
        ok = strips_to_silence(said)
        return ok, "silent" if ok else "spoke", captured, transcript
    asked = [a for n, a in captured[start if case.get("late") else 0:] if n == "ask_user"]
    ok, why = await judge(case["answer"], answer_turn, asked, cli)
    return ok, why, captured, transcript


async def _judge_check(cli: str | None) -> bool:
    by_id = {c["id"]: c for c in CASES}
    all_ok = True
    for case_id, label, reply, expected in JUDGE_CHECK:
        ok, why = await judge(by_id[case_id]["answer"], reply, [], cli)
        right = ok is expected
        all_ok = all_ok and right
        print(f"  [judge-check {label}] expected={'accept' if expected else 'reject'} "
              f"got={'accept' if ok else 'reject'} {'OK' if right else 'WRONG'} ({why})")
    return all_ok


async def _run(args) -> bool:
    if IN_CONTAINER:
        _seed_container_env("CLAUDE_CODE_OAUTH_TOKEN")
    else:
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT",
                  "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(k, None)
    if args.judge_check:
        ok = await _judge_check(args.cli)
        print(f"judge-check: {'PASS' if ok else 'FAIL'}")
        return ok
    system_prompt, model = _system_prompt(args.variant)
    print(f"variant={args.variant} model={model!r} judge={JUDGE_MODEL!r} reps={args.reps}")
    all_ok = True
    for case in CASES:
        if args.case and case["id"] not in args.case:
            continue
        passed = 0
        for i in range(args.reps):
            try:
                ok, why, captured, reply = await _one(
                    case, system_prompt, model, args.variant, args.cli)
            except Exception as exc:  # noqa: BLE001 — one broken run is a failed run
                ok, why, captured, reply = False, f"error: {exc!r}", [], ""
            passed += ok
            print(f"  [{case['id']} #{i + 1}] {'PASS' if ok else 'FAIL'} ({why}) "
                  f"calls={[n for n, _ in captured]} "
                  f"asked={json.dumps([a for n, a in captured if n == 'ask_user'], ensure_ascii=False)} "
                  f"reply={reply}",
                  flush=True)
        print(f"{case['id']}: {passed}/{args.reps}", flush=True)
        all_ok = all_ok and passed == args.reps
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=("doctrine", "shipped"), default="doctrine")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--case", action="append", help="run only this case id (repeatable)")
    parser.add_argument("--cli", default=None, help="Claude Code CLI to drive the model with")
    parser.add_argument("--judge-check", action="store_true")
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
