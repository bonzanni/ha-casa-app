"""Browser executor §5.5 (Plan 3 C2, C3): run admission from the operator's own
words.

When a plugin asks to start runs (``POST /internal/broker/run-admission``
during one of its ``callsCasa`` calls), Casa decides whether the operator asked
for them:

1. **Clean-context reading.** With operator words on the call, one small model
   call sees ONLY those words (``handoff.operator_words`` — never the message
   they reply to, the resident's proposal, the task text, or anything a web
   page wrote) and returns the sites they name, as he named them.
2. **Mechanical comparison** (``run_matches``). An account run is named when
   every site it opens with an ``account`` grant is a site he named — the same
   name once case, spacing and punctuation are ignored (``site_named``); its
   public grants need no naming. An open-web run holds nothing of his and is
   named when
   his words ask for any browsing at all. Every run named ⇒ admitted, no prompt.
3. **One tap.** Otherwise — no words (a continuation, a narration, a tap), a
   reading that names nothing ("collect them"), or one run he did not name —
   Casa posts ONE question listing every run, one line each, and answers
   ``confirmation_pending``. The tap settles the confirmation and queues a
   continuation turn (``admission:<id>``) whose plugin call reads it
   (``POST /internal/broker/run-admission-status``).

The kind of task is shown on the card but not compared: in part 1 every run only
reads, and what a run may do on a logged-in site is bounded by that site's own
approvals, so a kind compare would add taps without adding a guard. Site names
come from the plugin's own code (the app's registered display names); checking
that a run opens the sites it was admitted for is the plugin's and the app's.
Meaning is judged by the model; the clean context is the guard, because no web
text can reach it."""
from __future__ import annotations

import asyncio
import dataclasses
import json

from atomic_io import PRIVATE, atomic_write_json
import logging
import re
import threading
import time
import unicodedata
import uuid
from typing import Any

logger = logging.getLogger(__name__)

MAX_RUNS = 8
MAX_SITES = 8
MAX_NAME_CHARS = 64
MAX_TASK_CHARS = 2000
MAX_SITE_ID_CHARS = 128
MAX_DIGEST_CHARS = 128
CONFIRM_TIMEOUT_S = 3600.0
ANSWER_KEEP_S = 30 * 24 * 3600.0
MAX_CONFIRMATIONS = 2000
CONFIRMATIONS_PATH = "/data/plugin-admissions.json"
REQUEST_KEY_MAX = 128
ORIGIN_KIND = "operator_telegram"
MIN_SITE_MATCH_CHARS = 3
CARD_TASK_CHARS = 80

EXTRACT_PROMPT = (
    "You read ONE message a person wrote to their assistant. Decide which web "
    "browsing this message itself asks for, and on which websites or services.\n"
    "- For each site or service the person names for the task, give only its "
    "name, as they wrote it, without words around it (\"my UniFi gateway\" → "
    "\"UniFi\"; \"the RunPod invoice\" → \"RunPod\").\n"
    "- When they want something looked up or downloaded from the web in "
    "general, give one entry with site \"\".\n"
    "A message that only makes sense together with earlier conversation — "
    "\"yes\", \"do it\", \"collect them\", \"go ahead\" — asks for nothing. "
    "Do not guess tasks the message does not state.\n"
    "Answer with JSON only, no prose: {\"runs\": [{\"site\": \"...\"}]} — or "
    "{\"runs\": []}."
)


def _printable(value: Any, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit and value.isprintable()


def validate_runs(raw: Any) -> "tuple[list[dict], str | None]":
    """C2's run list: ``[{spec_digest, mode, task_kind, task_text, sites:
    [{site_id, access, name}]}]``, 1–8 runs, normalised."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_RUNS:
        return [], "bad_runs"
    out = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - {
                "spec_digest", "mode", "task_kind", "task_text", "sites"}:
            return [], "bad_runs"
        digest, mode = entry.get("spec_digest"), entry.get("mode")
        if not _printable(digest, MAX_DIGEST_CHARS) or not digest:
            return [], "bad_digest"
        if mode not in ("account", "open_web"):
            return [], "bad_mode"
        if not _printable(entry.get("task_kind", ""), 60):
            return [], "bad_task"
        task_text = entry.get("task_text", "")
        if not isinstance(task_text, str) or len(task_text) > MAX_TASK_CHARS:
            return [], "bad_task"
        sites = entry.get("sites", [])
        if not isinstance(sites, list) or len(sites) > MAX_SITES:
            return [], "bad_sites"
        clean_sites = []
        for s in sites:
            if (not isinstance(s, dict) or set(s) - {"site_id", "access", "name"}
                    or not _printable(s.get("site_id"), MAX_SITE_ID_CHARS) or not s["site_id"]
                    or s.get("access") not in ("account", "public")
                    or not _printable(s.get("name"), MAX_NAME_CHARS) or not s["name"].strip()):
                return [], "bad_sites"
            clean_sites.append({"site_id": s["site_id"], "access": s["access"],
                                "name": s["name"].strip()})
        if mode == "account" and not any(s["access"] == "account" for s in clean_sites):
            return [], "bad_sites"
        if mode == "open_web" and any(s["access"] == "account" for s in clean_sites):
            # an open-web run holds nothing of his — never a logged-in site (p3r4b)
            return [], "bad_sites"
        out.append({"spec_digest": digest, "mode": mode,
                    "task_kind": entry.get("task_kind", ""), "task_text": task_text,
                    "sites": clean_sites})
    return out, None


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[\W_]+", unicodedata.normalize("NFKC", name).casefold())
            if t]


def site_named(site_name: str, said: str) -> bool:
    """The registered *site_name* is the site *said* names: the two are the
    same name once case, spacing and punctuation are ignored ("Run Pod",
    "RunPod", "runpod" alike). Nothing less — not a word inside a longer name
    ("Chase" in "Chase Center"), not letters inside a word ("ING" in
    "Booking"), not part of the label ("GitHub" for "GitHub Enterprise"); a
    name he wrote another way goes to the card."""
    want = "".join(_tokens(site_name))
    return len(want) >= MIN_SITE_MATCH_CHARS and want == "".join(_tokens(said))


def parse_extraction(reply: str) -> list[str]:
    """The site names a model reply states (``""`` for the open web); anything
    unreadable is none — which goes to the tap."""
    m = re.search(r"\{.*\}", (reply or "").strip(), re.S)
    if m is None:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    runs = data.get("runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return []
    return [r["site"] for r in runs[:2 * MAX_RUNS]
            if isinstance(r, dict) and isinstance(r.get("site", None), str)]


def run_matches(run: dict, said: list[str]) -> bool:
    """Mechanical: an account run needs each of its account-grant sites named;
    an open-web run needs his words to ask for browsing at all."""
    if run["mode"] == "open_web":
        return bool(said)
    return all(any(site_named(s["name"], x) for x in said if x)
               for s in run["sites"] if s["access"] == "account")


async def _ask_model(words: str) -> str:
    """One clean-context call: Haiku, no tools, no settings sources, no
    session persisted — the tier classifier's posture. Only *words* is sent."""
    import claude_agent_sdk as sdk
    from claude_runtime import (
        CLAUDE_CLI_PATH, SDK_MAX_BUFFER_SIZE, cli_session_settings,
        ephemeral_extra_args, ephemeral_setting_sources,
        with_cross_session_tools_denied,
    )
    from config import effort_for, resolve_model

    model = resolve_model("haiku")
    opts = sdk.ClaudeAgentOptions(
        model=model, effort=effort_for(model), cli_path=CLAUDE_CLI_PATH,
        max_buffer_size=SDK_MAX_BUFFER_SIZE, system_prompt=EXTRACT_PROMPT,
        max_turns=4, tools=[], allowed_tools=[],
        disallowed_tools=with_cross_session_tools_denied(["Bash", "Task", "Agent"]),
        settings=cli_session_settings(), setting_sources=ephemeral_setting_sources(),
        extra_args=ephemeral_extra_args(), permission_mode="acceptEdits",
    )
    reply = ""
    prompt = f"The person's message:\n<message>\n{words}\n</message>"
    async for msg in sdk.query(prompt=prompt, options=opts):
        if isinstance(msg, sdk.AssistantMessage):
            for block in getattr(msg, "content", []) or []:
                t = getattr(block, "text", None)
                if isinstance(t, str):
                    reply += t
    return reply


async def sites_named(words: str) -> list[str]:
    """The sites *words* names; a model failure is none, never an error."""
    try:
        return parse_extraction(await _ask_model(words))
    except Exception as exc:  # noqa: BLE001 — admission falls back to the tap
        logger.warning("run admission: reading the operator's words failed (%s)",
                       type(exc).__name__)
        return []


def compose_question(runs: list[dict], requester_name: str) -> str:
    """Casa's own text for the batch tap: one line per run, from the
    validated fields; the task line is the plugin's, clipped."""
    head = ("Start this browsing run?" if len(runs) == 1
            else f"Start these {len(runs)} browsing runs?")
    lines = []
    for r in runs:
        where = ("the open web" if r["mode"] == "open_web"
                 else ", ".join(s["name"] for s in r["sites"]))
        task = " ".join((r["task_kind"] or r["task_text"]).split())[:CARD_TASK_CHARS]
        lines.append(f"• {where}" + (f" — {task}" if task else ""))
    return "\n".join([head, *lines, f"Asked for by {requester_name}."])


def _admissions(runs: list[dict]) -> list[dict]:
    return [{"spec_digest": r["spec_digest"], "origin_kind": ORIGIN_KIND,
             "origin_ref": "adm-" + uuid.uuid4().hex} for r in runs]


@dataclasses.dataclass
class Confirmation:
    """One admission request by its plugin-supplied key — the answer a replay
    of the key, or a status read, returns identically."""
    cid: str
    plugin: str
    operator_id: int
    binding_ref: str
    request_key: str
    status: str                 # pending | admitted | declined | expired | refused
    admissions: list
    created_at: float           # wall clock (survives a restart)
    answered_at: float = 0.0
    process: str = ""           # the process that posted a pending card


_PROCESS = uuid.uuid4().hex     # a pending card from another process is dead


class Confirmations:
    """Durable (``CONFIRMATIONS_PATH``, ``0600``): an answered request — by his
    words or by his tap — is kept ``ANSWER_KEEP_S`` after its answer and read
    back identically however often, across restarts; a pending one whose card
    belongs to an earlier process (a restart emptied the broker) reads
    ``expired``."""

    def __init__(self, path: str = "", *, now=time.time) -> None:
        self._path = path or CONFIRMATIONS_PATH
        self._now = now
        self._lock = threading.Lock()
        self._loaded = False
        self._items: dict[str, Confirmation] = {}

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self._path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 — a corrupt store answers nothing, loudly
            logger.warning("run admission: unreadable store %s ignored", self._path,
                           exc_info=True)
            return
        for entry in raw if isinstance(raw, list) else []:
            try:
                c = Confirmation(**entry)
            except TypeError:
                continue
            if c.status == "pending" and c.process != _PROCESS:
                c.status, c.answered_at = "expired", self._now()
            self._items[c.cid] = c

    def _save_locked(self) -> None:
        now = self._now()
        for cid, c in list(self._items.items()):
            if (c.status != "pending" and c.answered_at + ANSWER_KEEP_S <= now) or (
                    c.status == "pending" and c.created_at + CONFIRM_TIMEOUT_S
                    + ANSWER_KEEP_S <= now):
                del self._items[cid]
        if len(self._items) > MAX_CONFIRMATIONS:
            for c in sorted(self._items.values(), key=lambda c: c.created_at)[
                    :len(self._items) - MAX_CONFIRMATIONS]:
                del self._items[c.cid]
        try:
            import os
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            atomic_write_json(self._path, [dataclasses.asdict(c) for c in self._items.values()],
                              mode=PRIVATE)
        except OSError:
            logger.warning("run admission: could not write %s", self._path, exc_info=True)

    def by_key(self, *, plugin: str, operator_id: int, request_key: str) -> "Confirmation | None":
        with self._lock:
            self._load_locked()
            for c in self._items.values():
                if (c.plugin, c.operator_id, c.request_key) == (plugin, int(operator_id),
                                                                request_key):
                    return c
            return None

    def add(self, c: Confirmation) -> None:
        with self._lock:
            self._load_locked()
            if c.status == "pending":
                c.process = _PROCESS
            else:
                c.answered_at = c.answered_at or self._now()
            self._items[c.cid] = c
            self._save_locked()

    def settle(self, cid: str, status: str, admissions: list) -> "Confirmation | None":
        with self._lock:
            self._load_locked()
            c = self._items.get(cid)
            if c is None or c.status != "pending":
                return None
            c.status, c.admissions, c.answered_at = status, admissions, self._now()
            self._save_locked()
            return c

    def get(self, cid: Any, *, plugin: str, operator_id: int) -> "Confirmation | None":
        with self._lock:
            self._load_locked()
            c = self._items.get(cid) if isinstance(cid, str) else None
            if c is None or c.plugin != plugin or c.operator_id != int(operator_id):
                return None
            return c


CONFIRMATIONS = Confirmations()


def _answer(c: Confirmation) -> dict:
    """The one answer a request key gets, on C2 and C3 alike."""
    if c.status == "admitted":
        return {"status": "admitted", "request_key": c.request_key,
                "confirmation_id": c.cid, "admissions": list(c.admissions)}
    if c.status == "pending":
        return {"status": "confirmation_pending", "request_key": c.request_key,
                "confirmation_id": c.cid}
    if c.status == "refused":
        return {"status": "refused", "reason": "not_delivered", "request_key": c.request_key,
                "confirmation_id": c.cid}
    return {"status": c.status, "request_key": c.request_key, "confirmation_id": c.cid}


_KEY_LOCKS: dict[tuple, asyncio.Lock] = {}


async def request_admission(call, runs: list[dict], request_key: str) -> dict:
    """C2 for *call* (an ``operator_calls.OperatorCall``). A key this plugin
    and operator used before gets that request's answer again — no second
    reading, no second card — and requests with one key are decided one at a
    time, so an overlapping retry waits for the first and gets its answer."""
    key = (call.plugin, int(call.identity.operator_id), request_key)
    lock = _KEY_LOCKS.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            seen = CONFIRMATIONS.by_key(plugin=call.plugin,
                                        operator_id=call.identity.operator_id,
                                        request_key=request_key)
            if seen is not None:
                return _answer(seen)
            return await _decide(call, runs, request_key)
    finally:
        if not lock.locked() and _KEY_LOCKS.get(key) is lock:
            del _KEY_LOCKS[key]


async def _decide(call, runs: list[dict], request_key: str) -> dict:
    from operator_calls import binding_for, post_question

    started = time.monotonic()
    extract_ms = None
    binding = binding_for(call)
    cid = "conf-" + uuid.uuid4().hex
    if call.operator_words is not None:
        t0 = time.monotonic()
        said = await sites_named(call.operator_words)
        extract_ms = int((time.monotonic() - t0) * 1000)
        if all(run_matches(r, said) for r in runs):
            conf = Confirmation(cid=cid, plugin=call.plugin,
                                operator_id=int(call.identity.operator_id),
                                binding_ref=binding.ref, request_key=request_key,
                                status="admitted", admissions=_admissions(runs),
                                created_at=CONFIRMATIONS._now())
            CONFIRMATIONS.add(conf)
            logger.info("run admission: %d run(s) admitted by his words extract_ms=%s "
                        "total_ms=%d", len(runs), extract_ms,
                        int((time.monotonic() - started) * 1000))
            return _answer(conf)
    import tools as tools_mod
    conf = Confirmation(cid=cid, plugin=call.plugin, operator_id=int(call.identity.operator_id),
                        binding_ref=binding.ref, request_key=request_key, status="pending",
                        admissions=[], created_at=CONFIRMATIONS._now())
    CONFIRMATIONS.add(conf)

    async def _on_settle(outcome: dict) -> None:
        if outcome.get("outcome") == "answered" and outcome.get("choice") == 0:
            settled = CONFIRMATIONS.settle(cid, "admitted", _admissions(runs))
        elif outcome.get("outcome") == "answered":
            settled = CONFIRMATIONS.settle(cid, "declined", [])
        elif outcome.get("outcome") == "no_answer":
            settled = CONFIRMATIONS.settle(cid, "expired", [])
        else:                                   # shutdown or retirement
            CONFIRMATIONS.settle(cid, "expired", [])
            return
        if settled is not None:
            logger.info("run admission: confirmation %s settled %s", cid[:13], settled.status)
            import plugin_continuations
            plugin_continuations.CONTINUATIONS.request(
                binding, f"admission:{cid}", "operator_answer")

    start = "Start" if len(runs) == 1 else "Start all"
    landed = await post_question(
        identity=call.identity, role=call.role,
        text=compose_question(runs, tools_mod._display_name_for_role(binding.requester)),
        options=[start, "Not now"], timeout_s=CONFIRM_TIMEOUT_S, on_settle=_on_settle)
    logger.info("run admission: %d run(s) put to the operator (%s) extract_ms=%s",
                len(runs), "posted" if landed else "not delivered", extract_ms)
    if not landed:
        CONFIRMATIONS.settle(cid, "refused", [])
    return _answer(conf)


def confirmation_status(call, cid: Any) -> dict:
    """C3: the request's answer, identical on every read — ``admitted`` with
    the admissions, ``declined`` or ``expired`` (as a C2 replay answers it), or
    ``pending`` while the card waits; an id this Casa does not know for this
    plugin and operator reads ``expired``."""
    c = CONFIRMATIONS.get(cid, plugin=call.plugin, operator_id=call.identity.operator_id)
    if c is None:
        return {"status": "expired", "confirmation_id": cid if isinstance(cid, str) else ""}
    answer = _answer(c)
    if answer["status"] == "confirmation_pending":
        answer["status"] = "pending"
    return answer
