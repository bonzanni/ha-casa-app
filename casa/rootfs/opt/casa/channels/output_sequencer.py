"""Per-topic OUTPUT SEQUENCER + relay-mediated discrete-posting intent registry
(v0.79.0 Primitive A — engagement-topic UX; design §2, Sol r1-1/2/3, r11-1
consolidation, r12/r13).

This module owns the machinery that makes an engagement topic's message order a
property of a single serialized writer rather than of racing coroutines:

* :class:`OutputSequencer` — ONE serialization lock + ONE background intent
  watcher task per ``claude_code`` engagement topic. It is the ONLY writer to
  the topic: narration posts/edits (driven by ``drivers.topic_stream``), reply-
  tool sends, ask/permission keyboards, platform notices and summary edits all
  funnel through it. It owns the topic HIGH-WATER MARK (the newest
  sequencer-posted message id) and the per-message no-op edit cache (F1).

* :class:`IntentRegistry` — the relay-mediated discrete-posting state (§2, bullet
  RELAY-MEDIATED DISCRETE POSTING). A discrete send does NOT post itself: its
  ingress registers a :class:`SendIntent` (``pending``), arms it at the point of
  no return (``armed``), or cancels it (``cancelled`` → TOMBSTONE). The relay,
  processing the subprocess event stream in the ONE true causal order, matches
  intents at CONTENT-BLOCK positions and posts armed intents through the
  sequencer at their block. Late/absent intents post out of exact position via
  the 2s ordered-slot hold and the 10s intent timeout.

Concurrency note (deviation disclosed for T1 review): §2 phrases the serializer
as "one asyncio.Task + queue". This module realizes the SAME single-writer
invariant with an :class:`asyncio.Lock` guarding every post + high-water/narration
mutation, plus ONE background task (:meth:`OutputSequencer.run_watcher`) that
drives late/timeout discrete posts. A lock (rather than a literal queue) was
chosen so ``drivers.topic_stream``'s crash-safe cursor/checkpoint contract —
which the design mandates be PRESERVED — keeps its synchronous
post-then-checkpoint shape (the relay ``await``s a sequencer op, learns the
message id, then checkpoints). The observable §2 contract (ordering, sealing,
rollover, intent states, slot hold, timeout + consumption debt, reattachment,
no-op edit gate, de-dup-before-post) is identical either way.

Clocks are injectable (``_now`` / ``_sleep``); no code here patches the global
``asyncio.sleep`` (the module-local / injected-clock rule, CLAUDE.md memory
cage).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from authz_grants import canonical_args_hash

logger = logging.getLogger(__name__)

# -- tunables (module-local so tests can shrink them without touching the
#    process-wide asyncio module) --------------------------------------------
_SLOT_HOLD_S = 2.0        # §2(4): ordered-slot hold — wait for a pending/absent
#                           intent to arm/cancel before proceeding past a block.
_INTENT_TIMEOUT_S = 10.0  # §2(5): an armed intent unmatched by any block for
#                           this long posts out-of-band with a WARN + a debt.
_HOLD_POLL_S = 0.05       # slot-hold re-check cadence (the happy path arms well
#                           inside one poll; MCP calls land in ms).
_DISCRETE_CACHE_CAP = 64  # A9 (Sol r2-10b): bounded FIFO of discrete
#                           post_discrete/edit_discrete no-op-cache keys, so
#                           keyboard entries don't accumulate for the
#                           engagement's lifetime. Eviction past the cap drops
#                           the oldest DISCRETE _edit_cache entry (narration /
#                           summary entries are never in this FIFO, so they are
#                           untouched); the no-op gate then re-edits once.

# Canonical channel-MCP tool names (the ask/reply ingresses — §2 pinned
# ingress (a)). ``HOLD_ELIGIBLE_TOOLS`` is the set of tool kinds that ALWAYS
# correspond to a discrete post, so a block for one holds a slot even when no
# intent is registered yet. T2/T3 extend this set for emit_completion; permission
# keyboards fence on the GATED tool's own frame and are recognized reactively
# (only when a matching intent/tombstone already exists), never held blindly.
ASK_TOOL = "mcp__casa-engagement-channel__ask"
REPLY_TOOL = "mcp__casa-engagement-channel__reply"
# emit_completion is the svc_casa_mcp ingress (§2 pinned ingress (c)); its frame
# is hold-eligible so the completion post never overtakes a still-pending block.
EMIT_COMPLETION_TOOL = "mcp__casa-framework__emit_completion"
HOLD_ELIGIBLE_TOOLS: frozenset[str] = frozenset(
    {ASK_TOOL, REPLY_TOOL, EMIT_COMPLETION_TOOL})


# ---------------------------------------------------------------------------
# Pinned projection → hash (§2, "Hash identity"): computed AT THE INGRESS
# BOUNDARY from RAW args under pinned projections, using the v0.76 canonical
# helper. The SAME function is applied by the relay to each tool_use block, so
# an intent's transmitted hash and the block's computed hash agree.
# ---------------------------------------------------------------------------


def project_args(tool_name: str, raw_args: dict) -> dict:
    """Apply the pinned projection for *tool_name* to *raw_args*.

    * ``ask`` → ``{question, options, timeout_s-as-given, multi-as-given}``.
    * ``reply`` → ``{text}`` (drops the SDK-compat ``chat_id``).
    * everything else (a permission-gated tool's own frame, ``emit_completion``)
      → identity over the raw args.

    A5 · F-MULTI (v0.83.0): ``multi`` joins the ask projection — this MUST stay
    byte-identical to ``casa_engagement_channel._ask_projection_hash`` (the
    client side), or a multi ask's relay intent would never match its block.
    """
    if not isinstance(raw_args, dict):
        raw_args = {}
    if tool_name == ASK_TOOL:
        return {
            "question": raw_args.get("question"),
            "options": raw_args.get("options"),
            "timeout_s": raw_args.get("timeout_s"),
            "multi": raw_args.get("multi", False),
        }
    if tool_name == REPLY_TOOL:
        return {"text": raw_args.get("text")}
    return dict(raw_args)


def projection_hash(tool_name: str, raw_args: dict) -> str:
    """``canonical_args_hash`` of the pinned projection of *raw_args*."""
    return canonical_args_hash(project_args(tool_name, raw_args))


# ---------------------------------------------------------------------------
# Intent registry.
# ---------------------------------------------------------------------------


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class SendIntent:
    """One relay-mediated discrete-posting intent (§2, RELAY-MEDIATED DISCRETE
    POSTING).

    ``state`` walks ``pending → armed → posted|cancelled`` (§2(2)). A
    ``cancelled`` intent is a TOMBSTONE. ``timeout_posted`` marks the one-block
    CONSUMPTION DEBT left by the 10s-timeout out-of-band post (§2(5)):
    ``state`` is ``posted`` but the item stays MATCHABLE so its late-arriving
    block is consumed silently. ``consumed`` retires the item from matching
    once a block has bound (or consumed-cancelled / debt-consumed) it.
    """

    request_id: str
    tool_name: str
    projection_hash: str
    poster: Any                     # Callable[[], Awaitable[int|None]] | str
    registered_at: float
    seq: int
    state: str = "pending"          # pending | armed | posted | cancelled
    armed_at: float | None = None   # #332: the intent timeout runs from ARM
    #                                 time — an ingress can sit pending through
    #                                 validation far longer than the timeout.
    message_id: int | None = None
    outcome: dict | None = None
    posting: bool = False           # A3 · F-ORDER (Sol A3 wave 3): the writer is
    #                                 CURRENTLY awaiting this intent's poster under
    #                                 the lock. A serialized cancel that observes
    #                                 this NO-OPS — the in-flight post wins.
    slot_missed: bool = False       # relay slot timed out while this was pending
    timeout_posted: bool = False    # §2(5) one-block consumption debt
    consumed: bool = False          # retired from matching
    on_retire: Any = None           # wb2-4: called ONCE when the intent is pruned
    #                                 at turn end — the ask handler wires it to
    #                                 release the validation gate's lifecycle pin.
    _retired: bool = False          # wb2-4: guards the one-shot on_retire call
    post_failed: bool = False       # F3: poster failed — surfaced ok:false,
    #                                 retired from matching (NOT a success debt)

    # -- matchability predicates -------------------------------------------
    def matchable(self) -> bool:
        """Still eligible to bind a content-block (§2(3)).

        Pending/armed intents, un-consumed cancelled tombstones, and the
        one-block timeout-posted debt are matchable; a consumed or
        post-failed (F3) item is not.
        """
        if self.consumed or self.post_failed:
            return False
        if self.state in ("pending", "armed", "cancelled"):
            return True
        return self.state == "posted" and self.timeout_posted


class IntentRegistry:
    """Ordered per-engagement intent + tombstone store (§2(1)-(3),(6)).

    Registration order is total (``seq``); matching a content-block always
    picks the OLDEST matchable item with an equal ``(tool_name,
    projection_hash)`` — FIFO on equal hashes on both sides. The ``request_id``
    → intent map gives idempotent transport-retry REATTACHMENT (§2(1)); a
    retry whose id matches an existing intent returns that intent (and its
    recorded outcome) rather than creating a second one.
    """

    def __init__(self, *, _now: Callable[[], float] = time.monotonic) -> None:
        self._now = _now
        self._by_seq: list[SendIntent] = []
        self._by_request: dict[str, SendIntent] = {}
        self._next_seq = 0

    def by_request_id(self, request_id: str) -> SendIntent | None:
        return self._by_request.get(request_id)

    def register(
        self, *, request_id: str, tool_name: str, projection_hash: str, poster: Any,
        on_retire: Any = None,
    ) -> tuple[SendIntent, bool]:
        """Register (or REATTACH to) an intent. Returns ``(intent, created)``.

        A same-``request_id`` call REATTACHES idempotently (§2(1)): the existing
        intent is returned with ``created=False`` so a transport retry can read
        the recorded outcome (including the posted ``message_id``) and can
        neither double-post nor consume another frame.

        wb2-4: ``on_retire`` (optional) is called ONCE when the intent is pruned
        at turn end — the ask handler uses it to release its validation gate's
        intent-lifecycle pin. A REATTACH never overwrites the first
        registration's ``on_retire`` (idempotent — one pin, one release).
        """
        existing = self._by_request.get(request_id)
        if existing is not None:
            return existing, False
        intent = SendIntent(
            request_id=request_id,
            tool_name=tool_name,
            projection_hash=projection_hash,
            poster=poster,
            registered_at=self._now(),
            seq=self._next_seq,
            on_retire=on_retire,
        )
        self._next_seq += 1
        self._by_seq.append(intent)
        self._by_request[request_id] = intent
        return intent, True

    def set_poster(self, request_id: str, poster: Any) -> SendIntent | None:
        """Replace the intent's poster (T3: the ingress registers early for
        idempotent reattach detection, then installs the REAL relay-invoked
        poster before arming). No-op if the intent is gone."""
        intent = self._by_request.get(request_id)
        if intent is not None:
            intent.poster = poster
        return intent

    def arm(self, request_id: str) -> SendIntent | None:
        intent = self._by_request.get(request_id)
        if intent is not None and intent.state == "pending":
            intent.state = "armed"
            intent.armed_at = self._now()  # #332: timeout clock starts here
        return intent

    def cancel(self, request_id: str) -> SendIntent | None:
        """Cancel a pending/armed intent → TOMBSTONE (§2(2))."""
        intent = self._by_request.get(request_id)
        if intent is not None and intent.state in ("pending", "armed"):
            intent.state = "cancelled"
        return intent

    def oldest_matchable(
        self, tool_name: str, projection_hash: str,
    ) -> SendIntent | None:
        for intent in self._by_seq:
            if (
                intent.matchable()
                and intent.tool_name == tool_name
                and intent.projection_hash == projection_hash
            ):
                return intent
        return None

    def peek(self, tool_name: str, projection_hash: str) -> SendIntent | None:
        """F-OOB instrumentation (spec D7): the MOST RECENTLY REGISTERED intent
        for ``(tool_name, projection_hash)``, regardless of matchability.

        DIAGNOSTIC-ONLY — read-only, never mutates, and never consulted by the
        real FIFO matching path (:meth:`oldest_matchable`). Lets a log line
        report an intent's state and registration time even after
        ``post_for_block`` has already resolved (and possibly consumed) it, so
        a match-point log can still carry the "intent state at match time"
        dimension."""
        match: SendIntent | None = None
        for intent in self._by_seq:
            if (
                intent.tool_name == tool_name
                and intent.projection_hash == projection_hash
            ):
                match = intent
        return match

    def armed_unposted(self) -> list[SendIntent]:
        return [
            i for i in self._by_seq
            if i.state == "armed" and not i.consumed and not i.post_failed
            and i.message_id is None
        ]

    def all_intents(self) -> list[SendIntent]:
        """A stable snapshot of every registered intent/tombstone in ``seq``
        order (wb3-1/wb3-3: ``terminalize`` iterates this to abort unresolved
        intents before pruning)."""
        return list(self._by_seq)

    def has_any_matchable(self) -> bool:
        """True iff any intent/tombstone is still matchable — i.e. a discrete
        ingress is currently active for this engagement. Used to keep the slot
        hold DORMANT while no ingress has registered anything (the T1-stubbed
        state and any quiescent turn), so a hold-eligible tool_use block never
        stalls narration when there is provably nothing to wait for."""
        return any(i.matchable() for i in self._by_seq)

    def prune(self, *, keep: "Callable[[SendIntent], bool] | None" = None) -> None:
        """§2(6): drop all intents, tombstones and id→outcome at turn end.

        wb2-4: fire each intent's ``on_retire`` hook ONCE as it is dropped — this
        is the intent's definitive RETIREMENT point (a cancelled/consumed intent
        stays a matchable tombstone until here), so the ask handler's validation-
        gate pin is released exactly when the intent it protects ceases to exist.
        A hook raising must never leave the registry half-pruned.

        wb4-2: ``keep`` (optional) is a predicate; intents it selects are
        PRESERVED and their ``on_retire`` does NOT fire (they are not retiring).
        :meth:`OutputSequencer.terminalize` uses it to preserve an unresolved
        emit_completion consumption debt so finalize's completion drain still
        observes it. The turn-end callers pass no predicate — a full prune."""
        survivors: list[SendIntent] = []
        for intent in self._by_seq:
            if keep is not None and keep(intent):
                survivors.append(intent)
                continue
            hook = intent.on_retire
            if hook is not None and not intent._retired:
                intent._retired = True
                try:
                    hook()
                except Exception:  # noqa: BLE001 — gate-pin release is hygiene-only
                    logger.debug(
                        "send-intent on_retire hook failed (rid=%s)",
                        intent.request_id, exc_info=True)
        self._by_seq = survivors
        self._by_request = {i.request_id: i for i in survivors}


# ---------------------------------------------------------------------------
# No-op edit gate (F1): tri-state markup.
# ---------------------------------------------------------------------------

MARKUP_ABSENT = "absent"          # no reply_markup touched
MARKUP_EMPTY = "empty"            # explicit-empty (clear keyboard) — T3
_ABSENT = object()                # sentinel: caller passed no markup argument


def _markup_tristate(markup: Any) -> Any:
    """Map a markup argument to its tri-state cache key (F1, Sol r2-2).

    ``absent`` (no markup touched) | ``empty`` (explicit clear) |
    ``non-empty`` serialized to a stable ``str`` so presence-alone can never
    suppress a markup-only settlement.
    """
    if markup is _ABSENT or markup is None:
        return MARKUP_ABSENT
    if isinstance(markup, str) and markup == MARKUP_EMPTY:
        return MARKUP_EMPTY
    return f"markup:{markup!r}"


def _discrete_markup_tristate(markup: Any) -> Any:
    """Map a DISCRETE-write markup argument to its cache key (F4).

    Unlike :func:`_markup_tristate` (which conflates them so a text-only
    narration edit never accidentally reads as a keyboard change),
    ``post_discrete``/``edit_discrete`` distinguish the two keyboard operations:

    * ``_ABSENT`` — "leave the keyboard untouched" → ``MARKUP_ABSENT``;
    * ``None`` — "CLEAR the keyboard" → ``MARKUP_EMPTY`` (a clear IS the
      explicit-empty operation; matches ``edit_topic_message_markup``'s
      ``None``/``MARKUP_EMPTY`` → explicit-empty-keyboard wire semantics).

    Without this split a keyboard CLEAR (``markup=None``) after an identical
    text-only edit is no-op-suppressed, because ``_markup_tristate(None)``
    equals ``_markup_tristate(_ABSENT)`` and the ``(text, tri)`` cache matches.
    """
    if markup is _ABSENT:
        return MARKUP_ABSENT
    if markup is None:
        return MARKUP_EMPTY
    if isinstance(markup, str) and markup == MARKUP_EMPTY:
        return MARKUP_EMPTY
    return f"markup:{markup!r}"


# Result codes from edit_narration_if_latest / post_for_block.
APPLIED = "applied"
SEALED = "sealed"
FAILED = "failed"
# wb3-2 (whole-branch gate wave 3): a narration write REFUSED because the
# engagement has TERMINALIZED (the sequencer terminal latch is set). Distinct
# from FAILED (a wire error → retry/drop) and SEALED (rollover → repost below):
# a DISCARDED write must be dropped cleanly with NO retry, NO drop-mode, and NO
# repost — nothing may land below the terminal completion (D5 discard doctrine).
DISCARDED = "discarded"

# wb4-1 (whole-branch gate wave 4): sentinel returned by
# :meth:`OutputSequencer.register_intent` once the engagement has TERMINALIZED.
# Distinct from ``None`` (which means "no live sequencer" and correctly activates
# the ingress EAGER FALLBACK): a terminal sentinel is a recorded fail-closed
# outcome, so the ask/reply/anchor ingress surfaces ``engagement_terminal`` to
# the agent instead of registering + arming + posting a discrete send BELOW the
# terminal completion (D5 discard doctrine).
TERMINAL_REGISTRATION = object()


def _is_live_completion_debt(intent: "SendIntent") -> bool:
    """wb4-2: ``True`` for an UNRESOLVED emit_completion consumption debt — a
    posted-but-unconsumed one-block debt (``register_completion_consumption``).
    :meth:`OutputSequencer.terminalize` PRESERVES it across the prune so
    ``finalize_completion_post``'s :meth:`await_completion_drain` still blocks
    until the relay reaches the emit_completion block (every prior frame
    processed) rather than reading a pruned-away intent as trivially drained —
    which would let a lagging prior frame's platform notice overtake completion.
    """
    return (
        intent.tool_name == EMIT_COMPLETION_TOOL
        and intent.state == "posted"
        and intent.timeout_posted
        and not intent.consumed
        and not intent.post_failed
        and intent.outcome is None
    )


# ---------------------------------------------------------------------------
# The sequencer.
# ---------------------------------------------------------------------------

SendMessage = Callable[[int, str], Awaitable[int | None]]
EditMessage = Callable[[int, int, str], Awaitable[bool]]
# A9 markup-capable wire primitives (injected by the driver's _relay_* wrappers;
# production always supplies them, tests inject fakes). ``send_message_markup``
# posts plain text + an inline keyboard and returns the message id;
# ``edit_message_markup`` edits text and/or markup (``text=None`` ⇒ markup-only;
# ``markup is _ABSENT`` ⇒ leave the keyboard untouched).
SendMessageMarkup = Callable[..., Awaitable[int | None]]
EditMessageMarkup = Callable[..., Awaitable[bool]]


class OutputSequencer:
    """Serialized single-writer for ONE engagement topic (§2).

    Injected primitives ``send_message(topic_id, text) -> msg_id|None`` and
    ``edit_message(topic_id, msg_id, text) -> bool`` keep it unit-testable in
    isolation (mirrors ``drivers.topic_stream``'s style).
    """

    def __init__(
        self,
        *,
        engagement_id: str,
        topic_id: int,
        send_message: SendMessage,
        edit_message: EditMessage,
        send_message_markup: SendMessageMarkup | None = None,
        edit_message_markup: EditMessageMarkup | None = None,
        send_paged: SendMessage | None = None,
        _now: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], Awaitable[None]] | None = None,
        slot_hold_s: float = _SLOT_HOLD_S,
        intent_timeout_s: float = _INTENT_TIMEOUT_S,
        hold_poll_s: float = _HOLD_POLL_S,
    ) -> None:
        self.engagement_id = engagement_id
        self.topic_id = topic_id
        self.send_message = send_message
        self.edit_message = edit_message
        # A9: markup-capable wire primitives. Default None keeps the sequencer
        # constructible without them; post_discrete/edit_discrete raise a clear
        # RuntimeError if used un-injected (belt-and-suspenders — production
        # always injects via the driver's _ensure_sequencer wiring).
        self._send_message_markup = send_message_markup
        self._edit_message_markup = edit_message_markup
        # v0.109.0 (G5): paged rich sender for the terminal completion post —
        # a summary over the render caps ships as several rendered pages
        # inside ONE serialized write (never raw markdown). Optional: absent ⇒
        # completion posts through the ordinary one-message ``send_message``.
        self._send_paged = send_paged
        self._now = _now
        self._sleep = _sleep or _default_sleep
        self._slot_hold_s = slot_hold_s
        self._intent_timeout_s = intent_timeout_s
        self._hold_poll_s = hold_poll_s

        self._lock = asyncio.Lock()
        # wb3-1/wb3-2/wb3-3 (whole-branch gate wave 3): the persistent TERMINAL
        # LATCH. Set once by :meth:`terminalize` (under the writer lock) when the
        # engagement flips terminal; never cleared. It is the ONE truth source
        # consulted INSIDE the writer lock by every narration writer (discard —
        # nothing posts below the terminal completion, wb3-2), by the anchor
        # poster's wire re-read (never post + ledger a question on a closed
        # engagement, wb3-1), and by the relay's terminal seam.
        self._terminal = False
        # REENTRANT-PER-TASK ownership (Sol diff gate r2). The task currently
        # inside the serialization lock, or None. A poster the sequencer awaits
        # WHILE holding the lock (seal-narration + post is atomic) may call back
        # into ``edit_summary`` — e.g. the ask poster's ``note_ask_waiting`` →
        # SummaryController.submit_status → edit_summary — on this SAME task; a
        # plain non-reentrant ``asyncio.Lock`` would deadlock it forever. Owner
        # tracking lets that nested, already-serialized call proceed.
        self._lock_owner: asyncio.Task | None = None
        self.registry = IntentRegistry(_now=_now)
        # F3 fail-closed posting: per-request resolution events. A deferred
        # ask/reply/anchor handler AWAITS the intent's outcome (posted ok, or
        # poster-failure ok:false) bounded by ``post_await_budget`` and returns
        # ok only when the post actually landed — an ``ok:true`` response with a
        # failed post is structurally impossible.
        self._resolution_events: dict[str, asyncio.Event] = {}
        # HIGH-WATER: newest sequencer-posted message id (the sequencer is the
        # only writer, so it is authoritative). ``_narration_msg_id`` is the
        # current OPEN narration message, or None when narration is SEALED.
        self._high_water: int | None = None
        self._narration_msg_id: int | None = None
        # F1 no-op edit gate: msg_id -> (text, markup_tristate).
        self._edit_cache: dict[int, tuple[Any, Any]] = {}
        # A9 (Sol r2-10b): bounded FIFO of DISCRETE-write cache keys only. Narration
        # entries retire on seal and summary entries live forever above the log;
        # discrete keyboard entries would otherwise leak, so post_discrete/
        # edit_discrete register their mids here and eviction past the cap drops
        # the oldest discrete _edit_cache entry (never a narration/summary one).
        self._discrete_cache_fifo: deque[int] = deque()
        self._arm_event = asyncio.Event()
        # v0.79.0 (§3, Primitive B): reply-threading. An inbound operator
        # envelope's delivery sets this to its Telegram message id; the turn's
        # FIRST sequencer-posted message (narration open, or an ask/reply
        # poster via ``consume_turn_reply_to``) threads to it, then clears it.
        self._turn_reply_to: int | None = None

    # -- serialization (reentrant-per-task; §2 one serialized writer) -------

    @asynccontextmanager
    async def _serialized(self):
        """Acquire the ONE serialization lock, REENTRANTLY for the task that
        already holds it (owner-tracked).

        INVARIANT (Sol diff gate r2): a locked section of the sequencer may,
        via a poster it awaits, call back into :meth:`edit_summary` (the
        NON-narration summary path). ``edit_summary`` must be reentrant-safe for
        the lock-OWNING task and must never block on the lock it is nested
        within. This is correct because the summary edit does NOT touch
        narration / high-water / open-narration state (see :meth:`edit_summary`'s
        docstring) — reentrant execution from within a narration-post critical
        section cannot corrupt narration invariants.

        Concretely: an ask poster runs while the writer lock is held (seal-
        narration + post is atomic — see :meth:`_post_intent_locked`). That
        poster calls ``driver.note_ask_waiting`` → SummaryController.submit_status
        → ``edit_summary``, which re-enters here on the SAME task. A plain
        non-reentrant ``asyncio.Lock`` would deadlock that task forever.

        Reentrancy is safe because asyncio is single-threaded: the reentrant
        body only runs when the SAME task already holds the lock, so no
        concurrent mutation is possible. This keeps §2's single-writer
        invariant (one lock, not a second summary lock) intact — a DIFFERENT
        task still contends on ``self._lock`` and is fully serialized.
        """
        if self._lock_owner is asyncio.current_task():
            # Already serialized by this very task — reenter without re-acquiring.
            yield
            return
        async with self._lock:
            self._lock_owner = asyncio.current_task()
            try:
                yield
            finally:
                self._lock_owner = None

    def serialized(self):
        """PUBLIC alias of :meth:`_serialized` — the reentrant-per-task writer CM.

        GLOBAL LOCK-ORDER (Sol diff gate r3): the sequencer writer lock is the
        OUTER lock in the one sanctioned order ``sequencer → summary``.
        :class:`drivers.summary_controller.SummaryController` acquires THIS
        (reentrantly, if the caller — e.g. an armed ask poster the sequencer
        awaits under the held writer lock — already owns it) BEFORE its own
        summary lock, so *no code ever holds the summary lock while acquiring the
        sequencer lock*. That removes the former summary→sequencer ordering
        entirely, making an AB-BA cross-task cycle impossible: because holding the
        summary lock now REQUIRES first holding this writer lock, and only one task
        holds it at a time, a second task can never hold the summary lock while the
        first holds the sequencer lock.

        Returns the SAME reentrant-per-task context manager as ``_serialized``;
        internal callers keep using ``_serialized`` unchanged.
        """
        return self._serialized()

    # -- narration ----------------------------------------------------------

    @property
    def narration_msg_id(self) -> int | None:
        return self._narration_msg_id

    @property
    def high_water(self) -> int | None:
        return self._high_water

    def is_terminal(self) -> bool:
        """wb3-1/wb3-2: ``True`` once :meth:`terminalize` has latched this
        engagement terminal. A plain synchronous read of the persistent latch —
        the anchor poster (already under the writer lock) and the relay's
        terminal seam both consult it as the single truth source."""
        return self._terminal

    async def open_narration(self, text: str) -> "int | None | str":
        """Post a NEW narration message; it becomes the open narration and the
        high-water mark.

        wb3-2: returns :data:`DISCARDED` (posting NOTHING) when the terminal
        latch is set — re-checked HERE, inside the writer lock, so a write that
        was blocked on the lock at the instant of terminalization is discarded
        rather than landing below the terminal completion (the TOCTOU a bare
        outside-lock seam read leaves open)."""
        async with self._serialized():
            if self._terminal:
                return DISCARDED
            return await self._open_narration_locked(text)

    async def _open_narration_locked(self, text: str) -> int | None:
        # v0.79.0 (§3): thread the turn's FIRST post to the inbound envelope
        # that triggered the turn (reply-quoting). Consumed once — a later post
        # this turn is not a reply to the operator's message.
        # #332: consume the one-shot target only on a SUCCESSFUL send — the
        # topic-stream retry after a transient failure must still thread the
        # turn's first output to the inbound message.
        reply_to = self._turn_reply_to
        if reply_to is not None:
            mid = await _maybe_await(
                self.send_message(self.topic_id, text, reply_to=reply_to))
        else:
            mid = await _maybe_await(self.send_message(self.topic_id, text))
        if mid is None:
            return None
        if reply_to is not None and self._turn_reply_to == reply_to:
            self._turn_reply_to = None
        self._high_water = mid
        self._narration_msg_id = mid
        self._edit_cache[mid] = (text, MARKUP_ABSENT)
        return mid

    async def edit_narration_if_latest(
        self, msg_id: int, text: str, *, markup: Any = _ABSENT,
    ) -> str:
        """Edit *msg_id* IFF it is still the newest sequencer-posted message
        (§2). Otherwise return :data:`SEALED` — the caller opens a fresh
        narration message for the pending text.

        Routes through the F1 no-op edit gate: an identical (text, markup)
        edit is skipped (returns :data:`APPLIED`); a FAILED edit invalidates
        the cache entry so a retry is never suppressed.
        """
        async with self._serialized():
            if self._terminal:
                return DISCARDED  # wb3-2: terminal ⇒ no edit lands below completion
            if msg_id != self._narration_msg_id or msg_id != self._high_water:
                return SEALED
            tri = _markup_tristate(markup)
            if self._edit_cache.get(msg_id) == (text, tri):
                return APPLIED  # no-op skip
            ok = await _maybe_await(self.edit_message(self.topic_id, msg_id, text))
            if not ok:
                self._edit_cache.pop(msg_id, None)  # invalidate → retry allowed
                return FAILED
            self._edit_cache[msg_id] = (text, tri)
            return APPLIED

    async def post_unless_anchor_open(
        self, text: str, seam: Any, *, poster: Any = None,
    ) -> str:
        """§D5 (Sol r4-2 BLOCKER): the ATOMIC read-decide-write for anchor-scoped
        narration suppression.

        Under the ONE serialization lock, re-read *seam* (the driver-injected
        ``open_anchor_state`` — ``() -> (n, mid) | None``, sync or async): if it
        reports a genuinely open, unanswered free-text anchor, return
        :data:`"held"` and post NOTHING; otherwise run *poster* (the relay's
        normal narration post — reentrant under THIS same lock) and return
        :data:`"posted"`. When no *poster* is given, *text* is posted as a fresh
        narration message (belt-and-suspenders default; the relay always injects
        its rollover-aware poster).

        This is the ONLY way armed/arming narration reaches the wire. Making the
        seam read and the narration post ATOMIC forecloses the r4-2 interleave: a
        bare re-read races the late anchor poster — relay reads "no open anchor"
        → the intent watcher takes THIS lock and posts the late anchor
        (:meth:`process_intents_once`) → relay takes the lock and posts narration
        BELOW it, recreating F-LEAK2. Because the late poster and this op contend
        on the SAME lock (reentrant-per-task, so it also nests inside the relay's
        own writer paths), the anchor can never slip in between this op's seam
        read and its post: either the anchor posts first and the seam reports it
        open (⇒ held), or the narration posts first and the anchor lands below it
        (correct order). Returns :data:`"held"` or :data:`"posted"`."""
        async with self._serialized():
            # wb3-2: terminal ⇒ HOLD (post nothing). The relay's ``result``-time
            # flush-vs-discard drops the held buffer for a terminal engagement,
            # so treating a terminal latch like an open anchor discards this
            # prose without it ever reaching the wire below the completion.
            if self._terminal:
                return "held"
            state = await _maybe_await(seam()) if seam is not None else None
            if state is not None:
                return "held"
            if poster is not None:
                await _maybe_await(poster())
            else:
                await self._open_narration_locked(text)
            return "posted"

    async def edit_summary(self, msg_id: int, text: str) -> str:
        """Edit the pinned live SUMMARY message (§5 — the R1 exception).

        The summary is the FIRST topic message and lives ABOVE the append-only
        causal log; its edits are NON-narration. Unlike
        :meth:`edit_narration_if_latest`, this path deliberately does NOT touch
        the narration/high-water invariants: it never seals the open narration,
        never advances the high-water mark, and the summary message is never
        itself sealed. That keeps the summary a living message while the causal
        log below it stays strictly append-only (T1 invariants intact — the
        summary id is posted before any narration, so it is never the
        high-water/open-narration id).

        Still funnels through the single serialization lock (one writer) and the
        F1 no-op edit gate: an identical edit skips (returns :data:`APPLIED`); a
        FAILED edit invalidates the cache entry so a retry is never suppressed.
        """
        async with self._serialized():
            if self._edit_cache.get(msg_id) == (text, MARKUP_ABSENT):
                return APPLIED
            ok = await _maybe_await(self.edit_message(self.topic_id, msg_id, text))
            if not ok:
                self._edit_cache.pop(msg_id, None)
                return FAILED
            self._edit_cache[msg_id] = (text, MARKUP_ABSENT)
            return APPLIED

    async def seal_narration(self) -> None:
        """Explicitly SEAL the open narration (nothing edits it again)."""
        async with self._serialized():
            self._seal_narration_locked()

    def _seal_narration_locked(self) -> None:
        """SEAL the open narration and drop its now-dead no-op edit-cache entry
        (a sealed message is never edited again — its cache key is unreachable,
        so retaining it only grows the cache unbounded)."""
        if self._narration_msg_id is not None:
            self._edit_cache.pop(self._narration_msg_id, None)
        self._narration_msg_id = None

    async def advance_high_water_for_inbound(
        self, operator_msg_id: int | None = None,
    ) -> None:
        """§2: an inbound operator message is a causal event — it advances the
        high-water mark at handler entry and SEALS open narration.

        (The inbound-spool call site is wired by T2; this is the machinery it
        invokes.)
        """
        async with self._serialized():
            self._seal_narration_locked()
            if operator_msg_id is not None:
                if self._high_water is None or operator_msg_id > self._high_water:
                    self._high_water = operator_msg_id

    # -- reply-threading (v0.79.0 §3, Primitive B) -------------------------

    def set_turn_reply_to(self, message_id: int | None) -> None:
        """Record the inbound operator message id that the turn's FIRST
        sequencer post should reply-thread to (§3). Overwrites any prior
        un-consumed value — the most-recently delivered envelope wins."""
        self._turn_reply_to = message_id

    def consume_turn_reply_to(self) -> int | None:
        """Return and CLEAR the pending reply-threading target (§3).

        Used by ask/reply posters (T3) so whichever output posts first this
        turn — narration open or a discrete send — threads to the operator's
        message and the rest do not."""
        mid = self._turn_reply_to
        self._turn_reply_to = None
        return mid

    def restore_turn_reply_to(self, message_id: int | None) -> None:
        """#332: failure-path undo for :meth:`consume_turn_reply_to` — an
        ask/reply poster that consumed the one-shot target but FAILED its send
        re-arms it so the turn's first successful output still threads. Never
        clobbers a newer target set by a later envelope."""
        if message_id is not None and self._turn_reply_to is None:
            self._turn_reply_to = message_id

    # -- intent registration (the T2/T3 ingress API) -----------------------

    def register_intent(
        self, *, request_id: str, tool_name: str, projection_hash: str, poster: Any,
        on_retire: Any = None,
    ) -> "tuple[SendIntent, bool] | object":
        """Register (or reattach to) a discrete-send intent (§2(1)). See
        :meth:`IntentRegistry.register`. Ingresses (T2/T3) call this at fence
        entry with a ``poster`` coroutine-factory that performs the actual
        keyboard/text post when the relay reaches the block. wb2-4: ``on_retire``
        (optional) fires when the intent is pruned at turn end.

        wb4-1: once :meth:`terminalize` has latched the engagement TERMINAL,
        registration is REJECTED — returns :data:`TERMINAL_REGISTRATION` (a
        recorded fail-closed terminal outcome, NOT ``None``, which would activate
        the ingress eager-fallback) so the ingress returns ``engagement_terminal``
        instead of registering + arming + posting below the terminal completion.
        The belt-and-suspenders latch inside :meth:`_post_intent_locked` catches
        any intent that raced in a beat before this latch."""
        if self._terminal:
            return TERMINAL_REGISTRATION
        return self.registry.register(
            request_id=request_id, tool_name=tool_name,
            projection_hash=projection_hash, poster=poster,
            on_retire=on_retire,
        )

    def set_intent_poster(self, request_id: str, poster: Any) -> SendIntent | None:
        """Install the REAL relay-invoked poster on a registered intent (§2(3),
        T3). The ask/reply ingress registers the intent early (for reattach
        idempotency), then sets this poster and ARMS — the relay invokes it when
        it reaches the intent's tool_use block."""
        return self.registry.set_poster(request_id, poster)

    def arm_intent(self, request_id: str) -> SendIntent | None:
        """Move a pending intent to ``armed`` — the point of no return (§2(2))."""
        intent = self.registry.arm(request_id)
        if intent is not None:
            self._arm_event.set()
        return intent

    def cancel_intent(self, request_id: str) -> SendIntent | None:
        """Cancel a pending/armed intent → tombstone (§2(2))."""
        intent = self.registry.cancel(request_id)
        if intent is not None:
            self._arm_event.set()
        return intent

    def intent_outcome(self, request_id: str) -> dict | None:
        """Recorded outcome for a reattaching retry (§2(1))."""
        intent = self.registry.by_request_id(request_id)
        return intent.outcome if intent is not None else None

    def record_intent_refusal(self, request_id: str, outcome: dict) -> SendIntent | None:
        """A2 (F-EXPIRE, Sol review Finding 1): the operator-away gate REFUSED
        this ask. Tombstone the intent (like :meth:`cancel_intent`) AND record a
        refusal OUTCOME so a same-``request_id`` transport retry REATTACHES to the
        SAME refusal rather than: awaiting a dead intent (→ ``delivery_failed``)
        or re-registering a fresh broker request (→ full timeout burn, no
        keyboard). Signals any fail-closed awaiter so a blocked
        :meth:`await_intent_resolution` unblocks with the refusal (never ok:true
        on a never-posted intent). No-op if the intent is unknown."""
        intent = self.registry.cancel(request_id)
        if intent is None:
            return None
        intent.outcome = dict(outcome)
        self._arm_event.set()
        self._signal_resolution(request_id)
        return intent

    def record_intent_cancelled_nowait(
        self, request_id: str, outcome: dict,
    ) -> str:
        """A3 · F-ORDER (Sol A3 wave 4): FULLY SYNCHRONOUS transport-cancellation
        cleanup against an in-flight relay post — "the post wins" — with NO awaits.

        The wave-3 predecessor acquired the writer lock (awaited), which left a
        DOUBLE-cancel window: a second ``Task.cancel()`` during that await
        interrupted the cleanup, dropping control into the handler's outer
        ``finally`` while a bound poster was mid-post. Being awaitless, this
        method can never be interrupted part-way, so no such window exists.

        Correctness (asyncio is single-threaded): ``posting`` is set/cleared under
        the writer lock in :meth:`_post_intent_locked`, and the attribute write
        COMMITS before any await — so a plain sync read here can never observe a
        torn / mid-mutation flag. If ``posting`` is True (or ``state == "posted"``,
        or a terminal outcome is recorded) the post has won and owns both the
        marker and the outcome — return ``False``, never clobber. Otherwise the
        relay has NOT bound this intent (a still-pending, or armed-and-unposted
        intent): synchronously TOMBSTONE it via the registry's sync
        :meth:`IntentRegistry.cancel`, record the ``cancelled`` outcome (so a
        same-``request_id`` retry reattaches), signal resolution, and return
        ``True``. A later relay bind then sees the tombstone and consume-cancels
        the block — it can never post.

        wb1-1 (whole-branch gate wave 1): returns an explicit TRI-STATE so the
        separate ``/ask_cancel`` route can decide marker ownership correctly (a
        single bool conflated "post won" with "no intent", and the route cleared
        the marker in BOTH — stripping it mid-post admitted a second live
        question). ``"cancelled"`` — the cancel took effect (caller clears the
        marker); ``"post_won"`` — an in-flight / resolved post owns the marker
        (caller MUST NOT clear it, the poster clears at durable ownership);
        ``"absent"`` — no such intent (nothing to post, caller may clear)."""
        intent = self.registry.by_request_id(request_id)
        if intent is None:
            return "absent"
        if (
            intent.posting
            or intent.state == "posted"
            or intent.outcome is not None
        ):
            return "post_won"  # the in-flight / resolved post wins — never clobber
        if intent.state not in ("pending", "armed"):
            return "post_won"  # already-resolved / tombstoned — leave the marker
        self.registry.cancel(request_id)  # sync tombstone (pending/armed → cancelled)
        intent.outcome = dict(outcome)
        self._arm_event.set()
        self._signal_resolution(request_id)
        return "cancelled"

    async def mark_intent_posted(
        self, request_id: str, message_id: int | None,
    ) -> Any:
        """Record that a discrete ingress POSTED its message out-of-band (the
        keyboard/reply was sent eagerly by the handler rather than deferred to
        the relay's content-block). Marks the intent ``posted`` and leaves the
        §2(5) one-block CONSUMPTION DEBT (``timeout_posted``) so the relay
        silently consumes the matching tool_use block — sealing open narration
        at that position — instead of binding a later same-hash intent or
        emitting stray narration. Records the outcome (incl. ``message_id``) for
        response-loss-after-post retry reattachment (§2(1))."""
        async with self._serialized():
            intent = self.registry.by_request_id(request_id)
            if intent is None:
                return None
            intent.state = "posted"
            intent.consumed = False
            intent.timeout_posted = True
            intent.message_id = message_id
            intent.outcome = {
                "ok": message_id is not None,
                "message_id": message_id,
                "out_of_band": True,
            }
            if message_id is not None and (
                self._high_water is None or message_id > self._high_water
            ):
                self._high_water = message_id
            self._signal_resolution(request_id)
            return intent

    async def mark_intent_compensated(
        self, request_id: str, message_id: int,
    ) -> Any:
        """A3(c) COMPENSATED-INTENT path (Sol r5-5 + r6-1): account a physical
        wire message whose logical result is FAILURE.

        The A3 initial-anchor poster posts first, then strict-persists the
        ledger entry; on an ``add_open_question`` failure the message EXISTS on
        the wire but the ask must resolve ok:false. The poster best-effort
        edits the orphan to a withdrawn-copy via the RAW wire primitive (never
        ``edit_discrete`` — it runs under the sequencer lock on the relay task,
        no reacquisition) and calls this to reconcile the sequencer's causal
        accounting.

        Pinned invariants (spec §A3(c)): ``_high_water`` advances to the
        delivered *message_id* (so a later ask opens BELOW the orphan, never
        beside it), the intent resolves EXACTLY ONCE with ``{"ok": False,
        "message_id": message_id, "compensated": True}``, and ``post_failed`` is
        NOT separately re-fired.

        Divergence from :meth:`mark_intent_posted` (which records an ok success
        with a one-block ``timeout_posted`` debt): here the outcome is ok:false
        with a ``compensated`` marker, the intent is RETIRED from matching
        (``consumed=True``, so no relay/watcher re-fire and no phantom debt),
        and — unlike :meth:`_post_intent_locked`'s failure branch —
        ``post_failed`` stays False (the post is not a fail-closed miss; the
        message physically landed). Idempotent: a repeat call after
        compensation is a no-op (no double resolution / high-water re-advance)."""
        async with self._serialized():
            intent = self.registry.by_request_id(request_id)
            if intent is None:
                return None
            if intent.outcome is not None and intent.outcome.get("compensated"):
                return intent  # already compensated — exactly-once
            # Sol r1 (#332): the compensated message PHYSICALLY landed below
            # the open narration — seal it, exactly like the confirmed-post
            # path in _post_intent_locked (pre-#332 the unconditional
            # pre-poster seal covered this; the seal is now landed-only).
            self._seal_narration_locked()
            intent.state = "posted"
            intent.consumed = True          # retired from matching
            intent.timeout_posted = False   # no consumption debt (not a success)
            intent.post_failed = False      # NOT a fail-closed re-fire
            intent.message_id = message_id
            intent.outcome = {
                "ok": False, "message_id": message_id, "compensated": True,
            }
            if self._high_water is None or message_id > self._high_water:
                self._high_water = message_id
            self._signal_resolution(request_id)
            return intent

    # -- F3 fail-closed resolution await -----------------------------------

    def _signal_resolution(self, request_id: str) -> None:
        ev = self._resolution_events.get(request_id)
        if ev is not None:
            ev.set()

    @property
    def post_await_budget(self) -> float:
        """The bounded transport budget a deferred handler waits for a post to
        resolve (§4/T3-fix, F3): the slot hold plus the intent timeout plus a
        small margin so the 10s out-of-band watcher post is always covered."""
        return self._slot_hold_s + self._intent_timeout_s + 2.0

    async def await_intent_resolution(
        self, request_id: str, timeout: float | None = None,
    ) -> dict | None:
        """F3: block until intent *request_id* posts (ok) or fails (ok:false),
        bounded by *timeout* (defaults to :attr:`post_await_budget`). Returns the
        recorded outcome dict, or ``None`` if the intent is unknown or still
        unresolved at timeout (the caller treats a missing/failed outcome as
        ok:false — never ok:true). Edge-triggered: the outcome is checked under
        the lock BEFORE waiting, so a post that lands before the awaiter blocks
        is never missed."""
        if timeout is None:
            timeout = self.post_await_budget
        async with self._serialized():
            intent = self.registry.by_request_id(request_id)
            if intent is None:
                return None
            if intent.outcome is not None:
                return intent.outcome
            ev = self._resolution_events.get(request_id)
            if ev is None:
                ev = asyncio.Event()
                self._resolution_events[request_id] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        intent = self.registry.by_request_id(request_id)
        return intent.outcome if intent is not None else None

    async def await_completion_drain(
        self, request_id: str, timeout: float | None = None,
    ) -> bool:
        """F3: block until the relay CONSUMES the given consumption-debt intent
        (the emit_completion debt) — i.e. reaches its content block, having
        processed every PRIOR frame — so a completion post can never overtake
        lagging prior-frame narration.

        Bounded by *timeout* (default: the slot hold). Returns ``True`` when the
        debt has been consumed (or the intent is unknown / already consumed),
        ``False`` on timeout (the caller WARNs and proceeds — the ONE documented,
        bounded weakening). Edge-triggered: consumption is checked under the lock
        BEFORE waiting, so a consume that lands before the awaiter blocks is
        never missed."""
        if timeout is None:
            timeout = self._slot_hold_s
        async with self._serialized():
            intent = self.registry.by_request_id(request_id)
            if intent is None or intent.consumed:
                return True
            ev = self._resolution_events.get(request_id)
            if ev is None:
                ev = asyncio.Event()
                self._resolution_events[request_id] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        intent = self.registry.by_request_id(request_id)
        return intent is None or intent.consumed

    # -- F1(b) platform-origin discrete send -------------------------------

    async def post_platform_notice(
        self, text: str, *, reply_to: int | None = None,
    ) -> int | None:
        """F1(b): post a PLATFORM-ORIGIN discrete message (an inbound receipt /
        eviction / capacity notice) through the single writer.

        These have no subprocess frame, so they register no intent — but they
        MUST NOT post around the sequencer: a receipt slipping in below open
        narration while ``edit_narration_if_latest`` still returns APPLIED is
        exactly the ordering violation §2 forbids. So this seals open narration
        (the notice is a causal event below it), posts, and advances the
        high-water mark, all under the one serialization lock. Returns the posted
        message id, or ``None`` on send failure.

        wb4-2: DISCARDED (log + return ``None``, posting NOTHING) once the
        engagement has TERMINALIZED — no platform notice (an inbound receipt, a
        lagging mutating-tool violation notice) may land BELOW the terminal
        completion (D5 discard doctrine). The terminal completion text itself
        uses the dedicated :meth:`post_completion_notice` seam, the one path that
        bypasses the latch."""
        async with self._serialized():
            if self._terminal:
                logger.debug(
                    "engagement %s: platform notice discarded (terminal latch)",
                    self.engagement_id)
                return None
            return await self._post_notice_locked(text, reply_to)

    async def post_completion_notice(self, text: str) -> int | None:
        """wb4-2: the completion-only write seam. Posts the TERMINAL completion
        text through the single writer even though the terminal latch is set — it
        IS the terminal message every other post is forbidden to land below. No
        other caller may post post-terminal; :meth:`post_platform_notice`
        discards under the latch.

        v0.109.0 (G5): when a paged sender is injected, the completion posts
        through it — an over-cap summary becomes several rendered pages, all
        inside THIS one serialized write (an indivisible batch; nothing can
        interleave). The paged sender returns the LAST page's message_id —
        the correct high-water anchor."""
        async with self._serialized():
            if self._send_paged is not None:
                mid = await _maybe_await(self._send_paged(self.topic_id, text))
                if mid is None:
                    return None
                # #392: seal only after a CONFIRMED send (the #332 contract) —
                # a definite failure posts nothing below the narration, so it
                # must stay open and editable. The writer lock is held
                # throughout, so nothing interleaves between send and seal.
                self._seal_narration_locked()
                if self._high_water is None or mid > self._high_water:
                    self._high_water = mid
                return mid
            return await self._post_notice_locked(text, None)

    async def _post_notice_locked(
        self, text: str, reply_to: int | None,
    ) -> int | None:
        """Shared platform/completion post body (caller holds the lock): send,
        then — only on a CONFIRMED send — seal open narration (the notice is a
        causal event below it) and advance the high-water mark.

        #392: the seal follows the #332 contract post_discrete established —
        a definite send failure (wire wrapper returned ``None``) is no state
        change, so the narration stays open and editable; nothing landed below
        it. The writer lock is held throughout, so nothing can interleave
        between the send and the seal."""
        if reply_to is not None:
            mid = await _maybe_await(
                self.send_message(self.topic_id, text, reply_to=reply_to))
        else:
            mid = await _maybe_await(self.send_message(self.topic_id, text))
        if mid is None:
            return None
        self._seal_narration_locked()
        if self._high_water is None or mid > self._high_water:
            self._high_water = mid
        return mid

    # -- A9 keyboard-bearing discrete writes (Sol r1-8) --------------------

    def _register_discrete_cache(self, mid: int) -> None:
        """Register *mid* in the bounded discrete-cache FIFO, evicting the
        oldest discrete ``_edit_cache`` entry past the cap.

        Re-registering an existing mid moves it to the tail (most-recent), so a
        repeatedly-edited keyboard is not evicted ahead of a stale one. Only
        entries created by :meth:`post_discrete`/:meth:`edit_discrete` are in
        this FIFO — narration (retired on seal) and summary (append-above)
        entries are never touched by eviction."""
        if mid in self._discrete_cache_fifo:
            self._discrete_cache_fifo.remove(mid)
        self._discrete_cache_fifo.append(mid)
        while len(self._discrete_cache_fifo) > _DISCRETE_CACHE_CAP:
            evicted = self._discrete_cache_fifo.popleft()
            self._edit_cache.pop(evicted, None)

    def _forget_discrete_cache(self, mid: int) -> None:
        """Drop *mid* from the discrete FIFO (a FAILED edit invalidated its
        cache entry, so it must not linger as a phantom FIFO slot)."""
        if mid in self._discrete_cache_fifo:
            self._discrete_cache_fifo.remove(mid)

    async def post_discrete(
        self, text: str, *, markup: Any = None, reply_to: int | None = None,
        revalidate: Any = None, wire_timeout: float | None = None,
    ) -> int | None:
        """A9: post a keyboard-bearing DISCRETE message through the single writer
        (A3 anchor re-anchor). Mirrors :meth:`post_platform_notice` but sends via
        the markup-capable wire and maintains the F1 tri-state cache.

        Under the writer lock: run *revalidate* (sync or async — the A3
        answered/reserved final check) immediately before the send; a declined
        revalidation returns ``None`` with NO send and NO state change. On a
        successful send: SEAL open narration (the discrete message is a causal
        event below it), advance ``_high_water`` to the returned mid, seed the
        tri-state ``_edit_cache`` entry, and register the mid in the bounded
        discrete-cache FIFO. *reply_to* threads like the other sends.

        ``wire_timeout`` (v0.84.0, §D6 r17-2) puts an ``asyncio.wait_for`` BUDGET
        around the single wire await — default ``None`` keeps every other
        caller's behaviour unchanged. A timeout returns ``None`` with NO state
        change; the re-anchor unit treats that None (like any un-confirmed send)
        as an AMBIGUOUS send and takes the documented floor WITHOUT a wire retry
        (the wrapper cannot distinguish "not sent" from "accepted before the
        timeout").

        Deliberately NOT wrapped around ``ensure_posted`` posters (Sol r4-5): that
        runs its poster in a NEW task, which would deadlock against the
        relay-held, task-reentrant-only writer lock. Raises RuntimeError if the
        markup wire was not injected."""
        if self._send_message_markup is None:
            raise RuntimeError(
                "post_discrete requires an injected send_message_markup wire "
                "primitive (driver _ensure_sequencer wiring)")
        async with self._serialized():
            # wb5-1 (whole-branch gate wave 5): consult the TERMINAL latch under
            # the writer lock — the same DISCARDED/refusal semantics the wave-3
            # narration writers got — so a discrete send whose pass crossed
            # terminalization mid-flight (or any caller without a terminal-aware
            # ``revalidate``) posts NOTHING below the terminal completion. Returns
            # ``None`` (no send, no state change), which the re-anchor unit treats
            # as an un-confirmed send and floors without a wire retry. Belt-and-
            # suspenders with the driver's D6 pass-entry + locked-revalidate
            # terminal guard.
            if self._terminal:
                return None
            if revalidate is not None and not await _maybe_await(revalidate()):
                return None
            send = _maybe_await(self._send_message_markup(
                self.topic_id, text, markup, reply_to=reply_to))
            if wire_timeout is None:
                mid = await send
            else:
                try:
                    mid = await asyncio.wait_for(send, wire_timeout)
                except asyncio.TimeoutError:
                    # Sol r1 (#332): a TIMED-OUT send is AMBIGUOUS — Telegram
                    # may have accepted the message before the response was
                    # lost (§D6 r17-2 semantics). Seal conservatively so later
                    # streamed text can never edit narration ABOVE a keyboard
                    # that may exist below it. A confirmed None return (the
                    # wire wrapper caught a definite failure) stays
                    # no-state-change per the #332 contract.
                    self._seal_narration_locked()
                    return None
            if mid is None:
                return None
            # #332: seal only after a CONFIRMED send — the None/timeout
            # contract above is "no state change", and pre-sealing dropped
            # the open narration (and its edit-cache entry) with no discrete
            # message ever posted. The writer lock is held throughout, so
            # nothing can interleave between the send and this seal.
            self._seal_narration_locked()
            if self._high_water is None or mid > self._high_water:
                self._high_water = mid
            self._edit_cache[mid] = (text, _discrete_markup_tristate(markup))
            self._register_discrete_cache(mid)
            return mid

    async def edit_discrete(
        self, msg_id: int, *, text: Any = None, markup: Any = _ABSENT,
        revalidate: Any = None, wire_timeout: float | None = None,
    ) -> bool:
        """A9: markup-capable edit of a discrete message through the F1 tri-state
        no-op cache (A5 toggle redraw / multi settle edit).

        Touches NEITHER narration NOR high-water — it edits HISTORY, like
        :meth:`edit_summary`. ``text=None`` means a markup-only edit (a stable
        cache representation distinct from a text edit). Under the writer lock:
        run *revalidate* (the A5 terminal-race guard; declined → ``False``, no
        edit); F1 no-op gate — an identical ``(text, markup-tristate)`` returns
        ``True`` without any wire call; otherwise wire-edit and update the cache.

        ``wire_timeout`` (v0.84.0, §D6 r17-2) puts an ``asyncio.wait_for`` BUDGET
        around the single wire await — default ``None`` keeps every other
        caller's behaviour unchanged. A timeout counts as a FAILED attempt
        (``False`` + cache invalidated), so the re-anchor unit's finite-attempt
        marker edit retries within its budget rather than blocking unbounded.

        **Returns ``bool``, deliberately NOT the APPLIED/FAILED string codes
        (Sol r2-10):** every settle path feeds ``confirmed_settle_edit``, whose
        gate is ``bool(await do_edit())`` — the string ``"failed"`` is truthy and
        would count a failed wire edit as CONFIRMED, deleting the recovery
        record. ``True`` ⇔ applied or no-op-skip; ``False`` ⇔ failed or
        revalidation-declined. Raises RuntimeError if the markup wire was not
        injected."""
        if self._edit_message_markup is None:
            raise RuntimeError(
                "edit_discrete requires an injected edit_message_markup wire "
                "primitive (driver _ensure_sequencer wiring)")
        async with self._serialized():
            if revalidate is not None and not await _maybe_await(revalidate()):
                return False
            tri = _discrete_markup_tristate(markup)  # F4: None ⇒ CLEAR, not ABSENT
            if self._edit_cache.get(msg_id) == (text, tri):
                return True  # no-op skip — no wire call
            edit = _maybe_await(self._edit_message_markup(
                self.topic_id, msg_id, text, markup))
            if wire_timeout is None:
                ok = await edit
            else:
                try:
                    ok = await asyncio.wait_for(edit, wire_timeout)
                except asyncio.TimeoutError:
                    ok = False
            if not ok:
                self._edit_cache.pop(msg_id, None)   # invalidate → retry allowed
                self._forget_discrete_cache(msg_id)
                return False
            self._edit_cache[msg_id] = (text, tri)
            self._register_discrete_cache(msg_id)
            return True

    # -- F1(c) / F4 turn-boundary drain ------------------------------------

    async def flush_armed_intents(self) -> None:
        """F4: post every still-armed, un-posted intent out-of-band (WARN) so it
        RESOLVES before its registry entry is pruned. Turn-end pruning
        (topic_stream ``_finalize``) and finalize must never silently drop a
        late armed intent — a discrete send the agent believes is in flight
        would vanish and its awaiter would hang. Runs the same post path as the
        10s watcher; a failed post surfaces ok:false via F3."""
        async with self._serialized():
            for intent in self.registry.armed_unposted():
                await self._post_intent_locked(
                    intent, out_of_band=True, warn=True)

    def prune_turn(self) -> None:
        """§2(6): prune intents/tombstones/outcomes at turn end.

        Also CLEARS the causal-handoff one-shot reply anchor (§4): it "expires at
        turn end". A set-but-unconsumed anchor (a button answer that continued
        the turn but produced no output) must NOT leak into the next turn and
        mis-thread its first message.

        F3/F4: signal every outstanding resolution event before dropping them so
        an awaiter blocked past turn end unblocks (reading a resolved ok:false /
        ``None`` outcome) rather than hanging its transport budget out.

        NOTE (F6): ``topic_stream._finalize`` no longer calls this directly —
        it uses :meth:`drain_and_prune_turn`, which drains armed intents and
        prunes under ONE lock hold so a late-armed intent can't be dropped
        between a flush and this prune. This method stays for tests and any
        caller that needs a bare synchronous prune."""
        for ev in self._resolution_events.values():
            ev.set()
        self._resolution_events.clear()
        self.registry.prune()
        self._turn_reply_to = None

    async def drain_and_prune_turn(self) -> None:
        """F4+F6: atomically drain every still-armed intent, then prune + seal —
        under ONE lock hold.

        Replaces the former ``flush_armed_intents`` → ``prune_turn`` →
        ``seal_narration`` sequence in ``topic_stream._finalize``, which took and
        RELEASED the lock between those three steps. That gap let a late ingress
        register+arm an intent B during a flush poster-await AFTER the flush had
        snapshotted the armed set; ``prune_turn`` then deleted B before it could
        post (F6: intent B silently dropped, its awaiter left hanging / failing).

        Here the armed set is RE-SNAPSHOTTED under the held lock until it is
        empty, so every armed intent RESOLVES (posts out-of-band with a WARN, or
        fails closed via F3) first. There is NO await between the final
        empty-check and the synchronous prune, so a lock-free ``register_intent``
        /``arm_intent`` cannot slip an armed intent in between the two."""
        async with self._serialized():
            while True:
                pending = self.registry.armed_unposted()
                if not pending:
                    break
                for intent in pending:
                    await self._post_intent_locked(
                        intent, out_of_band=True, warn=True)
            # Registry is now stable (no await below until the clear).
            for ev in self._resolution_events.values():
                ev.set()
            self._resolution_events.clear()
            self.registry.prune()
            self._turn_reply_to = None
            self._seal_narration_locked()

    async def terminalize(self) -> None:
        """wb3-1/wb3-2/wb3-3: latch the engagement TERMINAL, abort every
        unresolved intent, and PRUNE the registry — all under the ONE writer
        lock, idempotent.

        Called at the START of the terminal finalize funnel (the driver's
        ``settle_all_open_questions``), and again as a backstop at sequencer
        teardown before ``_sequencers.pop``. What it guarantees:

        * **wb3-2 latch.** Sets :attr:`_terminal`, after which every locked
          narration writer (``open_narration`` / ``edit_narration_if_latest`` /
          ``post_unless_anchor_open``) DISCARDS rather than writing — nothing
          can land below the terminal completion, even a write that was blocked
          on the writer lock at the instant of terminalization.

        * **wb3-1 (c) in-flight poster wins.** Acquiring the writer lock waits
          for any poster CURRENTLY mid-post (a poster holds the lock across its
          whole wire send + ``add_open_question`` ledger write). So when this
          runs, that poster has already durably ledgered its question, and the
          settlement pass that follows this call includes it.

        * **wb3-1 (a) abort unresolved.** A still-pending / armed-but-unposted
          intent (no recorded outcome, not posted) is ABORTED: tombstoned,
          retired from matching (``post_failed`` — so a later ``flush_armed_
          intents`` / relay block never invokes its poster), resolved
          ``ok:false`` so its fail-closed awaiter wakes. It never posts, so it
          never ledgers a question the closed engagement would retain.

        * **wb3-1 (b) reject late registrations** — enforced by the poster's own
          terminal re-read and the latch on the narration writers.

        * **wb3-3 release gate pins.** Pruning fires every intent's
          ``on_retire`` hook EXACTLY once (the wb2-4 validation-gate pin
          release), so a terminal transition that precedes the relay's
          ``result`` can never leave a gate pinned forever.
        """
        async with self._serialized():
            if self._terminal:
                return
            self._terminal = True
            for intent in self.registry.all_intents():
                # Leave anything already resolved / posted / retired untouched —
                # its accounting (incl. an in-flight poster's just-landed ledger
                # entry) stands.
                if (
                    intent.consumed
                    or intent.post_failed
                    or intent.outcome is not None
                    or intent.state == "posted"
                ):
                    continue
                # Pending or armed-and-unposted → ABORT (never posts).
                self.registry.cancel(intent.request_id)  # → tombstone
                intent.post_failed = True                # retire from matching
                intent.outcome = {
                    "ok": False, "message_id": None, "terminal": True,
                }
                self._signal_resolution(intent.request_id)
            # Wake every outstanding awaiter, then PRUNE (fires on_retire once
            # per intent) so the gate pins release before the sequencer is
            # dropped — even if the relay never processes ``result`` (wb3-3).
            # wb4-2: PRESERVE an unresolved emit_completion consumption debt so
            # ``finalize_completion_post``'s ``await_completion_drain`` still
            # waits for the relay to reach its causal block (a pruned debt reads
            # as trivially drained, letting a lagging frame's platform notice
            # overtake completion). Its resolution event is re-created lazily by
            # ``await_completion_drain`` — it has no ``on_retire`` pin to leak.
            for ev in self._resolution_events.values():
                ev.set()
            self._resolution_events.clear()
            self.registry.prune(keep=_is_live_completion_debt)
            self._turn_reply_to = None

    # -- discrete posting driven by the relay at a content-block ------------

    async def post_for_block(self, tool_name: str, block_hash: str) -> str:
        """Resolve a content-block position (§2(3)-(4)).

        Returns one of ``"posted"`` / ``"consumed_cancelled"`` /
        ``"debt_consumed"`` / ``"no_match"`` / ``"slot_timeout"``. Blocks:

        * armed intent  → seal narration, post at THIS position, mark posted;
        * cancelled tombstone → the block is consumed-cancelled (nothing posts);
        * timeout-posted debt → the block is consumed silently (§2(5));
        * pending intent OR absent-but-hold-eligible → HOLD up to the slot
          (§2(4)); on timeout mark a still-pending intent ``slot_missed`` and
          proceed (its late post happens out-of-band via the watcher).
        * absent and not hold-eligible → ``no_match`` immediately.
        """
        deadline = self._now() + self._slot_hold_s
        while True:
            async with self._serialized():
                resolved = await self._resolve_block_locked(tool_name, block_hash)
                if resolved is not None:
                    return resolved
            if self._now() >= deadline:
                async with self._serialized():
                    # Last-look under the lock: an intent that armed exactly at
                    # the deadline still posts at THIS block; a still-pending one
                    # is marked slot_missed (its late post happens out-of-band).
                    resolved = await self._resolve_block_locked(tool_name, block_hash)
                    if resolved is not None:
                        return resolved
                    item = self.registry.oldest_matchable(tool_name, block_hash)
                    if item is not None and item.state == "pending":
                        item.slot_missed = True
                return "slot_timeout"
            await self._sleep(self._hold_poll_s)

    async def _resolve_block_locked(
        self, tool_name: str, block_hash: str,
    ) -> str | None:
        """Resolve a content block against the registry (caller holds the lock).

        Returns a terminal result code, or ``None`` when the block must HOLD
        (a matching intent is still pending) — i.e. the caller keeps waiting.
        """
        item = self.registry.oldest_matchable(tool_name, block_hash)
        if item is not None:
            if item.state == "armed":
                await self._post_intent_locked(item, out_of_band=False)
                return "posted"
            if item.state == "cancelled":
                item.consumed = True
                return "consumed_cancelled"
            if item.state == "posted" and item.timeout_posted:
                # §2(5) debt consumed. SEAL open narration at this block's
                # position: the debt's message was posted out-of-band (the
                # timeout out-of-band post, or an eager ask/reply ingress post),
                # so narration up to this causal point must close — nothing may
                # edit/append to it below the discrete message.
                self._seal_narration_locked()
                item.consumed = True
                # F3: unblock a completion drain waiting for this debt — the
                # relay reaching the emit_completion block means every prior
                # frame has been processed.
                self._signal_resolution(item.request_id)
                return "debt_consumed"
            return None  # pending → HOLD
        if tool_name not in HOLD_ELIGIBLE_TOOLS:
            # Non-fenced tool: keep the fast path — never stall narration.
            return "no_match"
        # F4: hold-eligible block (ask/reply/emit_completion) with NO matching
        # intent yet. The empty-registry short-circuit that used to proceed here
        # DEFEATED the designed relay-first race: the discrete ingress (an MCP
        # call landing in milliseconds) may register its intent a beat AFTER the
        # relay reads this block. HOLD the 2s slot regardless of registry
        # emptiness; on slot timeout ``post_for_block`` proceeds and a genuinely
        # absent intent costs only the bounded hold.
        return None  # hold-eligible → HOLD for the slot

    async def _post_intent_locked(
        self, intent: SendIntent, *, out_of_band: bool, warn: bool = False,
    ) -> None:
        """Post *intent* (caller holds the lock). SEALS open narration first —
        rollover-on-interleave (§2, "narration seals when anything else posts
        below")."""
        # wb4-1(b): belt-and-suspenders terminal latch, enforced under the writer
        # lock for EVERY send-intent kind. An intent that raced past
        # ``register_intent``'s latch (registered a beat before ``terminalize``,
        # then armed) must NOT post below the terminal completion — resolve it
        # fail-closed (terminal ok:false) instead. A recorded outcome (a
        # compensated self-account, an already-resolved intent) is never
        # clobbered.
        if self._terminal and intent.outcome is None:
            intent.post_failed = True
            intent.consumed = True
            intent.outcome = {"ok": False, "message_id": None, "terminal": True}
            self._signal_resolution(intent.request_id)
            return
        # Sol r1 (#332): the seal moved to the CONFIRMED-post paths below —
        # a poster that fails (returns None, no compensation) must leave the
        # open narration editable, exactly like a failed post_discrete. The
        # writer lock is held across poster + seal, so order is preserved.
        if warn:
            logger.warning(
                "output sequencer: intent %s (%s) unmatched by any block for "
                "%.0fs — posting out-of-band (engagement %s)",
                intent.request_id, intent.tool_name, self._intent_timeout_s,
                self.engagement_id,
            )
        # A3 · F-ORDER (Sol A3 wave 3/4): mark the intent as being-posted for the
        # WHOLE poster await, under the lock. A synchronous
        # ``record_intent_cancelled_nowait`` that observes ``posting`` NO-OPS — the
        # in-flight post wins. ``posting`` is written before the first await below,
        # so a sync read from another task never sees a torn flag. Cleared in
        # ``finally`` so a poster failure still re-opens the intent for its
        # fail-closed resolution below.
        intent.posting = True
        try:
            if callable(intent.poster):
                mid = await _maybe_await(intent.poster())
            else:
                mid = await _maybe_await(
                    self.send_message(self.topic_id, str(intent.poster))
                )
        except asyncio.CancelledError:
            # Sol r2 (#332): the poster may have SENT and then been cancelled
            # during its post-send bookkeeping — ambiguous, exactly like a
            # wire timeout. Seal conservatively (later narration can never
            # edit above a message that may exist below it) and resolve
            # fail-closed, retiring the intent from matching so the watcher
            # can never repost a possibly-landed message. A compensated
            # self-account recorded before the cancellation wins untouched.
            if intent.outcome is None:
                self._seal_narration_locked()
                intent.post_failed = True
                intent.consumed = True
                intent.outcome = {
                    "ok": False, "message_id": None,
                    "out_of_band": out_of_band, "cancelled": True,
                }
                self._signal_resolution(intent.request_id)
            raise
        except Exception as exc:  # noqa: BLE001 — a poster failure must not wedge
            logger.warning(
                "output sequencer: intent %s poster failed: %s",
                intent.request_id, exc,
            )
            mid = None
        finally:
            intent.posting = False
        if mid is None:
            # §A3(c): the poster may have SELF-ACCOUNTED a compensated physical
            # write (initial-anchor add-failure): it posted the wire message,
            # then called ``mark_intent_compensated`` (reentrant under this lock)
            # which already resolved the intent ok:false+compensated and advanced
            # high-water, and returned None. Do NOT re-resolve as a plain
            # post-failure — that would clobber the mid + high-water accounting.
            if intent.outcome is not None and intent.outcome.get("compensated"):
                return
            # F3 fail-closed: the post did NOT land. Do NOT terminally consume
            # the intent as if it succeeded (no consumption debt claiming a
            # phantom post). Mark it post-failed (retired from matching so the
            # relay/watcher never silently re-fires it), record an ok:false
            # outcome, and resolve so the awaiting handler returns ok:false —
            # the agent learns the send failed instead of a swallowed error.
            intent.post_failed = True
            intent.outcome = {
                "ok": False, "message_id": None, "out_of_band": out_of_band,
            }
            self._signal_resolution(intent.request_id)
            return
        # A3 · F-ORDER (Sol A3 wave 3/4) defensive no-double-resolve: with the
        # synchronous ``record_intent_cancelled_nowait`` (which NO-OPS while
        # ``posting`` is set) a terminal outcome can NEVER already be recorded when
        # a successful post resolves here. If one somehow is, the recorded terminal
        # outcome WINS — log and do not overwrite it (this branch should be
        # unreachable).
        if intent.outcome is not None:
            logger.warning(
                "output sequencer: intent %s post resolved (mid=%s) but a "
                "terminal outcome %r is already recorded — NOT double-resolving "
                "(A3 · F-ORDER; engagement %s)",
                intent.request_id, mid, intent.outcome, self.engagement_id,
            )
            return
        # Sol r1 (#332): the discrete message LANDED below the open narration —
        # seal it now (rollover-on-interleave, §2), on the confirmed path only.
        self._seal_narration_locked()
        intent.state = "posted"
        intent.consumed = True
        self._high_water = mid
        intent.message_id = mid
        intent.outcome = {
            "ok": True, "message_id": mid, "out_of_band": out_of_band,
        }
        self._signal_resolution(intent.request_id)

    async def process_intents_once(self) -> None:
        """One late/timeout intent pass (§2(4) late-arm, §2(5) timeout).

        Deterministic seam driven directly by unit tests; the background
        :meth:`run_watcher` calls it on a tick. Under the lock: a
        ``slot_missed`` armed intent posts out-of-band THREADED (no warn, no
        debt — its block already passed); an armed intent unmatched for
        ``intent_timeout_s`` posts out-of-band with a WARN and leaves a
        one-block consumption debt so its late frame is consumed silently.
        """
        async with self._serialized():
            self._arm_event.clear()
            now = self._now()
            for intent in self.registry.armed_unposted():
                if intent.slot_missed:
                    await self._post_intent_locked(intent, out_of_band=True)
                    self._log_late_post(intent, reason="slot_missed")
                # #332: the documented timeout applies to an ARMED intent
                # waiting for its block — measure from arm time, never from
                # registration (an ingress can sit pending through validation
                # past the whole timeout and must not post out-of-band the
                # moment it arms). Fallback to registered_at only for an
                # intent armed by a path that bypassed IntentRegistry.arm().
                elif (now - (intent.armed_at
                             if intent.armed_at is not None
                             else intent.registered_at)
                        >= self._intent_timeout_s):
                    await self._post_intent_locked(
                        intent, out_of_band=True, warn=True,
                    )
                    # §2(5): leave the one-block consumption debt. The intent is
                    # ``posted`` but stays matchable via ``timeout_posted`` so a
                    # subsequent same-hash intent can never bind its late block.
                    intent.consumed = False
                    intent.timeout_posted = True
                    self._log_late_post(intent, reason="timeout")

    def _log_late_post(self, intent: SendIntent, *, reason: str) -> None:
        """F-OOB instrumentation (spec D7): content-free INFO log at the
        LATE-POST WATCHER path (:meth:`process_intents_once`) — the
        counterpart to ``drivers.topic_stream``'s ``_match_discrete_block``
        match-point log for a block that earlier resolved ``slot_timeout``.

        Carries ONLY the pinned projection-hash PREFIX (8 hex — never the
        projected args), the post's block-resolution result (``posted`` /
        ``post_failed``, derived from the recorded outcome), the intent's
        state, the tool name and engagement id (system identifiers, not
        operator content), and the registration-to-post latency in ms.
        *reason* distinguishes a ``slot_missed`` late post (posts immediately
        on completion, per the A6 timing fix) from a full ``intent_timeout_s``
        out-of-band post. Together with the match-point log, this carries
        enough timing to reconstruct the observed F-OOB ~10s gap (Sol r1-8:
        likely the 2s slot hold + 10s intent timeout, not a hash defect).
        Pure logging — no state is read or written beyond what
        ``process_intents_once`` already mutated; NO behavioral change."""
        ok = bool(intent.outcome and intent.outcome.get("ok"))
        logger.info(
            "oob_late_post hash=%s result=%s intent_state=%s latency_ms=%.1f "
            "reason=%s tool=%s engagement=%s",
            intent.projection_hash[:8], "posted" if ok else "post_failed",
            intent.state, (self._now() - intent.registered_at) * 1000.0,
            reason, intent.tool_name, self.engagement_id,
        )

    async def run_watcher(self) -> None:  # pragma: no cover - background loop
        """Background task: drive late/timeout discrete posts (§2). Wakes on an
        arm/cancel signal or a periodic tick (for the 10s timeout)."""
        while True:
            try:
                await asyncio.wait_for(
                    self._arm_event.wait(), timeout=self._hold_poll_s * 4,
                )
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            try:
                await self.process_intents_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "output sequencer watcher error (engagement %s): %s",
                    self.engagement_id, exc,
                )


async def _default_sleep(seconds: float) -> None:  # pragma: no cover - trivial
    await asyncio.sleep(seconds)
