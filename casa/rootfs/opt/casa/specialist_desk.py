"""The specialist desk (S4): the thread one specialist keeps with one chat.

A swipe-reply the operator sends on a message Casa posted for a specialist
(an ``operator_message``/``operator_file``/``operator_link`` post, or a desk
reply) reaches that specialist as ONE desk turn carrying the operator's exact
words — never a resident turn (INV-DESK-001). The desk is a bounded dialogue
log injected into each turn's prompt, not an SDK session resume: the
specialist runner runs every delegated turn in a fresh CLI session and gives
the specialist its memory by prompt injection, and the plugin's own store is
the state a specialist works from; the desk keeps only the dialogue
(INV-DESK-003). The reply reaches the operator as the specialist's own
admitted, labelled, bounded text; a turn with no proven operator-visible
outcome ends in one labelled Casa notice; the chat's resident learns of the
turn only through a body-free line at its next turn, and no resident model
turn is spent on it (INV-DESK-002).

Bounds (design §10): twelve exchanges per desk, 400 characters per side,
10,000 per rendered block, idle reset after an hour, three waiters per desk,
task ≤ 4,096 (Telegram's own), quote ≤ 2,000, context ≤ 12,500.

#1192: the log is rendered from the specialist's point of view — a frame
line saying these are its OWN exchanges in this chat, and party labels
(``the operator``, ``you``, ``<resident>, on the operator's behalf``) — and
a desk turn's context opens with a line saying the message is from that same
operator, now. Only the rendering changed; the log stores what it stored.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import logging
import time
import uuid
from typing import Any, Callable

from stored_calls import TELL_LINE   # the one tell line (§14.7), shared with the result hook

logger = logging.getLogger(__name__)

DESK_LOG_EXCHANGES = 12
DESK_LOG_SIDE_CHARS = 400
DESK_LOG_CHARS = 10_000
DESK_IDLE_S = 3600.0
DESK_QUEUE_MAX = 3
DESK_TASK_CHARS = 4096
DESK_QUOTE_CHARS = 2000
DESK_CONTEXT_CHARS = 12_500
DESK_FRAMING_CHARS = 500
DESK_NAME_CHARS = 40          # a resident's display name inside a label or a frame
CLIP = "[…]"
POSTED_VIEW = "[posted a view]"
NO_REPLY = "[no reply]"
ECHO_OWNER_PREFIX = "desk:"
UNWINDING = "an earlier run is still unwinding"   # #1197: the desk's refusal reason
# S5: the pinned one-call turn (stored-call buttons)
STORED_CALL_RECEIPT_CHARS = 4000
NO_RECEIPT = "[no receipt]"
POSTED_PROPOSAL = "[posted a proposal]"
TELL_ECHO = " — the CLI reported arguments changed by an installed hook"
PROMPT_PREFIX = "(front desk) "
# §5.6/§6: the body-free echo line for an outcome of a silent turn that has
# no S3 echo event of its own, keyed by the outcome's kind — a landed post's
# delivery kind (`PostRecord.kind`) or a send's intent (`OperatorSend.intent`)
OUTCOME_ECHO = {"operator_link": "posted a link to your chat.",
                "media": "sent you a file.", "caption": "sent you a file.",
                "keyboard": "asked you a question.", "discrete": "sent you a message."}
OUTCOME_ECHO_OTHER = "sent you something."


def clip(text: str, limit: int) -> str:
    """*text* within *limit* characters, a visible marker when it was cut."""
    text = text or ""
    if len(text) <= limit:
        return text
    keep = max(limit - len(CLIP), 0)
    return text[:keep] + CLIP


# -- S6 §2.6: one bounded composer for every line the slice introduces or changes ----------

FIELD_MIN_CHARS = 8          # the least a label or a display field keeps (then CLIP)


def bounded_line(template: str, *, label: str, fields: "dict[str, Any] | None" = None,
                 limit: "int | None" = None) -> str:
    """ONE string for the notice and the resident's echo, within the echo cap:
    the literal text of *template* (the outcome clause and its connectives) is
    reserved first, then the label and the display fields — ``{label}`` and
    ``{<field>}`` placeholders — share what remains, each keeping at least
    ``FIELD_MIN_CHARS`` and clipped with the marker. A notice composed any other
    way lost its outcome when ``record_echo`` clipped it at 120 (S6 design §2.6:
    the echo-budget shape, found in three review rounds, generalised here)."""
    import result_broker as rb
    cap = rb.ECHO_LINE_MAX if limit is None else int(limit)
    values = {"label": str(label or "")}
    for key, value in (fields or {}).items():
        values[str(key)] = str(value if value is not None else "")
    literal = template
    for key in values:
        literal = literal.replace("{" + key + "}", "")
    budget = max(0, cap - len(literal))
    keys = list(values)
    floor = min(FIELD_MIN_CHARS, budget // max(1, len(keys)))
    alloc = {k: min(len(values[k]), floor) for k in keys}
    remaining = budget - sum(alloc.values())
    need = [k for k in keys if len(values[k]) > alloc[k]]
    while need and remaining > 0:
        share = max(1, remaining // len(need))
        progressed = False
        for key in list(need):
            extra = min(share, len(values[key]) - alloc[key], remaining)
            if extra > 0:
                alloc[key] += extra
                remaining -= extra
                progressed = True
            if alloc[key] >= len(values[key]):
                need.remove(key)
        if not progressed:
            break
    out = template
    for key in keys:
        value = values[key]
        out = out.replace("{" + key + "}", value if alloc[key] >= len(value) else clip(value, alloc[key]))
    return out


_FILE_OUTCOME_CLAUSES = {
    # agent_inbox.Outcome value -> the past-event clause after "could not take <name>: "
    "too_large": "over {cap} MB.",
    "mismatch": "not a {kind}.",
    "full": "inbox full ({files} files / {mb} MB).",
    "download_failed": "Telegram did not hand it over.",
    "local_path": "could not be saved; nothing kept.",
    "storage_failed": "could not be saved; nothing kept.",
}


def file_outcome(label: str, name: str, outcome: Any, *, kind: str = "file") -> "str | None":
    """The labelled, past-event line for a routed file's receipt outcome — Casa's
    own composer over ``agent_inbox.Outcome``, never Ellen's first-person
    ``_inbound_reply`` texts (S6 design §2.1). ``None`` for a stored file (the
    desk turn speaks for it); an UNCERTAIN outcome claims neither that the file
    was kept nor that it was not (INV-INBOX-005)."""
    import agent_inbox
    value = getattr(outcome, "value", outcome)
    if value == agent_inbox.Outcome.STORED.value:
        return None
    if value == agent_inbox.Outcome.UNCERTAIN.value:
        return bounded_line("{label} could not confirm that {name} was saved — it may or may not be in its inbox.",
                            label=label, fields={"name": name})
    clause = _FILE_OUTCOME_CLAUSES.get(value, "could not be saved; nothing kept.").format(
        cap=agent_inbox.CAP_BYTES // (1024 * 1024), kind=kind, files=agent_inbox.MAX_FILES,
        mb=agent_inbox.MAX_BYTES // (1024 * 1024))
    return bounded_line("{label} could not take {name}: " + clause, label=label, fields={"name": name})


@dataclasses.dataclass
class Exchange:
    who: str           # "operator" | "resident" | "specialist"
    text: str
    at: float


@dataclasses.dataclass(frozen=True)
class DeskFault:
    """S5 §14.8: why a desk is faulted — the pinned run whose termination
    stayed unconfirmed at its deadline, the plugin it ran, when."""
    run_id: str
    plugin: str
    since: float
    reason: str


class Desk:
    """One (chat, specialist) thread: the log, the activity stamp, the lock
    that serialises every use, and the queue reservation count."""

    def __init__(self, chat_id: int, role: str) -> None:
        self.chat_id = chat_id
        self.role = role
        self.log: list[Exchange] = []
        self.last_used: float | None = None
        self.lock = asyncio.Lock()
        # S5 §14.8: set when a pinned run's termination stayed unconfirmed at
        # its deadline; every later use is refused at once until restart.
        self.fault: DeskFault | None = None
        # #1197: the inner task of a use that left while it was still
        # unwinding past the bounded runner's teardown bound; the desk is
        # refused, like a faulted one, until that task has ended.
        self._unwinding: asyncio.Task | None = None
        # The queue in RESERVATION order: a use is admitted only when its
        # reservation is at the head, so a use reserved earlier but whose
        # task reached the desk later (a delegation still registering) is
        # never overtaken (arrival order = reservation order).
        self._queue: "collections.deque[Reservation]" = collections.deque()
        self._changed = asyncio.Event()

    @property
    def faulted(self) -> str | None:
        """The fault's reason, or None — the one truth every use checks.
        A run still unwinding (#1197) refuses the desk as a fault does, and
        only until it has ended: it is not a ``fault`` (no health row)."""
        if self.fault is not None:
            return self.fault.reason
        if self._unwinding is not None and not self._unwinding.done():
            return UNWINDING
        return None

    @faulted.setter
    def faulted(self, reason: str | None) -> None:
        self.fault = (None if reason is None
                      else DeskFault(run_id="", plugin="", since=DESKS.now(), reason=str(reason)))

    @property
    def waiting(self) -> int:
        return len(self._queue) + self._extra_waiting

    _extra_waiting = 0   # tests that simulate a full queue set ``waiting``

    @waiting.setter
    def waiting(self, value: int) -> None:
        self._extra_waiting = max(int(value) - len(self._queue), 0)

    # -- queue (reserved BEFORE any task exists; released on admission) ----
    def reserve(self) -> "Reservation | None":
        """One place in the queue, or ``None`` when DESK_QUEUE_MAX are
        already waiting. The reservation is released ONCE — on admission,
        or on any exit before it (a cancel while waiting, a task cancelled
        before its coroutine started: the holder's done-callback)."""
        if self.waiting >= DESK_QUEUE_MAX:
            return None
        reservation = Reservation(self)
        self._queue.append(reservation)
        return reservation

    def hold_unwinding(self, runs: list) -> None:
        """#1197: called under the lock as a use leaves, before its permit is
        released — a run the bounded runner gave up waiting for refuses the
        desk until it ends. Synchronous: no later use can enter first."""
        for run in runs:
            if not run.done():
                self._unwinding = run

    def _unreserve(self, reservation: "Reservation") -> None:
        try:
            self._queue.remove(reservation)
        except ValueError:
            return
        self._changed.set()

    @contextlib.asynccontextmanager
    async def use(self, reservation: "Reservation | None"):
        """One use of the desk: wait until *reservation* is at the head of
        the queue (an abandoned place ahead is skipped as it is released),
        take the lock, release the place, yield; the lock is released on
        every exit."""
        if reservation is not None:
            while self._queue and self._queue[0] is not reservation and not reservation.released:
                self._changed.clear()
                await self._changed.wait()
        async with self.lock:
            if reservation is not None:
                reservation.release()
            yield

    # -- the window (every call below runs under ``lock``) ------------------
    def begin_use(self, now: float) -> None:
        """The idle reset: a use that finds the desk idle past DESK_IDLE_S
        starts from an empty log — inactivity, not a sliding history."""
        if self.last_used is not None and now - self.last_used > DESK_IDLE_S:
            self.log = []

    def append(self, who: str, text: str, now: float) -> None:
        """One side of an exchange, clipped to the side cap; the window keeps
        the newest DESK_LOG_EXCHANGES entries; ``last_used`` is stamped."""
        self.log.append(Exchange(who, clip(text, DESK_LOG_SIDE_CHARS), now))
        del self.log[:-2 * DESK_LOG_EXCHANGES]        # twelve exchanges of two sides
        self.last_used = now


class Reservation:
    """A place in a desk's queue; ``release`` is idempotent."""

    def __init__(self, desk: Desk) -> None:
        self._desk = desk
        self._released = False

    def release(self, *_ignored: Any) -> None:
        if self._released:
            return
        self._released = True
        self._desk._unreserve(self)

    @property
    def released(self) -> bool:
        return self._released


class DeskBusy(Exception):
    """A desk use admitted by the lock found the specialist's permit held by
    a use outside the desk; the delegation reports ``busy``."""


class DeskFaulted(Exception):
    """A desk use admitted by the lock found the desk faulted (S5 §14.8: a
    pinned run's processes unconfirmed dead); the delegation reports
    ``desk_faulted`` and nothing runs."""


def faulted_line(label: str) -> str:
    return f"{label}'s desk is faulted; a Casa restart clears it."


def faulted_desk_issues() -> list:
    """S5 §14.8: the standing health report's rows for the faulted desks —
    one ``desk_faulted`` row per desk, against the plugin the pinned run
    ran and the specialist target, the chat and the time in the detail —
    recomputed on every regeneration, so the row stands exactly as long as
    the fault does (a restart clears both)."""
    from plugin_registry import PluginIssue
    rows = []
    for desk in DESKS.faulted():
        fault = desk.fault
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(fault.since))
        rows.append(PluginIssue(
            name=fault.plugin or "casa", target=f"specialist:{desk.role}", stage="verify",
            reason_code="desk_faulted",
            detail=f"chat {desk.chat_id}, since {when}"))
    return rows


class DeskRegistry:
    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._desks: dict[tuple[int, str], Desk] = {}
        self.now = now

    def get(self, chat_id: int, role: str) -> Desk | None:
        return self._desks.get((chat_id, role))

    def get_or_create(self, chat_id: int, role: str) -> Desk:
        desk = self._desks.get((chat_id, role))
        if desk is None:
            desk = Desk(chat_id, role)
            self._desks[(chat_id, role)] = desk
        return desk

    def faulted(self) -> list[Desk]:
        """Every faulted desk (S5 §14.8), until restart empties the registry."""
        return [d for d in self._desks.values() if d.fault is not None]


DESKS = DeskRegistry()


# #1192: the block's first line. The specialist read the earlier entries as a
# transcript of other parties relayed by the resident; the frame says whose
# conversation this is, and the labels name each party from the specialist's
# own point of view. A constant: its length is part of every block's budget.
DESK_FRAME = ("These are your own recent exchanges in this chat, oldest first — not a "
              "transcript of other parties. \"you\" is you; \"the operator\" is the person "
              "you work for in this chat; a name followed by \"on the operator's behalf\" is "
              "the chat's assistant asking you something for the operator.")
OPERATOR_LABEL = "the operator"
SPECIALIST_LABEL = "you"
RESIDENT_FALLBACK = "the assistant"


def _resident_name(name: str | None) -> str:
    """The resident's display name for a label or a frame, bounded."""
    return clip((name or "").strip() or RESIDENT_FALLBACK, DESK_NAME_CHARS)


def _label(who: str, resident_name: str) -> str:
    if who == "operator":
        return OPERATOR_LABEL
    if who == "specialist":
        return SPECIALIST_LABEL
    if who == "resident":
        return f"{resident_name}, on the operator's behalf"
    return who


def _line(exchange: Exchange, resident_name: str = RESIDENT_FALLBACK) -> str:
    stamp = time.strftime("%H:%M", time.localtime(exchange.at))
    return f"[{stamp}] {_label(exchange.who, resident_name)}: {exchange.text}"


def render_block(exchanges: list[Exchange], budget: int = DESK_LOG_CHARS,
                 resident_name: str | None = None) -> str:
    """The ``<desk>`` block — the frame line, then one labelled line per
    side — budgeted constructively: whole oldest exchanges are dropped until
    the rendered block fits *budget*; a lone newest exchange that alone
    exceeds it is clipped to fit. Empty for no log, and empty when *budget*
    cannot hold the tags and the frame (never an overrun)."""
    name = _resident_name(resident_name)
    head, tail = "<desk>\n" + DESK_FRAME + "\n", "\n</desk>"
    kept = list(exchanges)
    while kept:
        block = head + "\n".join(_line(e, name) for e in kept) + tail
        if len(block) <= budget:
            return block
        if len(kept) > 2:
            kept = kept[2:]                       # a whole oldest exchange (two sides)
        elif len(kept) == 2:
            kept = kept[1:]
        else:
            room = budget - len(head) - len(tail)
            if room <= 0:
                return ""
            return head + clip(_line(kept[0], name), room) + tail
    return ""


def turn_frame(resident_name: str | None, continuation: bool = False) -> str:
    """#1192: the desk turn's opening context line — the task is the
    operator's own message to the specialist, now, the same operator as in
    the exchanges shown, not something the resident relayed. An approval
    continuation's task is Casa's note of the operator's decision, not the
    operator's words, and its frame says that instead."""
    name = _resident_name(resident_name)
    if continuation:
        return ("The task above is Casa's note of the operator's decision on your request, "
                "made in this chat now — the same operator as in your earlier exchanges, "
                f"if any are shown. {name} did not write or relay it.")
    return ("The task above is a message from the operator, written to you directly in "
            "this chat now, as a reply to you — the same operator as in your earlier "
            f"exchanges, if any are shown. {name} did not write or relay it.")


def fit_block_for_delegation(exchanges: list[Exchange], context_len: int,
                             resident_name: str | None = None) -> str:
    """§8: the block fitted into what remains of DESK_CONTEXT_CHARS after the
    resident's own (already validated) context and the framing — newest
    exchanges kept, possibly none."""
    budget = DESK_CONTEXT_CHARS - int(context_len) - DESK_FRAMING_CHARS
    if budget <= 0:
        return ""
    return render_block(exchanges, budget=min(budget, DESK_LOG_CHARS),
                        resident_name=resident_name)


def label_for(role: str) -> str:
    """The specialist's label — the same ``📊 <display name>`` a post carries."""
    import result_broker as rb
    return rb.post_label(role)


def is_specialist(cfg: Any) -> bool:
    """A desk is a specialist's thread (§3, §8): the loaded role's ``kind`` —
    required at load to be resident, specialist or executor — is
    ``specialist``. A resident the chat's resident may delegate to holds no
    desk; a reply on its post is today's path."""
    return getattr(cfg, "kind", "") == "specialist"


def desk_target_ok(resident_role: str, role: str) -> bool:
    """The route's live ACL: *role* is a specialist the chat's resident
    currently declares as a delegate AND is dispatchable now — the same map
    the delegation ACL and the ``<delegates>`` block read."""
    import tools as tools_mod
    if not role or role == resident_role:
        return False
    cfg = tools_mod._agent_role_map.get(role)
    if cfg is None or not is_specialist(cfg):
        return False
    return bool(tools_mod.declares_delegate(resident_role, role))


# -- §6: the resident's echo -------------------------------------------------

def _echo_ledger():
    import result_broker as rb
    return rb.PostLedger(max_events=64)


DESK_ECHO = None  # created lazily below (result_broker imports tools lazily)


def _ledger():
    global DESK_ECHO
    if DESK_ECHO is None:
        DESK_ECHO = _echo_ledger()
    return DESK_ECHO


def outcome_phrases(kinds) -> list[str]:
    """The distinct echo phrases for the outcome *kinds*, in first-seen order."""
    phrases: list[str] = []
    for kind in kinds:
        phrase = OUTCOME_ECHO.get(kind, OUTCOME_ECHO_OTHER)
        if phrase not in phrases:
            phrases.append(phrase)
    return phrases


def turn_outcomes(turn_id: str, posts, scope) -> tuple[bool, list[str]]:
    """§5.6: what the turn proved to the operator, from the two records that
    exist by design — the post map (every message Casa posted for the turn,
    whatever the slot's kind) and the scope's own delivered sends. Returns
    (proven, the kinds that carry no S3 echo event of their own): a post the
    S3 ledger already describes is echoed by its line, not twice."""
    import result_broker as rb
    landed = rb.POST_MAP.owned(turn_id)
    # a send the sender confirmed is what the operator saw; a later one that
    # failed does not erase it (unlike the resident's closing-silence rule,
    # which asks for every send — that rule is the resident's, not the desk's)
    delivered = [send for send in scope.operator_sends if send.delivered]
    evented = {event.tool_use_id for event in posts}
    kinds = [r.kind for r in landed if r.tool_use_id not in evented]
    kinds.extend(send.intent for send in delivered)
    return bool(posts or landed or delivered), kinds


def record_echo(chat_id: int, line: str) -> None:
    """One body-free Casa line for the chat's resident, drained at its next
    turn; nothing for a chat id that is not a positive int."""
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id <= 0:
        return
    import result_broker as rb
    _ledger().record(f"{ECHO_OWNER_PREFIX}{chat_id}", clip(line, rb.ECHO_LINE_MAX))


def drain_echo_lines(chat_id: int) -> list[str]:
    """Read-and-clear: at most ECHO_MAX_LINES lines then ``…and N more.``."""
    import result_broker as rb
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id <= 0:
        return []
    events = _ledger().drain(f"{ECHO_OWNER_PREFIX}{chat_id}")
    lines = [str(e) for e in events[:rb.ECHO_MAX_LINES]]
    if len(events) > rb.ECHO_MAX_LINES:
        lines.append(f"…and {len(events) - rb.ECHO_MAX_LINES} more.")
    return lines


def prompt_prefix(chat_id: Any) -> str:
    """What the resident's next prompt starts with: the drained lines, each
    marked as Casa's, then a blank line — or nothing."""
    try:
        chat = int(chat_id)
    except (TypeError, ValueError):
        return ""
    lines = drain_echo_lines(chat)
    if not lines:
        return ""
    return "".join(f"{PROMPT_PREFIX}{line}\n" for line in lines) + "\n"


# -- §8: the resident's delegations are uses of the desk too ------------------

def desk_for_delegation(origin: dict, cfg: Any, agent_name: str) -> Desk | None:
    """The desk a resident's delegation uses, or ``None``: an authenticated
    operator's Telegram DM turn (the reserved ``_operator_turn`` marker, a
    canonical chat id) delegating to a specialist. A scheduled, webhook,
    engagement or voice turn, a job, or a non-specialist target: no desk."""
    from provenance import strict_positive_id
    origin = origin or {}
    if origin.get("_operator_turn") is not True or origin.get("channel") != "telegram":
        return None
    if origin.get("desk") is not None:
        return None
    if origin.get("synthetic") is not None:
        # provenance's rule for a `dm` transport: no synthetic marker — a
        # button continuation or a setup turn delegates as in v0.340.0
        return None
    chat = strict_positive_id(origin.get("chat_id"))
    if chat is None or not agent_name:
        return None
    if not is_specialist(cfg):
        return None
    return DESKS.get_or_create(chat, agent_name)


async def delegation_use(desk: Desk, *, reservation: "Reservation | None", run: Callable[..., Any],
                         cfg: Any, task_text: str, context_text: str, scope: str = "",
                         resolution: Any = None, output_format: Any = None) -> Any:
    """One delegation as one use of *desk* (§8): the lock first (the queue
    reservation released on admission — or on any earlier exit), the idle
    check and the block read under it, the block fitted after the resident's
    validated context, the specialist's permit acquired AFTER the lock (never
    waited for: a refusal is ``DeskBusy``, the tool's typed busy result), the
    run, the exchange committed at the clock of completion, the permit
    released inside the lock. The runner's output is returned unchanged for
    the caller's own classification."""
    import tools as tools_mod
    # the caller the runner itself names ("Context from <caller>"), read at
    # entry, before any await, from the task's own origin snapshot
    caller = str(tools_mod._snapshot_origin().get("role") or "")
    resident_name = tools_mod._display_name_for_role(caller) if caller else None
    try:
        async with desk.use(reservation):
            if desk.faulted:
                # a delegation queued before the fault must not run
                raise DeskFaulted(f"{cfg.role!r}'s desk is faulted")
            now = DESKS.now()
            desk.begin_use(now)
            block = fit_block_for_delegation(desk.log, len(context_text or ""),
                                             resident_name=resident_name)
            if block and context_text:
                context = f"{context_text}\n\n{block}"
            else:
                context = block or context_text
            permit = None
            limiter = tools_mod._specialist_limiter
            if limiter is not None and scope:
                permit = limiter.try_acquire(scope)
                if permit is None:
                    raise DeskBusy(f"{cfg.role!r} is busy outside its desk")
            output = None
            runs: list = []
            sink_tok = tools_mod._desk_run_sink.set(runs)
            try:
                output = await run(cfg, task_text, context, resolution=resolution,
                                   output_format=output_format)
            finally:
                tools_mod._desk_run_sink.reset(sink_tok)
                desk.hold_unwinding(runs)     # #1197: before the permit and the lock
                if permit is not None:
                    permit.release()          # inside the lock, before it is released
                done = DESKS.now()
                if output is not None:
                    text, failure = _outcome_text(output)
                else:
                    text, failure = None, "failed"
                desk.append("resident", task_text, done)
                desk.append("specialist", text if failure is None and text else NO_REPLY, done)
            return output
    finally:
        if reservation is not None:
            reservation.release()


# -- §5: the desk turn -------------------------------------------------------

def _desk_origin(*, resident_role: str, desk_role: str, chat_id: int, user_id: int,
                 user_name: str, message_id: Any, cid: str, text: str, turn_id: str) -> dict:
    """The DM's own Casa-owned context plus the three fields the classifier
    needs, the resident's role, the specialist as the executing role, depth 1
    (as after any delegation: the specialist cannot delegate onward; the
    runner's own increment only deepens it), the turn id as the quota key and echo owner, the
    operator's words raw, and the reserved desk marker."""
    return {
        "role": resident_role,
        "execution_role": desk_role,
        "channel": "telegram",
        "source": "telegram",
        "message_type": "channel_in",
        "chat_id": chat_id,
        "user_id": user_id,
        "user_name": user_name,
        "message_id": str(message_id) if message_id is not None else "",
        "cid": cid,
        "user_text": text,
        "delegation_depth": 1,
        "_delegation_id": turn_id,
        "_origin_route": "telegram",
        "_operator_turn": True,
        "desk": {"role": desk_role, "chat_id": chat_id},
    }


def _compose_context(block: str, quoted_text: str | None, record: Any, now: float,
                     resident_name: str | None = None, continuation: bool = False) -> str:
    """The desk turn's context: the turn frame (#1192) first, then the block,
    then the quote — at most DESK_CONTEXT_CHARS by construction (frame and
    quote header within DESK_FRAMING_CHARS), the slice only a backstop."""
    parts = [turn_frame(resident_name, continuation)]
    if block:
        parts.append(block)
    if quoted_text is not None and record is not None:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(getattr(record, "posted_at", now)))
        parts.append(
            f"The operator replied to your post (slot {getattr(record, 'slot', '?')}, "
            f"posted {when}) which read:\n{clip(quoted_text, DESK_QUOTE_CHARS)}")
    context = "\n\n".join(parts)
    return context[:DESK_CONTEXT_CHARS]


def _outcome_text(output: Any) -> tuple[str | None, str | None]:
    """``(text, failure_kind)``: the runner's output classified exactly as
    ``delegate_to_agent`` classifies it — a CLI-aborted run yields no text."""
    import specialist_limits
    import tools as tools_mod
    if getattr(output, "run_aborted", False):
        return None, tools_mod._run_abort_kind(getattr(output, "run_subtype", None))
    text, _truncated = specialist_limits.truncate_output(str(getattr(output, "text", "") or ""))
    return text, None


async def handle_reply(
    *, channel: Any, resident_role: str, chat_id: int, user_id: int, user_name: str,
    message_id: Any, cid: str, text: str, quoted_text: str | None, record: Any,
    desk_role: str, continuation: bool = False, reservation: "Reservation | None" = None,
) -> None:
    """One desk use: the queue place (handed over by the route, or taken
    here), the desk lock, the idle reset, the log read, the specialist run on
    the operator's words with the desk block and the quote as context, the
    classification, the labelled reply or the one notice, the exchange
    committed at the clock of completion, the resident's echo — the permit
    released inside the lock, the reservation and the typing lease released
    on every exit."""
    import result_broker as rb
    import tools as tools_mod
    from channels import DeliveryOutcome
    from channels.tg_richtext import render_paged
    from output_boundary import IntentKind, TurnScope, strips_to_silence

    label = label_for(desk_role)
    context = {"chat_id": str(chat_id), "cid": cid}
    desk = DESKS.get_or_create(chat_id, desk_role)

    async def _notice(line: str) -> None:
        try:
            await channel.deliver_desk_notice(chat_id, line)
        except Exception as exc:  # noqa: BLE001 — nothing more is attempted
            logger.warning("desk notice failed: %s", type(exc).__name__)
        record_echo(chat_id, line)

    if reservation is None:
        if desk.faulted:
            await _notice(faulted_line(label))
            channel._release_typing(context, str(chat_id))
            return
        reservation = desk.reserve()
        if reservation is None:
            await _notice(f"{label} is busy; try again in a moment.")
            channel._release_typing(context, str(chat_id))
            return
    try:
        async with desk.use(reservation):
            if desk.faulted:
                # S5 §14.8: a reply queued before the fault finds it here,
                # before the idle reset, the run and any permit
                await _notice(faulted_line(label))
                return
            now = DESKS.now()
            desk.begin_use(now)
            resident_name = tools_mod._display_name_for_role(resident_role)
            block = render_block(desk.log, resident_name=resident_name)
            cfg = tools_mod._agent_role_map.get(desk_role)
            if cfg is None:
                await _notice(f"{label} could not continue (not delegable).")
                return
            turn_id = uuid.uuid4().hex
            task_text = clip(text or "", DESK_TASK_CHARS)
            origin = _desk_origin(
                resident_role=resident_role, desk_role=desk_role, chat_id=chat_id,
                user_id=user_id, user_name=user_name, message_id=message_id, cid=cid,
                text=task_text, turn_id=turn_id)
            origin["turn_scope"] = TurnScope.for_desk(
                origin, display_name=tools_mod._display_name_for_role(desk_role))
            context_text = _compose_context(block, quoted_text, record, now,
                                            resident_name=resident_name,
                                            continuation=continuation)
            # the permit AFTER the lock; it never waits
            permit = None
            limiter = tools_mod._specialist_limiter
            if limiter is not None:
                permit = limiter.try_acquire(tools_mod._delegation_scope(origin, desk_role))
                if permit is None:
                    await _notice(f"{label} is busy; try again in a moment.")
                    return
            output, failure = None, None
            runs: list = []
            try:
                import agent as agent_mod
                ov = agent_mod.origin_var.set(origin)
                qk = tools_mod._delegation_quota_key.set(turn_id)
                sk = tools_mod._desk_run_sink.set(runs)
                try:
                    run = asyncio.create_task(
                        tools_mod._run_delegated_agent_bounded(cfg, task_text, context_text))
                finally:
                    agent_mod.origin_var.reset(ov)
                    tools_mod._delegation_quota_key.reset(qk)
                    tools_mod._desk_run_sink.reset(sk)
                output = await run
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the class, never the text
                failure = tools_mod._classify_error(exc).value
                logger.warning("desk turn for %s failed: %s", desk_role, type(exc).__name__)
            finally:
                desk.hold_unwinding(runs)     # #1197: before the permit and the lock
                if permit is not None:
                    permit.release()          # inside the lock, before it is released
            if failure is None:
                reply_text, failure = _outcome_text(output)
            posts = rb.POSTS.drain(turn_id)
            scope = origin["turn_scope"]
            proven, unevented = turn_outcomes(turn_id, posts, scope)
            specialist_side = NO_REPLY
            if failure is not None:
                await _notice(f"{label} could not handle your reply ({failure}).")
            elif strips_to_silence(reply_text):
                if proven:
                    specialist_side = POSTED_VIEW
                    # every outcome without an S3 line of its own gets one
                    # Casa line per kind, named from the records, never
                    # from the bodies
                    for phrase in outcome_phrases(unevented):
                        record_echo(chat_id, f"{label} {phrase}")
                else:
                    await _notice(f"{label} had nothing to add.")
            else:
                admitted = scope.admit(IntentKind.FINAL_REPLY, reply_text)
                labelled = admitted.with_text(f"{label}\n{admitted}")
                post = rb.PostRecord(role=desk_role, operator_id=user_id, plugin="",
                                     slot="desk", tool_use_id=turn_id, owner=turn_id,
                                     posted_at=DESKS.now(), kind="desk_reply")
                delivered = False
                try:
                    outcome = await channel.send_response(labelled, {**context, "_post": post})
                    delivered = outcome is DeliveryOutcome.DELIVERED
                except Exception as exc:  # noqa: BLE001 — not proven
                    logger.warning("desk reply send failed: %s", type(exc).__name__)
                specialist_side = reply_text
                if delivered:
                    pages = len(render_paged(labelled))
                    record_echo(chat_id, f"{label} answered your reply "
                                         f"({pages} page{'s' if pages != 1 else ''}).")
                else:
                    await _notice(f"{label} answered; the reply did not go out.")
            # the exchange and the idle clock are stamped once the use has
            # settled — delivery or notice included — never at its start
            done = DESKS.now()
            desk.append("operator", task_text, done)
            desk.append("specialist", specialist_side, done)
            for line in rb.echo_lines(posts):
                record_echo(chat_id, line)
    finally:
        reservation.release()                 # idempotent: a cancel while waiting
        channel._release_typing(context, str(chat_id))


# -- S5 §5: the tap's desk use — the pinned one-call turn -------------------

def _tap_prompt(label: str, runtime_name: str, canonical: str) -> str:
    """§5.2.3: the only text the model is given besides its role prompt and
    the desk block. The model is the hands; the pin enforces the one call."""
    return (f'[casa stored call] The operator tapped "{label}" on your proposal. '
            f"Call the tool `{runtime_name}` exactly once, with exactly these arguments "
            f"and nothing else: {canonical}. Then stop. Do not call any other tool and do "
            f"not write anything to the operator — Casa posts the tool's result.")


def _assigned_plugin(resolution: Any, seg: str) -> Any:
    """The resolved plugin whose runtime segment is *seg*, or None."""
    from plugin_registry import runtime_name as _runtime_name
    from text_util import sanitize_segment
    for rp in getattr(resolution, "plugins", None) or []:
        if sanitize_segment(_runtime_name(rp)) == seg:
            return rp
    return None


def _profile_refuses(plan: Any, runtime_name: str) -> bool:
    """§4.3: when the plan has an entry whose prefixes cover the name, the
    name must be in its allowed names (S8's per-prefix guard); no entry, no
    further restriction."""
    for entry in getattr(plan, "entries", None) or ():
        if runtime_name.startswith(tuple(entry.prefixes)):
            return runtime_name not in entry.allowed_names
    return False


async def handle_tap(
    *, channel: Any, resident_role: str, chat_id: int, user_id: int, cid: str,
    desk_role: str, meta: dict, idx: int, request_id: str, reservation: "Reservation | None",
) -> None:
    """One desk use for a committed tap (§5.2, ``handle_reply``'s sibling):
    under the lock the faulted check, the re-checks of §4.3 on ONE captured
    build input and the deadline; the permit after the lock; the pinned run
    created DIRECTLY as a task (never the bounded wrapper) under the
    ``PinnedRun`` ContextVar, the ceiling driven here; the outcome — a
    validated capture is the receipt, authoritative over an aborted turn;
    anything else is one labelled notice; the model's text is discarded —
    the exchange ``[tapped: <label>]`` and one body-free echo line. The
    reservation, the typing lease and the permit are released on every exit.
    """
    import agent as agent_mod
    import plugin_erasure
    import result_broker as rb
    import tools as tools_mod
    from channels import DeliveryOutcome
    from output_boundary import IntentKind, TurnScope
    from pinned_run import PinnedRun
    from stored_calls import stored_call_still_ok

    label = label_for(desk_role)
    context = {"chat_id": str(chat_id), "cid": cid}
    desk = DESKS.get_or_create(chat_id, desk_role)
    calls, options = list(meta.get("calls") or []), list(meta.get("options") or [])
    call = dict(calls[idx]) if 0 <= idx < len(calls) else {}
    button = str(options[idx]) if 0 <= idx < len(options) else "?"
    runtime = str(call.get("runtime_name") or "")
    canonical = str(call.get("canonical") or "")
    seg = str(meta.get("plugin_seg") or "")

    async def _notice(line: str) -> None:
        try:
            await channel.deliver_desk_notice(chat_id, line)
        except Exception as exc:  # noqa: BLE001 — nothing more is attempted
            logger.warning("desk notice failed: %s", type(exc).__name__)

    async def _mark(line: str) -> None:
        mark = getattr(channel, "mark_proposal", None)
        if mark is None:
            return
        try:
            await mark(meta, line)
        except Exception as exc:  # noqa: BLE001 — the keyboard line is cosmetic
            logger.warning("proposal mark failed: %s", type(exc).__name__)

    async def _refuse(reason: str) -> None:
        """§10: the keyboard line, one notice, the refused echo line; not a use."""
        await _mark(f"✖ {reason}")
        await _notice(f"{label} could not apply your tap ({reason}).")
        record_echo(chat_id, f"{label} refused your tap ({button}): {reason}.")

    async def _tell_faulted() -> None:
        line = faulted_line(label)
        await _notice(line)
        record_echo(chat_id, line)

    try:
        async with desk.use(reservation):
            if desk.faulted:
                await _mark("✖ faulted")
                await _tell_faulted()
                return
            # §4.3: the re-checks, on one captured build input, under the lock
            if not desk_target_ok(resident_role, desk_role):
                await _refuse("not delegable")
                return
            cfg = tools_mod._agent_role_map.get(desk_role)
            build = tools_mod._capture_build_input(cfg)
            rp = _assigned_plugin(build.resolution, seg)
            if rp is None:
                await _refuse("plugin unassigned")
                return
            if str(getattr(rp, "artifact_id", "") or "") != str(meta.get("artifact_id") or ""):
                await _refuse("plugin changed")
                return
            if _profile_refuses(build.plan, runtime):
                await _refuse("profile")
                return
            if plugin_erasure.FENCE.fenced(str(getattr(rp, "name", "") or "")):
                await _refuse("plugin erasing")
                return
            why = stored_call_still_ok(runtime, contract_map=build.contract_map,
                                       protected=build.protected)
            if why is not None:
                await _refuse(why)
                return
            deadline = meta.get("deadline")
            if (not isinstance(deadline, (int, float))
                    or asyncio.get_running_loop().time() >= deadline):
                await _mark("⌛ expired")           # no notice, no exchange
                return
            now = DESKS.now()
            desk.begin_use(now)
            resident_name = tools_mod._display_name_for_role(resident_role)
            block = render_block(desk.log, resident_name=resident_name)
            run_id = uuid.uuid4().hex
            tapped = f"[tapped: {button}]"
            origin = _desk_origin(
                resident_role=resident_role, desk_role=desk_role, chat_id=chat_id,
                user_id=user_id, user_name="", message_id=meta.get("message_id"), cid=cid,
                text=tapped, turn_id=run_id)
            origin["stored_call"] = {"run_id": run_id, "runtime_name": runtime,
                                     "canonical": canonical, "label": button}
            origin["turn_scope"] = TurnScope.for_desk(
                origin, display_name=tools_mod._display_name_for_role(desk_role))
            task_text = _tap_prompt(button, runtime, canonical)
            context_text = _compose_context(block, None, None, now, resident_name=resident_name)
            # the permit AFTER the lock; it never waits
            permit = None
            limiter = tools_mod._specialist_limiter
            if limiter is not None:
                permit = limiter.try_acquire(tools_mod._delegation_scope(origin, desk_role))
                if permit is None:
                    await _mark("✖ busy")
                    await _notice(f"{label} is busy; the specialist will propose again, "
                                  "or type your verdict.")
                    record_echo(chat_id, f"{label} refused your tap ({button}): busy.")
                    return
            owner = PinnedRun(run_id=run_id, runtime_name=runtime, canonical=canonical,
                              label=button, build_input=build)
            plugin_name = str(getattr(rp, "name", "") or "")
            failure: str | None = None
            told_failed = False
            run = None
            mode = "normal"
            pending_cancel: BaseException | None = None

            async def _tell_failed(kind: str) -> None:
                nonlocal told_failed
                await _refuse_failed(_mark, _notice, label, button, chat_id, kind)
                told_failed = True

            async def _on_alive() -> None:
                nonlocal failure
                failure = "processes alive"
                await _tell_failed(failure)

            def _disclose_cancelled(capture) -> None:
                """A cancelled tap whose call RAN (a capture exists): the
                receipt cannot be posted on a stopping channel — an ERROR
                names the run, the exchange is logged and the resident's line
                records the applied tap; nothing is silent."""
                logger.error("stored call %s: the tap was cancelled after its call ran; the "
                             "receipt was not posted", run_id)
                side = (clip(str(capture.text or ""), STORED_CALL_RECEIPT_CHARS).split("\n", 1)[0]
                        if capture.kind in ("receipt", "no_post") else POSTED_PROPOSAL)
                stamp = DESKS.now()
                desk.append("operator", tapped, stamp)
                desk.append("specialist", side, stamp)
                record_echo(chat_id, f"{label} applied your tap ({button})"
                                     f"{TELL_ECHO if capture.rewritten else ''}.")

            try:
                ov = agent_mod.origin_var.set(origin)
                qk = tools_mod._delegation_quota_key.set(run_id)
                pk = tools_mod._pinned_run.set(owner)
                try:
                    run = asyncio.create_task(tools_mod._run_delegated_agent(
                        cfg, task_text, context_text, resolution=build.resolution))
                finally:
                    agent_mod.origin_var.reset(ov)
                    tools_mod._delegation_quota_key.reset(qk)
                    tools_mod._pinned_run.reset(pk)
                _done, pending = await asyncio.wait({run}, timeout=tools_mod._DELEGATION_CEILING_S)
                if pending:
                    # §5.2.4: the notice at once; the desk and the permit stay
                    # held through the bounded termination path
                    mode = "ceiling"
                    failure = "timed out"
                    await _tell_failed(failure)
                else:
                    exc = run.exception()
                    if exc is not None:
                        failure = tools_mod._classify_error(exc).value
                        logger.warning("pinned run for %s failed: %s", desk_role,
                                       type(exc).__name__)
                    else:
                        _text, failure = _outcome_text(run.result())    # the text is discarded
            except asyncio.CancelledError as exc:
                mode = "cancel"
                pending_cancel = exc
            except Exception as exc:  # noqa: BLE001 — the launch itself failed: settle as a normal end
                failure = tools_mod._classify_error(exc).value
                logger.warning("pinned run for %s could not start: %s", desk_role, type(exc).__name__)
            # THE one release path — the normal end, the ceiling and a
            # cancellation alike (BRAIN, diff round 2): the confirmation, the
            # fault decision, the transcript delete, the fd close and the
            # permit release happen together, in one function, shielded from
            # the cancellation; nothing in this turn releases by another route.
            _confirmed, interrupted = await _shielded(_settle_pinned_run(
                owner, run, mode, permit=permit, desk=desk, desk_role=desk_role, chat_id=chat_id,
                run_id=run_id, plugin_name=plugin_name, on_alive=_on_alive,
                tell_faulted=_tell_faulted))
            if pending_cancel is not None or interrupted:
                # cancelled before or DURING the settle (Terra, diff round 4): the
                # turn does not continue — a receipt captured meanwhile is
                # disclosed, never posted as if nothing had happened
                if owner.captured is not None and owner.captured.kind in ("receipt", "no_post", "delivered"):
                    _disclose_cancelled(owner.captured)
                if pending_cancel is not None:
                    raise pending_cancel
                raise asyncio.CancelledError()
            # §5.2.5/§5.4/§10: the outcome
            rb.POSTS.drain(run_id)
            capture = owner.settle(failure or "no_call")
            tell = capture.rewritten
            applied = f"{label} applied your tap ({button}){TELL_ECHO if tell else ''}."
            specialist_side = NO_RECEIPT
            try:
                if capture.kind in ("receipt", "no_post"):
                    receipt = clip(str(capture.text or ""), STORED_CALL_RECEIPT_CHARS)
                    body = f"{TELL_LINE}\n{receipt}" if tell else receipt
                    admitted = origin["turn_scope"].admit(IntentKind.FINAL_REPLY, body)
                    labelled = admitted.with_text(f"{label}\n{admitted}")
                    post = rb.PostRecord(role=desk_role, operator_id=user_id, plugin=seg,
                                         slot="receipt", tool_use_id=run_id, owner=run_id,
                                         posted_at=DESKS.now(), kind="receipt")
                    delivered = False
                    try:
                        outcome = await channel.send_response(labelled, {**context, "_post": post})
                        delivered = outcome is DeliveryOutcome.DELIVERED
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — not proven
                        logger.warning("receipt send failed: %s", type(exc).__name__)
                    specialist_side = receipt.split("\n", 1)[0]
                    if not delivered:
                        await _notice(f"{label} applied your tap; the receipt did not go out.")
                    record_echo(chat_id, applied)
                elif capture.kind == "delivered":
                    specialist_side = POSTED_PROPOSAL        # the landed proposal is the receipt
                    record_echo(chat_id, applied)
                else:
                    if capture.kind == "error":
                        kind = capture.text or "error"
                    elif capture.kind == "withheld":
                        kind = capture.text or "not delivered"
                    else:
                        kind = failure or "no_call"
                    if not told_failed:
                        await _tell_failed(kind)
            except asyncio.CancelledError:
                # a cancellation during the telling: an executed call is never
                # silent (Astra, diff round 2)
                if capture.kind in ("receipt", "no_post", "delivered"):
                    _disclose_cancelled(capture)
                raise
            done = DESKS.now()
            desk.append("operator", tapped, done)
            desk.append("specialist", specialist_side, done)
    finally:
        if reservation is not None:
            reservation.release()             # idempotent: a cancel while waiting
        channel._release_typing(context, str(chat_id))


async def _settle_pinned_run(owner: Any, run: "asyncio.Task | None", mode: str, *, permit: Any,
                             desk: Desk, desk_role: str, chat_id: int, run_id: str,
                             plugin_name: str, on_alive: Callable[[], Any],
                             tell_faulted: Callable[[], Any]) -> bool:
    """THE one release path of a pinned run (BRAIN, diff round 2 — "same
    shape twice: cut the mechanism"). Called once per run, for the normal
    end (``mode == "normal"``: the controller's orderly close, then the
    termination path if a pinned process is still alive, told first through
    *on_alive*), the ceiling and a cancellation (``"ceiling"`` / ``"cancel"``:
    the termination path on the execution task). Its only input is the
    confirmation predicate — every pinned fd's own pidfd readable, every
    callback drained, every identity established — and on it, together and
    in this order: the fault decision (unconfirmed, or a teardown itself
    cancelled by a loop shutdown ⇒ the desk is faulted, told, and the health
    report refreshed), the pinned run's transcript delete, the fd close and
    the permit release. Nothing else in ``handle_tap`` performs any of these
    (pinned structurally in tests/test_desk_tap.py)."""
    import tools as tools_mod
    confirmed = False
    try:
        try:
            if mode == "normal":
                confirmed = await owner.finish()
                if not confirmed:
                    await on_alive()
                    confirmed = await owner.terminate()
            else:
                confirmed = await owner.terminate(run)
        except asyncio.CancelledError:
            confirmed = False               # the teardown itself was cancelled: unconfirmed
        if not confirmed:
            desk.fault = DeskFault(run_id=run_id, plugin=plugin_name, since=DESKS.now(),
                                   reason=f"run {run_id}: termination unconfirmed ({mode})")
            logger.error("desk %s/%s faulted: run %s termination unconfirmed (%s)",
                         chat_id, desk_role, run_id, mode)
            try:
                await tell_faulted()
                await tools_mod._refresh_plugin_health_live()   # the report says so at once
            except asyncio.CancelledError:
                logger.warning("faulted-desk telling interrupted by a cancellation")
            except Exception as exc:  # noqa: BLE001 — the next regeneration carries it
                logger.warning("faulted-desk telling failed: %s", type(exc).__name__)
        late = False
        if owner.transcript is not None:
            # after termination is CONFIRMED — the runner's own delete assumes
            # the client exited before it (INV-ENG-023); an unconfirmed writer
            # may still flush, so its delete is deferred to a detached waiter
            # that runs after the pinned fds report exit (Astra, diff round 3)
            sid, directory = owner.transcript
            if confirmed:
                try:
                    await tools_mod._delete_own_delegated_transcript(sid, directory, desk_role)
                except asyncio.CancelledError:
                    logger.warning("pinned run %s: transcript delete interrupted by a cancellation",
                                   run_id)
            else:
                owner.schedule_after_exit(
                    lambda: tools_mod._delete_own_delegated_transcript(sid, directory, desk_role))
                late = True
    finally:
        if not late:
            owner.close_fds()               # a deferred waiter owns the fds otherwise
        if permit is not None:
            permit.release()                # inside the desk lock, before it is released
    return confirmed


async def _shielded(coro) -> "tuple[Any, bool]":
    """Run *coro* to completion although the caller is being cancelled: the
    work is a task of its own, awaited through a shield, and a repeated
    cancellation only re-arms the wait — the coroutine is bounded by its
    own deadlines, never by the caller's. Returns ``(result, interrupted)``:
    *interrupted* says a cancellation reached the caller meanwhile, so the
    caller can honour it once the work is done (Terra, diff round 4: a
    cancellation absorbed here must never let the turn continue as if it had
    not happened)."""
    task = asyncio.ensure_future(coro)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # the caller is being cancelled: re-arm the wait for the work
            # (Terra, diff round 2: a cancellation landing as the work finishes
            # must not lose its result)
            interrupted = True
            continue
    # the work's own result — or its own cancellation, when the work's task
    # was itself cancelled (a loop shutdown): never a spin, never a swallow
    return task.result(), interrupted


async def _refuse_failed(mark, notice, label: str, button: str, chat_id: int, kind: str) -> None:
    """§10's ``✖ failed`` row: the keyboard line, the notice, the refused echo."""
    await mark("✖ failed")
    await notice(f"{label} could not apply your tap ({kind}).")
    record_echo(chat_id, f"{label} refused your tap ({button}): {kind}.")
