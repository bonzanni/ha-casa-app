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

One kind of request skips all three: a specialist asking for the runs in a
turn that belongs to a task the operator started
(``handoff.helper_under_operator_task``: the turn carries Casa's mark of that
task and the requester is a specialist, not the resident). Those runs are
admitted without a reading or a question and get the status card. A resident's
own request, the agent's own run, and scheduled or agent-started work keep the
steps above.

No card carries a Stop: the operator stops the runs by asking the agent, which
calls ``stop_work`` (``plugin_stop``).

The kind of task is shown on the card but not compared: admission stays
site-based because every change a run would make is asked per action (the
action check, ``action_judgement``), so a kind compare would add taps without
adding a guard. The operator's words, when the call has them, are kept for each
requested run's spec digest (``action_judgement.WORDS``, memory-only) whether
the runs are admitted by them or put to the tap, so the check of that run's
actions reads them too. Site names
come from the plugin's own code (the app's registered display names); checking
that a run opens the sites it was admitted for is the plugin's and the app's.
Meaning is judged by the model; the clean context is the guard, because no web
text can reach it.

The card goes to the call's delivery (``GrantIdentity.delivery``): the
executing agent's own chat, or the resident's for its own plugin; a call with
no delivery is refused as not delivered and posts nothing anywhere. A request
is identified by its plugin, the Telegram bot of that delivery, the operator
and the plugin's request key: one plugin can serve agents on two bots, and its
request keys may be equal on both. The bot is read from that endpoint when the
request is decided; before it has verified its id the request is refused as
not delivered, as when its card cannot be posted, and nothing is stored."""
from __future__ import annotations

import asyncio
import dataclasses
import json

from atomic_io import PRIVATE, atomic_write_json
from channels.tg_address import AddressError, TgAddress
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
        with_cross_session_tools_denied, with_system_prompt_file,
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
    async for msg in sdk.query(prompt=prompt, options=with_system_prompt_file(opts)):
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


def _run_lines(runs: list[dict]) -> list[str]:
    lines = []
    for r in runs:
        where = ("the open web" if r["mode"] == "open_web"
                 else ", ".join(s["name"] for s in r["sites"]))
        task = " ".join((r["task_kind"] or r["task_text"]).split())[:CARD_TASK_CHARS]
        lines.append(f"• {where}" + (f" — {task}" if task else ""))
    return lines


#: the longest title a request is stored with (``key_title``)
TITLE_CHARS = 40


def key_title(runs: list[dict]) -> str:
    """What the request does, stored with it as its title: the first run's task and
    `` +N`` for the runs after it; ``browsing`` when that run names no task. Never a site.
    A long task is cut so that the whole title, count included, fits ``TITLE_CHARS``."""
    first = runs[0] if runs else {}
    task = " ".join(str(first.get("task_kind") or first.get("task_text") or "").split())
    more = f" +{len(runs) - 1}" if len(runs) > 1 else ""
    room = TITLE_CHARS - len(more)
    if len(task) > room:
        task = task[:room - 1].rstrip() + "…"
    return (task or "browsing") + more


def compose_question(runs: list[dict], requester_name: "str | None") -> str:
    """The batch tap's text, in the browsing agent's own voice: who asked it
    (*requester_name*, ``None`` when the run is its own), then one line per run
    from the validated fields; the task line is the plugin's, clipped."""
    what = ("this browsing run" if len(runs) == 1
            else f"these {len(runs)} browsing runs")
    head = (f"OK to start {what}?" if requester_name is None
            else f"{requester_name} asked me for {what}. OK to start?")
    return "\n".join([head, *_run_lines(runs)])


def compose_status(runs: list[dict], requester_name: "str | None") -> str:
    """The status card's text when his words admitted the runs (no start card exists): the
    same lines under "On it for <requester>:" ("On it:" when the run is the agent's own)."""
    who = "" if requester_name is None else f" for {requester_name}"
    count = "" if len(runs) == 1 else f" ({len(runs)} runs)"
    return "\n".join([f"On it{who}{count}:", *_run_lines(runs)])


def _requester_name(binding, role: str) -> "str | None":
    """Who asked *role* for the runs, by name; ``None`` when it asked itself."""
    import tools as tools_mod
    if binding.requester == role:
        return None
    return tools_mod._display_name_for_role(binding.requester)


def _admissions(runs: list[dict]) -> list[dict]:
    return [{"spec_digest": r["spec_digest"], "origin_kind": ORIGIN_KIND,
             "origin_ref": "adm-" + uuid.uuid4().hex} for r in runs]


@dataclasses.dataclass
class Confirmation:
    """One admission request by its plugin-supplied key — the answer a replay
    of the key, or a status read, returns identically."""
    cid: str
    plugin: str
    bot_id: int                 # the bot the request was asked on (its binding's route)
    operator_id: int
    binding_ref: str
    request_key: str
    status: str                 # pending | admitted | declined | expired | refused
    admissions: list
    created_at: float           # wall clock (survives a restart)
    answered_at: float = 0.0
    process: str = ""           # the process that posted a pending card
    # the start or status card of the request (None: none landed, or the request no longer
    # runs): its address ``[bot, chat, message]`` and its text as shown, so a Stop from any
    # path marks it (``plugin_stop.settle_run_card``)
    card: "list | None" = None
    card_text: str = ""
    # what the request does (``key_title``)
    title: str = ""

    def card_address(self) -> "TgAddress | None":
        """The card's address, or None when no card is held."""
        return TgAddress.from_json(self.card) if self.card else None


def _stored_ok(entry: Any) -> bool:
    """A stored confirmation carries its bot, and its card (if any) is a full address."""
    if not isinstance(entry, dict):
        return False
    bot = entry.get("bot_id")
    if not isinstance(bot, int) or isinstance(bot, bool) or bot <= 0:
        return False
    card = entry.get("card")
    if card is None:
        return True
    try:
        TgAddress.from_json(card)
    except AddressError:
        return False
    return True


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
        refused = 0
        for entry in raw if isinstance(raw, list) else []:
            if not _stored_ok(entry):
                refused += 1
                continue
            try:
                c = Confirmation(**entry)
            except TypeError:
                refused += 1
                continue
            if c.status == "pending" and c.process != _PROCESS:
                c.status, c.answered_at = "expired", self._now()
            self._items[c.cid] = c
        if refused:
            logger.error("run admission: %d stored request(s) without a bot or with a "
                         "malformed card refused", refused)

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

    def by_key(self, *, plugin: str, bot_id: int, operator_id: int,
               request_key: str) -> "Confirmation | None":
        want = (plugin, int(bot_id), int(operator_id), request_key)
        with self._lock:
            self._load_locked()
            for c in self._items.values():
                if (c.plugin, c.bot_id, c.operator_id, c.request_key) == want:
                    return c
            return None

    def by_key_on_any_bot(self, *, plugin: str, operator_id: int,
                          request_key: str) -> "list[Confirmation]":
        """Every request of *plugin* and the operator with this key, whatever
        bot it was asked on, oldest first."""
        want = (plugin, int(operator_id), request_key)
        with self._lock:
            self._load_locked()
            return sorted((c for c in self._items.values()
                           if (c.plugin, c.operator_id, c.request_key) == want),
                          key=lambda c: c.created_at)

    def add(self, c: Confirmation) -> None:
        with self._lock:
            self._load_locked()
            if c.status == "pending":
                c.process = _PROCESS
            else:
                c.answered_at = c.answered_at or self._now()
            self._items[c.cid] = c
            self._save_locked()

    def settle(self, cid: str, status: str, admissions: list, *,
               card: "TgAddress | None" = None, card_text: str = "") -> "Confirmation | None":
        with self._lock:
            self._load_locked()
            c = self._items.get(cid)
            if c is None or c.status != "pending":
                return None
            c.status, c.admissions, c.answered_at = status, admissions, self._now()
            if card is not None:
                c.card, c.card_text = card.to_json(), card_text
            self._save_locked()
            return c

    def set_card(self, cid: str, card: TgAddress, text: str) -> None:
        """The status card landed: its address and text are recorded with the request."""
        with self._lock:
            self._load_locked()
            c = self._items.get(cid)
            if c is None:
                return
            c.card, c.card_text = card.to_json(), text
            self._save_locked()

    def clear_card(self, cid: str) -> None:
        """The request no longer runs (``plugin_stop.settle_ended``, ``settle_stop``): its
        card is forgotten, so no later pass marks it again. Its text stays."""
        with self._lock:
            self._load_locked()
            c = self._items.get(cid)
            if c is None or not c.card:
                return
            c.card = None
            self._save_locked()

    def open_cards(self, bot_id: int) -> "list[Confirmation]":
        """The requests asked on bot *bot_id* that still hold a card whose Stop works:
        admitted, with a card neither a stop nor a natural end has forgotten
        (``clear_card``), and a binding that still exists — what the Stop callback itself
        requires. Oldest first. Read-only."""
        from plugin_bindings import BINDINGS
        with self._lock:
            self._load_locked()
            held = [c for c in self._items.values()
                    if c.status == "admitted" and c.card and c.bot_id == int(bot_id)]
        return sorted((c for c in held if BINDINGS.peek(c.binding_ref) is not None),
                      key=lambda c: c.created_at)

    def open_cards_on_bot(self, bot_id: int) -> int:
        """How many requests asked on bot *bot_id* still hold a card whose Stop works
        (``open_cards``)."""
        return len(self.open_cards(bot_id))

    def peek(self, cid: Any) -> "Confirmation | None":
        """Casa's own read (the Stop callback): no plugin or operator check — the caller
        compares the tapper itself."""
        with self._lock:
            self._load_locked()
            return self._items.get(cid) if isinstance(cid, str) else None

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


def _not_delivered(request_key: str) -> dict:
    """The answer of a request whose card cannot be posted, when nothing was stored for it:
    its bot has no id yet, so a later request with the key is decided afresh."""
    return {"status": "refused", "reason": "not_delivered", "request_key": request_key,
            "confirmation_id": "conf-" + uuid.uuid4().hex}


async def request_admission(call, runs: list[dict], request_key: str) -> dict:
    """C2 for *call* (an ``operator_calls.OperatorCall``). A key this plugin
    and operator used before on the same bot gets that request's answer again —
    no second reading, no second card — and requests with one key are decided
    one at a time, so an overlapping retry waits for the first and gets its
    answer."""
    from operator_calls import binding_for
    from plugin_bindings import ChannelUnavailable, delivery_target
    delivery = getattr(call.identity, "delivery", None)
    if delivery is None:
        logger.warning("run admission: refused, this call has nowhere to post")
        return _not_delivered(request_key)
    binding = binding_for(call)
    try:
        endpoint, bot_id = delivery_target(delivery)
    except ChannelUnavailable:
        logger.warning("run admission: refused, the delivery's bot has no id yet")
        return _not_delivered(request_key)
    key = (call.plugin, bot_id, int(call.identity.operator_id), request_key)
    lock = _KEY_LOCKS.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            seen = CONFIRMATIONS.by_key(plugin=call.plugin, bot_id=bot_id,
                                        operator_id=call.identity.operator_id,
                                        request_key=request_key)
            if seen is not None:
                return _answer(seen)
            return await _decide(call, runs, request_key, binding, endpoint, bot_id,
                                 int(delivery.chat_id))
    finally:
        if not lock.locked() and _KEY_LOCKS.get(key) is lock:
            del _KEY_LOCKS[key]


async def _decide(call, runs: list[dict], request_key: str, binding, endpoint,
                  bot_id: int, chat_id: int) -> dict:
    from handoff import helper_under_operator_task
    from operator_calls import post_question

    started = time.monotonic()
    extract_ms = None
    cid = "conf-" + uuid.uuid4().hex
    if helper_under_operator_task(call.origin, call.chain):
        # a colleague asked, in a turn of a task the operator started: no
        # question, no reading; the status card is posted
        conf = Confirmation(cid=cid, plugin=call.plugin, bot_id=bot_id,
                            operator_id=int(call.identity.operator_id),
                            binding_ref=binding.ref, request_key=request_key,
                            status="admitted", admissions=_admissions(runs),
                            created_at=CONFIRMATIONS._now(), title=key_title(runs))
        CONFIRMATIONS.add(conf)
        logger.info("run admission: %d run(s) admitted for a colleague under the "
                    "operator's task", len(runs))
        _spawn_status_card(call, runs, binding, cid, endpoint, chat_id)
        return _answer(conf)
    if call.operator_words is not None:
        from action_judgement import WORDS
        for r in runs:      # his words for the task, admitted by them or not
            WORDS.record(plugin=call.plugin, bot_id=bot_id,
                         operator_id=call.identity.operator_id,
                         spec_digest=r["spec_digest"], words=call.operator_words)
        t0 = time.monotonic()
        said = await sites_named(call.operator_words)
        extract_ms = int((time.monotonic() - t0) * 1000)
        if all(run_matches(r, said) for r in runs):
            conf = Confirmation(cid=cid, plugin=call.plugin, bot_id=bot_id,
                                operator_id=int(call.identity.operator_id),
                                binding_ref=binding.ref, request_key=request_key,
                                status="admitted", admissions=_admissions(runs),
                                created_at=CONFIRMATIONS._now(), title=key_title(runs))
            CONFIRMATIONS.add(conf)
            logger.info("run admission: %d run(s) admitted by his words extract_ms=%s "
                        "total_ms=%d", len(runs), extract_ms,
                        int((time.monotonic() - started) * 1000))
            _spawn_status_card(call, runs, binding, cid, endpoint, chat_id)
            return _answer(conf)
    conf = Confirmation(cid=cid, plugin=call.plugin, bot_id=bot_id,
                        operator_id=int(call.identity.operator_id),
                        binding_ref=binding.ref, request_key=request_key, status="pending",
                        admissions=[], created_at=CONFIRMATIONS._now(), title=key_title(runs))
    CONFIRMATIONS.add(conf)

    async def _on_settle(outcome: dict) -> None:
        started = outcome.get("outcome") == "answered" and outcome.get("choice") == 0
        if started:
            settled = CONFIRMATIONS.settle(
                cid, "admitted", _admissions(runs), card=outcome.get("address"),
                card_text=str(outcome.get("text") or ""))
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
            from plugin_bindings import ChannelUnavailable
            try:
                plugin_continuations.CONTINUATIONS.request(
                    binding, f"admission:{cid}", "operator_answer")
            except ChannelUnavailable:
                logger.warning("run admission: no continuation queued, the binding's bot "
                               "is unavailable; the driver sweep finds the run")

    start = "Start" if len(runs) == 1 else "Start all"
    landed = await post_question(
        endpoint=endpoint, identity=call.identity, role=call.role,
        text=compose_question(runs, _requester_name(binding, call.role)),
        options=[start, "Not now"], timeout_s=CONFIRM_TIMEOUT_S, on_settle=_on_settle,
        chat_id=chat_id, post=_card_post(call, cid))
    logger.info("run admission: %d run(s) put to the operator (%s) extract_ms=%s",
                len(runs), "posted" if landed else "not delivered", extract_ms)
    if not landed:
        CONFIRMATIONS.settle(cid, "refused", [])
    return _answer(conf)


_CARD_TASKS: set = set()


def _card_post(call, cid: str):
    """The post record a request's card (its question, or its status card) is
    filed under: the calling agent's, owned by the call's task (its job or its
    delegation, ``result_broker._post_owner``), so a reply to the card reaches
    the task the call runs in."""
    import result_broker as rb
    return rb.PostRecord(role=call.role, operator_id=int(call.identity.operator_id),
                         plugin=call.plugin, slot="run_admission", tool_use_id=cid,
                         owner=rb._post_owner(call.identity), posted_at=time.time())


def _spawn_status_card(call, runs: list[dict], binding, cid: str, endpoint,
                       chat_id: int) -> None:
    """The runs were admitted without a start card (by his words, or for a colleague
    under his task): one status card is posted beside the answer (never awaited by it),
    through *endpoint* in *chat_id*, the call's delivery; it carries no button. A
    colleague's card under his task is sent without a notification. A failed post is
    logged and nothing else — ``stop_work`` still stops the request."""
    async def _post() -> None:
        from handoff import helper_under_operator_task
        from result_broker import compose_operator_message, label_at
        text = compose_operator_message(
            compose_status(runs, _requester_name(binding, call.role)),
            label_at(call.role, endpoint))
        silent = helper_under_operator_task(call.origin, call.chain)
        mid = await endpoint.deliver_run_card(chat_id, text, post=_card_post(call, cid),
                                              silent=silent)
        if isinstance(mid, int) and not isinstance(mid, bool):
            CONFIRMATIONS.set_card(cid, TgAddress(endpoint.bot_id, chat_id, mid), text)
        else:
            logger.warning("run admission: the status card was not posted")

    async def _guarded() -> None:
        try:
            await _post()
        except Exception as exc:  # noqa: BLE001 — logged; the answer stands
            logger.warning("run admission: status card failed (%s)", type(exc).__name__)

    task = asyncio.get_running_loop().create_task(_guarded())
    _CARD_TASKS.add(task)
    task.add_done_callback(_CARD_TASKS.discard)


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
