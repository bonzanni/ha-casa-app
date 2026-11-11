"""The specialist desk (S4): the thread one specialist keeps with one chat.

A swipe-reply the operator sends on a message Casa posted for a specialist
(an ``operator_message``/``operator_file``/``operator_link`` post, or a desk
reply) reaches that specialist as ONE desk turn carrying the operator's exact
words — never a resident turn (INV-DESK-005). The configurator holds a desk
the same way (:func:`has_desk`). The desk is a bounded dialogue
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

A desk's lock, queue and fault are its specialist's: one lane and one fault
per executing agent (:mod:`agent_lane`), shared by every chat and by every
other use of that agent. A tap on a stored-call button is settled by
:mod:`stored_call_settler`. The names this module gave them stay, for their
callers.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
import uuid
from typing import Any, Callable

import agent_lane
from agent_lane import LaneBusy, LaneFaulted, Reservation
from stored_call_settler import (  # noqa: F401 — the settler's names, kept for their callers
    NO_RECEIPT, OPERATOR_FILE, POSTED_FILE, POSTED_PROPOSAL, STORED_CALL_RECEIPT_CHARS,
    TELL_ECHO, TELL_LINE, UNCONFIRMED, _assigned_plugin, _outcome_text, _post_next_card,
    _posted, _profile_refuses, _refuse_failed, _settle_pinned_run, _shielded, _tap_prompt,
    handle_tap, keeps_card)

logger = logging.getLogger(__name__)

DESK_LOG_EXCHANGES = 12
DESK_LOG_SIDE_CHARS = 400
DESK_LOG_CHARS = 10_000
DESK_IDLE_S = 3600.0
DESK_QUEUE_MAX = agent_lane.QUEUE_MAX
DESK_TASK_CHARS = 4096
DESK_QUOTE_CHARS = 2000
DESK_CONTEXT_CHARS = 12_500
DESK_FRAMING_CHARS = 500
DESK_NAME_CHARS = 40          # a resident's display name inside a label or a frame
CLIP = "[…]"
POSTED_VIEW = "[posted a view]"
NO_REPLY = "[no reply]"
ECHO_OWNER_PREFIX = "desk:"
UNWINDING = agent_lane.UNWINDING   # #1197: the desk's refusal reason
# #1198: a swipe-reply starting with "/" reaches the specialist as text — the
# route runs ahead of the /new interception — and the desk turn's context says so.
# It is framing: with the longest frame and quote header it stays within
# DESK_FRAMING_CHARS, so it never costs the block or the quote a character.
SLASH_TASK_LINE = ('The "/" task is plain text: Casa did not run it as a command and reset '
                   "nothing; earlier exchanges stand.")
PROMPT_PREFIX = "(front desk) "
# §5.6/§6: the body-free echo line for an outcome of a silent turn that has
# no S3 echo event of its own, keyed by the outcome's kind — a landed post's
# delivery kind (`PostRecord.kind`) or a send's intent (`OperatorSend.intent`)
OUTCOME_ECHO = {"operator_link": "posted a link to your chat.",
                "media": "sent you a file.", "caption": "sent you a file.",
                "keyboard": "asked you a question.", "discrete": "sent you a message."}
OUTCOME_ECHO_OTHER = "sent you something."


import chat_notes  # noqa: E402
from chat_notes import FIELD_MIN_CHARS, clip  # noqa: E402,F401 — re-exported


# -- #1314: a reply the desk does not take ----------------------------------------

REPLY_QUOTE_CHARS = 600


def reply_note(who: str, posted: str | None, quoted_text: str | None) -> str:
    """The Casa note the resident's turn carries when the operator's message
    replied to another and the desk did not take it: who posted that message
    (a phrase the channel chose from Casa's records and Telegram's sender
    ids, never from text), when, and what it read, clipped."""
    when = f", posted {posted}" if posted else ""
    body = clip(quoted_text, REPLY_QUOTE_CHARS) if quoted_text else "(no text)"
    return (f"The person sent this as a reply to {who}{when}, which read:\n"
            f"«{body}»\n"
            "Their words answer that message, which may not be your latest one.")


# -- S6 §2.6: one bounded composer for every line the slice introduces or changes ----------

def bounded_line(template: str, *, label: str, fields: "dict[str, Any] | None" = None,
                 limit: "int | None" = None) -> str:
    """:func:`chat_notes.bounded_line`."""
    return chat_notes.bounded_line(template, label=label, fields=fields, limit=limit)


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
    own composer over ``agent_inbox.Outcome``, never the assistant's first-person
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


DeskFault = agent_lane.Fault


class Desk:
    """One (bot, chat, specialist) thread: the log and the activity stamp. The
    bot is the endpoint the operator wrote to: two bots' private chats with
    the operator share the chat id, so a log keyed by the chat alone would mix
    two conversations. The lock, the queue and the fault are the specialist's
    lane (:mod:`agent_lane`), shared by all of its desks."""

    def __init__(self, bot_id: int, chat_id: int, role: str) -> None:
        self.bot_id = bot_id
        self.chat_id = chat_id
        self.role = role
        self.log: list[Exchange] = []
        self.last_used: float | None = None

    @property
    def lane(self) -> agent_lane.Lane:
        return agent_lane.LANES.get(self.role)

    @property
    def lock(self) -> asyncio.Lock:
        return self.lane.lock

    @property
    def fault(self) -> "DeskFault | None":
        return agent_lane.FAULTS.faulted(self.role)

    @fault.setter
    def fault(self, value: "DeskFault | None") -> None:
        agent_lane.FAULTS.set(self.role, value)

    @property
    def faulted(self) -> str | None:
        """The fault's reason, or None — the one truth every use checks.
        A run still unwinding (#1197) refuses the desk as a fault does, and
        only until it has ended: it is not a ``fault`` (no health row)."""
        return self.lane.faulted

    @faulted.setter
    def faulted(self, reason: str | None) -> None:
        self.fault = (None if reason is None
                      else DeskFault(run_id="", plugin="", since=agent_lane.FAULTS.now(),
                                     reason=str(reason)))

    @property
    def waiting(self) -> int:
        return self.lane.waiting

    @waiting.setter
    def waiting(self, value: int) -> None:
        self.lane.waiting = value

    def reserve(self) -> "Reservation | None":
        return self.lane.reserve()

    def hold_unwinding(self, runs: list) -> None:
        self.lane.hold_unwinding(runs)

    def use(self, reservation: "Reservation | None", deadline: "float | None" = None):
        return self.lane.use(reservation, deadline)

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


DeskBusy = LaneBusy
DeskFaulted = LaneFaulted
faulted_line = agent_lane.faulted_line


def _require_bot(bot_id: Any) -> int:
    from channels.tg_address import AddressError
    if not isinstance(bot_id, int) or isinstance(bot_id, bool) or bot_id <= 0:
        raise AddressError(f"not a Telegram bot id: {bot_id!r}")
    return bot_id


def _positive_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def faulted_desk_issues() -> list:
    """The standing health report's rows for the faulted agents."""
    return agent_lane.FAULTS.issues()


class DeskRegistry:
    """The desks, by (bot, chat, specialist). A new registry is a fresh
    world: it starts from no lanes and no faults, with *now* as their clock.
    The module's own registry (``adopt_lanes``) takes them as they are."""

    def __init__(self, now: Callable[[], float] = time.time, *, adopt_lanes: bool = False) -> None:
        self._desks: dict[tuple[int, int, str], Desk] = {}
        self.now = now
        if not adopt_lanes:
            agent_lane.reset(now=now)

    def get(self, bot_id: int, chat_id: int, role: str) -> Desk | None:
        return self._desks.get((bot_id, chat_id, role))

    def get_or_create(self, bot_id: int, chat_id: int, role: str) -> Desk:
        """The desk of *role* in *chat_id* on the bot *bot_id*. The bot must
        be an int, the id of the endpoint that received the operator's
        message (a route value never keys a desk)."""
        key = (_require_bot(bot_id), chat_id, role)
        desk = self._desks.get(key)
        if desk is None:
            desk = Desk(bot_id, chat_id, role)
            self._desks[key] = desk
        return desk

    def faulted(self) -> list[Desk]:
        """Every desk whose specialist is faulted, until restart."""
        return [d for d in self._desks.values() if agent_lane.FAULTS.faulted(d.role) is not None]


DESKS = DeskRegistry(adopt_lanes=True)


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


def turn_frame(resident_name: str | None, continuation: bool = False, file: bool = False) -> str:
    """#1192: the desk turn's opening context line — the task is the
    operator's own message to the specialist, now, the same operator as in
    the exchanges shown, not something the resident relayed. An approval
    continuation's task is Casa's note of the operator's decision, not the
    operator's words, and its frame says that instead."""
    name = _resident_name(resident_name)
    if file:
        # S6 (diff round 3, Terra): a file turn's task is Casa's note of the file — by a
        # swipe-reply or after a 📎 tap — not the operator's own words
        return ("The task above is Casa's note that the operator sent you a file in this chat "
                "now, with the operator's own words if they wrote any — the same operator as in "
                f"your earlier exchanges, if any are shown. {name} did not write or relay it.")
    if continuation:
        return ("The task above is Casa's note of the operator's decision on your request, "
                "made in this chat now — the same operator as in your earlier exchanges, "
                f"if any are shown. {name} did not write or relay it.")
    return ("The task above is a message from the operator, written to you directly in "
            "this chat now, as a reply to you — the same operator as in your earlier "
            f"exchanges, if any are shown. {name} did not write or relay it.")


def desk_log(cfg: Any, desk: "Desk") -> list:
    """The exchanges a use of *desk* is shown: the desk's log, or none for a
    role that declares ``session.long_runs: true`` — each of its requests is
    one long conversation of its own, so an earlier request's text never
    enters another's. The log itself is kept either way."""
    session = getattr(cfg, "session", None)
    if getattr(session, "long_runs", False) is True:
        return []
    return desk.log


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


def has_desk(cfg: Any) -> bool:
    """:func:`agent_lane.has_lane`."""
    return agent_lane.has_lane(cfg)


def desk_target_ok(resident_role: str, role: str) -> bool:
    """The route's live ACL: *role* holds a desk (:func:`has_desk`), the chat's resident
    currently declares as a delegate AND is dispatchable now — the same map
    the delegation ACL and the ``<delegates>`` block read."""
    import tools as tools_mod
    if not role or role == resident_role:
        return False
    cfg = tools_mod._agent_role_map.get(role)
    if cfg is None or not has_desk(cfg):
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


def turn_outcomes(turn_id: str, posts, scope, *,
                  approval: bool = False) -> tuple[bool, list[str]]:
    """§5.6: what the turn proved to the operator, from the two records that
    exist by design — the post map (every message Casa posted for the turn,
    whatever the slot's kind) and the scope's own delivered sends. Returns
    (proven, the kinds that carry no S3 echo event of their own): a post the
    S3 ledger already describes is echoed by its line, not twice.

    *approval* (#1252): the turn ended with an approval keyboard pending — a
    question the operator can see even when it was posted under an earlier
    turn's scope (a PENDING deny reuses it), so it is an outcome of its own."""
    import result_broker as rb
    landed = rb.POST_MAP.owned(turn_id)
    # a send the sender confirmed is what the operator saw; a later one that
    # failed does not erase it (unlike the resident's closing-silence rule,
    # which asks for every send — that rule is the resident's, not the desk's)
    delivered = [send for send in scope.operator_sends if send.delivered]
    evented = {event.tool_use_id for event in posts}
    kinds = [r.kind for r in landed if r.tool_use_id not in evented]
    kinds.extend(send.intent for send in delivered)
    if approval:
        kinds.append("keyboard")
    return bool(posts or landed or delivered or approval), kinds


def _echo_owner(bot_id: int, chat_id: int) -> str:
    """The echo ledger's owner for one chat on one bot: ``desk:<bot>:<chat>``.
    Each bot's resident reads only the lines of its own chat."""
    return f"{ECHO_OWNER_PREFIX}{bot_id}:{chat_id}"


def record_echo(bot_id: int, chat_id: int, line: str) -> None:
    """One body-free Casa line for the resident of *chat_id* on the bot
    *bot_id*, drained at its next turn; nothing for a bot or chat id that is
    not a positive int."""
    if not _positive_int(bot_id) or not _positive_int(chat_id):
        return
    import result_broker as rb
    _ledger().record(_echo_owner(bot_id, chat_id), clip(line, rb.ECHO_LINE_MAX))


def drain_echo_lines(bot_id: int, chat_id: int) -> list[str]:
    """Read-and-clear: at most ECHO_MAX_LINES lines then ``…and N more.``."""
    import result_broker as rb
    if not _positive_int(bot_id) or not _positive_int(chat_id):
        return []
    events = _ledger().drain(_echo_owner(bot_id, chat_id))
    lines = [str(e) for e in events[:rb.ECHO_MAX_LINES]]
    if len(events) > rb.ECHO_MAX_LINES:
        lines.append(f"…and {len(events) - rb.ECHO_MAX_LINES} more.")
    return lines


def prompt_lines(bot_id: Any, chat_id: Any) -> list[str]:
    """The drained lines for the resident's next prompt on the bot *bot_id*,
    each marked as Casa's — or none. The turn carries them in its Casa notes
    block (``timekeeping.compose_turn_preamble``), which the readback strips
    (#1317)."""
    try:
        chat = int(chat_id)
    except (TypeError, ValueError):
        return []
    return [f"{PROMPT_PREFIX}{line}" for line in drain_echo_lines(bot_id, chat)]


def prompt_prefix(bot_id: Any, chat_id: Any) -> str:
    """:func:`prompt_lines` as one text, a blank line after — or nothing."""
    lines = prompt_lines(bot_id, chat_id)
    if not lines:
        return ""
    return "".join(f"{line}\n" for line in lines) + "\n"



# -- §8: the resident's delegations are uses of the desk too ------------------

def desk_for_delegation(origin: dict, cfg: Any, agent_name: str) -> Desk | None:
    """The desk a resident's delegation uses, or ``None``: an authenticated
    operator's Telegram DM turn (the reserved ``_operator_turn`` marker, a
    canonical chat id) delegating to a specialist. A scheduled, webhook,
    engagement or voice turn, a job, or a non-specialist target: no desk.
    The desk is the one on the bot that received the operator's message: the
    DM turn's ``_tg_bot``, stamped by that endpoint (an int; any other value
    names no desk)."""
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
    bot = origin.get("_tg_bot")
    if chat is None or not _positive_int(bot) or not agent_name:
        return None
    if not has_desk(cfg):
        return None
    return DESKS.get_or_create(bot, chat, agent_name)


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
    import configurator_run
    # a configurator request's whole wait, desk then lease, is one bound
    deadline = configurator_run.wait_deadline() if configurator_run.is_configurator(cfg) else None
    try:
        async with desk.use(reservation, deadline) as admitted:
            if not admitted:
                raise configurator_run.busy_error()
            if desk.faulted:
                # a delegation queued before the fault must not run
                raise DeskFaulted(f"{cfg.role!r}'s desk is faulted")
            now = DESKS.now()
            desk.begin_use(now)
            block = fit_block_for_delegation(desk_log(cfg, desk), len(context_text or ""),
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
                with (configurator_run.waiting_until(deadline) if deadline is not None
                      else contextlib.nullcontext()):
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

def _desk_origin(*, resident_role: str, desk_role: str, bot_id: int, chat_id: int,
                 user_id: int, user_name: str, message_id: Any, cid: str, text: str,
                 turn_id: str, operator_typed: bool = False) -> dict:
    """The DM's own Casa-owned context plus the three fields the classifier
    needs, the resident's role, the specialist as the executing role, depth 1
    (as after any delegation: the specialist cannot delegate onward; the
    runner's own increment only deepens it), the turn id as the quota key and echo owner, the
    operator's words raw, the operator's read clearance, the reserved desk marker, and
    the route ``_tg_bot``: the bot that received the operator's message, which every
    reply of the turn goes back through."""
    return {
        "_tg_bot": _require_bot(bot_id),
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
        # The desk is the operator's own: the operator writing to a specialist
        # on its desk reads at his clearance, as a delegation from his
        # private chat does.
        "_origin_clearance": "private",
        "_operator_turn": True,
        "desk": {"role": desk_role, "chat_id": chat_id},
        # browser executor §5.5: only a reply the operator typed carries his
        # words; an approval continuation or a tap carries Casa's text
        **({"_operator_words": text} if operator_typed else {}),
    }


def _compose_context(block: str, quoted_text: str | None, record: Any, now: float,
                     resident_name: str | None = None, continuation: bool = False,
                     file: bool = False, task: str | None = None) -> str:
    """The desk turn's context: the turn frame (#1192) first, then — for an
    operator's reply ``task`` starting with "/" — the slash line (#1198), then
    the block, then the quote — at most DESK_CONTEXT_CHARS by construction
    (frame, slash line and quote header within DESK_FRAMING_CHARS), the slice
    only a backstop."""
    parts = [turn_frame(resident_name, continuation, file)]
    if not continuation and task is not None and task.lstrip().startswith("/"):
        parts.append(SLASH_TASK_LINE)
    if block:
        parts.append(block)
    if quoted_text is not None and record is not None:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(getattr(record, "posted_at", now)))
        parts.append(
            f"The operator replied to your post (slot {getattr(record, 'slot', '?')}, "
            f"posted {when}) which read:\n{clip(quoted_text, DESK_QUOTE_CHARS)}")
    context = "\n\n".join(parts)
    return context[:DESK_CONTEXT_CHARS]


async def handle_reply(
    *, channel: Any, resident_role: str, chat_id: int, user_id: int, user_name: str,
    message_id: Any, cid: str, text: str, quoted_text: str | None, record: Any,
    desk_role: str, continuation: bool = False, reservation: "Reservation | None" = None,
    file_name: "str | None" = None, file_path: "str | None" = None,
    bot_id: "int | None" = None,
) -> None:
    """One desk use: the queue place (handed over by the route, or taken
    here), the desk lock, the idle reset, the log read, the specialist run on
    the operator's words with the desk block and the quote as context, the
    classification, the labelled reply or the one notice, the exchange
    committed at the clock of completion, the resident's echo — the permit
    released inside the lock, the reservation and the typing lease released
    on every exit. *channel* is the endpoint that received the reply; the
    desk, the echo and the turn's route are keyed by its bot (*bot_id*,
    which defaults to ``channel.bot_id``)."""
    import result_broker as rb
    import tools as tools_mod
    from channels import DeliveryOutcome
    from channels.tg_richtext import render_paged
    from output_boundary import (IntentKind, TurnScope, closing_silence_prefix, strips_to_silence,
                                 without_sentinels)

    label = label_for(desk_role)
    bot = channel.bot_id if bot_id is None else bot_id
    context = {"chat_id": str(chat_id), "cid": cid, "_tg_bot": bot}
    desk = DESKS.get_or_create(bot, chat_id, desk_role)

    async def _tell(tail: str, *, notice: bool = False, receipt: bool = True,
                    fields: "dict[str, Any] | None" = None, raw: bool = False,
                    outcome: "str | None" = None) -> None:
        """S6 §2.2: the ONE place a desk turn records anything — the notice to
        the operator (when asked) and the resident's echo, the same string,
        composed by ``bounded_line`` (§2.6). A file-started turn (``file_name``
        set) carries the receipt once per record; a raw S3 line keeps its own
        label. Nothing else in this function may call ``record_echo`` or
        ``deliver_desk_notice`` (pinned)."""
        if raw and (file_name is None or not receipt):
            line = tail
        else:
            # a raw S3 line re-labelled for a file turn: its outcome comes apart from its
            # label (rb.echo_parts), never by stripping a label the event may not carry
            body = outcome if outcome is not None else tail
            values = dict(fields or {})
            if file_name is not None:
                values.setdefault("name", file_name)
            if file_name is not None and receipt:
                template = "{label} received your file {name}; " + body
            else:
                template = "{label}" + ("" if body.startswith("'") else " ") + body
            line = bounded_line(template, label=label, fields=values)
        if notice:
            try:
                await channel.deliver_desk_notice(chat_id, line)
            except Exception as exc:  # noqa: BLE001 — nothing more is attempted
                logger.warning("desk notice failed: %s", type(exc).__name__)
        record_echo(bot, chat_id, line)

    if reservation is None:
        if desk.faulted:
            await _tell(faulted_line(label), notice=True, raw=True, receipt=False)
            channel._release_typing(context, str(chat_id))
            return
        reservation = desk.reserve()
        if reservation is None:
            await _tell("'s desk was full when the place was requested.", notice=True, receipt=False)
            channel._release_typing(context, str(chat_id))
            return
    try:
        if not continuation and file_name is None:
            # INV-MEM-043: what he typed is his message; memory keeps it when
            # this specialist keeps none (never the post he quoted, never
            # the specialist's reply)
            from delegated_memory import capture_desk_reply
            capture_desk_reply(
                channel=channel, chat_id=chat_id, user_id=user_id, user_name=user_name,
                message_id=message_id, resident=resident_role, desk_role=desk_role,
                text=text or "")
        import configurator_run
        # a configurator request's whole wait, desk then lease, is one bound
        deadline = (configurator_run.wait_deadline() if configurator_run.is_configurator(
            tools_mod._agent_role_map.get(desk_role)) else None)
        async with desk.use(reservation, deadline) as admitted:
            if not admitted:
                busy = str(configurator_run.busy_error())
                await _tell(f"{busy.removeprefix('the configurator ')}.", notice=True,
                            receipt=False)
                return
            if desk.faulted:
                # S5 §14.8: a reply queued before the fault finds it here,
                # before the idle reset, the run and any permit
                await _tell(faulted_line(label), notice=True, raw=True, receipt=False)
                return
            now = DESKS.now()
            desk.begin_use(now)
            resident_name = tools_mod._display_name_for_role(resident_role)
            block = render_block(desk_log(tools_mod._agent_role_map.get(desk_role), desk),
                                 resident_name=resident_name)
            cfg = tools_mod._agent_role_map.get(desk_role)
            # S6 §2.2: loaded AND still declared a delegate (desk_target_ok), under the lock
            if cfg is None or not desk_target_ok(resident_role, desk_role):
                await _tell("could not continue (not delegable).", notice=True, receipt=False)
                return
            turn_id = uuid.uuid4().hex
            task_text = clip(text or "", DESK_TASK_CHARS)
            origin = _desk_origin(
                resident_role=resident_role, desk_role=desk_role, bot_id=bot,
                chat_id=chat_id, user_id=user_id, user_name=user_name,
                message_id=message_id, cid=cid, text=task_text, turn_id=turn_id,
                # a file-routed turn's task is Casa's composition (name, path,
                # instructions), not what he typed (p3r3b)
                operator_typed=not continuation and file_name is None)
            origin["turn_scope"] = TurnScope.for_desk(
                origin, display_name=tools_mod._display_name_for_role(desk_role))
            if file_name is not None and file_path:
                # #1218: the file this turn received is its whole obligation
                origin["turn_scope"].receive_files(((file_path, file_name),))
            context_text = _compose_context(block, quoted_text, record, now,
                                            resident_name=resident_name,
                                            continuation=continuation,
                                            file=file_name is not None, task=task_text)
            # the permit AFTER the lock; it never waits
            permit = None
            limiter = tools_mod._specialist_limiter
            if limiter is not None:
                permit = limiter.try_acquire(tools_mod._delegation_scope(origin, desk_role))
                if permit is None:
                    # S6 §2.2: a past event; no exchange is opened
                    await _tell("was at its concurrent-work limit when the turn was attempted.",
                                notice=True, receipt=False)
                    return
            output, failure = None, None
            runs: list = []
            try:
                import agent as agent_mod
                ov = agent_mod.origin_var.set(origin)
                qk = tools_mod._delegation_quota_key.set(turn_id)
                sk = tools_mod._desk_run_sink.set(runs)
                try:
                    with (configurator_run.waiting_until(deadline) if deadline is not None
                          else contextlib.nullcontext()):
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
                import configurator_run
                if isinstance(exc, configurator_run.ConfiguratorBusy):
                    # nothing of this reply ran: say what is running instead
                    failure = "configurator_busy"
                    busy_line = f"{str(exc).removeprefix('the configurator ')}."
            finally:
                desk.hold_unwinding(runs)     # #1197: before the permit and the lock
                if permit is not None:
                    permit.release()          # inside the lock, before it is released
            if failure is None:
                reply_text, failure = _outcome_text(output)
            posts = rb.POSTS.drain(turn_id)
            scope = origin["turn_scope"]
            # #1252: a run that ended with an approval pending shows only the
            # words written before that call; the desk log keeps them all
            kept = scope.approval_kept if failure is None else None
            shown = reply_text if failure is None else None
            if kept is not None:
                import specialist_limits
                shown, _truncated = specialist_limits.truncate_output(kept)
            proven, unevented = turn_outcomes(turn_id, posts, scope,
                                              approval=kept is not None)
            specialist_side = NO_REPLY
            # #1283 (#1075 rule 2): a closing run of sentinel-only messages
            # after earlier text — judged on the runner's own message list
            # against the words shown (after the #1252 cut), so a list that is
            # not this text's leaves the reply whole
            trimmed = None
            if failure is None and not strips_to_silence(shown):
                trimmed = closing_silence_prefix(shown, getattr(output, "reply_messages", ()))
            if failure == "configurator_busy":
                await _tell(busy_line, notice=True, receipt=False)
            elif failure is not None:
                what = "your file {name}" if file_name is not None else "your reply"
                await _tell(f"could not handle {what} ({failure}).", notice=True, receipt=False)
            elif strips_to_silence(shown) or (trimmed is not None and proven and kept is None):
                # #1433 (operator ruling, the #1075 rule 1 on the desk): a turn
                # that proved an outcome — its card posted — and closes in
                # silence is silent; the words written before are not posted
                if proven:
                    specialist_side = reply_text if kept is not None else POSTED_VIEW
                    # every outcome without an S3 line of its own gets one
                    # Casa line per kind, named from the records, never
                    # from the bodies
                    for phrase in outcome_phrases(unevented):
                        await _tell(phrase)
                else:
                    await _tell("had nothing to add.", notice=True)
            else:
                # #1283 (#1075 rule 2): no proven outcome (or an approval
                # cut) — the closing run is dropped, the earlier messages are
                # the reply
                if trimmed is not None:
                    shown = trimmed
                # #1342: words and a sentinel in one message (or a sentinel
                # before words) — the words are the reply, the tag is dropped
                shown = without_sentinels(shown)
                admitted = scope.admit(IntentKind.FINAL_REPLY, shown)
                labelled = admitted.with_text(f"{label}\n{admitted}")
                # a reply cut at a pending approval is not the turn's answer
                post = rb.PostRecord(role=desk_role, operator_id=user_id, plugin="",
                                     slot="desk", tool_use_id=turn_id, owner=turn_id,
                                     posted_at=DESKS.now(),
                                     kind=(rb.DESK_REPLY if kept is None
                                           else rb.DESK_REPLY_BEFORE_APPROVAL))
                delivered = False
                try:
                    outcome = await channel.send_response(labelled, {**context, "_post": post})
                    delivered = outcome is DeliveryOutcome.DELIVERED
                except Exception as exc:  # noqa: BLE001 — not proven
                    logger.warning("desk reply send failed: %s", type(exc).__name__)
                specialist_side = reply_text
                if delivered:
                    pages = len(render_paged(labelled))
                    count = f"({pages} page{'s' if pages != 1 else ''})."
                    await _tell(("answered " if file_name is not None else "answered your reply ") + count)
                else:
                    # S6 §2.2: a page may have landed — unconfirmed, not "did not go out"
                    await _tell("answered; complete delivery could not be confirmed.", notice=True)
            # the exchange and the idle clock are stamped once the use has
            # settled — delivery or notice included — never at its start
            done = DESKS.now()
            desk.append("operator", task_text, done)
            desk.append("specialist", specialist_side, done)
            for line, (_, outcome) in zip(rb.echo_lines(posts), rb.echo_parts(posts)):
                await _tell(line, raw=True, outcome=outcome)
            # a restart the configurator asked for: the button follows its answer
            import configurator_run
            await configurator_run.offer_restart(turn_id)
    finally:
        reservation.release()                 # idempotent: a cancel while waiting
        channel._release_typing(context, str(chat_id))
