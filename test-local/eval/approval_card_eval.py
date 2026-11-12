#!/usr/bin/env python3
"""Approval card eval: how well the model-written card carries what a request
does, measured on real tools and Casa's own card builders.

Every case is built by the builder production uses: a protected tool call by
``ChallengeCoordinator._tool_card_request`` (with the tool's real MCP
description and input schema placed where the background listing puts them),
each consent by its module's ``*_card_request``, a specialist install or
update from a real specialist checkout inspected the way the install tool
inspects it. The card is then written by the real ``approval_card.write_card``
against a real Claude Code CLI, once per sample:

  * the module cache is cleared before every sample, and ``_ask`` is wrapped
    to count its calls per sample (a context variable, so concurrent samples
    never share a count). A sample whose count did not rise was served from
    somewhere other than the model and is not a measurement: the run stops.
  * a card that comes back as the request's fallback is recorded as a
    fallback (a failure of the model path) and is not judged.

Configurations (``--configs``): ``sonnet-default`` (the module's model at the
effort Casa passes for it), ``sonnet-low`` (same model, effort forced to
``low`` by replacing ``approval_card.effort_for`` inside this process) and
``haiku`` (a reference, at its own effort). Only module globals are swapped,
never product code on disk.

Each written card is judged by ONE separate Opus call, run the same contained
way (no tools, no session, no settings), given the request facts the writer
saw, the card text and the case's hand-written material facts. It returns, per
fact, present / missing / wrong; the warning line right / wrong / missing /
none_needed; the scope exact / overstated / softened; a one-line note.
Code-like tokens in the card (snake_case, ALL_CAPS_WITH_UNDERSCORES, ``mcp__``,
paths, HTTP methods, placeholders, markdown) are counted mechanically. Latency
is wall clock around ``write_card`` (CLI start included); length is the
rendered card in UTF-16 units.

The write budget defaults to 60 s rather than the module's 15 s so the full
latency is visible; the summary also reports the share of cards slower than
the module's budget, which production would have replaced with the fallback.

Run from a checkout, against its tree and a logged-in CLI (subscription; an
``ANTHROPIC_API_KEY`` in the environment is refused):

    venv_test/bin/python test-local/eval/approval_card_eval.py \\
        --out /tmp/approval-card-eval --cli "$(command -v claude)" \\
        --finance-repo ../casa-specialist-finance

``--check`` builds every request and prints its fallback and fact size
without calling a model. ``--cases-dir`` adds case directories (repeatable).
Exit 0 when every sample ran; 2 when a sample was not a model measurement.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import dataclasses
import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
CODE_ROOT = HERE.parent.parent / "casa" / "rootfs" / "opt" / "casa"
DEFAULT_CASES = HERE / "approval_card_cases"
JUDGE_MODEL = "claude-opus-5-5"
CONFIGS = {
    "sonnet-default": {"model": "sonnet", "effort": None},
    "sonnet-low": {"model": "sonnet", "effort": "low"},
    "haiku": {"model": "haiku", "effort": None},
}

# The child CLI must see what production's sees: a session-scoped variable of
# the shell this runs from (an outer Claude Code session, say) is dropped.
_SCRUB_PREFIXES = ("CLAUDE_CODE_",)
_SCRUB_NAMES = ("CLAUDECODE", "CLAUDE_EFFORT", "CLAUDE_PID", "CLAUDE_PLUGIN_DATA",
                "CLAUDE_PLUGIN_ROOT")


def _scrub_env() -> None:
    for name in list(os.environ):
        if name.startswith(_SCRUB_PREFIXES) or name in _SCRUB_NAMES:
            del os.environ[name]


# --- cases -----------------------------------------------------------------------

def load_cases(dirs: list[Path]) -> tuple[list[dict], dict]:
    """(cases, tool definitions by plugin) from every *.json in *dirs*."""
    cases: list[dict] = []
    tools: dict = {}
    for d in dirs:
        for path in sorted(d.glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            if "tools" in doc:
                tools[doc["plugin"]] = doc
            for case in doc.get("cases", []):
                cases.append({**case, "_file": path.name})
    ids = [c["id"] for c in cases]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise SystemExit(f"duplicate case ids: {sorted(dup)}")
    return cases, tools


def _validate_args(args: dict, schema: dict) -> list[str]:
    """Problems of *args* against the parts of JSON Schema the cases use."""
    problems = []
    props = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if name not in args:
            problems.append(f"missing required {name}")
    for name, value in args.items():
        spec = props.get(name)
        if spec is None:
            if schema.get("additionalProperties") is False or props:
                problems.append(f"unknown argument {name}")
            continue
        types = [s.get("type") for s in spec.get("anyOf", [])] or [spec.get("type")]
        pytype = {"string": str, "boolean": bool, "array": list, "object": dict,
                  "integer": int, "number": (int, float), "null": type(None)}
        if not any(isinstance(value, pytype[t]) for t in types if t in pytype):
            problems.append(f"{name}: {type(value).__name__} is not {types}")
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"{name}: {value!r} not in {spec['enum']}")
        if "pattern" in spec and isinstance(value, str) and not re.search(spec["pattern"], value):
            problems.append(f"{name}: {value!r} does not match {spec['pattern']}")
    return problems


class Builders:
    """Casa's own card builders, fed from the cases."""

    def __init__(self, tool_defs: dict, finance_repo: "Path | None", workdir: Path):
        self.tool_defs = tool_defs
        self.finance_repo = finance_repo
        self.workdir = workdir
        self._inspected = None

    def build(self, case: dict):
        kind = case["type"]
        return getattr(self, f"_{kind}")(case)

    def _tool(self, case: dict):
        import tool_descriptions
        from authz_grants import ChallengeCoordinator, GrantKey, canonical_args_json
        from text_util import sanitize_segment
        plugin_doc = self.tool_defs[case["plugin"]]
        tool = case["tool"]
        spec = plugin_doc["tools"][tool]
        problems = _validate_args(case["arguments"], spec["inputSchema"])
        if problems:
            raise SystemExit(f"{case['id']}: arguments invalid against the tool's schema: "
                             f"{problems}")
        plugin = case["plugin"]
        artifact_id = "eval-" + hashlib.sha256(plugin.encode()).hexdigest()[:12]
        # Where the background listing leaves a protected tool's description.
        tool_descriptions._cache[(artifact_id, tool)] = {
            "description": spec["description"], "inputSchema": spec["inputSchema"]}
        canonical = canonical_args_json(case["arguments"])
        full = (f"mcp__plugin_{sanitize_segment(plugin)}_"
                f"{sanitize_segment(plugin_doc['server'])}__{sanitize_segment(tool)}")
        key = GrantKey(operator_id=1, bot_id=2, chat_id=1, enforcement_role=case["role"],
                       artifact_id=artifact_id, tool_name=full,
                       args_hash=hashlib.sha256(canonical.encode()).hexdigest())
        return ChallengeCoordinator._tool_card_request(
            key, tool_name=full, canonical_json=canonical,
            enforcement_role=case["role"], summary=spec.get("summary"),
            display_name=case["who"], plugin=plugin, declared_tool=tool)

    def _trigger(self, case: dict):
        import plugin_triggers
        import trigger_consent
        i = case["inputs"]
        target = i["target"]
        return trigger_consent.trigger_card_request(
            plugin=i["plugin"], effective=i["effective"],
            role=target.partition(":")[2] or target, auth=i["auth"],
            clearance=i.get("clearance", "public"), deliver=i.get("deliver", "none"),
            max_body_kib=i.get("max_body_kib", plugin_triggers.MAX_BODY_KIB_DEFAULT))

    def _callback(self, case: dict):
        import callback_consent
        i = case["inputs"]
        return callback_consent.callback_card_request(plugin=i["plugin"], effective=i["effective"])

    def _event(self, case: dict):
        import event_consent
        i = case["inputs"]
        return event_consent.event_card_request(
            subscriber=i["subscriber"], emitter=i["emitter"], event=i["event"],
            targets=list(i["targets"]))

    def _persona(self, case: dict):
        import persona_install_consent
        i = case["inputs"]
        inspection = SimpleNamespace(persona_id=i["persona_id"], version=i["version"],
                                     display_name=i["display_name"], checksum=i["checksum"])
        return persona_install_consent.persona_card_request(inspection,
                                                            requester_name=case["who"])

    def _erase(self, case: dict):
        """The erase question as the uninstall tool composes it: an erase
        choice only when every eraser offers it, each eraser with the summary
        its plugin's manifest declares for that tool."""
        import plugin_erase_consent as pec
        i = case["inputs"]
        specs = i["erasers"]

        def summary(plugin: str, tool: str):
            return (self.tool_defs.get(plugin, {}).get("tools", {}).get(tool) or {}).get("summary")
        offers = {pec.ERASE_DATA_ONLY: lambda s: bool(s.get("data_tool")),
                  pec.ERASE: lambda s: bool(s.get("tool"))}
        choices = tuple(c for c in pec.DEFAULT_ORDER
                        if c not in offers or all(offers[c](s) for s in specs))
        erasers = []
        for s in specs:
            if pec.ERASE_DATA_ONLY in choices:
                erasers.append((s["plugin"], s["data_tool"], summary(s["plugin"], s["data_tool"])))
            if pec.ERASE in choices:
                erasers.append((s["plugin"], s["tool"], summary(s["plugin"], s["tool"])))
        return pec.erase_card_request(i["what"], erasers, choices, requester_name=case["who"])

    def _inspect_specialist(self):
        """A real inspection of the specialist checkout, as the install tool
        runs it, with the fetch replaced by a copy of the checkout's HEAD."""
        if self._inspected is not None:
            return self._inspected
        import specialist_install
        import tools
        from specialist_registry import InstalledSpecialistIndex
        tmp = self.workdir / "specialist"
        src = tmp / "src"
        src.mkdir(parents=True)
        subprocess.run(f"git -C '{self.finance_repo}' archive HEAD | tar -x -C '{src}'",
                       shell=True, check=True)

        def _fetch(repo, ref, subdir, dest, *, expected_revision=None):
            shutil.copytree(src, dest)
            return "0" * 40
        specialist_install.resolve_and_fetch = _fetch
        index = InstalledSpecialistIndex(specialists_dir=str(tmp / "specialists"))
        index.load()
        result = specialist_install.inspect_specialist_repo(
            "example/specialist", "eval", staging_root=tmp / "staging",
            installed_index=index, receipts_dir=tmp / "receipts",
            specialists_dir=tmp / "specialists")
        checked = specialist_install.validate_resume_inputs(
            staged_dir=result.staged_dir, receipt_id=result.receipt_id,
            root_digest=result.root_digest, expect_slug=result.slug,
            receipts_dir=tmp / "receipts")
        if not checked.ok:
            raise SystemExit(f"specialist checkout did not validate: {checked.kind}")
        tools._SPECIALISTS_DIR = tmp / "specialists"
        staged = Path(result.staged_dir)
        inspection = tools._inspection_from_checked(checked, staged)
        card_facts = tools._install_card_facts(checked, staged, slug=result.slug,
                                               mode="install")
        self._inspected = (inspection, card_facts, tmp)
        return self._inspected

    def _specialist(self, case: dict):
        if self.finance_repo is None:
            return None
        import specialist_install
        from specialist_install_consent import install_card_request
        inspection, card_facts, tmp = self._inspect_specialist()
        i = case["inputs"]
        mode = i["mode"]
        secrets = sorted(i.get("secret_names_provided") or [])
        config = dict(i.get("config") or {})
        # Nothing is installed in the scratch tree, so an update's settings
        # are previewed as an install's; the installed version an update
        # replaces is the case's own.
        effective = specialist_install.preview_effective_config(
            "specialist_install", inspection, slug=inspection.slug, config=config,
            secret_names_provided=secrets, specialists_dir=tmp / "specialists")
        facts = dict(card_facts)
        if mode == "upgrade" and i.get("from_version"):
            facts["from_version"] = i["from_version"]
        return install_card_request(
            inspection, mode=mode, values={"config": effective, "secrets": secrets},
            submitted_config=config, requester_name=case["who"], **facts)


# --- measurement ------------------------------------------------------------------

_SAMPLE: contextvars.ContextVar = contextvars.ContextVar("approval_card_sample", default=None)


def install_counter(approval_card) -> None:
    """Wrap ``_ask`` so each sample counts its own model calls and keeps the
    raw replies."""
    original = approval_card._ask

    async def counted(system: str, user: str) -> str:
        box = _SAMPLE.get()
        if box is not None:
            box["asks"] += 1
        reply = await original(system, user)
        if box is not None:
            box["replies"].append(reply)
        return reply
    approval_card._ask = counted


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"https?://\S+")
_FILE = re.compile(r"(?<![/\w])[\w.-]+\.(?:pdf|png|jpe?g|csv|xlsx?|zip|docx?|txt)\b", re.I)
_LEAKS = [
    ("snake_case", re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")),
    ("ALL_CAPS", re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")),
    ("mcp", re.compile(r"mcp__\S*")),
    ("path", re.compile(r"(?<![\w.:/])/[\w.-]+/[\w./-]*")),
    ("http_method", re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)\b")),
    ("placeholder", re.compile(r"\$\{[^}]*\}|\{[a-z_]+\}")),
    ("markdown", re.compile(r"\*\*|\]\(|`|^#", re.M)),
]


def code_tokens(text: str) -> list[str]:
    """Code-like tokens in a card. Email addresses, web addresses and file
    names are human data and are not counted (a web address is still seen
    by the markdown pattern when it sits in link syntax)."""
    found = [m.group(0) for m in _LEAKS[-1][1].finditer(text)]
    rest = _FILE.sub(" ", _URL.sub(" ", _EMAIL.sub(" ", text)))
    for _name, pattern in _LEAKS[:-1]:
        found += [m.group(0) for m in pattern.finditer(rest)]
    return found


JUDGE_SYSTEM = """\
You grade an approval card: the short text a person reads on a phone before tapping Approve or Deny on a request from their home assistant. The card was written by another model from REQUEST FACTS. You get the facts, the CARD exactly as shown (a line starting with "⚠️" is the one line flagged as needing attention), the MATERIAL FACTS a correct card must carry, and the WARNING EXPECTATION.

For each material fact, in order:
- "present": the card states it, in plain words or verbatim, accurately. Grouping is fine (e.g. "2 bank keys" for two named keys) unless the fact asks for something verbatim (addresses, amounts, dates, names with versions): then it must appear exactly or unambiguously.
- "missing": the card does not state it anywhere (title, summary or lines).
- "wrong": the card states it incorrectly, misleadingly, or contradicts it; also when the fact says something must NOT be claimed and the card claims it.

Warning ("warning"), given the expectation:
- expected "yes": "right" if the flagged line is about what the expectation names (or an equivalent), "wrong" if a line is flagged but about something else, "missing" if nothing is flagged.
- expected "no": "none_needed" if nothing is flagged, "wrong" if anything is.
- expected "optional": "none_needed" if nothing is flagged, "right" if the flagged line is about what the expectation names or something equally unexpected or risky in the request, "wrong" if it flags the core of the ask or something unremarkable.

Scope ("scope"): "overstated" if the card claims more power, reach or effect than the request has; "softened" if it makes the request sound smaller, safer, reversible or more limited than it is (e.g. leaves out irreversibility, says "archive" for "delete", calls the operator the only recipient when there are more); otherwise "exact". If both apply, answer "softened".

Reply with JSON only:
{"facts": [{"n": 1, "verdict": "present|missing|wrong", "why": "<few words>"}], "warning": "right|wrong|missing|none_needed", "scope": "exact|overstated|softened", "note": "<one line: the most important problem, or 'good'>"}"""

_FACT_VERDICTS = {"present", "missing", "wrong"}
_WARNING_VERDICTS = {"right", "wrong", "missing", "none_needed"}
_SCOPE_VERDICTS = {"exact", "overstated", "softened"}


async def _contained_query(model: str, effort: "str | None", system: str, user: str,
                           cli: str) -> str:
    """One query run the way ``approval_card._ask`` runs one: no tools, no
    session, no settings."""
    import claude_agent_sdk as sdk
    from claude_runtime import (SDK_MAX_BUFFER_SIZE, cli_session_settings,
                                ephemeral_extra_args, ephemeral_setting_sources,
                                with_cross_session_tools_denied)
    opts = sdk.ClaudeAgentOptions(
        model=model, effort=effort, cli_path=cli, max_buffer_size=SDK_MAX_BUFFER_SIZE,
        system_prompt=system, max_turns=8, tools=[], allowed_tools=[],
        disallowed_tools=with_cross_session_tools_denied(["Bash", "Task", "Agent"]),
        settings=cli_session_settings(), setting_sources=ephemeral_setting_sources(),
        extra_args=ephemeral_extra_args(), permission_mode="acceptEdits")
    reply = ""
    async for msg in sdk.query(prompt=user, options=opts):
        if isinstance(msg, sdk.AssistantMessage):
            for block in getattr(msg, "content", []) or []:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    reply += text
    return reply


def _parse_judgement(reply: str, n_facts: int) -> "dict | None":
    start, end = reply.find("{"), reply.rfind("}")
    if not 0 <= start < end:
        return None
    try:
        data = json.loads(reply[start:end + 1])
    except ValueError:
        return None
    facts = data.get("facts")
    if (not isinstance(facts, list) or len(facts) != n_facts
            or any(not isinstance(f, dict) or f.get("verdict") not in _FACT_VERDICTS
                   for f in facts)
            or data.get("warning") not in _WARNING_VERDICTS
            or data.get("scope") not in _SCOPE_VERDICTS):
        return None
    return {"facts": [{"verdict": f["verdict"], "why": str(f.get("why", ""))} for f in facts],
            "warning": data["warning"], "scope": data["scope"],
            "note": str(data.get("note", ""))}


async def judge(case: dict, facts_text: str, card_text: str, cli: str) -> dict:
    from config import effort_for
    material = "\n".join(f"{n}. {f}" for n, f in enumerate(case["material_facts"], 1))
    w = case["warning"]
    user = (f"REQUEST FACTS:\n{facts_text}\n\nCARD:\n{card_text}\n\n"
            f"MATERIAL FACTS:\n{material}\n\n"
            f"WARNING EXPECTATION: {w['expected']}" + (f" -- on: {w['on']}" if w.get("on") else ""))
    last = ""
    for _ in range(2):
        async with asyncio.timeout(240):
            last = await _contained_query(JUDGE_MODEL, effort_for(JUDGE_MODEL),
                                          JUDGE_SYSTEM, user, cli)
        parsed = _parse_judgement(last, len(case["material_facts"]))
        if parsed is not None:
            return parsed
    return {"error": "invalid judgement", "reply": last[:2000]}


@dataclasses.dataclass
class Run:
    approval_card: object
    cli: str
    budget_s: float
    card_slots: asyncio.Semaphore
    judge_slots: asyncio.Semaphore


async def sample(run: Run, config: str, case: dict, req, n: int) -> dict:
    ac = run.approval_card
    req = dataclasses.replace(req, budget_s=run.budget_s)
    box = {"asks": 0, "replies": []}
    async with run.card_slots:
        token = _SAMPLE.set(box)
        try:
            ac.clear_cache()
            t0 = time.monotonic()
            card = await ac.write_card(req)
            latency = time.monotonic() - t0
        finally:
            _SAMPLE.reset(token)
    if box["asks"] < 1:
        raise NotAMeasurement(f"{config} {case['id']} run {n}: no model call was made")
    text, _entities = ac.render_card(card)
    from text_util import utf16_len
    out = {"config": config, "case": case["id"], "run": n, "asks": box["asks"],
           "fallback": card is req.fallback, "latency_s": round(latency, 2),
           "length_u16": utf16_len(text), "card": text, "leaks": code_tokens(text),
           "replies": box["replies"] if card is req.fallback else []}
    if not out["fallback"]:
        async with run.judge_slots:
            try:
                out["judge"] = await judge(case, ac.facts_json(req.facts), text, run.cli)
            except Exception as exc:  # noqa: BLE001 -- recorded, the run goes on
                out["judge"] = {"error": f"{type(exc).__name__}: {exc}"}
    print(f"  {config:15} {case['id']:45} #{n} {latency:5.1f}s"
          f"{' FALLBACK' if out['fallback'] else ''}", flush=True)
    return out


class NotAMeasurement(Exception):
    pass


def _set_config(ac, name: str, real_effort_for) -> None:
    spec = CONFIGS[name]
    ac.MODEL_SHORTNAME = spec["model"]
    if spec["effort"] is None:
        ac.effort_for = real_effort_for
    else:
        forced = spec["effort"]
        ac.effort_for = lambda model: forced


# --- summary ----------------------------------------------------------------------

def _pct(values: list[float], q: float) -> "float | None":
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(q * len(s) + 0.5)) - 1))]


def _rate(num: int, den: int) -> str:
    return f"{100 * num / den:.1f}% ({num}/{den})" if den else "n/a"


def _badness(s: dict) -> float:
    j = s.get("judge") or {}
    if s["fallback"] or "facts" not in j:
        return -1
    v = [f["verdict"] for f in j["facts"]]
    return (3 * v.count("wrong") + 2 * v.count("missing")
            + (3 if j["scope"] == "softened" else 0) + (1 if j["scope"] == "overstated" else 0)
            + (2 if j["warning"] in ("wrong", "missing") else 0) + 0.5 * len(s["leaks"]))


def summarise(samples: list[dict], cases: dict, meta: dict, budget_prod: float) -> str:
    lines = ["# Approval card eval", "",
             f"{meta['started']} · CLI {meta['cli_version']} · {meta['runs']} runs per case · "
             f"write budget {meta['budget_s']} s (production {budget_prod} s) · judge {JUDGE_MODEL}",
             ""]
    configs = [c for c in CONFIGS if any(s["config"] == c for s in samples)]
    header = ("| metric | " + " | ".join(configs) + " |\n|---|" + "---|" * len(configs))
    rows: dict[str, list[str]] = {}

    def row(name: str, value: str) -> None:
        rows.setdefault(name, []).append(value)
    for c in configs:
        ss = [s for s in samples if s["config"] == c]
        judged = [s for s in ss if "facts" in (s.get("judge") or {})]
        verdicts = [f["verdict"] for s in judged for f in s["judge"]["facts"]]
        lat = [s["latency_s"] for s in ss]
        length = [s["length_u16"] for s in ss if not s["fallback"]]
        row("cases × runs", f"{len({s['case'] for s in ss})} × {meta['runs']} = {len(ss)}")
        row("missed material facts", _rate(verdicts.count("missing"), len(verdicts)))
        row("wrong facts", _rate(verdicts.count("wrong"), len(verdicts)))
        row("⚠️ accuracy", _rate(sum(s["judge"]["warning"] in ("right", "none_needed")
                                    for s in judged), len(judged)))
        row("⚠️ missing (expected, absent)",
            _rate(sum(s["judge"]["warning"] == "missing" for s in judged), len(judged)))
        row("⚠️ wrong (unneeded or misplaced)",
            _rate(sum(s["judge"]["warning"] == "wrong" for s in judged), len(judged)))
        row("scope overstated", _rate(sum(s["judge"]["scope"] == "overstated" for s in judged),
                                      len(judged)))
        row("scope softened", _rate(sum(s["judge"]["scope"] == "softened" for s in judged),
                                    len(judged)))
        nf = [s for s in ss if not s["fallback"]]
        row("technical leak (cards with ≥1 code-like token)",
            _rate(sum(bool(s["leaks"]) for s in nf), len(nf)))
        row("fallback (invalid / failed)", _rate(sum(s["fallback"] for s in ss), len(ss)))
        row(f"slower than {budget_prod:g} s (production would fall back)",
            _rate(sum(s["latency_s"] > budget_prod for s in ss), len(ss)))
        row("retried (2 asks)", _rate(sum(s["asks"] > 1 for s in ss), len(ss)))
        row("latency p50 / p95 (s)", f"{_pct(lat, .5)} / {_pct(lat, .95)}")
        row("length p50 / max (UTF-16)", f"{_pct(length, .5)} / {max(length) if length else None}")
        row("judge errors", str(sum("error" in (s.get("judge") or {}) for s in nf)))
    lines.append(header)
    for name, values in rows.items():
        lines.append(f"| {name} | " + " | ".join(values) + " |")
    lines.append("")
    for c in configs:
        ss = [s for s in samples if s["config"] == c]
        missed: dict[tuple, int] = {}
        for s in ss:
            for n, f in enumerate((s.get("judge") or {}).get("facts") or []):
                if f["verdict"] != "present":
                    k = (s["case"], n)
                    missed[k] = missed.get(k, 0) + 1
        lines += [f"## {c}: facts most often missed or wrong", ""]
        for (case_id, n), count in sorted(missed.items(), key=lambda kv: -kv[1])[:8]:
            lines.append(f"- {count}× `{case_id}` — {cases[case_id]['material_facts'][n]}")
        lines += ["", f"## {c}: the 5 worst cards", ""]
        for s in sorted(ss, key=_badness, reverse=True)[:5]:
            j = s.get("judge") or {}
            bad = [f"{n + 1}:{f['verdict']} ({f['why']})"
                   for n, f in enumerate(j.get("facts") or []) if f["verdict"] != "present"]
            lines += [f"**`{s['case']}` run {s['run']}** — warning {j.get('warning')}, "
                      f"scope {j.get('scope')}, leaks {s['leaks'] or 'none'}"
                      + (f"; not present: {'; '.join(bad)}" if bad else ""),
                      f"Judge: {j.get('note', j.get('error'))}", "", "```", s["card"], "```", ""]
    fallbacks = [s for s in samples if s["fallback"]]
    if fallbacks:
        lines += ["## Fallbacks", "",
                  "Why each reply was refused (the module's bounds, in UTF-16 units):", ""]
        for s in fallbacks:
            why = " | ".join(", ".join(reply_problems(r)) or "valid" for r in s["replies"])
            lines.append(f"- {s['config']} `{s['case']}` run {s['run']}: {s['asks']} asks, "
                         f"{s['latency_s']} s — {why or 'no reply'}")
    return "\n".join(lines) + "\n"


def reply_problems(reply: str) -> list[str]:
    """Which of ``card_from_reply``'s checks a refused reply fails."""
    import approval_card as ac
    from text_util import is_unsafe_text, utf16_len
    start, end = reply.find("{"), reply.rfind("}")
    try:
        data = json.loads(reply[start:end + 1]) if 0 <= start < end else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return ["not a JSON object"]
    out = []

    def check(name: str, value: object, limit: int) -> None:
        if not isinstance(value, str):
            out.append(f"{name} not a string")
        elif utf16_len(value) > limit:
            out.append(f"{name} {utf16_len(value)} > {limit}")
        elif is_unsafe_text(value):
            out.append(f"{name} unsafe")
    check("action", data.get("action"), ac._ACTION_MAX)
    check("summary", data.get("summary"), ac._SUMMARY_MAX)
    lines = data.get("lines")
    if not isinstance(lines, list):
        return out + ["lines not a list"]
    if len(lines) > ac._LINES_MAX:
        out.append(f"{len(lines)} lines > {ac._LINES_MAX}")
    for line in lines:
        if not isinstance(line, dict):
            out.append("line not an object")
            continue
        check("label", line.get("label"), ac._LABEL_MAX)
        check("value", line.get("value"), ac._VALUE_MAX)
        if not isinstance(line.get("sensitive", False), bool):
            out.append("sensitive not a boolean")
    return out


# --- main -------------------------------------------------------------------------

async def run_all(args, cases: list[dict], requests: dict, ac) -> list[dict]:
    from config import effort_for as real_effort_for
    install_counter(ac)
    ac.CLAUDE_CLI_PATH = args.cli
    run = Run(approval_card=ac, cli=args.cli, budget_s=args.budget,
              card_slots=asyncio.Semaphore(args.concurrency),
              judge_slots=asyncio.Semaphore(args.judge_concurrency))
    samples: list[dict] = []
    for config in args.configs:
        _set_config(ac, config, real_effort_for)
        chosen = [c for c in cases if c["id"] in requests]
        if args.half and config != "sonnet-default":
            chosen = chosen[::2]
        print(f"{config}: {len(chosen)} cases × {args.runs} runs", flush=True)
        jobs = [sample(run, config, c, requests[c["id"]], n)
                for n in range(1, args.runs + 1) for c in chosen]
        samples += await asyncio.gather(*jobs)
    _set_config(ac, "sonnet-default", real_effort_for)
    return samples


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", type=Path, help="directory for results.json and summary.md")
    p.add_argument("--cli", default=None, help="the Claude Code CLI to drive")
    p.add_argument("--cases-dir", type=Path, action="append", default=[],
                   help="extra case directory (repeatable)")
    p.add_argument("--finance-repo", type=Path, default=None,
                   help="a specialist checkout for the install and update cases")
    p.add_argument("--configs", default=",".join(CONFIGS),
                   type=lambda s: [c for c in s.split(",") if c])
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--half", action="store_true",
                   help="configs other than sonnet-default run every other case")
    p.add_argument("--only", default="", help="comma-separated case ids")
    p.add_argument("--budget", type=float, default=60.0)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--judge-concurrency", type=int, default=3)
    p.add_argument("--check", action="store_true")
    p.add_argument("--summary-only", action="store_true",
                   help="rewrite summary.md from the results.json in --out")
    args = p.parse_args()
    unknown = [c for c in args.configs if c not in CONFIGS]
    if unknown:
        p.error(f"unknown configs {unknown}; known: {list(CONFIGS)}")
    if args.concurrency > 3:
        p.error("--concurrency is at most 3, the module's own cap")
    if "ANTHROPIC_API_KEY" in os.environ:
        p.error("ANTHROPIC_API_KEY is set; this eval runs on a logged-in CLI only")

    _scrub_env()
    sys.path.insert(0, str(CODE_ROOT))
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    import approval_card

    if args.summary_only:
        if args.out is None:
            p.error("--summary-only needs --out")
        data = json.loads((args.out / "results.json").read_text(encoding="utf-8"))
        (args.out / "summary.md").write_text(
            summarise(data["samples"], data["cases"], data["meta"], approval_card.BUDGET_S),
            encoding="utf-8")
        return 0

    cases, tool_defs = load_cases([DEFAULT_CASES, *args.cases_dir])
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    workdir = Path(tempfile.mkdtemp(prefix="approval-card-eval-"))
    builders = Builders(tool_defs, args.finance_repo, workdir)
    requests = {}
    for case in cases:
        req = builders.build(case)
        if req is None:
            print(f"skipped {case['id']}: needs --finance-repo", file=sys.stderr)
            continue
        requests[case["id"]] = req
    if args.check:
        for case in cases:
            req = requests.get(case["id"])
            if req is None:
                continue
            print(f"== {case['id']} ({req.kind}, who={req.who}, "
                  f"{len(approval_card.facts_json(req.facts))} chars of facts)")
            print(approval_card.render_card(req.fallback)[0])
            print()
        print(f"{len(requests)} requests built")
        return 0

    if args.out is None or args.cli is None:
        p.error("--out and --cli are required for a run")
    args.cli = shutil.which(args.cli) or args.cli
    version = subprocess.run([args.cli, "--version"], capture_output=True, text=True,
                             check=False).stdout.strip()
    args.out.mkdir(parents=True, exist_ok=True)
    meta = {"started": datetime.datetime.now().isoformat(timespec="seconds"),
            "cli": args.cli, "cli_version": version, "runs": args.runs,
            "budget_s": args.budget, "production_budget_s": approval_card.BUDGET_S,
            "configs": args.configs, "half": args.half, "cases": len(requests),
            "judge": JUDGE_MODEL}
    t0 = time.monotonic()
    try:
        samples = asyncio.run(run_all(args, cases, requests, approval_card))
    except NotAMeasurement as exc:
        print(f"not a measurement: {exc}", file=sys.stderr)
        return 2
    meta["wall_s"] = round(time.monotonic() - t0, 1)
    by_id = {c["id"]: c for c in cases}
    (args.out / "results.json").write_text(json.dumps(
        {"meta": meta, "cases": {i: {k: v for k, v in by_id[i].items() if not k.startswith("_")}
                                 for i in requests},
         "facts": {i: approval_card.facts_json(r.facts) for i, r in requests.items()},
         "samples": samples}, indent=1, ensure_ascii=False), encoding="utf-8")
    (args.out / "summary.md").write_text(
        summarise(samples, by_id, meta, approval_card.BUDGET_S), encoding="utf-8")
    print(f"wrote {args.out / 'results.json'} and {args.out / 'summary.md'} "
          f"in {meta['wall_s']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
