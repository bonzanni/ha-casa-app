"""HTTP handlers for /internal/channel/* — POSTed by casa_engagement_channel
over the casa-main Unix socket. Phase 1 exposes /internal/channel/send_to_topic
only; later phases extend the dict returned by ``_make_channel_handlers``.

Body shape: ``{engagement_id: str, ...fields per handler}``.

Response shape:
- success: ``{"ok": True, "message_id": int}``
- known failure: ``{"ok": False, "error": <code>}``

Error codes (Phase 1):
- ``bad_json`` — request body was not valid JSON / not a dict
- ``missing_engagement_id`` — body missing/falsy ``engagement_id``
- ``unknown_engagement`` — registry.get returned None
- ``no_topic_bound`` — engagement record carries no ``topic_id``
- ``send_failed`` — the underlying telegram call raised
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from typing import Any, Awaitable, Callable

from aiohttp import web
from telegram.error import BadRequest

from settle_gate import confirmed_settle_edit
from text_util import utf16_len

logger = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# ---------------------------------------------------------------------------
# Module-level state — ask validation gate (D1, round 4, Task A4)
# ---------------------------------------------------------------------------
#
# Sol r9-1/r10-1/r10-2 protocol (design ref: docs/superpowers/specs/2026-07-16-
# engagement-ask-labels-round4-design.md §D1 "Validator placement"). A
# concurrent reattacher can win broker creation before the owner finishes
# post-allocation size validation; without a handshake, a refusal by the
# owner would leave the reattacher burning its full timeout against a broker
# nobody will ever commit (Sol r3-1). The fix is a per-request_id validation
# gate: NO path — owner or reattacher — registers a broker request / arms a
# send-intent poster until the gate resolves PASSED. This module supplies
# the self-consistent gate primitive only; wiring it into the ask/ask_cancel/
# reattach handlers is Task A5.
#
# Gate state = TWO components (r9-1 BLOCKER — a single once-only slot cannot
# express PASSED -> CANCELLED):
#   (a) an IMMUTABLE completion slot (PASSED / FAILED(payload)), set exactly
#       once, owner-only (the single validation owner — the original ask
#       call, never a reattacher);
#   (b) a MONOTONIC cancellation latch, settable by ANYONE at ANY time —
#       before the gate even has a completion (the get-or-create tombstone,
#       r5-1), during validation, or AFTER PASSED (r9-1: ``ask_cancel``
#       fires whenever the original call exits without a response,
#       regardless of what the owner already decided).
# ONE ``asyncio.Event`` is set by whichever of cancellation or completion
# happens FIRST (and stays set), so a reattacher already blocked in
# ``await gate.event.wait()`` wakes on latch-only cancellation, not just
# completion (r10-1). Every reader snapshots latch-first-then-completion via
# :meth:`AskValidationGate.effective` — CANCELLED always wins over a
# completed PASSED/FAILED, which in turn wins over PENDING.


class AskValidationGate:
    """Per-request_id validation gate (spec §D1). ``completion`` is
    owner-only and immutable once set; ``cancelled`` is a monotonic latch
    anyone may set at any time (including after ``PASSED``); one ``event``
    wakes waiters on whichever terminal arrives first. See the module
    docstring above for the executable protocol (Sol r9-1/r10-1)."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.completion: tuple[str, dict | None] | None = None
        self.cancelled: bool = False
        self.event = asyncio.Event()
        self._clock = clock
        self._refcount = 0
        self._resolved_at: float | None = None

    # -- completion: owner-only, immutable once set -------------------------

    def set_passed(self) -> None:
        """Validation PASSED. Owner-only; no-op if ``completion`` is already
        set — a second call (or a call after a FAILED/cancelled gate) never
        clobbers the first resolution."""
        if self.completion is not None:
            return
        self.completion = ("PASSED", None)
        self._resolve()

    def set_failed(self, payload: dict) -> None:
        """Validation FAILED with *payload* — the EXACT response body every
        current and future reattacher gets back byte-identical (r10-1/
        r10-2). Owner-only; no-op if ``completion`` is already set."""
        if self.completion is not None:
            return
        self.completion = ("FAILED", payload)
        self._resolve()

    # -- cancellation: monotonic latch, settable by anyone, any time --------

    def set_cancelled(self) -> None:
        """Set the cancellation latch. Monotonic (idempotent) and NEVER
        gated by ``completion`` — ``ask_cancel`` may call this before the
        gate has any completion, during validation, or after ``PASSED``
        (r9-1/r10-1: cancellation is orthogonal to, and always overrides,
        the completion slot)."""
        self.cancelled = True
        self._resolve()

    # -- shared terminal bookkeeping -----------------------------------------

    def _resolve(self) -> None:
        """Record the FIRST terminal-resolution timestamp (retention reads
        it) and wake every current/future waiter. Idempotent: a SECOND
        terminal event (e.g. a cancel arriving after ``PASSED``) re-sets an
        already-set event harmlessly and never moves ``_resolved_at``."""
        if self._resolved_at is None:
            self._resolved_at = self._clock()
        self.event.set()

    def effective(self) -> tuple[str, dict | None]:
        """Latch-first snapshot (r9-1/r10-1): CANCELLED beats ``completion``,
        which beats PENDING. Every consumer — owner, reattacher, poster —
        reads this instead of the raw fields."""
        if self.cancelled:
            return ("CANCELLED", None)
        if self.completion is not None:
            return self.completion
        return ("PENDING", None)

    # -- retention refcount (A5 pins gates it still references) -------------

    def acquire(self) -> None:
        """Pin the gate — an owner/reattacher/send-intent holds a live
        reference. Pair with :meth:`release`; :func:`maybe_retire_gate`
        never drops a gate while :attr:`refcount` is above zero."""
        self._refcount += 1

    def release(self) -> None:
        """Drop a pin taken by :meth:`acquire`. Never goes negative — a
        stray extra release is a no-op, not a crash: retention is a hygiene
        optimization (bounding ``ASK_GATES``' size), never a correctness
        mechanism (a leaked gate is merely never cleaned up)."""
        if self._refcount > 0:
            self._refcount -= 1

    @property
    def refcount(self) -> int:
        return self._refcount

    def retirable(self, *, bound: float | None = None) -> bool:
        """True iff no active reference remains AND the reattach retention
        bound has elapsed since the FIRST terminal resolution (spec §D1
        "Tombstone retention, stated precisely"). A gate that never resolved
        (still PENDING) is never retirable — there is no terminal outcome to
        retain, so retiring it would just re-litigate the get-or-create race
        it exists to settle. ``bound`` defaults to
        :func:`_reattach_retention_bound` — see that docstring for what the
        default actually is (and is not)."""
        if self._refcount > 0:
            return False
        if self._resolved_at is None:
            return False
        if bound is None:
            bound = _reattach_retention_bound()
        return (self._clock() - self._resolved_at) >= bound


def _reattach_retention_bound() -> float:
    """Gate-retention window: reuses ``verdict_broker._RETIRE_S`` as the
    CLOSEST AVAILABLE numeric analogue — NOT because it is "the" reattach
    refusal-outcome bound. The round-3 refusal-outcome machinery
    (``_record_intent_*`` → send-intent registry) is retained STRUCTURALLY
    at turn end (``OutputSequencer.prune_turn``), not by any TTL; no
    numeric refusal-retention constant exists to reuse. ``_RETIRE_S`` is the
    broker's retired-tombstone window — the nearest guarantee of the same
    shape (a resolved request staying observable to late reattachers) —
    and retention here is pure map hygiene, never correctness: ``retirable``
    is refcount-gated first, so a referenced gate is immortal regardless of
    this value. A5's wiring may pass an explicit ``bound=`` if its window
    differs. Read lazily as a module attribute (never captured at import)
    so monkeypatched ``verdict_broker._RETIRE_S`` is honoured."""
    import verdict_broker
    return verdict_broker._RETIRE_S


ASK_GATES: dict[str, AskValidationGate] = {}

# Request_ids with a LIVE validation owner running IN THIS PROCESS (Task A5). A
# reattacher only BLOCKS on a PENDING gate when a local owner is present to
# resolve it: register_send_intent is atomic, so a reattacher (created=False)
# always registers AFTER the owner (created=True), which adds itself here
# synchronously right after determining ownership (no await between) — so a
# genuine same-process reattacher always observes the owner. When NO owner is
# present (a transport retry reattaching to an intent whose owner already
# exited / lives in another process — the F5 fail-closed case), the reattacher
# must NOT block on a gate nobody will resolve; it falls through to the
# existing intent/broker reattach path instead.
_ASK_VALIDATION_OWNERS: set[str] = set()


def get_or_create_gate(
    request_id: str, *, clock: Callable[[], float] | None = None,
) -> AskValidationGate:
    """Idempotent per-request_id gate lookup (r10-1). The FIRST caller for a
    given *request_id* — the validation owner's ask path OR a racing
    ``ask_cancel``/reattacher — creates the gate; every LATER caller for the
    SAME *request_id* gets the identical instance. This is what makes
    cancel-first ordering safe (spec §D1 r5-1/r10-1): an ``ask_cancel`` that
    lands before the original ``/ask`` call creates the gate and sets its
    cancellation latch immediately; the ask path's later
    ``get_or_create_gate`` call for the same request_id finds that SAME
    gate, latch already set, and never registers a broker request.

    ``clock`` is test-only (never patch module-level ``asyncio.sleep`` — or
    ``time.monotonic`` — inject the clock instead); production callers never
    pass it. This function does NOT bump :attr:`AskValidationGate.refcount`
    — callers that need retention protection call
    :meth:`AskValidationGate.acquire` explicitly."""
    gate = ASK_GATES.get(request_id)
    if gate is None:
        gate = AskValidationGate() if clock is None else AskValidationGate(clock=clock)
        ASK_GATES[request_id] = gate
    return gate


def maybe_retire_gate(request_id: str) -> bool:
    """Drop the gate for *request_id* from :data:`ASK_GATES` iff it is
    :meth:`AskValidationGate.retirable` — piggybacking on the SAME
    reattach-bound cleanup shape round 3 already uses for send-intent
    refusal outcomes (release-then-maybe-retire), not a new sweep/TTL
    mechanism (spec §D1). Returns ``True`` iff a gate was actually retired.
    Safe to call speculatively (e.g. after every
    :meth:`AskValidationGate.release`) — a non-retirable or already-gone
    gate is a no-op."""
    gate = ASK_GATES.get(request_id)
    if gate is None or not gate.retirable():
        return False
    del ASK_GATES[request_id]
    return True


# wb2-4 (whole-branch gate wave 2): gate pins tied to the SEND-INTENT lifecycle.
# The gate's own handler pin is released in the ask handler's ``finally`` (when
# the HTTP request ends), but the send-intent it authorized outlives the handler
# — the shielded broker request / relay-deferred post stays live until turn-end
# prune. The 60s ``_reattach_retention_bound`` stand-in could then retire a
# resolved (PASSED) gate WHILE its intent is still live, so a same-``request_id``
# retry gets a fresh PENDING gate instead of the resolved one. Pinning the gate
# to the intent's lifetime (acquire at registration, release at the intent's
# turn-end retirement via ``SendIntent.on_retire``) makes a gate un-retirable
# while its intent lives; the retention window then only governs POST-retirement
# observability, exactly as the spec's §D1 tombstone-retention states.
_INTENT_GATE_PINS: dict[str, AskValidationGate] = {}


def _pin_gate_to_intent(gate: AskValidationGate, request_id: str) -> None:
    """Acquire an intent-lifecycle pin on *gate* (idempotent per request_id — a
    same-``request_id`` reattach never double-pins, since the intent's single
    ``on_retire`` releases exactly one pin)."""
    if request_id in _INTENT_GATE_PINS:
        return
    gate.acquire()
    _INTENT_GATE_PINS[request_id] = gate


def _retire_intent_gate_pin(request_id: str) -> None:
    """Release the intent-lifecycle pin for *request_id* and speculatively retire
    the gate — wired to ``SendIntent.on_retire``, so it fires when the intent is
    pruned at turn end. No-op when no pin is held."""
    gate = _INTENT_GATE_PINS.pop(request_id, None)
    if gate is None:
        return
    gate.release()
    maybe_retire_gate(request_id)


def _sweep_retirable_gates() -> None:
    """A5 review, Finding 1: opportunistically retire every CURRENTLY-
    retirable gate in :data:`ASK_GATES`.

    ``maybe_retire_gate`` alone never actually fires in production: it is
    only ever called at an ask's OWN ``finally`` (or ``ask_cancel``),
    microseconds after that SAME gate's own resolution — nowhere near the
    60s :func:`_reattach_retention_bound`, so that self-check is always a
    no-op and every ask leaks one gate into ``ASK_GATES`` forever
    (reviewer-verified empirically: "retired at finally? False | still in
    ASK_GATES? True; ASK_GATES size after one successful ask: 1").

    The fix is not a new TTL/sweep-thread mechanism — it is calling the
    SAME ``maybe_retire_gate`` speculative check against EVERY gate, not
    just the current request's own, at the entry of the next ask/
    ask_cancel call. By the time a LATER call runs, an EARLIER gate's bound
    has had a real chance to elapse. ``ASK_GATES`` stays small in practice
    (few in-flight + recently-resolved asks), so this is O(gates) per ask,
    not O(all asks ever)."""
    for rid in list(ASK_GATES):
        maybe_retire_gate(rid)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engagement_auth_error(
    engagement_registry: Any, engagement_id: str | None, body: dict,
) -> str | None:
    """#335: verify the body's ``engagement_token`` against the record.

    Every /internal/channel/* route acts with the named engagement's
    authority (posts to its topic, answers/cancels its questions, delivers
    verdicts into its broker scope), and the internal socket is reachable
    from any in-container process — so the client-supplied id alone must
    never be honored. Returns ``"engagement_auth_failed"`` when a record
    exists and the token is missing/mismatched (fail-closed, including for
    a token-less record); ``None`` when the claim is authenticated OR the
    record is unknown (the caller's own ``unknown_engagement`` handling
    stays authoritative for that case).
    """
    if not engagement_id:
        return None
    try:
        rec = engagement_registry.get(engagement_id)
    except Exception:  # noqa: BLE001
        rec = None
    if rec is None:
        return None
    from internal_handlers import engagement_auth_ok
    if not engagement_auth_ok(rec, body.get("engagement_token")):
        logger.warning(
            "/internal/channel/*: rejected engagement id claim for %s: "
            "missing/invalid engagement token", str(engagement_id)[:8],
        )
        return "engagement_auth_failed"
    return None


def _resolve_topic(
    engagement_registry: Any, engagement_id: str | None,
    body: dict | None = None,
) -> tuple[int | None, str | None]:
    """Resolve ``engagement_id`` → ``topic_id`` via the registry.

    Returns ``(topic_id, None)`` on success or ``(None, error_code)`` on
    failure. Error codes: ``missing_engagement_id`` (missing/falsy id),
    ``unknown_engagement`` (registry.get returned None),
    ``no_topic_bound`` (record had no ``topic_id``),
    ``engagement_auth_failed`` (#335 — ``body`` supplied and its
    ``engagement_token`` does not authenticate the record).
    """
    if not engagement_id:
        return None, "missing_engagement_id"
    rec = engagement_registry.get(engagement_id)
    if rec is None:
        return None, "unknown_engagement"
    if body is not None:
        err = _engagement_auth_error(engagement_registry, engagement_id, body)
        if err is not None:
            return None, err
    topic_id = getattr(rec, "topic_id", None)
    if topic_id is None:
        return None, "no_topic_bound"
    return topic_id, None


# ---------------------------------------------------------------------------
# Handler factories
# ---------------------------------------------------------------------------


def _make_send_to_topic(
    telegram_channel: Any, engagement_registry: Any,
    record_reply: Callable[[str, str], None] | None = None,
) -> Handler:
    """Build the aiohttp POST handler for /internal/channel/send_to_topic.

    ``record_reply`` (W1, optional): every text through this endpoint is a
    ``reply()`` from the engagement; on a successful post it is recorded per
    engagement so the claude_code driver's live topic-stream relay can de-dup
    a streamed turn that is byte-identical to a reply already posted.
    """

    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad_json"})

        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "bad_json"})

        engagement_id = body.get("engagement_id")
        topic_id, err = _resolve_topic(engagement_registry, engagement_id, body)
        if err is not None:
            return web.json_response({"ok": False, "error": err})

        text = body.get("text") or ""

        request_id = body.get("request_id")
        projection_hash = body.get("projection_hash")
        driver = _resolve_active_driver()

        # §A3(a) LIVE-PENDING REPLY GATE (Sol r1-9 + r5-2): refuse this reply
        # while the engagement has a LIVE unresolved question. The check runs
        # UNDER THE SAME per-engagement ask-maintenance lock as the ask ingress
        # reservation (linearizing the reply against the reservation — closing the
        # r5-2 race where parallel ask+reply both pass before the ask reaches
        # durable ownership); the lock is held for the CHECK ONLY. Gating on
        # actual pending state means reply-then-ask, a tap-answered ask (broker
        # empty), an EXPIRED ask (no live request), and an answered-but-
        # unconfirmed-settle anchor (the answered/reserved split) are all allowed.
        _reply_gate_lock = _ask_maint_lock(driver, engagement_id)
        if _reply_gate_lock is not None:
            async with _reply_gate_lock:
                if _ask_pending_predicate(driver, engagement_id):
                    return _reply_pending_response(driver, engagement_id)

        # The actual post + post-side bookkeeping (advance first-contact, reply
        # de-dup hint). Invoked RELAY-SIDE (§2, review C1) at the reply's
        # tool_use block position — AFTER any preceding narration — or directly
        # here in the no-driver/degraded fallback (pre-v0.79 eager behavior).
        async def _do_post() -> int | None:
            # #332: the turn's first output threads to the inbound operator
            # message — consume the one-shot target; a failed send restores it.
            reply_to = _consume_turn_reply_to(driver, engagement_id)
            _kw = ({"reply_to_message_id": reply_to}
                   if reply_to is not None else {})
            try:
                mid = await telegram_channel.send_response_to_topic(
                    topic_id, text, **_kw)
            except asyncio.CancelledError:
                # Terra/Sol r1 (#332): cancellation bypasses the except
                # below — the send may or may not have landed, but the
                # restore never clobbers a newer target and a doubly
                # threaded pair is cosmetic, so re-arm and re-raise.
                _restore_turn_reply_to(driver, engagement_id, reply_to)
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "send_to_topic failed for engagement=%s topic=%s: %s",
                    engagement_id, topic_id, exc,
                )
                _restore_turn_reply_to(driver, engagement_id, reply_to)
                return None
            if mid is None:
                _restore_turn_reply_to(driver, engagement_id, reply_to)
                return None
            # W2/Sol B9 (Task 7): the agent's first outbound reply flips
            # first_contact_required -> awaiting_operator. getattr-tolerant.
            advance = getattr(
                engagement_registry, "advance_interaction_state", None)
            if advance is not None:
                await advance(engagement_id, "first_contact")
            if record_reply is not None and text:
                try:
                    record_reply(engagement_id, text)
                except Exception:  # noqa: BLE001 — de-dup hint is best-effort
                    logger.debug("record_reply hook failed", exc_info=True)
            return mid

        # v0.79.0 (§2, review C1): DEFERRED posting. The reply ingress registers
        # + arms a discrete-send INTENT whose poster performs the actual send;
        # the RELAY posts it at the reply's tool_use block (sealing preceding
        # narration first). A response-loss-after-post retry (same request_id)
        # reattaches to the recorded outcome instead of posting a SECOND reply.
        if driver is not None and request_id and projection_hash:
            from channels.output_sequencer import (
                REPLY_TOOL, TERMINAL_REGISTRATION,
            )
            res = driver.register_send_intent(
                engagement_id=engagement_id, request_id=request_id,
                tool_name=REPLY_TOOL, projection_hash=projection_hash,
                poster=_noop_poster,
            )
            # wb4-1: the engagement terminalized after this ingress entered but
            # before the relay posted — refuse fail-closed rather than eager-post
            # a reply BELOW the terminal completion.
            if res is TERMINAL_REGISTRATION:
                return web.json_response(
                    {"ok": False, "error": "engagement_terminal"})
            if res is not None:
                _intent, created_intent = res
                if not created_intent:
                    prior = driver.send_intent_outcome(engagement_id, request_id)
                    if prior is not None and prior.get("message_id") is not None:
                        # Response-loss-after-post retry: the reply already
                        # posted — return its id, no second post (§2(1)).
                        return web.json_response(
                            {"ok": True, "message_id": prior["message_id"]})
                    # F5 fail-closed: reattach BEFORE the relay posted — AWAIT the
                    # same bounded resolution rather than returning ok:true on an
                    # UNRESOLVED intent. A None/timeout/failed outcome maps to
                    # ok:false (never a phantom ok:true with no post).
                    outcome = await _await_deferred_post(
                        driver, engagement_id, request_id)
                    if outcome is not None and outcome.get("message_id") is not None:
                        return web.json_response(
                            {"ok": True, "message_id": outcome["message_id"]})
                    return web.json_response(
                        {"ok": False, "error": "send_failed"})
                driver.set_send_intent_poster(engagement_id, request_id, _do_post)
                driver.arm_send_intent(engagement_id, request_id)
                # F3/F5 fail-closed: AWAIT the relay-mediated post's outcome
                # (bounded by the sequencer's transport budget). An unresolved
                # (None/timeout) or failed outcome is ok:false — never an ok:true
                # with no post.
                outcome = await _await_deferred_post(
                    driver, engagement_id, request_id)
                if outcome is None or not outcome.get("ok"):
                    return web.json_response(
                        {"ok": False, "error": "send_failed"})
                return web.json_response(
                    {"ok": True, "message_id": outcome.get("message_id")})

        # EAGER fallback (no live sequencer): post now and return the id.
        msg_id = await _do_post()
        if msg_id is None:
            return web.json_response({"ok": False, "error": "send_failed"})
        return web.json_response({"ok": True, "message_id": msg_id})

    return handler


def _make_post_inline_keyboard(
    telegram_channel: Any, engagement_registry: Any,
) -> Handler:
    """Build the aiohttp POST handler for /internal/channel/post_inline_keyboard.

    Phase 2 (Task 19): renders an inline-keyboard prompt in the engagement
    topic. The channel server uses this for U1 permission relay (Task 18).

    Body shape: ``{engagement_id, text, buttons: [[{text, callback_data?,
    url?}, ...], ...], parse_mode?, request_id?}``. ``request_id`` is logged
    by the channel server for traceability but ignored by this layer.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad_json"})
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "bad_json"})

        engagement_id = body.get("engagement_id")
        topic_id, err = _resolve_topic(engagement_registry, engagement_id, body)
        if err is not None:
            return web.json_response({"ok": False, "error": err})

        rows = body.get("buttons") or []
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    text=btn["text"],
                    callback_data=btn.get("callback_data"),
                    url=btn.get("url"),
                )
                for btn in row
            ]
            for row in rows
        ])

        # v0.109.0 (G4): render the body rich when the caller did NOT request
        # an explicit parse_mode (parse_mode wins — entities and parse_mode
        # are mutually exclusive at the Bot API; no caller passes parse_mode
        # today, the passthrough stays for that contract). Plain body / over
        # caps ⇒ entities None ⇒ a plain send of ``plain_text`` (#1330).
        _text = body.get("text") or ""
        _extra: dict = {}
        if not body.get("parse_mode"):
            from channels.tg_richtext import plain_text, render as _render
            _display, _entities = _render(_text)
            if _entities is not None:
                _text = _display
                _extra["entities"] = _entities
            else:
                _text = plain_text(_text)  # #1330: escapes consumed
        try:
            try:
                msg_id = await telegram_channel.send_to_topic(
                    topic_id,
                    _text,
                    reply_markup=keyboard,
                    parse_mode=body.get("parse_mode"),
                    **_extra,
                )
            except BadRequest:
                if not _extra:
                    raise
                # Entity rejection posted NOTHING — one plain resend of the
                # ORIGINAL text (fail-literal, mirrors the other primitives).
                msg_id = await telegram_channel.send_to_topic(
                    topic_id,
                    body.get("text") or "",
                    reply_markup=keyboard,
                    parse_mode=body.get("parse_mode"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "post_inline_keyboard failed for engagement=%s topic=%s: %s",
                engagement_id, topic_id, exc,
            )
            return web.json_response({"ok": False, "error": "send_failed"})

        return web.json_response({"ok": True, "message_id": msg_id})

    return handler


# ---------------------------------------------------------------------------
# v0.75.0 (W5) — engagement_ask: operator-facing multiple-choice question,
# posted by the casa_engagement_channel `ask` MCP tool.
# ---------------------------------------------------------------------------

# Telegram callback_data v1|engagement_ask|<rid>|<idx> caps request_id at the
# same headroom the permission namespace uses (_RID_MAX_LEN in hooks.py); the
# ask tool's request_id is always a full uuid4().hex (32 chars), well under.
# v0.85.0 (round 4, D3/D4): the A6 enumerator strip and A7 embedded-options
# regex gate (and their shared ``_ENUMERATOR_RE`` grammar) are REMOVED — an
# option's label/short and an anchor's question are rendered VERBATIM. No
# regex catches every phrasing and it was never an authorization boundary;
# doctrine (not code) now tells the agent enumerable answers belong in
# ``options``, never pre-labelled prose.

_ASK_MIN_OPTIONS = 2
_ASK_MAX_OPTIONS = 8
# D1 (round 4, spec §D1 bullets 1-2): the invented LENGTH caps
# (``_ASK_MAX_LABEL_LEN`` = 48, ``_ASK_MAX_SHORT_LEN`` = 25, the 1024-char
# question cap) are REMOVED — they were v0.75.0/v0.83.0 leftovers with no real
# Telegram-limit backing (option text never enters the 64-byte callback_data;
# the body isn't the 4096-char message limit). Option COUNT stays capped at
# ``_ASK_MAX_OPTIONS`` (a documented product-contract exception, not a length
# heuristic); the real per-ask rendered-body limit is enforced elsewhere
# (Task A3's lifecycle body-limit validator), not here.
_ASK_MIN_TIMEOUT_S = 30.0
_ASK_MAX_TIMEOUT_S = 570.0
_ASK_DEFAULT_TIMEOUT_S = 300.0

# v0.84.0 (round 4, D1 bullets 3 & 6): the render-and-measure lifecycle
# body-limit validator (``ask_lifecycle_suffixes`` + ``render_ask_body``). The
# check runs AFTER Q-number allocation and BEFORE broker registration/posting
# (spec §D1 "Validator placement"), using the REAL allocated number — the
# validation gate (Task A4/A5) owns the tombstone/marker/gate cleanup on
# refusal, so the old pre-allocation ``_ASK_WORST_CASE_NUMBER`` approximation
# is obsolete and removed.
_ASK_BODY_LIMIT = 4096

# v0.79.0 §4 — pinned settle copy (appended below the canonical question text
# when the keyboard settles; the keyboard is cleared via clear_keyboard=True).
# v0.84.0 (round 4, D1 bullet 3): the old ``_SETTLE_ANSWERED = "\n✅ {label}"``
# re-appended the operator's CHOSEN FULL LABEL — unbounded, so the
# render-and-measure body-limit validator couldn't know the settled length at
# ask time. Replaced by :func:`_positional_settle_suffix` below, which renders
# a BOUNDED positional copy (``✅ Option 2`` / ``✅ Options 1, 3``) instead.
# F-EXPIRE (v0.83.0, A2a): a live-ask keyboard that expires unanswered now
# SUSPENDS the engagement (operator-away) rather than inviting an immediate
# re-ask, so the settle copy tells the operator the engagement is paused and how
# to resume it.
_SETTLE_EXPIRED = "\n⌛ expired — engagement paused; reply here to continue"
_SETTLE_CANCELLED = "\n🚫 cancelled"
_SETTLE_SUPERSEDED = "\n🚫 superseded by your message below"
# v0.79.0 §4 (Sol F6): the open-question ledger write failed AFTER the keyboard
# posted — settle it fail-closed so no keyboard the boot reconciler can't see
# stays live-tappable.
_SETTLE_INTERNAL_ERROR = "\n⚠️ internal error — question withdrawn, please resend"

# v0.79.0 §4 — free-text anchor: an ``options: []`` ask posts a numbered anchor
# with NO keyboard; the next operator text settles it.
_SETTLE_ANSWERED_BELOW = "\n✅ answered below"

# v0.79.0 §4 — inbound-gate refusal (unread operator message pending). The
# refusal consumes no timeout budget; from the 3rd consecutive refusal per turn
# the sterner variant is returned + a WARN counter logged (Sol r2-5: no hard
# force-end primitive exists — this is soft anti-livelock).
_ASK_REFUSAL = (
    "the operator sent a message you have not seen — it will arrive next "
    "turn; end your turn now, silently (no sign-off), and re-ask after reading it"
)
_ASK_REFUSAL_STERN = (
    "STOP ASKING. The operator has a message waiting that you have NOT read. "
    "It is delivered to you the moment you end this turn. Do not ask another "
    "question — END YOUR TURN NOW, silently, read the operator's message, then "
    "decide."
)
_ASK_REFUSAL_ESCALATE_AT = 3

# F-EXPIRE (v0.83.0, A2a) — operator-away copies.
# The enriched ``no_answer`` response returned when a live ask expires: the
# engagement is now PAUSED and the agent must end its turn silently rather than
# re-ask (the live incident was a 21-ask loop).
_ASK_PAUSED_MESSAGE = (
    "The operator did not answer in time. The engagement is now PAUSED — end "
    "your turn silently (no sign-off message). Do NOT re-ask: your question "
    "stays on record and the operator's reply will start your next turn."
)
# The refusal returned to EVERY further ask while operator-away, with no broker
# registration / keyboard / timeout burn.
_ASK_AWAY_REFUSAL = (
    "The operator is away — your last question expired unanswered. END YOUR "
    "TURN NOW, silently. Do not ask again; the operator's return starts your "
    "next turn."
)

# v0.83.0 §A3 — F-ORDER structural gates. ``question_pending`` is returned by the
# reply gate (a) and the ask-ingress stacking gate (c) when a question is already
# LIVE for the engagement. ``{n}`` is filled from the pending question number
# when known; the number-less variant is used when it cannot be resolved.
_REPLY_PENDING_NUMBERED = (
    "you have an open question (Q{n}) — end your turn silently and wait for "
    "the answer"
)
_REPLY_PENDING_GENERIC = (
    "you have an open question — end your turn silently and wait for the answer"
)
_ASK_PENDING_NUMBERED = (
    "Q{n} is still open — wait for the answer (end your turn silently) instead "
    "of asking another question"
)
_ASK_PENDING_GENERIC = (
    "a question is still open — wait for the answer (end your turn silently) "
    "instead of asking another question"
)
# §A3(c): the withdrawn-anchor copy edited over an orphan whose ledger write
# failed after posting (RAW-wire edit, never edit_discrete — see _post_anchor).
_ANCHOR_WITHDRAWN = "⚠️ internal error — question withdrawn, please resend"
# §A3(c): the compensated / withdrawn ask's tool response copy (add-failure).
_ASK_INTERNAL_ERROR_MSG = (
    "the question could not be recorded — it was withdrawn; end your turn and "
    "re-ask"
)


def _ask_maint_lock(driver: Any, eng_id: str) -> Any:
    """The per-engagement ask-maintenance lock, or ``None`` when the driver
    predates the §A3 gate seam (unit fakes / degraded boot) — the reply /
    stacking gates then simply don't engage. Getattr-tolerant."""
    fn = getattr(driver, "ask_maintenance_lock", None) if driver is not None else None
    return fn(eng_id) if fn is not None else None


def _clear_ask_marker(driver: Any, eng_id: str, request_id: str) -> None:
    """Clear the ingress marker (CAS on request_id), getattr-tolerant."""
    fn = getattr(driver, "clear_ask_inflight", None) if driver is not None else None
    if fn is not None:
        fn(eng_id, request_id)


def _ask_pending_predicate(
    driver: Any, eng_id: str, *, exclude_request_id: str | None = None,
) -> bool:
    """§A3 live-pending predicate (shared by the reply gate + the ask stacking
    gate). True iff the engagement has a LIVE unresolved question: a live broker
    ask (``BROKER.pending`` non-empty) OR an unanswered free-text anchor (the
    driver's EFFECTIVE view — Task 6 ``answered`` + Task 7 reserved excluded) OR
    the ``ask_inflight`` ingress marker set to a DIFFERENT request_id (the
    marker→durable-ownership gap). Evaluated UNDER the ask-maintenance lock."""
    from verdict_broker import BROKER
    if BROKER.pending(namespace="engagement_ask", scope=eng_id):
        return True
    if driver is None:
        return False
    anchor_fn = getattr(driver, "effective_open_anchor", None)
    if anchor_fn is not None:
        try:
            if anchor_fn(eng_id) is not None:
                return True
        except Exception:  # noqa: BLE001 — degrade to "no anchor"
            logger.debug("effective_open_anchor read failed", exc_info=True)
    marker_fn = getattr(driver, "ask_inflight", None)
    marker = marker_fn(eng_id) if marker_fn is not None else None
    return marker is not None and marker != exclude_request_id


def _pending_question_number(driver: Any, eng_id: str) -> int | None:
    """The smallest EFFECTIVE-open question number for the refusal copy, or
    ``None`` when unknown (e.g. a button ask reserved but not yet ledger-added,
    or a degraded registry). Number-less copy is acceptable then."""
    if driver is None:
        return None
    fn = getattr(driver, "_effective_open_question_numbers", None)
    if fn is None:
        return None
    try:
        nums = fn(eng_id)
    except Exception:  # noqa: BLE001
        return None
    return min(nums) if nums else None


def _reply_pending_response(driver: Any, eng_id: str) -> web.Response:
    n = _pending_question_number(driver, eng_id)
    msg = (
        _REPLY_PENDING_NUMBERED.format(n=n) if n is not None
        else _REPLY_PENDING_GENERIC
    )
    return web.json_response(
        {"ok": False, "error": "question_pending", "message": msg})


def _ask_pending_payload(driver: Any, eng_id: str) -> dict:
    """The canonical ``question_pending`` refusal body (Task A5: also published
    through the validation gate so a blocked reattacher gets it byte-identically)."""
    n = _pending_question_number(driver, eng_id)
    msg = (
        _ASK_PENDING_NUMBERED.format(n=n) if n is not None
        else _ASK_PENDING_GENERIC
    )
    return {"ok": False, "error": "question_pending", "message": msg}


def _ask_pending_response(driver: Any, eng_id: str) -> web.Response:
    return web.json_response(_ask_pending_payload(driver, eng_id))


def _internal_error_payload() -> dict:
    """The canonical allocator-failure refusal body (Task A5: published through
    the validation gate AND recorded as the intent tombstone outcome)."""
    return {"ok": False, "error": "internal_error"}


def _record_intent_internal_error(
    driver: Any, eng_id: str, request_id: str,
) -> None:
    """§A3(c) allocation-failure: tombstone the intent with an internal_error
    OUTCOME so a same-request_id retry reattaches and short-circuits (no fresh
    post). Degrades to a bare cancel on a driver without the seam."""
    fn = getattr(driver, "record_send_intent_refusal", None)
    if fn is not None:
        try:
            fn(eng_id, request_id, _internal_error_payload())
            return
        except Exception:  # noqa: BLE001 — fall back to a bare tombstone
            logger.debug("record_send_intent_refusal(internal) failed", exc_info=True)
    cancel = getattr(driver, "cancel_send_intent", None)
    if cancel is not None:
        cancel(eng_id, request_id)


def _record_intent_invalid_args(
    driver: Any, eng_id: str, request_id: str, payload: dict,
) -> None:
    """D1 (Task A5) — tombstone the intent with the detailed ``invalid_args``
    body-limit refusal OUTCOME (``payload`` is the EXACT self-explaining
    response) so a same-request_id retry reattaches via the refusal-outcome
    short-circuit (``_refused_intent_outcome`` now recognises ``invalid_args``)
    and returns byte-identical detail. Degrades to a bare cancel on a driver
    without the seam."""
    fn = getattr(driver, "record_send_intent_refusal", None)
    if fn is not None:
        try:
            fn(eng_id, request_id, payload)
            return
        except Exception:  # noqa: BLE001 — fall back to a bare tombstone
            logger.debug("record_send_intent_refusal(invalid_args) failed",
                         exc_info=True)
    cancel = getattr(driver, "cancel_send_intent", None)
    if cancel is not None:
        cancel(eng_id, request_id)


def _operator_away_active(driver: Any, eng_id: str) -> bool:
    """§A2.4 gate read — getattr-tolerant so a driver without operator-away
    support (unit fakes / degraded boot) simply never gates."""
    if driver is None:
        return False
    fn = getattr(driver, "operator_away_active", None)
    if fn is None:
        return False
    try:
        return bool(fn(eng_id))
    except Exception:  # noqa: BLE001 — a gate read must never wedge the ask
        logger.debug("operator_away_active read failed", exc_info=True)
        return False


def _away_refusal_payload() -> dict:
    """The canonical operator-away refusal body. Shared by the live refusal
    response AND the intent-refusal outcome recorded for a transport retry
    (Finding 1) so a reattaching retry returns byte-identical JSON."""
    return {"ok": False, "error": "operator_away", "message": _ASK_AWAY_REFUSAL}


def _away_refusal_response(driver: Any, eng_id: str) -> web.Response:
    """§A2.4 refusal — bump the per-episode away-refusal counter (Task 5's
    force-turn-boundary backstop reads it) and return the fixed refusal copy."""
    bump = getattr(driver, "record_away_refusal", None) if driver is not None else None
    if bump is not None:
        try:
            bump(eng_id)
        except Exception:  # noqa: BLE001 — the refusal copy stands either way
            logger.debug("record_away_refusal failed", exc_info=True)
    return web.json_response(_away_refusal_payload())


def _record_intent_refusal(driver: Any, eng_id: str, request_id: str) -> None:
    """§A2.4 (Finding 1): record the operator-away refusal OUTCOME on the intent
    instead of a bare cancel. A same-``request_id`` transport retry then hits the
    reattach path FIRST, reads this recorded outcome, and short-circuits to the
    SAME refusal — never awaiting the dead intent (→ ``delivery_failed``, anchor)
    nor re-registering a fresh broker request (→ timeout burn, no keyboard,
    button). Degrades to the bare cancel on a driver predating the seam."""
    fn = getattr(driver, "record_send_intent_refusal", None)
    if fn is not None:
        try:
            fn(eng_id, request_id, _away_refusal_payload())
            return
        except Exception:  # noqa: BLE001 — fall back to the bare tombstone
            logger.debug("record_send_intent_refusal failed", exc_info=True)
    driver.cancel_send_intent(eng_id, request_id)  # tombstone (pre-A2 behaviour)


def _unread_refusal_payload(copy: str) -> dict:
    """The refusal-count-FREE ``unread_inbound`` body recorded on the intent for a
    transport retry (Sol A2 wave-3, Finding 3). The LIVE refusal response carries
    ``refusal_count`` (a fresh bump); the recorded reattach copy omits it so a
    retry never re-bumps the counter."""
    return {"ok": False, "error": "unread_inbound", "message": copy}


def _record_intent_unread_refusal(
    driver: Any, eng_id: str, request_id: str, copy: str,
) -> None:
    """Sol A2 wave-3, Finding 3 (symmetric with :func:`_record_intent_refusal`):
    record the ``unread_inbound`` refusal OUTCOME on the intent instead of a bare
    cancel. A same-``request_id`` transport retry then hits the reattach path
    FIRST, reads this recorded outcome, and short-circuits to the SAME refusal —
    never awaiting the dead intent (→ the deferred-post budget → ``delivery_failed``,
    anchor) nor re-registering. Degrades to the bare cancel on a driver predating
    the seam."""
    fn = getattr(driver, "record_send_intent_refusal", None)
    if fn is not None:
        try:
            fn(eng_id, request_id, _unread_refusal_payload(copy))
            return
        except Exception:  # noqa: BLE001 — fall back to the bare tombstone
            logger.debug("record_send_intent_refusal(unread) failed", exc_info=True)
    cancel = getattr(driver, "cancel_send_intent", None)
    if cancel is not None:
        cancel(eng_id, request_id)


# wb1-1 (whole-branch gate wave 1) — the tri-state a cancel-recording call
# resolves to. Only ``POST_WON`` transfers marker ownership to the poster; on
# ``CANCELLED`` and ``ABSENT`` the cancel is authoritative and the caller clears
# the ingress marker (there is no live post to strip it from under).
INTENT_CANCEL_CANCELLED = "cancelled"
INTENT_CANCEL_POST_WON = "post_won"
INTENT_CANCEL_ABSENT = "absent"


def _record_intent_cancelled(
    driver: Any, eng_id: str, request_id: str,
) -> str:
    """B1/B2 (§A3 wave 2) + A3 · F-ORDER (Sol A3 wave 3/4): a transport
    CANCELLATION between arming/registering an ask intent and its post TOMBSTONES
    the intent AND records a terminal ``cancelled`` outcome — but "the post wins"
    when a relay post is already in flight. Closes:

    * the armed/pending intent stays MATCHABLE as a tombstone, so the relay
      consume-cancels its block (NOTHING posts) — a marker cleared without this
      would let a DIFFERENT ask pass the gate while the armed original still
      posts (the one-question invariant broken, B1);
    * a same-``request_id`` retry reads this recorded outcome via the reattach
      path (``_refused_intent_outcome`` recognises ``cancelled``) and
      short-circuits — never awaiting a never-armed intent to the transport
      budget (anchor hang, B2) nor registering a fresh broker request (button
      timeout burn, B2).

    A3 · F-ORDER wave 4: FULLY SYNCHRONOUS — no awaits. It routes through the
    driver's ``record_send_intent_cancelled_nowait`` seam, which reads the
    intent's ``posting`` flag synchronously and takes effect ONLY while the intent
    is still cancellable (not currently being posted / posted / already resolved).
    Being awaitless it can never be interrupted mid-flight by a SECOND
    ``Task.cancel()`` — the double-cancel window the awaited wave-3 seam left open
    (a second cancel dropping control into the caller's outer ``finally`` while a
    poster was mid-post) is closed.

    wb1-1 (whole-branch gate wave 1): returns an explicit TRI-STATE
    (``INTENT_CANCEL_CANCELLED`` / ``_POST_WON`` / ``_ABSENT``) rather than a
    bool. The old bool collapsed "post won" and "no intent" into ``False``, and
    ``/ask_cancel`` cleared the marker on both — stripping it mid-post admitted a
    second live question. Only ``POST_WON`` leaves the marker to the poster.

    Getattr-tolerant and swallows its own errors: an intent-cleanup failure must
    never mask the original cancellation being re-raised. Degrades to the
    pre-seam synchronous tombstone (best-effort) on a driver without the wave-4
    seam."""
    if driver is None:
        return INTENT_CANCEL_ABSENT
    rec_nowait = getattr(driver, "record_send_intent_cancelled_nowait", None)
    if rec_nowait is not None:
        try:
            res = rec_nowait(
                eng_id, request_id, {"ok": False, "error": "cancelled"})
            if isinstance(res, str):
                return res
            # Tolerate a legacy bool-returning seam: True == cancel took effect;
            # a falsy result is conservatively read as "post won" (never clear a
            # marker a possibly-live poster may still own).
            return INTENT_CANCEL_CANCELLED if res else INTENT_CANCEL_POST_WON
        except Exception:  # noqa: BLE001 — fall back to the pre-seam path
            logger.debug("record_send_intent_cancelled_nowait failed", exc_info=True)
    # --- pre-seam fallback (no nowait guard available) ----------------------
    outcome_fn = getattr(driver, "send_intent_outcome", None)
    if outcome_fn is not None:
        try:
            if outcome_fn(eng_id, request_id) is not None:
                # already resolved (posted / compensated) — no clobber
                return INTENT_CANCEL_POST_WON
        except Exception:  # noqa: BLE001 — degrade to attempting the tombstone
            logger.debug("send_intent_outcome read failed", exc_info=True)
    rec = getattr(driver, "record_send_intent_refusal", None)
    if rec is not None:
        try:
            res = rec(eng_id, request_id, {"ok": False, "error": "cancelled"})
            # A None result means the intent was unknown (nothing tombstoned).
            return INTENT_CANCEL_ABSENT if res is None else INTENT_CANCEL_CANCELLED
        except Exception:  # noqa: BLE001 — fall back to a bare tombstone
            logger.debug("record_send_intent_refusal(cancelled) failed",
                         exc_info=True)
    cancel = getattr(driver, "cancel_send_intent", None)
    if cancel is not None:
        try:
            cancel(eng_id, request_id)
            return INTENT_CANCEL_CANCELLED
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.debug("cancel_send_intent failed", exc_info=True)
    return INTENT_CANCEL_ABSENT


def _refused_intent_outcome(prior: Any) -> bool:
    """True iff a reattached intent's recorded outcome is a refusal the retry
    returns verbatim — an operator-away refusal (Finding 1), an unread-inbound
    refusal (Sol A2 wave-3, Finding 3), OR a CANCELLED tombstone (§A3 wave 2,
    B1/B2: a transport cancellation between arm/register and post). All are
    terminal recorded outcomes; the retry returns them as-is rather than
    awaiting the dead intent or re-registering a fresh broker request."""
    return (
        isinstance(prior, dict)
        and prior.get("error") in (
            "operator_away", "unread_inbound", "cancelled", "invalid_args")
    )


async def _no_answer_response(
    driver: Any, eng_id: str, request_id: str, req: Any,
) -> web.Response:
    """§A2.1 expiry — ENTER operator-away (generation-CAS via THIS waiter's own
    ``req.meta`` ``inbound_gen`` — Finding 2: never re-query the broker by key
    after the await, where a retired/reused tombstone could hand back a NEWER
    generation and re-wedge) and return the enriched PAUSED response. Degrades to
    the plain ``no_answer`` response on a driver without operator-away support
    (unit / eager fallback) so existing no-driver callers are byte-unchanged."""
    note = (
        getattr(driver, "note_operator_away", None)
        if driver is not None else None
    )
    if note is None:
        return web.json_response({"ok": True, "outcome": "no_answer"})
    # Finding 2: use the waiter's OWN req.meta (the handler holds ``req`` in both
    # the main and reattach paths; register returns the tombstone-backed req for
    # a retired key), never ``BROKER.get_meta`` by key after the await.
    meta = getattr(req, "meta", None)
    gen = meta.get("inbound_gen") if isinstance(meta, dict) else None
    if gen is not None:  # tolerate a missing gen — just skip the away entry
        try:
            res = note(eng_id, gen=gen)
            if inspect.isawaitable(res):
                await res
        except Exception:  # noqa: BLE001 — away entry is best-effort
            logger.debug("note_operator_away failed", exc_info=True)
    return web.json_response({
        "ok": True, "outcome": "no_answer", "engagement_paused": True,
        "message": _ASK_PAUSED_MESSAGE,
    })


async def _ask_final_response(
    outcome: dict, options: list, driver: Any, eng_id: str, request_id: str,
    req: Any,
) -> web.Response:
    """Map a broker ask outcome to the tool response, intercepting ``no_answer``
    to enter operator-away + return the PAUSED response (F-EXPIRE). Every other
    outcome delegates to ``_ask_outcome_response`` byte-identically."""
    if outcome.get("outcome") == "no_answer":
        return await _no_answer_response(driver, eng_id, request_id, req)
    return _ask_outcome_response(outcome, options)


def _validate_ask_args(
    body: dict,
) -> tuple[str, list, float, list] | None:
    """Validate + clamp the `ask` request body.

    Returns ``(question, options, clamped_timeout_s, shorts)`` on success, or
    ``None`` on any validation failure (caller maps to ``invalid_args``).
    ``options`` is the list of FULL labels (downstream sees these everywhere —
    body VERBATIM, broker meta, settle ✅, ``_ask_outcome_response``); ``shorts``
    is a PARALLEL list, one entry per option: the agent-supplied short label
    (str) or ``None``. The ONLY consumer of ``shorts`` is the keyboard.

    v0.79.0 §4: ``options: []`` is ACCEPTED (a free-text numbered anchor); a
    non-empty list still requires ``_ASK_MIN_OPTIONS..MAX``, unique, non-empty
    labels.

    v0.83.0 (A4 · F-BTN): each option may be a plain ``str`` (unchanged) OR a
    ``{"label": str, "short": str}`` dict — ``label.strip()`` non-empty.
    Mixed str+dict lists are allowed. The projection hash is computed
    client-side over the RAW args, so this server-side normalization does not
    affect relay matching. All validation lives here server-side (the channel
    subprocess transmits raw args and lets this gate refuse — r8-1).

    v0.84.0 (round 4, D1 bullets 1-2): the invented LENGTH caps on the
    question, FULL labels, and ``short`` are GONE — Structural checks remain
    ONLY for the question and FULL labels: type, non-blank after strip,
    uniqueness. ``short`` is optional ADVISORY data and is NEVER a rejection
    cause: a missing, blank, duplicate, over-budget, or non-string ``short``
    reaches the D2 whole-set resolver (``telegram.resolve_button_labels``)
    UNTOUCHED, which floors the WHOLE button set rather than rejecting the
    ask. A non-string ``short`` is normalized to ``None`` (treated as absent)
    here so downstream consumers only ever see ``str | None``.

    v0.85.0 (round 4, D4): the A6 leading-enumerator strip is REMOVED — each
    FULL label (and str-typed dict ``short``) is stored/rendered VERBATIM, no
    normalization. Doctrine (not code) tells the agent not to pre-label
    options; downstream logic treats labels as opaque values indexed by
    callback position, so a redundant agent-authored ``"1. Option A — …"``
    label is stable, not a bug. The uniqueness + non-emptiness checks below
    run on the RAW labels.
    """
    question = body.get("question")
    # wb4-4 (D1): NONBLANK AFTER STRIP — a whitespace-only question ("   ")
    # would otherwise pass a bare truthiness check and render a blank numbered
    # anchor. The verbatim (unstripped) text is still returned/stored unchanged.
    if not isinstance(question, str) or not question.strip():
        return None
    options = body.get("options")
    if not isinstance(options, list):
        return None
    if len(options) != 0 and not (_ASK_MIN_OPTIONS <= len(options) <= _ASK_MAX_OPTIONS):
        return None
    # A5 · F-MULTI: a multi-select ask REQUIRES ≥2 options — a multi anchor
    # (``options: []``) or a degenerate single-option multi is refused
    # ``invalid_args``. Non-multi anchors (``multi`` absent/False) are unaffected.
    if body.get("multi") and len(options) < _ASK_MIN_OPTIONS:
        return None
    labels: list[str] = []
    shorts: list[str | None] = []
    for o in options:
        if isinstance(o, str):
            if not o:
                return None
            labels.append(o)
            shorts.append(None)
        elif isinstance(o, dict):
            label = o.get("label")
            if not isinstance(label, str) or not label.strip():
                return None
            labels.append(label)
            short = o.get("short")
            # D1 (round 4): a non-string ``short`` is treated as absent; a
            # string ``short`` flows through UNTOUCHED (D4 round 4: verbatim,
            # no enumerator normalization) — blank/duplicate/over-budget is
            # the D2 resolver's concern, never a rejection here.
            shorts.append(short if isinstance(short, str) else None)
        else:
            return None
    # a marker-only or whitespace-only label is refused via ``.strip() != ""``
    # (bare truthiness would accept whitespace-only); uniqueness is checked
    # on the RAW labels (D4 round 4: no enumerator normalization to collide
    # against).
    for lab in labels:
        if not lab.strip():
            return None
    if len(set(labels)) != len(labels):
        return None
    try:
        timeout_s = float(body.get("timeout_s", _ASK_DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return None
    # #347: NaN survives min(max(...)) — every NaN comparison is False, so
    # both clamps return it unchanged and the broker's timer fires
    # immediately (→ instant no_answer on an already-posted keyboard).
    # Reject every non-finite value (inf would arm a timer that never fires).
    if not math.isfinite(timeout_s):
        return None
    timeout_s = min(max(timeout_s, _ASK_MIN_TIMEOUT_S), _ASK_MAX_TIMEOUT_S)
    return question, labels, timeout_s, shorts


def _canonical_question(question: str, number: int) -> str:
    """v0.79.0 §4 — the DISPLAYED question prefix is ALWAYS the allocated
    durable number: ``Q<number>: `` is PREPENDED to the question. v0.85.0
    (round 4, D4): the old leading ``Q<digits>:`` strip is REMOVED — the
    agent's question text is preserved VERBATIM (an agent-authored
    ``"Q7: which flavor?"`` renders ``"Q<n>: Q7: which flavor?"``); doctrine,
    not a regex, tells the agent not to self-number."""
    return f"Q{number}: {question}"


def render_ask_body(number: "int | None", question: str, options: list) -> str:
    """v0.81.0 (W-R3, Sol r1-5) — the SINGLE canonical rendered ask body.

    Used IDENTICALLY by all four ask consumers so they can never disagree:
    the initial keyboard post, the finish-hook settlement base, the persisted
    ``open_questions[].text``, and boot reconciliation. If the displayed
    message and the persisted text ever diverged, a tap/reconcile would drop
    the option list — this one source prevents that bug.

    Format::

        Q<n>: <question>

        1. <opt0>
        2. <opt1>
        …

    EVERY option is rendered VERBATIM (no truncation, no ellipsis), 1-based
    numbered. A free-text anchor (``options == []``) renders the numbered
    question ALONE — no option list. ``number`` may be ``None`` (no durable
    number allocated / degraded boot), in which case the bare question is used
    without the ``Q<n>:`` prefix.

    v0.84.0 (round 4, D1): the LENGTH caps this docstring used to lean on for a
    "no overflow path" claim are gone (``_validate_ask_args`` no longer bounds
    question/label length; only option COUNT stays capped at
    ``_ASK_MAX_OPTIONS``). The real Telegram 4096-char body limit is now
    enforced by a dedicated per-ask render-and-measure lifecycle validator
    (Task A3, spec §D1 bullet 2) rather than by an invented length heuristic
    here — this function never truncates.
    """
    base = _canonical_question(question, number) if number else question
    if not options:
        return base
    numbered = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
    return f"{base}\n\n{numbered}"


def _positional_settle_suffix(indices: list[int]) -> str:
    """v0.84.0 (round 4, D1 bullet 3, Task A3) — the BOUNDED positional settle
    copy for an ``answered`` outcome, replacing the old chosen-FULL-LABEL
    re-appending: an operator-chosen option's rendered length was previously
    unknown until settle time, which made the render-and-measure body-limit
    validator unboundable at ask time (an arbitrarily long label could push a
    settled message past 4096 chars with no way to have refused up front).

    1-based, ascending, de-duplicated option POSITIONS (never the label
    text): a single position renders ``\\n✅ Option <n>``; two or more render
    ``\\n✅ Options <n1>, <n2>, …``. An empty/all-invalid ``indices`` (a
    malformed outcome) falls back to ``\\n✅ Option ?`` — never raises.

    This is also the single source :func:`drivers.claude_code_driver
    .ask_lifecycle_suffixes` renders through for the live-answered worst-case
    form (multi worst case = every option selected)."""
    positions = sorted({i + 1 for i in indices})
    if not positions:
        return "\n✅ Option ?"
    if len(positions) == 1:
        return f"\n✅ Option {positions[0]}"
    return "\n✅ Options " + ", ".join(str(p) for p in positions)


def _ask_settle_text(question: str, outcome: dict, options: list) -> str:
    """v0.79.0 §4 — render the pinned settle copy below the canonical question.

    answered ⇒ the BOUNDED positional copy (v0.84.0 D1 bullet 3) —
    ``\\n✅ Option <n>`` (single-select) or ``\\n✅ Options <n1>, <n2>, …``
    (multi, 1-based ascending) — never the chosen label(s); expired ⇒
    ``\\n⌛ expired — answer by text below``; cancelled via a fresh operator
    message ⇒ ``\\n🚫 superseded by your message below``; any other cancel ⇒
    ``\\n🚫 cancelled``.
    """
    o = outcome.get("outcome")
    if o == "answered":
        # A5 · F-MULTI: a multi submit carries ``option_indices`` → settle
        # copy lists every chosen POSITION (single-select stays one position).
        indices = outcome.get("option_indices")
        if indices:
            valid = [
                i for i in indices
                if isinstance(i, int) and 0 <= i < len(options)
            ]
            return question + _positional_settle_suffix(valid)
        idx = outcome.get("option_index")
        valid_idx = (
            [idx] if isinstance(idx, int) and 0 <= idx < len(options) else []
        )
        return question + _positional_settle_suffix(valid_idx)
    if o == "cancelled":
        reason = outcome.get("reason")
        if reason == "superseded_by_text":
            return question + _SETTLE_SUPERSEDED
        if reason == "internal_error":
            return question + _SETTLE_INTERNAL_ERROR
        return question + _SETTLE_CANCELLED
    # no_answer / timeout.
    return question + _SETTLE_EXPIRED


def _ask_keyboard_finish(
    telegram_channel: Any, topic_id: int | None, message_id: int,
    question: str, options: list,
    *, on_settle: "Callable[[], Awaitable[None]] | None" = None,
    sleep: "Callable[[float], Awaitable[None]]" = asyncio.sleep,
    settle_edit: "Callable[[str], Awaitable[bool]] | None" = None,
    eng_id: str | None = None, number: int | None = None,
) -> Callable[[dict], "Awaitable[None]"]:
    """Broker finish-hook (r3-B3 shape, mirrors ``hooks._perm_keyboard_finish``)
    -- the engagement_ask namespace's ONLY keyboard-message writer. Fires
    exactly once on outcome (delivered by the broker even if the posting
    HTTP handler was cancelled/disconnected) and edits the posted question
    message to show the resolution AND CLEARS the keyboard (v0.79.0 §4, the
    real S1: ``clear_keyboard=True`` sends an explicit empty markup so the
    settled question can never be re-tapped). ``_on_inline_callback`` never
    edits the message itself -- it only ``cq.answer()``s.

    ``on_settle`` (§4): a callback run once on any terminal outcome to close the
    question's entry in the registry ``open_questions`` ledger.

    W-R1 (v0.81.0, Sol r2-2) — CONFIRMED-EDIT GATING: the settle edit can fail
    transiently (``edit_topic_message`` returns ``False`` on a timeout /
    non-'not-modified' BadRequest). Because the broker fires this hook exactly
    ONCE, a later tap cannot re-drive settlement. So bounds-retry the edit
    (``confirmed_settle_edit``: 3 attempts, 0.5→1→2 backoff, injected ``sleep``)
    and run ``on_settle`` (which closes the ledger entry) ONLY on a CONFIRMED
    edit. An unconfirmed edit leaves the keyboard live AND the ledger entry
    INTACT so the NEXT boot reconciliation (itself confirmed-edit gated) settles
    it — there is deliberately no later-tap re-drive.
    """

    async def _finish(outcome: dict) -> None:
        text = _ask_settle_text(question, outcome, options)
        # A5 · F-MULTI: a multi ask's settle edit routes through the SAME
        # sequencer markup primitive (``edit_discrete``) that the toggle redraw
        # uses, so the two writers serialize on ONE lock — a stale toggle redraw
        # can never land after (and resurrect) a settled keyboard. The finish
        # hook stays the sole TERMINAL writer. Single-select settles via the
        # RICH edit primitive (R2b/c, v0.89.0): ``edit_topic_message_rich``
        # re-renders the settled body's markdown (so a formatted ask does not
        # revert to literal ``**`` on answer/expire/supersede) while retaining
        # the explicit keyboard-clear (S1). An edit is a single edit with no
        # cancellation gate, so its render→plain fallback is acceptable.
        if settle_edit is not None:
            do_edit = lambda: settle_edit(text)  # noqa: E731
        else:
            do_edit = lambda: telegram_channel.edit_topic_message_rich(  # noqa: E731
                topic_id, message_id, text, clear_keyboard=True)
        confirmed = await confirmed_settle_edit(do_edit, sleep=sleep)
        if not confirmed:
            logger.warning(
                "ask keyboard finish-hook settle edit UNCONFIRMED after retries "
                "(topic=%s message_id=%s) — leaving keyboard live and the "
                "open-question ledger entry INTACT for boot reconciliation",
                topic_id, message_id,
            )
            return
        # A8 · Q1-settle observability: one INFO line per CONFIRMED settle (the
        # keyboard cleared on screen). Outcome mirrors the settle copy chosen by
        # ``_ask_settle_text`` — answered / no_answer→expired / cancelled (with
        # superseded + internal-error→withdrawn sub-reasons).
        _o = outcome.get("outcome")
        if _o == "answered":
            _outcome = "answered"
        elif _o == "cancelled":
            _outcome = {
                "superseded_by_text": "superseded",
                "internal_error": "withdrawn",
            }.get(outcome.get("reason"), "cancelled")
        else:
            _outcome = "expired"
        logger.info(
            "ask settle CONFIRMED (eng=%s q=%s mid=%s outcome=%s)",
            eng_id[:8] if eng_id else "-",
            number if number is not None else "-", message_id, _outcome)
        if on_settle is not None:
            try:
                await on_settle()
            except Exception:  # noqa: BLE001 — never raise from a finish hook
                logger.warning(
                    "ask keyboard finish-hook on_settle failed "
                    "(topic=%s message_id=%s)", topic_id, message_id,
                    exc_info=True,
                )

    return _finish


def _consume_turn_reply_to(driver: Any, eng_id: str) -> int | None:
    """#332: fetch-and-clear the sequencer's one-shot turn reply target via
    the driver seam so a deferred ask/reply that is the turn's first output
    threads to the operator's inbound message (v0.79.0 §3). getattr-tolerant:
    fakes/degraded drivers without the seam simply don't thread."""
    if driver is None:
        return None
    consume = getattr(driver, "consume_turn_reply_to", None)
    if consume is None:
        return None
    try:
        return consume(eng_id)
    except Exception:  # noqa: BLE001 — threading is cosmetic, never blocks a post
        logger.debug("consume_turn_reply_to failed", exc_info=True)
        return None


def _restore_turn_reply_to(driver: Any, eng_id: str, mid: int | None) -> None:
    """#332 failure arm: re-arm the consumed one-shot target after a FAILED
    send so the turn's first successful output still threads."""
    if driver is None or mid is None:
        return
    restore = getattr(driver, "restore_turn_reply_to", None)
    if restore is None:
        return
    try:
        restore(eng_id, mid)
    except Exception:  # noqa: BLE001
        logger.debug("restore_turn_reply_to failed", exc_info=True)


def _resolve_active_driver() -> Any:
    """Resolve the live ``claude_code`` driver (``agent.active_claude_code_driver``)
    for the inbound gate + discrete-intent registration. Returns ``None`` when
    no driver is attached (unit tests / degraded boot) so the ask handler falls
    back to its pre-v0.79.0 behavior (post + await, no gate/intent)."""
    try:
        import agent as _agent_mod
        return getattr(_agent_mod, "active_claude_code_driver", None)
    except Exception:  # noqa: BLE001
        return None


async def _maybe_allocate_number(engagement_registry: Any, eng_id: str) -> int | None:
    """Allocate the next durable Q-number, distinguishing ABSENT from RAISING
    (Sol r8-4). An ABSENT allocator (fake registry / degraded boot without the
    method) returns ``None`` → the legacy un-numbered degraded path. An allocator
    that RAISES PROPAGATES the exception so the caller can refuse the ask BEFORE
    any wire post (a successful, operator-visible, UNTRACKED ask would defeat the
    gap-free ``ask_inflight`` → durable-ownership handoff)."""
    alloc = getattr(engagement_registry, "allocate_question_number", None)
    if alloc is None:
        return None
    return await alloc(eng_id)


def _make_ask(
    telegram_channel: Any, engagement_registry: Any,
) -> Handler:
    """POST /internal/channel/ask — casa_engagement_channel's `ask` MCP tool.

    v0.75.0 (W5): registers on ``verdict_broker.BROKER`` (namespace
    ``"engagement_ask"``, scope = engagement_id), posts a tappable
    multiple-choice keyboard via the broker-owned shielded setup task
    (``ensure_posted``), then awaits the operator's tap.

    Body: ``{engagement_id, request_id, question, options: [str], timeout_s}``.
    Response: ``{"ok": True, "outcome": "answered", "option": <label>,
    "option_index": <int>}`` | ``{"ok": True, "outcome": "no_answer"}`` |
    ``{"ok": False, "error": <code>}``.

    A dropped HTTP connection (the caller's transport, not a logical
    cancel) leaves the broker request live for a same-``request_id`` retry
    to reattach -- ``await_result``'s shielded future is unaffected by this
    handler's own task being cancelled. Genuine caller cancellation is the
    separate explicit ``ask_cancel`` route (``_make_ask_cancel``).
    """

    async def handler(request: web.Request) -> web.Response:
        from verdict_broker import BROKER
        from channels.output_sequencer import ASK_TOOL, TERMINAL_REGISTRATION

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_args"})
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "invalid_args"})

        eng_id = body.get("engagement_id")
        request_id = body.get("request_id")
        if not eng_id or not request_id:
            return web.json_response({"ok": False, "error": "invalid_args"})

        # #335: an ask posted under another engagement's identity phishes the
        # operator in that engagement's topic AND routes the answer to the
        # forger — authenticate the id claim before any broker/keyboard work.
        auth_err = _engagement_auth_error(engagement_registry, eng_id, body)
        if auth_err is not None:
            return web.json_response({"ok": False, "error": auth_err})

        validated = _validate_ask_args(body)
        if validated is None:
            return web.json_response({"ok": False, "error": "invalid_args"})
        # ``options`` = FULL labels (body/meta/settle/response see these);
        # ``shorts`` = parallel agent-supplied shorts (keyboard-only, A4 rule 3).
        question, options, timeout_s, shorts = validated
        # A5 · F-MULTI: a toggle-many keyboard + ✅ Submit. Read AFTER validation
        # (which already refused multi + <2 options / anchor). Only the BUTTON
        # ask path can be multi — the anchor path below is never reached.
        multi = bool(body.get("multi", False))

        # D1 bullets 3 & 6 — render-and-measure lifecycle body-limit validator.
        # Moved to its spec-literal placement (AFTER Q-number allocation, with
        # the REAL number, BEFORE broker registration/posting): see the owner
        # paths below where ``_ask_body_limit_refusal`` is applied. The gate
        # (Task A4/A5) owns the refusal cleanup — the old pre-allocation
        # approximation is gone.
        from drivers.claude_code_driver import ask_lifecycle_suffixes

        def _ask_body_limit_refusal(number: "int | None") -> dict | None:
            """The self-explaining ``invalid_args`` payload iff THIS ask's
            rendered body + worst-case terminal lifecycle suffix would exceed
            Telegram's 4096-char message limit; ``None`` when it fits. Rendered
            with the REAL allocated ``number`` (multi worst case = every option
            selected) so the measurement is exact (spec §D1 bullets 3 & 6)."""
            # #328 family (Terra r1): measured in UTF-16 units — Telegram's
            # unit — so an astral-heavy body is refused ``invalid_args`` here
            # instead of passing ``len()`` and failing the keyboard post.
            body_ = render_ask_body(number, question, options)
            worst_suffix_len = max(
                utf16_len(s)
                for s in ask_lifecycle_suffixes(number, options, multi))
            rendered_len = utf16_len(body_) + worst_suffix_len
            if rendered_len <= _ASK_BODY_LIMIT:
                return None
            return {
                "ok": False, "error": "invalid_args",
                "detail": (
                    "rendered question+options would exceed Telegram's "
                    f"4096-char message limit (was {rendered_len} incl. "
                    "lifecycle suffix); shorten the question or reduce "
                    "options"
                ),
            }

        # A5 review, Finding 1: sweep every currently-retirable gate from a
        # PRIOR ask before creating/finding THIS request's own gate — see
        # ``_sweep_retirable_gates`` for why this (not this call's own
        # ``finally``) is where retirement actually has a chance to fire.
        # No await between this and the lookup below, so no other coroutine
        # can interleave and observe ``request_id``'s gate mid-sweep.
        _sweep_retirable_gates()

        # ---- Validation gate (spec §D1, Task A5) --------------------------
        # Create/find the per-request_id gate BEFORE any intent/allocation/
        # broker record. NO path (owner or reattacher) registers a broker
        # request until the single validation owner marks the gate PASSED;
        # every pre-PASSED terminal exit publishes its EXACT refusal payload
        # (or the cancellation latch) through the gate so a blocked reattacher
        # wakes with the byte-identical outcome, never burning its timeout.
        gate = get_or_create_gate(request_id)
        gate.acquire()
        # True once this call is known to be the single validation OWNER (not a
        # reattacher). The finally uses it as a safety net: an owner that somehow
        # exits with the gate still PENDING (a never-reached publication) would
        # strand a blocked reattacher — publish a generic internal_error so the
        # reattacher always wakes. A reattacher exiting PENDING (e.g. its own
        # transport cancel) never resolves the gate — only the owner does.
        _validation_owner = False
        try:
            # Cancel-first (spec §D1 r5-1): an ``ask_cancel`` that landed BEFORE
            # this /ask created the gate already latched cancellation — abort
            # now, before anything exists to clean up.
            if gate.effective()[0] == "CANCELLED":
                return web.json_response({"ok": False, "error": "cancelled"})
            rec = engagement_registry.get(eng_id)
            if rec is None:
                # wb7-2 (D1): a pre-PASSED terminal exit PUBLISHES its EXACT
                # refusal payload through the gate (``set_failed``) and returns the
                # SAME dict, so a same-request retry reads the RETAINED resolved
                # outcome (byte-identical) under the normal resolved-gate retention
                # — instead of the finally's never-owned-PENDING delete, which
                # dropped the gate and forced every retry to re-derive.
                payload = {"ok": False, "error": "unknown_engagement"}
                gate.set_failed(payload)
                return web.json_response(payload)
            if getattr(rec, "status", None) not in ("active", "idle"):
                payload = {"ok": False, "error": "engagement_terminal"}
                gate.set_failed(payload)
                return web.json_response(payload)

            driver = _resolve_active_driver()
            projection_hash = body.get("projection_hash")

            async def _advance_first_contact() -> None:
                advance = getattr(
                    engagement_registry, "advance_interaction_state", None)
                if advance is not None:
                    await advance(eng_id, "first_contact")

            # INBOUND GATE (§4): an unseen operator message means "end your turn" —
            # applies to EVERY kind of ask (button and free-text anchor). Consumes
            # no timeout budget; escalates from the 3rd consecutive refusal.
            def _refusal_response(record_intent: bool = False) -> web.Response:
                n = driver.record_ask_refusal(eng_id)
                copy = (
                    _ASK_REFUSAL_STERN if n >= _ASK_REFUSAL_ESCALATE_AT
                    else _ASK_REFUSAL
                )
                if record_intent:
                    # Sol A2 wave-3, Finding 3: record the refusal-count-FREE outcome
                    # on the intent (not a bare cancel) so a same-request_id retry
                    # reattaches to it and returns unread_inbound IMMEDIATELY, never
                    # awaiting the dead intent (→ deferred-post budget → delivery_failed).
                    _record_intent_unread_refusal(driver, eng_id, request_id, copy)
                # Task A5: publish the count-FREE unread refusal through the gate
                # (owner-only exit) — a blocked reattacher wakes with the SAME
                # count-free body it would read off the intent tombstone (the live
                # owner response keeps its ``refusal_count``; a retry never re-bumps).
                gate.set_failed(_unread_refusal_payload(copy))
                return web.json_response({
                    "ok": False, "error": "unread_inbound",
                    "message": copy, "refusal_count": n,
                })

            # --- FREE-TEXT ANCHOR (§4): options: [] posts a numbered anchor with
            # NO keyboard, registered in open_questions; the NEXT operator text
            # settles it (driver-side). Non-blocking — no broker request, no tap.
            # Posting is RELAY-DEFERRED (§2, review C1): the handler registers+arms
            # a discrete-send intent whose poster posts the numbered anchor, and the
            # relay posts it at the ask tool_use block (AFTER any preceding
            # narration). No driver/hash ⇒ eager fallback (pre-v0.79 behavior). ---
            if not options:
                # F5: register the discrete-send intent and check for a REATTACH
                # BEFORE allocating a Q-number — parity with the button-ask reattach
                # (a transport retry must NOT burn a fresh number or post a second
                # anchor). ``created_intent`` is True only on the genuinely-first
                # attempt; None when there is no live sequencer (eager fallback).
                #
                # GATE ORDERING (Sol A2 wave-2, Finding 4): the reattach-outcome
                # check runs FIRST — BEFORE the unread and away gates — exactly like
                # the button path. Previously the unread-inbound gate ran first, so a
                # same-request_id retry whose original was refused ``operator_away``
                # could get ``unread_inbound`` (an inbound cleared away but is still
                # unread) instead of its RECORDED outcome. Order now mirrors the
                # button path: reattach → away gate → unread gate.
                created_intent: bool | None = None
                if driver is not None and projection_hash:
                    res = driver.register_send_intent(
                        engagement_id=eng_id, request_id=request_id,
                        tool_name=ASK_TOOL, projection_hash=projection_hash,
                        poster=_noop_poster,
                        # wb2-4: pin the gate to THIS intent's lifetime; the pin
                        # releases when the intent retires at turn-end prune.
                        on_retire=lambda rid=request_id: _retire_intent_gate_pin(
                            rid),
                    )
                    # wb4-1: engagement terminalized mid-ingress — refuse rather
                    # than posting an anchor below the terminal completion.
                    if res is TERMINAL_REGISTRATION:
                        # wb7-2 (D1): publish the refusal through the gate + retain
                        # (see the rec-is-None exit); a retry reads it back.
                        payload = {"ok": False, "error": "engagement_terminal"}
                        gate.set_failed(payload)
                        return web.json_response(payload)
                    if res is not None:
                        _intent, created_intent = res
                        if created_intent:
                            _pin_gate_to_intent(gate, request_id)
                        if not created_intent:
                            # REATTACH: reuse the first attempt's outcome. If it
                            # already posted, return its id; if still UNRESOLVED,
                            # AWAIT the same bounded resolution and map None/timeout/
                            # failed to ok:false (F5 fail-closed) — never ok:true on
                            # an unresolved intent. No new number, no second anchor.
                            prior = driver.send_intent_outcome(eng_id, request_id)
                            # Finding 1: a prior operator-away refusal recorded an
                            # ``operator_away`` outcome — return it verbatim instead
                            # of awaiting the dead intent (→ delivery_failed).
                            if _refused_intent_outcome(prior):
                                return web.json_response(prior)
                            # GATE HANDSHAKE (spec §D1 r3-1, Task A5): the anchor
                            # reattacher, like the button reattacher, waits for the
                            # single validation owner to resolve the gate before
                            # acting. PENDING → await (ONLY while a local owner is
                            # present to resolve it — else fall through to the
                            # existing fail-closed reattach); FAILED/CANCELLED →
                            # return byte-identically (no deferred-post, no post).
                            eff = gate.effective()
                            if (eff[0] == "PENDING"
                                    and request_id in _ASK_VALIDATION_OWNERS):
                                await gate.event.wait()
                                eff = gate.effective()
                            if eff[0] == "CANCELLED":
                                return web.json_response(
                                    {"ok": False, "error": "cancelled"})
                            if eff[0] == "FAILED":
                                return web.json_response(eff[1])
                            if prior is None:
                                prior = await _await_deferred_post(
                                    driver, eng_id, request_id)
                            if prior is None or not prior.get("ok"):
                                return web.json_response(
                                    {"ok": False, "error": "delivery_failed"})
                            return web.json_response({
                                "ok": True, "outcome": "anchored",
                                "question_number": None,
                                "message_id": prior.get("message_id"),
                            })

                # Past the reattach short-circuit, this call is the anchor
                # validation OWNER — its terminal exits publish through the gate.
                # Announce ownership synchronously (no await since register) so a
                # same-process reattacher observes it and safely blocks on the gate.
                _validation_owner = True
                _ASK_VALIDATION_OWNERS.add(request_id)

                # F-EXPIRE (§A2.4) GATE: while operator-away, refuse a genuinely-new
                # anchor immediately — no number, no post, no broker. Placed AFTER the
                # reattach check so a transport retry of an already-posted anchor
                # still returns its recorded outcome above.
                if _operator_away_active(driver, eng_id):
                    if created_intent:
                        # Finding 1: record the refusal OUTCOME (not a bare cancel)
                        # so a same-id retry reattaches to it above.
                        _record_intent_refusal(driver, eng_id, request_id)
                    gate.set_failed(_away_refusal_payload())
                    return _away_refusal_response(driver, eng_id)

                # INBOUND GATE (§4): an unseen operator message means "end your turn".
                # Placed AFTER the reattach + away checks (Finding 4) — a genuinely-new
                # anchor is refused here; a same-id retry never reaches this point. A
                # freshly-created intent records the unread_inbound OUTCOME (Sol A2
                # wave-3, Finding 3 — NOT a bare cancel) so a retry reattaches to it.
                if driver is not None and driver.inbound_unread_depth(eng_id) > 0:
                    return _refusal_response(record_intent=bool(created_intent))

                # §A3(c) INGRESS RESERVATION (Sol r2-8/r3-6): under the ask-
                # maintenance lock, atomically CHECK the live-pending predicate and
                # CLAIM the ``ask_inflight`` marker. A second concurrent ask (any
                # kind) then sees the marker/predicate and refuses ``question_pending``
                # — making "one question at a time" structural. The lock is held for
                # the CHECK + marker ONLY, never across the post/await below. Sample
                # the operator-generation at this reserve point for the post-add
                # re-check (a message that lands between reserve and add is the answer).
                gen_at_entry = (
                    driver.inbound_generation(eng_id) if driver is not None else 0)
                _anchor_lock = _ask_maint_lock(driver, eng_id)
                if _anchor_lock is not None:
                    async with _anchor_lock:
                        if _ask_pending_predicate(
                                driver, eng_id, exclude_request_id=request_id):
                            if created_intent:
                                driver.cancel_send_intent(eng_id, request_id)
                            anchor_pending = _ask_pending_payload(driver, eng_id)
                            gate.set_failed(anchor_pending)
                            return web.json_response(anchor_pending)
                        driver.set_ask_inflight(eng_id, request_id)

                # First attempt (created intent) OR eager fallback: allocate the
                # durable number. A RAISING allocator (Sol r8-4) is TERMINAL BEFORE
                # any wire post — clear the marker, tombstone the intent with an
                # internal_error outcome (retries short-circuit), refuse. An ABSENT
                # allocator returns None → the un-numbered legacy degraded path.
                try:
                    number = await _maybe_allocate_number(engagement_registry, eng_id)
                except Exception:  # noqa: BLE001
                    logger.warning("anchor number allocation failed (eng=%s)",
                                   eng_id[:8], exc_info=True)
                    _clear_ask_marker(driver, eng_id, request_id)
                    if created_intent:
                        _record_intent_internal_error(driver, eng_id, request_id)
                    gate.set_failed(_internal_error_payload())
                    return web.json_response(_internal_error_payload())
                except BaseException:
                    # B1: from the moment ``set_ask_inflight`` claimed the marker, ANY
                    # non-durable-ownership exit MUST clear it. Transport CANCELLATION
                    # (CancelledError) during this awaited allocation bypasses ``except
                    # Exception``; without this the marker wedges and every later
                    # ask/reply is refused ``question_pending`` until restart. The CAS
                    # clear is a no-op once durable ownership took over (add_open_question
                    # clears it synchronously), so it is safe on every path.
                    _clear_ask_marker(driver, eng_id, request_id)
                    # B2 (wave 2): the PENDING intent (registered, not yet armed) is
                    # still matchable — a same-request_id retry would hang on the
                    # transport budget waiting for a never-armed intent (→
                    # delivery_failed). Tombstone it + record a cancelled outcome so
                    # the retry short-circuits to the recorded outcome. Synchronous —
                    # a still-pending intent has no in-flight post, so the cancel
                    # takes effect; the marker was already cleared above.
                    if created_intent:
                        _record_intent_cancelled(driver, eng_id, request_id)
                    # Task A5: transport cancellation → gate CANCELLATION latch.
                    gate.set_cancelled()
                    raise
                # W-R3: canonical body (anchor ⇒ options == [] ⇒ numbered question
                # ALONE, no option list — unchanged from the pre-W-R3 anchor copy).
                display = render_ask_body(number, question, options)

                # D1 bullets 3 & 6 (Task A5) — render-and-measure body-limit
                # validator at its spec-literal placement (post-allocation, REAL
                # number, pre-post). On refusal the gate owns the cleanup.
                anchor_body_refusal = _ask_body_limit_refusal(number)
                if anchor_body_refusal is not None:
                    _clear_ask_marker(driver, eng_id, request_id)
                    if created_intent:
                        _record_intent_invalid_args(
                            driver, eng_id, request_id, anchor_body_refusal)
                    gate.set_failed(anchor_body_refusal)
                    return web.json_response(anchor_body_refusal)

                # Final latch-then-completion read + PASSED marker. A6 (spec §D1
                # "Anchors get the same protection"): from this read through
                # installing/arming the placeholder's poster there is NO await —
                # the PASSED → ARM handoff is one synchronous section, so a cancel
                # can never slip a half-armed intent through. A cancel that
                # latches AFTER this point is caught by the poster's wire re-read
                # (below), which no-ops on CANCELLED immediately before its ONE
                # plain send.
                if gate.effective()[0] == "CANCELLED":
                    _clear_ask_marker(driver, eng_id, request_id)
                    if created_intent:
                        _record_intent_cancelled(driver, eng_id, request_id)
                    return web.json_response({"ok": False, "error": "cancelled"})
                gate.set_passed()

                async def _post_anchor() -> int | None:
                    # A3 · F-ORDER (Sol A3 wave 5): once the post WINS a transport-
                    # cancel race (``_post_wins``), the handler's outer ``finally`` is
                    # gated OFF and the poster OWNS the ``ask_inflight`` clear. The
                    # ``finally`` below guarantees it on EVERY exit that did NOT reach
                    # durable ownership — the wire send raising / returning None, the
                    # add-failure compensation, a never-durable escape — so the marker
                    # can never wedge (a wedged marker refuses every later ask/reply
                    # ``question_pending`` until restart). ``_durable`` gates the clear
                    # off once durable ownership was reached in THIS invocation (the
                    # sync clear at that point already ran, and the ``finally`` would
                    # CAS-no-op anyway — but a running turn's later ⏳/settle awaits
                    # must not have the live anchor's marker stripped from under them).
                    _durable = False
                    try:
                        # A6 (spec §D1, Sol r9-2): FINAL cancellation revalidation
                        # at the wire. This poster runs UNDER the sequencer's
                        # writer lock (the relay awaits it inside
                        # ``_post_intent_locked``) — or inline in the eager
                        # no-sequencer fallback — so re-reading the gate here is
                        # the LAST thing before the physical send. A cancel that
                        # latched after PASSED/ARM (or whose intent tombstone lost
                        # the "post wins" race) no-ops HERE: nothing posts, the
                        # awaiting handler resolves ok:false. The marker is cleared
                        # by the ``finally`` below (this is a non-durable exit).
                        if gate.effective()[0] == "CANCELLED":
                            return None
                        # wb3-1: revalidate TERMINAL lifecycle in the SAME locked
                        # section as the cancel re-read (this poster runs under the
                        # sequencer's writer lock). A terminal latch that won the
                        # race means the engagement is closing/closed — post
                        # NOTHING and ledger NOTHING, so a closed engagement can
                        # never retain a visibly-open, durably-ledgered anchor
                        # (the completion flush would otherwise invoke this
                        # still-PASSED poster AFTER the sole settlement pass).
                        _seq_terminal = getattr(
                            driver, "sequencer_is_terminal", None)
                        if _seq_terminal is not None and _seq_terminal(eng_id):
                            return None
                        # A6 (spec §D1, Sol r9-2) + R2b (v0.89.0): SINGLE-ATTEMPT
                        # rich send — like the button-ask body, ``post_ask_body_rich``
                        # performs exactly ONE physical send and FAILS CLOSED on an
                        # entity BadRequest (no plain retry). The rich
                        # ``send_response_to_topic`` could send twice (rich →
                        # BadRequest → plain fallback), and a cancel landing during
                        # the awaited first attempt would not be re-checked before
                        # the fallback posted the abandoned anchor — the double-send
                        # this forecloses. Plain bodies send plain_text (render() →
                        # entities=None, #1330), so this remains one send either
                        # way; markdown now renders as MessageEntity spans.
                        try:
                            mid = await telegram_channel.post_ask_body_rich(
                                rec.topic_id, display)
                        except Exception:  # noqa: BLE001
                            logger.warning("free-text anchor post failed (eng=%s)",
                                           eng_id[:8], exc_info=True)
                            return None
                        if not isinstance(mid, int):
                            return None
                        # DURABLE OWNERSHIP: register open_questions ONLY after a
                        # successful post (a crash before the relay reaches the block
                        # leaves NO dangling ledger entry). §A3(c) COMPENSATION (Sol
                        # r5-5/r6-1): an ``add_open_question`` failure AFTER the wire
                        # post leaves an orphan message — best-effort WITHDRAW-edit it
                        # via the RAW wire primitive (never edit_discrete — this poster
                        # runs under the sequencer lock on the relay task, no
                        # reacquisition) and account the COMPOUND outcome
                        # (``mark_send_intent_compensated``: high-water advances, intent
                        # resolves ok:false+compensated). The exception NEVER escapes
                        # the poster; the compensation is a NON-durable exit, so the
                        # ``finally`` (CAS) still clears the marker.
                        added = False
                        if number is not None:
                            add = getattr(
                                engagement_registry, "add_open_question", None)
                            if add is not None:
                                try:
                                    # wb2-1: record the ask's projection hash as the
                                    # anchor's SOURCE HASH so the relay can bind its
                                    # narration-suppression candidate positively to
                                    # THIS anchor (never a prior/co-existing one).
                                    await add(eng_id, number, mid, text=display,
                                              kind="anchor",
                                              source_hash=projection_hash)
                                    added = True
                                except Exception:  # noqa: BLE001
                                    logger.warning(
                                        "anchor add_open_question failed — withdrawing "
                                        "(eng=%s Q%s)", eng_id[:8], number,
                                        exc_info=True)
                                    await _withdraw_anchor(
                                        telegram_channel, rec.topic_id, mid)
                                    if driver is not None:
                                        comp = getattr(
                                            driver, "mark_send_intent_compensated",
                                            None)
                                        if comp is not None:
                                            try:
                                                await comp(eng_id, request_id, mid)
                                            except Exception:  # noqa: BLE001
                                                logger.debug(
                                                    "compensate seam failed",
                                                    exc_info=True)
                                    return None
                        # Marker cleared SYNCHRONOUSLY at durable ownership (no
                        # maintenance lock — the unanswered-anchor clause takes over
                        # gap-free). In the ABSENT-allocator degraded mode (no number,
                        # no add) this is the poster's terminal path and the
                        # one-question invariant is UNAVAILABLE (Sol r9-4).
                        _clear_ask_marker(driver, eng_id, request_id)
                        _durable = True
                        # POST-ADD GENERATION RE-CHECK: an operator envelope that
                        # arrived between reserve and add IS this anchor's answer —
                        # mark it answered + settle instead of leaving it ⏳ waiting.
                        gen_bumped = (
                            added and driver is not None
                            and driver.inbound_generation(eng_id) != gen_at_entry
                        )
                        if gen_bumped:
                            settle = getattr(driver, "settle_answered_anchor", None)
                            if settle is not None:
                                try:
                                    await settle(eng_id, number)
                                except Exception:  # noqa: BLE001 — best-effort
                                    logger.debug(
                                        "gen-recheck settle failed", exc_info=True)
                        else:
                            # W-R2: a posted, un-answered anchor hands the ball to the
                            # operator → ⏳ waiting for your reply (driven from the ask
                            # lifecycle; the next operator text settles it driver-side).
                            if driver is not None:
                                note = getattr(driver, "note_ask_waiting", None)
                                if note is not None:
                                    await note(eng_id)
                        await _advance_first_contact()
                        return mid
                    finally:
                        # Sol A3 wave 5: the poster owns the marker clear on every
                        # non-durable exit (CAS — a no-op once ``_durable`` cleared it
                        # or a later ask re-claimed the marker).
                        if not _durable:
                            _clear_ask_marker(driver, eng_id, request_id)

                # A3 · F-ORDER (Sol A3 wave 4): when a transport cancel LOSES to an
                # in-flight relay post, the poster owns the marker (it clears it at
                # durable ownership) — the outer ``finally`` must NOT clear it, or a
                # SECOND cancel that lands mid-post would strip the marker while a
                # question is still being posted, admitting a second live question.
                _post_wins = False
                try:
                    if created_intent:
                        # DEFERRED (relay-mediated) created path: install the poster,
                        # ARM, and AWAIT the outcome fail-closed (F3/F5).
                        driver.set_send_intent_poster(eng_id, request_id, _post_anchor)
                        driver.arm_send_intent(eng_id, request_id)
                        try:
                            outcome = await _await_deferred_post(
                                driver, eng_id, request_id)
                        except BaseException:
                            # B1 (wave 2) + A3 · F-ORDER (Sol A3 wave 3/4): a transport
                            # CANCELLATION after ARM. The cleanup is FULLY SYNCHRONOUS
                            # (no await ⇒ immune to a second Task.cancel()). If the relay
                            # has NOT started the post, the cancel takes effect: the
                            # armed intent tombstones so the relay consume-cancels the
                            # block (nothing posts) and a same-id retry reads the
                            # recorded cancelled outcome — the finally then clears the
                            # marker. If the relay IS mid-post (``posting`` set, holding
                            # the writer lock inside the poster), the sync cancel reads
                            # ``posting`` and NO-OPS (returns False) — the cancel is LOST
                            # ("the post wins"), the intent keeps its SUCCESS outcome,
                            # and the marker is left to the poster (which clears it at
                            # durable ownership). Gate the finally on that decision.
                            if (_record_intent_cancelled(driver, eng_id, request_id)
                                    == INTENT_CANCEL_POST_WON):
                                _post_wins = True
                            raise
                        # §A3(c): the compensated add-failure maps to ok:false
                        # internal_error (the wire message exists but the question was
                        # withdrawn) — distinct from a plain delivery_failed.
                        if outcome is not None and outcome.get("compensated"):
                            return web.json_response({
                                "ok": False, "error": "internal_error",
                                "message": _ASK_INTERNAL_ERROR_MSG,
                            })
                        if outcome is None or not outcome.get("ok"):
                            return web.json_response(
                                {"ok": False, "error": "delivery_failed"})
                        return web.json_response({
                            "ok": True, "outcome": "anchored",
                            "question_number": number,
                            "message_id": outcome.get("message_id"),
                        })

                    # EAGER fallback (no live sequencer): post the anchor now.
                    mid = await _post_anchor()
                    if mid is None:
                        return web.json_response(
                            {"ok": False, "error": "delivery_failed"})
                    return web.json_response({
                        "ok": True, "outcome": "anchored",
                        "question_number": number, "message_id": mid,
                    })
                finally:
                    # Terminal-failure BACKSTOP: clear the marker if this request
                    # still owns it (CAS — a no-op when the poster already cleared it
                    # at durable ownership, or a later ask claimed the marker). SKIP
                    # the clear when a transport cancel LOST to an in-flight post
                    # (``_post_wins``): the winning poster owns the marker and clears
                    # it at durable ownership — clearing here would strip it mid-post
                    # and admit a second live question (Sol A3 wave 4 double-cancel).
                    if not _post_wins:
                        _clear_ask_marker(driver, eng_id, request_id)

            # --- BUTTON ask ---------------------------------------------------
            # Register the discrete-send INTENT (pending) at the ingress boundary for
            # idempotent transport-retry REATTACHMENT (§2(1)). The REAL relay-invoked
            # poster is installed just before we ARM (below) — posting is
            # RELAY-DEFERRED (§2, review C1): the relay posts the keyboard at the
            # ask's tool_use block, AFTER any preceding narration in the same frame.

            # Reserve the operator-message generation for the post-then-recheck race
            # AND the F-EXPIRE operator-away CAS. Sampled ONCE at entry, BEFORE any
            # BROKER.register, and stamped into the ask's static meta as
            # ``inbound_gen`` so both the main waiter and a same-request_id reattacher
            # (live request OR retired tombstone — both retain meta) read the SAME
            # generation for ``note_operator_away`` (Sol r2-2: a lost-response retry
            # reusing the FIRST attempt's generation can never re-wedge a cleared
            # away state with a fresher generation).
            gen_at_entry = (
                driver.inbound_generation(eng_id) if driver is not None else 0)

            def _ask_static_meta() -> dict:
                # F1 (Sol r3): the keyboard's STATIC metadata (options + topic_id +
                # operator_id + inbound_gen), seeded ATOMICALLY at broker creation.
                # The old code seeded meta AFTER register (``if created:
                # req.meta.update(...)``) ONLY on the main path, which lost the
                # metadata whenever a concurrent same-request_id RETRY created the
                # broker request first: the first attempt, suspended in number
                # allocation, resumed to find ``created=False`` and skipped the init,
                # leaving meta = {"message_id": ...} only ⇒ every tap rejected
                # (topic_id/operator_id both absent). Now BOTH the reattach path and
                # the main path pass ``meta=`` to ``register`` (a single synchronous
                # op — register only seeds meta on creation, with no await between),
                # so whichever call wins the create race installs the complete static
                # metadata.
                # D2 items 2-3 (Task A5): resolve the whole-set button labels
                # ONCE, here, and persist them as ``button_labels`` static meta
                # BEFORE ``BROKER.register`` — so whichever party wins the create
                # race registers COMPLETE captions and the initial render AND
                # every multi redraw consume the SAME persisted captions
                # (byte-identical, never re-resolved from ``shorts``).
                from channels.telegram import resolve_button_labels
                combined = [
                    {
                        "label": str(opt),
                        "short": (
                            shorts[i] if shorts is not None and i < len(shorts)
                            else None
                        ),
                    }
                    for i, opt in enumerate(options)
                ]
                return {
                    "options": options,
                    "topic_id": rec.topic_id,
                    "operator_id": rec.origin.get("user_id"),
                    "inbound_gen": gen_at_entry,
                    # A5 · F-MULTI: the tap dispatcher reads ``multi`` to branch and
                    # ``shorts`` to rebuild the toggle keyboard on every redraw
                    # (``selected`` is created lazily by ``toggle_selection``).
                    "multi": multi,
                    "shorts": shorts,
                    "button_labels": resolve_button_labels(combined, multi),
                }

            intent_registered = False
            if driver is not None and projection_hash:
                res = driver.register_send_intent(
                    engagement_id=eng_id, request_id=request_id,
                    tool_name=ASK_TOOL, projection_hash=projection_hash,
                    poster=_noop_poster,
                    # wb2-4: pin the gate to THIS intent's lifetime (released at
                    # the intent's turn-end retirement).
                    on_retire=lambda rid=request_id: _retire_intent_gate_pin(rid),
                )
                # wb4-1: engagement terminalized mid-ingress — refuse rather than
                # posting a keyboard below the terminal completion.
                if res is TERMINAL_REGISTRATION:
                    # wb7-2 (D1): publish the refusal through the gate + retain
                    # (see the rec-is-None exit); a retry reads it back.
                    payload = {"ok": False, "error": "engagement_terminal"}
                    gate.set_failed(payload)
                    return web.json_response(payload)
                if res is not None:
                    _intent, created_intent = res
                    if created_intent:
                        _pin_gate_to_intent(gate, request_id)
                    if not created_intent:
                        # Finding 1: a prior operator-away refusal recorded an
                        # ``operator_away`` outcome on the intent → return it verbatim
                        # BEFORE touching the broker (reattach-outcome check → away
                        # gate → broker). Without this, the retry re-registers a fresh
                        # broker request and burns the full timeout with no keyboard.
                        prior = driver.send_intent_outcome(eng_id, request_id)
                        if _refused_intent_outcome(prior):
                            return web.json_response(prior)
                        # #347: a recorded ``ok: false`` post outcome (the
                        # deferred keyboard post failed, or the sequencer's
                        # terminal latch resolved it fail-closed) is TERMINAL
                        # — ``ensure_posted`` unregistered the broker request
                        # WITHOUT a tombstone, so falling through to the F2
                        # reattach would register a FRESH request that no
                        # poster will ever serve, burning the full timeout to
                        # ``no_answer`` (which can pause the engagement).
                        # Return the same delivery_failed the first attempt
                        # returned — but ONLY when the broker really has no
                        # live/tombstoned record for this id (Terra r1): a
                        # posted-then-withdrawn ask (add_open_question
                        # failure → internal_error cancel) can leave an
                        # ok:false shape on the intent while the broker
                        # tombstone still holds the ACCURATE outcome; the F2
                        # reattach below returns that verbatim.
                        if (
                            isinstance(prior, dict)
                            and prior.get("ok") is False
                            and BROKER.get_meta(
                                namespace="engagement_ask", scope=eng_id,
                                request_id=request_id) is None
                        ):
                            return web.json_response(
                                {"ok": False, "error": "delivery_failed"})
                        # GATE HANDSHAKE (spec §D1 r3-1, Task A5): a reattacher
                        # NEVER registers a broker request until the single
                        # validation owner marks the gate PASSED. A PENDING gate
                        # means the owner is still validating (allocation/render/
                        # size check) — await its resolution (ONLY while a local
                        # owner is present to resolve it; else fall through to the
                        # existing broker reattach), then act on the effective
                        # (latch-first) outcome so a refusal/cancel is returned
                        # byte-identically without burning the timeout against a
                        # broker the owner will never commit.
                        eff = gate.effective()
                        if (eff[0] == "PENDING"
                                and request_id in _ASK_VALIDATION_OWNERS):
                            await gate.event.wait()
                            eff = gate.effective()
                        if eff[0] == "CANCELLED":
                            return web.json_response(
                                {"ok": False, "error": "cancelled"})
                        if eff[0] == "FAILED":
                            return web.json_response(eff[1])
                        # F2 (was N1): a same-request_id retry REATTACHES (§2(1)) —
                        # whether the relay has already posted (prior has a
                        # message_id) OR the first attempt is still in flight and the
                        # keyboard has not posted yet (not-yet-posted, armed). EITHER
                        # WAY: NO new number allocation, NO second keyboard, NO eager
                        # fallback. Reattach to the broker request (idempotent by
                        # request_id) and await the same tap outcome. The old code
                        # only took this path when a message_id was recorded and
                        # otherwise fell through — allocating a fresh Q-number and
                        # posting a SECOND keyboard eagerly (the probe: Q2 posting
                        # before the relay's Q1, both ledger entries surviving).
                        # F1: create-with-metadata atomically. If THIS reattach wins
                        # the create race (the first attempt is still suspended in
                        # number allocation), it seeds the complete static metadata;
                        # if the request already exists, ``meta`` is ignored (register
                        # only seeds on creation) and the existing meta is reused.
                        req, _c = BROKER.register(
                            namespace="engagement_ask", scope=eng_id,
                            request_id=request_id, timeout_s=timeout_s,
                            meta=_ask_static_meta(),
                        )
                        outcome = await BROKER.await_result(req)
                        return await _ask_final_response(
                            outcome, options, driver, eng_id, request_id, req)
                    intent_registered = True

            # Past the reattach short-circuit, this call is the single validation
            # OWNER (created the intent, or the eager no-sequencer fallback). Every
            # terminal exit below publishes its EXACT payload / cancellation through
            # the gate so a blocked reattacher wakes byte-identically. Announce
            # ownership synchronously (no await since register) so a same-process
            # reattacher observes it and safely blocks on the gate.
            _validation_owner = True
            _ASK_VALIDATION_OWNERS.add(request_id)

            # F-EXPIRE (§A2.4) GATE: while operator-away, refuse a genuinely-new ask
            # immediately — no broker request, no keyboard, no timeout burn. Placed
            # AFTER the reattach check so a transport retry of an already in-flight
            # ask still reattaches to its live/tombstoned outcome above.
            if _operator_away_active(driver, eng_id):
                if intent_registered:
                    # Finding 1: record the refusal OUTCOME (not a bare cancel) so a
                    # same-id retry reattaches to it above.
                    _record_intent_refusal(driver, eng_id, request_id)
                gate.set_failed(_away_refusal_payload())
                return _away_refusal_response(driver, eng_id)

            # INBOUND GATE (§4): an unseen operator message means "end your turn". A
            # registered intent records the unread_inbound OUTCOME (Sol A2 wave-3,
            # Finding 3 — NOT a bare cancel) so a same-request_id retry reattaches to
            # it and returns unread_inbound immediately instead of delivery_failed.
            if driver is not None and driver.inbound_unread_depth(eng_id) > 0:
                return _refusal_response(record_intent=intent_registered)

            # §A3(c) INGRESS RESERVATION (Sol r2-8/r3-6): atomically CHECK the live-
            # pending predicate + CLAIM the ``ask_inflight`` marker under the ask-
            # maintenance lock (held for the check + marker ONLY). A second concurrent
            # ask sees it and refuses ``question_pending``. The marker clears at
            # BROKER.register below (durable ownership — the broker-pending clause
            # takes over gap-free) and on the allocation-failure path.
            _btn_lock = _ask_maint_lock(driver, eng_id)
            if _btn_lock is not None:
                async with _btn_lock:
                    if _ask_pending_predicate(
                            driver, eng_id, exclude_request_id=request_id):
                        if intent_registered:
                            driver.cancel_send_intent(eng_id, request_id)
                        pending_payload = _ask_pending_payload(driver, eng_id)
                        gate.set_failed(pending_payload)
                        return web.json_response(pending_payload)
                    driver.set_ask_inflight(eng_id, request_id)

            # A RAISING allocator (Sol r8-4) is TERMINAL BEFORE any wire post — clear
            # the marker, tombstone the intent (internal_error outcome; retries
            # short-circuit), refuse. An ABSENT allocator returns None (un-numbered
            # legacy path).
            try:
                number = await _maybe_allocate_number(engagement_registry, eng_id)
            except Exception:  # noqa: BLE001
                logger.warning("button number allocation failed (eng=%s)",
                               eng_id[:8], exc_info=True)
                _clear_ask_marker(driver, eng_id, request_id)
                if intent_registered:
                    _record_intent_internal_error(driver, eng_id, request_id)
                gate.set_failed(_internal_error_payload())
                return web.json_response(_internal_error_payload())
            except BaseException:
                # B1: transport CANCELLATION during the awaited allocation (the only
                # await between ``set_ask_inflight`` and the durable ``BROKER.register``
                # handoff below) must NOT wedge the ingress marker — clear it (CAS)
                # before re-raising. Durable ownership disarms this by clearing the
                # marker itself synchronously at register.
                _clear_ask_marker(driver, eng_id, request_id)
                # B2 (wave 2): the PENDING intent stays matchable — a same-request_id
                # button retry would reattach, find no recorded outcome, and register a
                # FRESH broker request that never posts (full timeout burn, no
                # keyboard). Tombstone it + record a cancelled outcome so the retry
                # short-circuits before touching the broker. Synchronous — a
                # still-pending intent has no in-flight post, so the cancel takes
                # effect; the marker was already cleared above.
                if intent_registered:
                    _record_intent_cancelled(driver, eng_id, request_id)
                # Task A5: transport cancellation of the owner task sets the
                # gate's CANCELLATION latch (never the completion slot) + wakes
                # any blocked reattacher, which then reads CANCELLED.
                gate.set_cancelled()
                raise
            # W-R3 (Sol r1-5): the SINGLE canonical body — full options VERBATIM,
            # numbered, below the question. This exact string feeds the keyboard
            # post, the persisted ``open_questions[].text``, the finish-hook settle
            # base, and (via the persisted text) boot reconciliation.
            display = render_ask_body(number, question, options)

            # D1 bullets 3 & 6 (Task A5) — render-and-measure body-limit validator
            # at its spec-literal placement: AFTER Q-number allocation (REAL
            # number) and BEFORE broker registration/posting. On refusal, the gate
            # owns the cleanup: clear the ingress marker, tombstone the intent with
            # the detailed invalid_args outcome (so a retry short-circuits), publish
            # FAILED(exact payload) so a blocked reattacher wakes byte-identically.
            body_refusal = _ask_body_limit_refusal(number)
            if body_refusal is not None:
                _clear_ask_marker(driver, eng_id, request_id)
                if intent_registered:
                    _record_intent_invalid_args(
                        driver, eng_id, request_id, body_refusal)
                gate.set_failed(body_refusal)
                return web.json_response(body_refusal)

            # No-yield PASSED → BROKER.register handoff (spec §D1): re-read the
            # cancellation latch a final time — a cancel that landed during
            # allocation aborts here with NO broker record and NO post. Otherwise
            # mark the gate PASSED and register with NO await in between, so
            # cancellation winning ANY ordering means no broker/no post/marker clear.
            if gate.effective()[0] == "CANCELLED":
                _clear_ask_marker(driver, eng_id, request_id)
                if intent_registered:
                    _record_intent_cancelled(driver, eng_id, request_id)
                return web.json_response({"ok": False, "error": "cancelled"})
            gate.set_passed()
            # wb3-4 (D2 item 4): emit the CONTENT-FREE floor telemetry EXACTLY
            # ONCE, here at the single validation OWNER's PASSED point (a
            # reattacher never reaches ``set_passed``), when the whole-set button
            # resolver FLOORED this ask. This is the accepted telemetry-gated-
            # Haiku decision's data source (previously ``floored_ask_telemetry``
            # was defined but never called in production). A non-floored ask logs
            # nothing; the line carries only count/reason/bitmap/hash — never the
            # option or question text.
            from channels.telegram import floored_ask_telemetry_line
            # Rebuild the SAME {label, short} combined view the resolver classifies
            # in ``_ask_static_meta`` (``options`` here is full labels; ``shorts``
            # the parallel agent-supplied array), so the floor decision + bitmap
            # match the captions actually rendered.
            _floor_combined = [
                {
                    "label": str(opt),
                    "short": (
                        shorts[i] if shorts is not None and i < len(shorts)
                        else None
                    ),
                }
                for i, opt in enumerate(options)
            ]
            _floor_line = floored_ask_telemetry_line(_floor_combined, multi=multi)
            if _floor_line is not None:
                logger.info(_floor_line)
            # F1: create-with-metadata atomically (STATIC meta seeded at creation so
            # a fast tap never sees incomplete metadata — r3-B3 fast-tap — AND a
            # concurrent reattach that created the request first still finds it
            # complete). message_id + finish_hook are set later by the broker-owned
            # setup task (r8-B3). ``meta`` is ignored if the request already exists.
            req, _created = BROKER.register(
                namespace="engagement_ask", scope=eng_id, request_id=request_id,
                timeout_s=timeout_s, meta=_ask_static_meta(),
            )
            # §A3(c): durable ownership reached — the request is live in the broker
            # (BROKER.pending non-empty), so the ingress marker clears SYNCHRONOUSLY
            # here (no maintenance lock) and the broker-pending clause of the gate
            # predicate takes over gap-free.
            _clear_ask_marker(driver, eng_id, request_id)

            # W-R2 linearization pin (Sol r2-1): the finish hook can become runnable
            # (a FAST TAP) before ``_post_ask`` finishes registering the open
            # question and setting ⏳ waiting. Gate the settlement recompute behind
            # this event — set by ``_post_ask`` ONLY after durable registration + the
            # waiting submission — so the recompute's revision is always allocated
            # LAST and a fast tap can never leave the summary stuck-waiting. The
            # event is ALWAYS set by ``_post_ask``'s finally (even on supersede /
            # add failure), and the finish hook exists only once ``_post_ask``
            # reached ``ensure_posted`` (which wires it), so this wait cannot hang.
            _ask_registered = asyncio.Event()

            async def _close_question() -> None:
                await _ask_registered.wait()
                if number is not None:
                    close = getattr(engagement_registry, "close_open_question", None)
                    if close is not None:
                        try:
                            await close(eng_id, number)
                        except Exception:  # noqa: BLE001
                            # M4: close_open_question is now STRICT (rollback + raise).
                            # Treat a raise as RETAINED — the entry stays for a later
                            # settle / boot-reconcile; still recompute the summary.
                            logger.warning(
                                "engagement %s: close_open_question failed on settle "
                                "(Q%s) — entry retained", eng_id[:8], number,
                                exc_info=True)
                # Recompute the summary status from the remaining open questions
                # (still ⏳ waiting while any question is open; ⚙️ working once none
                # remain and the turn is running).
                if driver is not None:
                    recompute = getattr(driver, "recompute_engagement_status", None)
                    if recompute is not None:
                        await recompute(eng_id)

            # The DEFERRED poster (§2, review C1): the relay invokes this at the
            # ask's tool_use block (or the slot/intent-timeout watcher posts it
            # out-of-band). It posts the keyboard + wires the finish hook +
            # message_id via ``ensure_posted`` (post-once contract preserved), then
            # continues REACTIVELY off the posted message id: generation re-check,
            # open_questions registration, first-contact advance. Registering the
            # open question only AFTER a successful, non-superseded post means a
            # crash before the relay reaches the block leaves NO dangling ledger
            # entry — the broker TTL expires the ask instead.
            # Pass ``shorts`` to the keyboard ONLY when at least one option carried an
            # agent short — str-only asks keep today's call shape (backward-compatible
            # with existing keyboard fakes that don't accept the kwarg). A5 · F-MULTI:
            # a multi ask always passes ``multi=True`` (and ``shorts`` so the toggle
            # rows carry the agent shorts / heuristic labels).
            _kbd_kwargs: dict[str, Any] = {}
            if any(shorts):
                _kbd_kwargs["shorts"] = shorts
            if multi:
                _kbd_kwargs["multi"] = True
                _kbd_kwargs["shorts"] = shorts

            # A5 · F-MULTI: the multi settle edit routes through the sequencer's
            # ``edit_discrete`` (via the driver seam) so it serializes on the same
            # lock as the toggle redraw. Degrades to the direct ``edit_topic_message``
            # path when there is no live driver/sequencer (eager fallback / fakes).
            def _make_settle_edit(mid: int):
                if not multi or driver is None:
                    return None
                settle = getattr(driver, "settle_ask_keyboard", None)
                if settle is None:
                    return None
                async def _settle(text: str) -> bool:
                    return await settle(eng_id, mid, text)
                return _settle

            async def _post_ask() -> int | None:
                # #332: a deferred ask that is the turn's first output
                # threads to the inbound operator message — consume the
                # one-shot target; a failed post restores it below.
                _ask_reply_to = _consume_turn_reply_to(driver, eng_id)
                _post_kwargs = dict(_kbd_kwargs)
                if _ask_reply_to is not None:
                    _post_kwargs["reply_to"] = _ask_reply_to
                try:
                    await BROKER.ensure_posted(
                        req,
                        lambda: telegram_channel.post_options_keyboard(
                            engagement_id=eng_id, request_id=request_id,
                            question=display, options=options, **_post_kwargs),
                        lambda mid: _ask_keyboard_finish(
                            telegram_channel, rec.topic_id, mid, display, options,
                            on_settle=_close_question,
                            settle_edit=_make_settle_edit(mid),
                            eng_id=eng_id, number=number),
                    )
                    mid = req.meta.get("message_id")
                    if not isinstance(mid, int):
                        # ensure_posted unregistered the request (post raised/None) →
                        # await_result below returns delivery_failed.
                        _restore_turn_reply_to(driver, eng_id, _ask_reply_to)
                        return None
                    # GENERATION RE-CHECK (§4, Sol r1-4 — reserve→post→re-check, now
                    # relay-mediated): an operator envelope that arrived between
                    # reserve and post supersedes this ask — settle it (broker cancel
                    # → finish hook renders the superseded copy + clears buttons),
                    # consuming no timeout budget.
                    superseded = (
                        driver is not None
                        and driver.inbound_generation(eng_id) != gen_at_entry
                    )
                    if superseded:
                        BROKER.cancel(
                            namespace="engagement_ask", scope=eng_id,
                            request_id=request_id, reason="superseded_by_text",
                        )
                    else:
                        if number is not None:
                            add = getattr(
                                engagement_registry, "add_open_question", None)
                            if add is not None:
                                try:
                                    await add(eng_id, number, mid, text=display)
                                except Exception:  # noqa: BLE001 — F6 strict-persist
                                    # The ledger write failed AFTER the keyboard
                                    # posted. Fail closed: settle the keyboard
                                    # (internal-error copy via the finish hook) and
                                    # refuse — a live-tappable keyboard the boot
                                    # reconciler can never see is worse than a
                                    # withdrawn question.
                                    logger.warning(
                                        "engagement %s: add_open_question failed — "
                                        "withdrawing ask Q%s", eng_id[:8], number,
                                        exc_info=True,
                                    )
                                    BROKER.cancel(
                                        namespace="engagement_ask", scope=eng_id,
                                        request_id=request_id, reason="internal_error",
                                    )
                                    return None
                        # W-R2: a successful, non-superseded ask post → ⏳ waiting for
                        # your reply, driven from the ask LIFECYCLE (not the turn
                        # result). Ordered BEFORE any settlement recompute by the
                        # ``_ask_registered`` pin (set in the finally below).
                        if driver is not None:
                            note = getattr(driver, "note_ask_waiting", None)
                            if note is not None:
                                await note(eng_id)
                    # W2/Sol B9 (Task 7): asking is an outbound agent action —
                    # advance only after the keyboard actually posted.
                    await _advance_first_contact()
                    return mid
                except asyncio.CancelledError:
                    # Terra/Sol r1 (#332): cancellation bypasses the normal
                    # result-based restore. Re-arm the one-shot target ONLY
                    # when no keyboard message id was recorded (a posted
                    # keyboard already consumed it correctly), then re-raise.
                    if not isinstance(req.meta.get("message_id"), int):
                        _restore_turn_reply_to(driver, eng_id, _ask_reply_to)
                    raise
                finally:
                    # Unblock the (possibly already-runnable) settlement path: the
                    # registration + waiting submission above are now durable.
                    _ask_registered.set()
                    # Sol A3 wave 5: parity with the anchor poster — the poster owns
                    # the ``ask_inflight`` clear on every exit. Durable ownership for a
                    # button ask is the BROKER.register-side clear that ran BEFORE this
                    # poster, so this CAS is a belt-and-suspenders no-op in the normal
                    # case (marker already cleared, or a later ask re-claimed it); it
                    # guarantees no poster-failure path can ever leave the marker set.
                    _clear_ask_marker(driver, eng_id, request_id)

            if intent_registered:
                # Install the real poster and ARM — the point of no return
                # (validation passed + broker registered). Only armed intents are
                # postable (§2(2)); the relay posts at the ask's tool_use block.
                driver.set_send_intent_poster(eng_id, request_id, _post_ask)
                driver.arm_send_intent(eng_id, request_id)
            else:
                # EAGER fallback (no live sequencer / degraded boot): post now.
                await _post_ask()

            # Shielded future (in await_result): a CancelledError here (transport
            # disconnect) propagates to OUR caller without cancelling the broker's
            # shared future -- the request stays live for a same-id reattach. The
            # future is decoupled from posting (resolved by the tap finish hook), so
            # nothing here needs the posted message id synchronously.
            outcome = await BROKER.await_result(req)
            return await _ask_final_response(
                outcome, options, driver, eng_id, request_id, req)
        finally:
            # Owner safety net: never strand a reattacher blocked on an
            # unresolved gate. Only the validation owner resolves it (a
            # reattacher exiting PENDING leaves resolution to the owner) and
            # drops its ownership marker.
            if _validation_owner:
                if gate.effective()[0] == "PENDING":
                    gate.set_failed(_internal_error_payload())
                _ASK_VALIDATION_OWNERS.discard(request_id)
            # Retention is hygiene-only (Task A4): the gate stays pinned while
            # this owner/reattacher holds it, then release + speculatively
            # retire (a still-referenced or not-yet-elapsed gate is never
            # dropped, so a late reattacher can still find the resolved gate).
            gate.release()
            if not _validation_owner and gate.effective()[0] == "PENDING":
                # A5 review, Finding 1 (second half): this call exited WITHOUT
                # ever becoming the validation owner AND without publishing
                # anything to the gate — it is still PENDING. ``retirable()``
                # would never fire for it: ``_resolved_at`` stays ``None``
                # forever, so the bound-based sweep can never catch it either.
                # But since no owner was ever registered in
                # ``_ASK_VALIDATION_OWNERS`` for this request_id, no reattacher
                # can possibly be blocked on this gate's event — it carries no
                # terminal outcome worth retaining. Drop it outright, once
                # unreferenced, instead of leaking it forever.
                # wb7-2: the pre-PASSED TERMINAL exits (``unknown_engagement`` /
                # ``engagement_terminal`` / ``TERMINAL_REGISTRATION``) no longer
                # reach here — they now ``set_failed`` their exact payload, so the
                # gate is RESOLVED and takes the normal resolved-gate retention
                # branch below. What remains for this delete is the genuinely
                # unpublishable case: a reattacher (never the owner) that found a
                # PENDING gate with no local owner to resolve it and fell through
                # the fail-closed path.
                if gate.refcount == 0 and ASK_GATES.get(request_id) is gate:
                    del ASK_GATES[request_id]
            else:
                maybe_retire_gate(request_id)

    return handler


async def _await_deferred_post(driver: Any, eng_id: str, request_id: str) -> dict | None:
    """F3 fail-closed: await a deferred ask/reply/anchor intent's resolution so
    the handler returns ``ok`` ONLY when the post actually landed. Degrades to
    ``None`` (old immediate-return behavior) when the driver predates the await
    seam — an ``ok:true`` response with a failed post is then still impossible on
    the live path (the real driver always exposes it)."""
    awaiter = getattr(driver, "await_send_intent", None)
    if awaiter is None:
        return None
    try:
        return await awaiter(eng_id, request_id)
    except Exception:  # noqa: BLE001 — never wedge the handler on the await seam
        logger.debug("await_send_intent failed (eng=%s)", eng_id[:8], exc_info=True)
        return None


async def _withdraw_anchor(
    telegram_channel: Any, topic_id: int | None, mid: int,
) -> None:
    """§A3(c) compensation: best-effort confirmed WITHDRAW-edit of an orphan
    anchor (posted, but its ledger write failed) via the RAW wire edit primitive
    — NEVER ``edit_discrete`` (the initial-anchor poster runs under the sequencer
    lock on the relay task; no reacquisition from poster context). R2c (v0.89.0):
    routes through the RICH edit primitive for parity with every other ask/anchor
    lifecycle edit (the withdrawn copy is plain, so render() ⇒ entities=None ⇒
    ``plain_text``, which is the copy unchanged — behaviour-preserving). An unconfirmed edit leaves one stale
    plain-text line — the documented visual-orphan class."""
    try:
        confirmed = await confirmed_settle_edit(
            lambda: telegram_channel.edit_topic_message_rich(
                topic_id, mid, _ANCHOR_WITHDRAWN, clear_keyboard=True),
        )
        if not confirmed:
            logger.warning(
                "anchor withdraw edit UNCONFIRMED (topic=%s mid=%s) — stale "
                "plain-text orphan left", topic_id, mid)
    except Exception:  # noqa: BLE001 — compensation edit is best-effort
        logger.debug("anchor withdraw edit raised", exc_info=True)


async def _noop_poster() -> int | None:
    """Placeholder poster used ONLY between an intent's early registration (for
    idempotent transport-retry reattach detection) and the point where the ask/
    reply handler installs the REAL relay-invoked poster via
    ``set_send_intent_poster`` — always before ARMING (§2(2), review C1). The
    relay never invokes this: a pending intent is never postable, and by arm
    time the real poster is in place."""
    return None


def _ask_outcome_response(outcome: dict, options: list) -> web.Response:
    """Map a broker ask outcome to the tool's JSON response."""
    o = outcome.get("outcome")
    if o == "answered":
        # A5 · F-MULTI: a multi submit resolves with ``option_indices`` → the
        # response lists ALL selected labels + indices, KEEPING the single
        # ``option``/``option_index`` fields (the FIRST selection) for
        # downstream compat.
        indices = outcome.get("option_indices")
        if indices:
            labels = [
                options[i] for i in indices
                if isinstance(i, int) and 0 <= i < len(options)
            ]
            return web.json_response({
                "ok": True, "outcome": "answered",
                "options": labels, "option_indices": list(indices),
                "option": labels[0] if labels else None,
                "option_index": indices[0],
            })
        idx = outcome["option_index"]
        return web.json_response({
            "ok": True, "outcome": "answered",
            "option": options[idx], "option_index": idx,
        })
    if o == "no_answer":
        return web.json_response({"ok": True, "outcome": "no_answer"})
    if o == "cancelled":
        if outcome.get("reason") == "superseded_by_text":
            return web.json_response({
                "ok": False, "error": "superseded", "message": _ASK_REFUSAL,
            })
        if outcome.get("reason") == "internal_error":
            return web.json_response({
                "ok": False, "error": "internal_error",
                "message": ("the question could not be recorded — it was "
                            "withdrawn; end your turn and re-ask"),
            })
        return web.json_response({"ok": False, "error": "cancelled"})
    # delivery_failed (keyboard post raised or returned None, r10-B3).
    return web.json_response({"ok": False, "error": "delivery_failed"})


def _make_ask_cancel(engagement_registry: Any) -> Handler:
    """POST /internal/channel/ask_cancel — explicit caller cancellation.

    v0.75.0 (W5): the `ask` MCP tool's ``finally`` calls this on genuine
    cancellation (NOT a transport retry) so a same-id reattach can never
    resurrect a stale keyboard tap. Always ``{"ok": True}`` -- cancelling an
    already-resolved or never-registered request is a harmless no-op
    (``BROKER.cancel`` returns False but we don't surface that distinction;
    the caller only wants "stop waiting for this", which is unconditionally
    true after the call returns).

    v0.84.0 (spec §D1 r4-1/r5-1, Task A5): the ask handshake means NO broker
    request exists until validation passes — so a cancel landing WHILE the
    owner is still in Q-allocation/validation would be a broker no-op and let
    the owner later post a question its caller already abandoned. Fix:
    GET-OR-CREATE the request's validation gate and SET ITS CANCELLATION LATCH
    (never the completion slot) — get-or-create because the cancel is a
    separate HTTP request that can arrive BEFORE the original ``/ask`` created
    anything. Then tombstone the send intent if one exists (a retry
    short-circuits to ``cancelled``), clear the ``ask_inflight`` ingress
    marker (wedge-free marker lifecycle), and finally the existing
    ``BROKER.cancel`` (resolves an already-registered broker request).
    """

    async def handler(request: web.Request) -> web.Response:
        from verdict_broker import BROKER

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_args"})
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "invalid_args"})

        eng_id = body.get("engagement_id")
        request_id = body.get("request_id")
        if not eng_id or not request_id:
            return web.json_response({"ok": False, "error": "invalid_args"})

        # #335: cancelling another engagement's live question kills a
        # legitimate operator interaction — authenticate the id claim first.
        auth_err = _engagement_auth_error(engagement_registry, eng_id, body)
        if auth_err is not None:
            return web.json_response({"ok": False, "error": auth_err})

        # A5 review, Finding 1: sweep prior asks' currently-retirable gates
        # here too — ``ask_cancel`` is the other entry point that calls
        # ``get_or_create_gate`` (see ``_sweep_retirable_gates``).
        _sweep_retirable_gates()

        # Latch cancellation FIRST (get-or-create so a cancel-before-ask still
        # creates the tombstone the later /ask will find) — this wakes any
        # reattacher blocked on the gate and pre-empts a not-yet-passed owner.
        gate = get_or_create_gate(request_id)
        gate.set_cancelled()

        driver = _resolve_active_driver()
        # Tombstone the send intent if one exists so a same-request_id retry
        # short-circuits to the recorded ``cancelled`` outcome (never awaits a
        # never-armed intent / re-registers a fresh broker request). wb1-1:
        # clear the ingress marker ONLY when the cancel is authoritative
        # (``cancelled`` or ``absent`` — no live post to strip it from under). On
        # ``post_won`` a relay poster passed its own gate and is mid-post: it OWNS
        # the marker and clears/settles it at durable ownership. Clearing here
        # would let a DIFFERENT ask pass the pending gate before Q1 is durable →
        # the serialized poster posts Q2 with no re-check → two live questions.
        if driver is not None:
            if (_record_intent_cancelled(driver, eng_id, request_id)
                    != INTENT_CANCEL_POST_WON):
                _clear_ask_marker(driver, eng_id, request_id)

        BROKER.cancel(
            namespace="engagement_ask", scope=eng_id, request_id=request_id,
            reason="caller_cancelled",
        )
        # Speculative hygiene retire (a still-referenced or not-yet-elapsed gate
        # is never dropped).
        maybe_retire_gate(request_id)
        return web.json_response({"ok": True})

    return handler


def _make_update_state(telegram_channel: Any, engagement_registry: Any) -> Handler:
    """POST /internal/channel/update_state — channel server → casa-main.

    Phase 2 (Task 23): the per-engagement channel server flips the topic
    title's state emoji (awaiting / active) via this handler when permission
    is requested / verdict received. Terminal-state transitions (completed /
    failed / cancelled) come from ``_finalize_engagement`` directly via the
    same ``update_topic_state`` helper on the channel — no internal POST
    needed in that path.

    Body shape: ``{engagement_id, new_state}``. Channel decides which states
    are meaningful (this handler just forwards). Failure returns
    ``update_failed`` so the caller can decide whether to retry.
    """

    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad_json"})
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "bad_json"})

        eng_id = body.get("engagement_id")
        new_state = body.get("new_state")
        if not eng_id or not new_state:
            return web.json_response({"ok": False, "error": "bad_params"})

        # #335: flipping another engagement's topic state is a (mild) spoof —
        # same authenticated-id rule as every other route here.
        auth_err = _engagement_auth_error(engagement_registry, eng_id, body)
        if auth_err is not None:
            return web.json_response({"ok": False, "error": auth_err})

        try:
            await telegram_channel.update_topic_state(
                engagement_id=eng_id, new_state=new_state,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "update_state failed (engagement=%s state=%s): %s",
                eng_id, new_state, exc,
            )
            return web.json_response({"ok": False, "error": "update_failed"})

        return web.json_response({"ok": True})

    return handler


def _make_channel_handlers(
    *, telegram_channel: Any, engagement_registry: Any,
    record_reply: Callable[[str, str], None] | None = None,
) -> dict[str, Handler]:
    """Return a path → handler dict for /internal/channel/* POSTs.

    Phase 1: ``send_to_topic``.
    Phase 2: ``post_inline_keyboard`` (Task 19), ``update_state`` (Task 23).
    v0.75.0 (W5): ``ask`` / ``ask_cancel`` (Task 3).
    #469: ``permission_verdict`` was REMOVED — permission verdicts have no
    internal writer. The operator's Telegram tap delivers in-process
    (``telegram._on_inline_callback`` → ``BROKER.claim``); an internal POST
    route here authenticated only the engagement's own token, which let an
    executor approve its own gated tool call.
    v0.75.0 (W1): ``record_reply`` hook threads reply() texts to the
    claude_code driver's live topic-stream relay de-dup.
    Phase 2+ will extend with ``set_progress``, ``typing``, etc. — see spec §A.3.
    """
    return {
        "/internal/channel/send_to_topic": _make_send_to_topic(
            telegram_channel=telegram_channel,
            engagement_registry=engagement_registry,
            record_reply=record_reply,
        ),
        "/internal/channel/post_inline_keyboard": _make_post_inline_keyboard(
            telegram_channel=telegram_channel,
            engagement_registry=engagement_registry,
        ),
        "/internal/channel/update_state": _make_update_state(
            telegram_channel=telegram_channel,
            engagement_registry=engagement_registry,
        ),
        "/internal/channel/ask": _make_ask(
            telegram_channel=telegram_channel,
            engagement_registry=engagement_registry,
        ),
        "/internal/channel/ask_cancel": _make_ask_cancel(
            engagement_registry=engagement_registry,
        ),
    }


def _make_channel_get_handlers(
    *, engagement_registry: Any,
) -> dict[str, Handler]:
    """Return a path → handler dict for /internal/channel/* GETs.

    v0.75.0 (W5/Sol B3,B4): the ``permission_pending`` long-poll (Task 21)
    was removed — verdicts now flow through ``verdict_broker.BROKER``
    directly via the in-process Telegram callback, no queue/poll needed. Kept
    as an (empty, for now) factory so ``casa_core``'s generic
    ``router.add_get`` loop over this dict needs no changes when a real GET
    handler is added here in the future.
    """
    # engagement_registry isn't strictly needed by the GET handler family
    # today, but keeping the symmetric (engagement_registry=) signature lets
    # a future GET (e.g. /internal/channel/status?engagement_id=) reuse it
    # without adding another factory.
    del engagement_registry
    return {}
