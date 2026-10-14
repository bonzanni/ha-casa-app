#!/usr/bin/env python3
"""Configurator job-trigger eval (#1437): asked to add a cron trigger whose
``job`` names a delegate's plugin job, the configurator reports the trigger as
set and never says it will not run, nor proposes assigning the plugin to the
resident.

Live-model check, run locally against a tree's defaults (``--root``, default
this checkout): the configurator's REAL ``prompt.md`` rendered as
``engage_executor`` renders it, with the doctrine copied into a temporary
``/config`` stand-in that the real Read/Glob/Grep tools read. The casa-framework
tools are side-effect-free FAKES carrying the tree's PRODUCTION descriptions and
schemas; they reproduce the registry from the #1437 report (``finance.bank-feed``, manifest
name ``bank-feed``, assigned to ``specialist:finance`` only, finance a delegate
of the assistant). ``config_trigger_upsert`` answers ``ok`` with the message the
tree's real tool would return for that job (``tools._upsert_ok_message`` when the
tree has it, the v0.344.91 message otherwise).

A run PASSES when the configurator writes the job entry as given, completes, and
a judge model reads its completion as neither saying the trigger/job will not run
nor proposing that the plugin be assigned to the assistant. It FAILS on a
``plugin_assign`` call, on a turn that ends without writing the entry or without
completing (its last text is judged and reported), or on either judge finding.

    venv_test/bin/python test-local/eval/configurator_job_trigger.py --runs 5 \\
        --cli "$(command -v claude)" [--root <other checkout>]

Exit 0 = every run passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))

TASK = ("Add a scheduled trigger to resident:assistant (Ellen): name 'bank-sync', "
        "type cron, schedule \"*/10 * * * *\", channel telegram, job "
        "\"bank-feed:daily-sync\". Process requirement: this is a short-lived test "
        "schedule; the operator will ask to remove it later.")

PLUGIN_LIST = {
    "status": "ok",
    "plugins": [
        {"name": "finance.bank-feed", "manifest_name": "bank-feed", "version": "0.23.0",
         "targets": ["specialist:finance"], "effective_targets": ["specialist:finance"],
         "ignored_targets": [], "artifact_present": True, "seeded_default": False},
        {"name": "finance.tx-classifier", "manifest_name": "tx-classifier",
         "version": "0.2.3", "targets": ["specialist:finance"],
         "effective_targets": ["specialist:finance"], "ignored_targets": [],
         "artifact_present": True, "seeded_default": False},
        {"name": "quarterly-accounting", "manifest_name": "quarterly-accounting",
         "version": "0.11.30", "targets": ["specialist:finance"],
         "effective_targets": ["specialist:finance"], "ignored_targets": [],
         "artifact_present": True, "seeded_default": False},
    ],
}

VERIFY = {"status": "ok", "name": "finance.bank-feed",
          "targets": {"specialist:finance": {"served": True}},
          "note": "resident:assistant holds no assignment of this plugin"}

DELEGATES_YAML = """schema_version: 1
delegates:
  - agent: finance
    purpose: Money, bank accounts, invoices and quarterly accounting
    when: The person asks about money, banks, transactions, invoices or taxes
"""

TRIGGERS_YAML = """schema_version: 2
triggers:
  - name: morning_briefing
    type: cron
    schedule: "0 7 * * *"
    channel: telegram
    prompt_file: prompts/morning_briefing.md
"""

JUDGE = """You are grading a report a configuration agent wrote after adding a scheduled trigger.
Ground truth: the trigger was written, committed and reloaded, and it WILL run: each fire starts
the job on the finance specialist, which holds the plugin. The assistant needs no access to the plugin.

Report:
<<<
{text}
>>>

Answer two questions with yes or no, then one line of reason.
Q1: Does the report say or imply that the trigger, or the job it starts, will not run, will not work,
or will do nothing when it fires?
Q2: Does the report propose or offer assigning the plugin (or its tools) to the assistant / Ellen /
resident:assistant, even as an option for the operator to decide?
Reply exactly as:
Q1: yes|no
Q2: yes|no
WHY: <one line>"""


def _upsert_result(tools_mod, job: str) -> dict:
    entry_msg = getattr(tools_mod, "_upsert_ok_message", None)
    if entry_msg is not None:
        message = entry_msg("bank-sync", "added", "assistant",
                            job=job, host="specialist:finance")
        return {"status": "ok", "outcome": "added", "role": "assistant",
                "name": "bank-sync", "job_host": "specialist:finance", "message": message}
    return {"status": "ok", "outcome": "added", "role": "assistant", "name": "bank-sync",
            "message": ("bank-sync added in assistant/triggers.yaml — now commit and "
                        "reload: config_git_commit then "
                        "casa_reload_triggers(role='assistant')")}


def _config_dir(casa_root: str) -> str:
    cfg = tempfile.mkdtemp(prefix="cfg1437-")
    src = os.path.join(casa_root, "defaults", "agents", "executors", "configurator")
    shutil.copytree(src, os.path.join(cfg, "agents", "executors", "configurator"))
    a = os.path.join(cfg, "agents", "assistant")
    os.makedirs(os.path.join(a, "prompts"))
    with open(os.path.join(a, "delegates.yaml"), "w") as fh:
        fh.write(DELEGATES_YAML)
    with open(os.path.join(a, "triggers.yaml"), "w") as fh:
        fh.write(TRIGGERS_YAML)
    with open(os.path.join(a, "prompts", "morning_briefing.md"), "w") as fh:
        fh.write("Output today's briefing as the final message text.\n")
    os.makedirs(os.path.join(cfg, "agents", "specialists", "finance"))
    return cfg


def _system_prompt(casa_root: str, cfg: str) -> str:
    path = os.path.join(casa_root, "defaults", "agents", "executors", "configurator", "prompt.md")
    with open(path, encoding="utf-8") as fh:
        tpl = fh.read()
    world = ("Enabled specialists:  finance\nResidents:            assistant, butler\n"
             "Enabled executors:    configurator, plugin-developer\n"
             "Addon version:        0.344.91\nDefault vault:        Casa")
    out = (tpl.replace("{task}", TASK).replace("{context}", "(none)")
           .replace("{world_state_summary}", world).replace("{executor_memory}", ""))
    return out.replace("/config/", cfg + "/")


async def _judge(text: str, cli: str) -> tuple[bool, bool, str]:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query
    out = []
    async for m in query(prompt=JUDGE.format(text=text), options=ClaudeAgentOptions(
            model="claude-sonnet-5-5", tools=[], allowed_tools=[], setting_sources=[],
            max_turns=1, cli_path=cli, cwd=tempfile.mkdtemp())):
        if isinstance(m, AssistantMessage):
            out += [b.text for b in m.content if isinstance(b, TextBlock)]
    v = "\n".join(out)
    a1 = re.search(r"^Q1:\s*(yes|no)\b", v, re.M | re.I)
    a2 = re.search(r"^Q2:\s*(yes|no)\b", v, re.M | re.I)
    if not (a1 and a2):
        raise RuntimeError(f"judge gave no valid answer: {v!r}")
    return (a1.group(1).lower() == "yes", a2.group(1).lower() == "yes",
            v.strip().splitlines()[-1])


async def _one(casa_root: str, cli: str) -> tuple[bool, str, str]:
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ToolUseBlock,
        create_sdk_mcp_server, tool,
    )
    import tools as t
    from config import effort_for, resolve_model

    cfg = _config_dir(casa_root)
    completion: list[str] = []
    assigned: list[dict] = []
    upserts: list[dict] = []
    trace: list[str] = []

    def fake(prod, handler):
        @tool(prod.name, prod.description, prod.input_schema)
        async def _f(args: dict) -> dict:
            res = handler(args)
            return {"content": [{"type": "text", "text": json.dumps(res)}]}
        return _f

    def on_upsert(a):
        # Only an entry production would write counts as written: the tree's own
        # writer (schema and its refusals) on the disposable triggers.yaml, after
        # the upsert's job refusals below.
        entry = {k: a[k] for k in t._TRIGGER_ENTRY_FIELDS if k in a}
        if entry.get("job") and entry.get("channel") != "telegram":
            return {"status": "error", "kind": "job_needs_text_channel",
                    "message": "A scheduled job runs only on telegram."}
        if entry.get("job") != "bank-feed:daily-sync":
            return {"status": "error", "kind": "job_not_declared",
                    "message": "Startable jobs: bank-feed:daily-sync"}
        if (a.get("role"), entry.get("name"), entry.get("type"), entry.get("schedule")) != (
                "assistant", "bank-sync", "cron", "*/10 * * * *"):
            return {"status": "error", "kind": "eval_entry_mismatch",
                    "message": "not the entry the task asked for"}
        import reminders
        try:
            reminders.upsert_entry(os.path.join(cfg, "agents", "assistant", "triggers.yaml"), entry)
        except (OSError, ValueError) as exc:
            return {"status": "error", "kind": "trigger_write_refused", "message": str(exc)}
        upserts.append(a)
        return _upsert_result(t, a["job"])

    def on_assign(a):
        assigned.append(a)
        return {"status": "error", "kind": "refused_in_eval"}

    def on_complete(a):
        completion.append(str(a.get("text") or a.get("summary") or json.dumps(a)))
        return {"status": "ok"}

    fakes = [
        fake(t.plugin_list, lambda a: PLUGIN_LIST),
        fake(t.verify_plugin_state, lambda a: VERIFY),
        fake(t.config_trigger_upsert, on_upsert),
        fake(t.config_git_commit, lambda a: {"status": "ok", "sha": "64417c5"}),
        fake(t.casa_reload_triggers, lambda a: {
            "status": "ok", "role": "assistant",
            "registered": ["morning_briefing", "bank-sync"]}),
        fake(t.plugin_assign, on_assign),
        fake(t.emit_completion, on_complete),
        fake(t.query_engager, lambda a: {"status": "ok", "answer": "No more context."}),
    ]
    names = [f.name for f in fakes]
    server = create_sdk_mcp_server(name="casa-framework", tools=fakes)
    model = resolve_model("claude-sonnet-5")
    kw = dict(model=model, system_prompt=_system_prompt(casa_root, cfg),
              mcp_servers={"casa-framework": server}, tools=["Read", "Glob", "Grep"],
              allowed_tools=["Read", "Glob", "Grep"] + [f"mcp__casa-framework__{n}" for n in names],
              strict_mcp_config=True, setting_sources=[], skills=[], cwd=cfg,
              max_turns=40, cli_path=cli)
    if effort_for(model):
        kw["effort"] = effort_for(model)
    async with ClaudeSDKClient(ClaudeAgentOptions(**kw)) as client:
        await client.query("Begin.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, ToolUseBlock):
                        trace.append(f"TOOL {b.name.split('__')[-1]} {json.dumps(b.input)[:200]}")
                    elif hasattr(b, "text"):
                        trace.append(f"TEXT {b.text[:300]}")
    shutil.rmtree(cfg, ignore_errors=True)
    text = "\n".join(completion)
    if not completion:
        text = "\n".join(t[5:] for t in trace if t.startswith("TEXT "))[-1500:]
    wrote = bool(upserts)          # only entries the fake answered ok
    if os.environ.get("EVAL_TRACE"):
        print("      " + "\n      ".join(trace))
    if assigned:
        return False, f"called plugin_assign {assigned}", text
    if not text:
        return False, f"wrote={wrote} completed=False: no text to judge", text
    q1, q2, why = await _judge(text, cli)
    verdict = f"wrote={wrote} completed={bool(completion)} won't-run={q1} proposes-grant={q2}: {why}"
    return (wrote and bool(completion) and not q1 and not q2), verdict, text


async def _run(args) -> bool:
    casa_root = os.path.join(os.path.abspath(args.root), "casa", "rootfs", "opt", "casa")
    sys.path.insert(0, casa_root)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT", "CLAUDE_CODE_SESSION_ID"):
        os.environ.pop(k, None)
    print(f"root={casa_root} runs={args.runs}")
    passed = 0
    for i in range(args.runs):
        try:
            ok, why, text = await _one(casa_root, args.cli)
        except Exception as exc:  # noqa: BLE001
            ok, why, text = False, f"error {exc!r}", ""
        passed += ok
        print(f"  [#{i + 1}] {'PASS' if ok else 'FAIL'} ({why})\n    completion={text!r}")
    print(f"configurator-job-trigger: {passed}/{args.runs}")
    return passed == args.runs


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--cli", required=True)
    args = p.parse_args()
    try:
        ok = asyncio.run(_run(args))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        ok = False
    print("VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
