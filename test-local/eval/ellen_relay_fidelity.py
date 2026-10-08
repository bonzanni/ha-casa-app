#!/usr/bin/env python3
"""Ellen relay-fidelity eval (#1332): a delegate's answer reaches the person
once, in the delegate's own words, not rewritten into a second version.

Live-model gate, the shape of ``ellen_delegation_fidelity.py``. It drives the
resident's REAL compiled text projection (``prompt_compiler.projection_for``,
loaded with ``binding_commit=False`` so nothing under /config or /data is
written) plus a ``<delegates>``/``<jobs>`` block of the shape
``agent._render_prompt_surface`` renders, through ONE turn per case.
``delegate_to_agent``, ``ask_user`` and ``start_job`` are side-effect-free
FAKES carrying the PRODUCTION descriptions and input schemas from
``tools.py``; the fake ``delegate_to_agent`` answers with the case's desk text
in the production sync result shape. Nothing is started, asked or posted.

A run passes when the resident's reply is exactly the case's expected
answer — the delegate's text attributed "Alex: ", whitespace collapsed — or,
for a question the delegate asks Ellen to put to the person, when the reply is
the delegate's answer attributed and verbatim (its closing full stop may become
a dash or a comma joining the question), then that one question in one of its
faithful forms (``QUESTION``, full match) and nothing more. A ``silent`` case passes only
when the reply strips to silence.

``--variant baseline`` serves the projection as compiled. ``--variant doctrine``
serves the same projection with ``DOCTRINE_PARAGRAPH`` inserted after the
"When a request falls within …" paragraph, where the change puts it, so a
deployed image need not carry the change to measure it.

Run inside the deployed container, one-shot, writing only under /tmp:

    cat test-local/eval/ellen_relay_fidelity.py \\
        | ssh <host> -- sudo -n docker exec -i <container> python3 - --variant doctrine --runs 3

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

from agent_loader import load_agent_from_dir  # noqa: E402
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
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
    "When a delegate's result answers the person, that answer is your whole "
    "reply: pass it on in the delegate's own words, introduced only by its "
    "name (\"Alex: …\") so that its \"I\" and \"me\" stay the delegate's. "
    "Write nothing of your own around it: do not announce that you are asking "
    "the delegate, and add no rewrite or restatement, no question, offer or "
    "next step it did not make, no note on what you will do. A line meant for "
    "you rather than the person is not passed on, except a question or request "
    "it asks you to put to them: put that to them. Your other rules on what a "
    "reply may hold still apply to what you pass on."
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

# The desk texts are the two the operator saw rewritten on 2026-10-08 (#1332),
# plus one plain data answer: each has exactly one right reply, the text
# attributed to Alex. Two cases carry a line meant for Ellen: one only for her
# (not passed on), one asking her to put a question to the person (passed on).
Q1 = ("Q1 2026 isn't in the books yet: they start 1 Apr 2026. Ask me to do Q1 and "
      "I'll start the books from 1 Jan.")
Q3 = "Checking the bank and your email — I'll post the result here."
DATA = "September groceries came to €412.30 over 9 payments, most of it at Esselunga."
# The faithful forms of the one question Alex asks Ellen to put to the person:
# which of the two invoices the payment is for, the two names as choices.
QUESTION = re.compile(
    r"(?:which (?:one|invoice) (?:does it pay|does the payment pay|is it(?: for)?|"
    r"was it(?: for)?|does it cover|should it be matched to)"
    r"|please tell me which (?:one|invoice) it pays)"
    r"[:,]? (?:the )?snelstart(?: one| invoice)? or (?:the )?moneybird(?: one| invoice)?[.?]",
    re.IGNORECASE)
CASES = [
    {"id": "q1-not-in-books", "user": "do Q1 2026", "desk": Q1,
     "expect": [f"Alex: {Q1}", f'Alex: "{Q1}"']},
    {"id": "q3-started", "user": "do Q3", "desk": Q3,
     "expect": [f"Alex: {Q3}", f'Alex: "{Q3}"']},
    {"id": "data-answer", "user": "how much did we spend on groceries in September?",
     "desk": DATA, "expect": [f"Alex: {DATA}", f'Alex: "{DATA}"']},
    {"id": "line-for-ellen", "user": "how much did we spend on groceries in September?",
     "desk": DATA + "\n\nEllen: nothing for you to log from this one.",
     "expect": [f"Alex: {DATA}", f'Alex: "{DATA}"']},
    {"id": "question-via-ellen", "user": "match the €120 payment from 3 September",
     "desk": ("Two invoices fit the €120 payment of 3 September. Ellen, please ask "
              "them which one it pays: Snelstart or Moneybird."),
     "prefix": "Alex: Two invoices fit the €120 payment of 3 September",
     "question": QUESTION},
    # The rule must not override the already-posted rule: a result that is
    # only Casa's post lines (as ``tools._with_post_echo`` appends them) stays
    # silent.
    {"id": "posted-card-silent", "user": "check Q2", "silent": True,
     "desk": ("\n\n📊 Alex posted a proposal to your chat (2 buttons).\n"
              + casa_tools.POST_ECHO_SILENCE_NOTE)},
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


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'")).strip()


def _judge(case: dict, reply: str) -> tuple[bool, str]:
    r = _norm(reply)
    if "expect" in case:
        ok = r in {_norm(e) for e in case["expect"]}
        return ok, "exact" if ok else "not the attributed answer"
    prefix = _norm(case["prefix"])
    if not r.startswith(prefix):
        return False, "answer not passed on verbatim"
    rest = r[len(prefix):].lstrip()
    if not re.match(r"[.:;,—–-]", rest):
        return False, "answer not passed on verbatim"
    rest = rest[1:].strip()
    if not case["question"].fullmatch(rest):
        return False, "not the delegate's question alone"
    return True, "question reached the person"


async def _one(case: dict, system_prompt: str, model: str) -> tuple[bool, str, list, str]:
    captured: list = []
    result = json.dumps({"status": "ok", "delegation_id": "0f3c9a2e-eval",
                         "agent": "finance", "elapsed_s": 6.2, "text": case["desk"],
                         "output_truncated": False}, ensure_ascii=False)
    fakes = [
        _fake(casa_tools.delegate_to_agent, captured, result),
        _fake(casa_tools.ask_user, captured, '{"status": "pending"}'),
        _fake(casa_tools.start_job, captured, '{"status": "pending"}'),
    ]
    server = create_sdk_mcp_server(name="casa-framework", tools=fakes)
    opts = ClaudeAgentOptions(
        model=model, system_prompt=system_prompt,
        mcp_servers={"casa-framework": server}, tools=[],
        allowed_tools=[f"mcp__casa-framework__{f.name}" for f in fakes],
        strict_mcp_config=True, setting_sources=[], skills=[],
        cwd=tempfile.mkdtemp(), max_turns=4,
    )
    from timekeeping import compose_time_envelope, resolve_tz
    from datetime import datetime
    query = compose_time_envelope(datetime.now(resolve_tz())) + case["user"]
    texts: list[str] = []
    async with ClaudeSDKClient(opts) as client:
        await client.query(query)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
    reply = "\n".join(texts)
    delegated = any(name == "delegate_to_agent" for name, _ in captured)
    if not delegated:
        return False, "never delegated", captured, reply
    if case.get("silent"):
        from output_boundary import strips_to_silence
        ok = strips_to_silence(reply)
        return ok, "silent" if ok else "spoke", captured, reply
    ok, why = _judge(case, reply)
    return ok, why, captured, reply


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
                  f"reply={reply!r}")
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
