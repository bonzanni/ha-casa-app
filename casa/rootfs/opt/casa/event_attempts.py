"""Plugin-event delivery-record bookkeeping — pure schema, envelope, and
schedule logic.

Leaf module (stdlib only, zero local imports). The plugin-events facility's
per-delivery ledger is the sibling of the callback facility's attempt
ledger (``callback_attempts.py``, read whole before touching this file —
its idioms are mirrored here): casa's durable record of every minted event
delivery AND the worker's read surface for the nudge sweep. This module is
the calculation half of that ledger: it builds, validates, terminalizes,
and schedules delivery records. It performs no I/O — the spool (a later
module) owns every read/write (marker discipline, atomic replace, the
spool lock); everything here is a pure function that returns fresh copies
and never mutates its inputs.

Three concerns, matching ``callback_attempts.py``'s structure:

- **Record schema**: the exact 15-key record (``new_record``), and a total
  fail-closed validator (``validate_record``) — a malformed file is never
  trusted; the caller re-derives it from live artifacts.
- **Mint envelope**: bounded fail-closed parsing of the pending-delivery
  marker payload (``parse_envelope``) — EXACTLY ``{"v": 1}`` is accepted;
  any other shape (extra key, wrong version, non-dict, oversized) degrades
  to ``None``. Unlike ``callback_attempts.parse_envelope``, there is no v2
  / ``meta`` channel at all — an event delivery carries no consumer-authored
  payload through this ledger, so there is nothing to echo and nothing to
  drop for forward compat.

- **Nudge schedule**: ONE set of offsets (0 s, 5 m, 30 m, 2 h, 6 h, 24 h),
  all anchored on the record's ``minted_ts`` — unlike
  ``callback_attempts``'s two-phase (result-then-outcome) schedule, an
  event delivery has no separate "result published" moment: minting IS the
  dispatch, so a single anchor covers the whole ladder. A total budget of
  ``MAX_NUDGES`` bus-ACCEPTED dispatches; an escalating capped deferral for
  a bus-REJECTED dispatch that spends no budget at all.
"""
from __future__ import annotations

import json
import math
import re

SCHEMA_VERSION = 1
ENVELOPE_MAX_BYTES = 4096
MAX_NUDGES = 6
#: The whole nudge ladder, anchored on ``minted_ts`` alone: 0 s, 5 m, 30 m,
#: 2 h, 6 h, 24 h. One tuple, unlike ``callback_attempts``' two-phase
#: (``RESULT_PHASE_OFFSETS`` / ``OUTCOME_PHASE_OFFSETS``) split — an event
#: record has no second anchor to switch to at terminalization.
PHASE_OFFSETS = (0.0, 300.0, 1800.0, 7200.0, 21600.0, 86400.0)
#: How long the LAST accepted wake has to ack before the record is
#: terminalized ``exhausted`` (#1643): the sixth wake is a real chance to
#: handle the event, so its turn must be able to run and ack first.
FINAL_GRACE_S = 3600.0
DEFERRAL_BASE_S = 60.0
DEFERRAL_CAP_S = 1800.0
#: Deferral count past which the schedule SATURATES without exponentiating.
#: ``deferrals`` is read off a file a subscriber's own environment can
#: influence indirectly (a bus that always rejects), and ``2 ** deferrals``
#: for a large-but-valid integer is a memory/CPU bomb (and overflows the
#: float multiply long before that). The cap is reached at 5 deferrals with
#: the constants above, so saturating here is exact, not an approximation.
#: Mirrors ``callback_attempts.DEFERRAL_MAX_SHIFT`` exactly.
DEFERRAL_MAX_SHIFT = 32
#: Absolute bound, in epoch seconds, on every clock field of a VALID
#: record. A validated record is ARITHMETIC-SAFE by contract: the ladder
#: adds an offset to ``minted_ts``, the worker compares ``next_nudge_ts``
#: against a clock. An unbounded integer defeats that while type-checking
#: perfectly — mirrors ``callback_attempts.TS_ABS_MAX`` exactly, including
#: the rationale (an accepted dispatch whose budget update never lands is a
#: worker that can dispatch indefinitely).
TS_ABS_MAX = 1e15
STATUSES = ("pending", "done")
OUTCOMES = frozenset({"acked", "revoked", "removed", "exhausted"})

_RECORD_KEYS = frozenset({
    "v", "emitter", "event", "subscriber", "gen", "ack_token", "minted_ts",
    "status", "outcome", "nudges", "last_nudge_ts", "next_nudge_ts",
    "deferrals", "noted", "ended_ts",
})

#: The event-name grammar, RE-STATED here because this is a LEAF module
#: (this file has no local imports — the layer that builds the spool's
#: ``delivery/<event>--<subscriber>.json`` filenames and
#: ``/data/events/<emitter>/`` directories, a later task, imports THIS
#: module, never the reverse). Canonical copy: ``plugin_events._NAME_RE``
#: plus its subscribe-side injectivity rails (no ``--``, no leading ``-``,
#: no trailing ``-``, no ``plg-`` prefix) — mirrored, not imported, exactly like
#: ``callback_attempts._HASH_RE`` restates the spool's state-hash grammar.
#: A validated record's ``event`` is a path COMPONENT, not free text: a
#: charset-only check would let ``event="../../../etc"`` validate today and
#: reach path construction once the spool exists.
_EVENT_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

#: The registry plugin-name grammar, likewise re-stated (not imported) —
#: canonical copy: ``plugin_registry.NAME_RE`` (plain) and
#: ``plugin_registry.OWNED_NAME_RE`` (scoped ``slug.manifest_name``, for a
#: bundled/specialist plugin's events). Both ``emitter`` and ``subscriber``
#: are plugin identities that become a spool directory/filename component,
#: so they take the same registry-name shape a subscribe entry's emitter
#: reference does in ``plugin_events._valid_emitter_ref`` — including the
#: scoped dot form (``finance.bank-feed`` must validate).
_PLUGIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_OWNED_PLUGIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}\.[a-z0-9][a-z0-9-]{0,39}$")


def _valid_event(value) -> bool:
    """The event-name grammar, plus the injectivity rails a declared/
    subscribed event name is already held to in ``plugin_events.py``: no
    ``--`` (reserved effective-name separator), no leading ``-``
    (ambiguous plugin-name separator), no trailing ``-`` (ambiguous
    event/token separator — ``<event>--<u32hex>.json`` misparses, Critical-3),
    no ``plg-`` prefix (reserved)."""
    return (isinstance(value, str) and bool(_EVENT_RE.match(value))
            and "--" not in value and not value.startswith("-")
            and not value.endswith("-")
            and not value.startswith("plg-"))


def _valid_plugin_ref(value) -> bool:
    """The registry-name grammar for a plugin identity — accepts both the
    plain and scoped (owned-plugin) forms. Used for both ``emitter`` and
    ``subscriber``: either can be a scoped bundled/specialist plugin."""
    return (isinstance(value, str)
            and bool(_PLUGIN_NAME_RE.match(value)
                     or _OWNED_PLUGIN_NAME_RE.match(value)))


def _is_ts(value, *, allow_none: bool = True) -> bool:
    """A usable timestamp field: (optionally) ``None``, or a FINITE,
    arithmetic-safe number that is not a bool.

    Identical to ``callback_attempts._is_ts`` in every branch — a ``bool``
    is an ``int`` subclass and never a clock; a plain ``int`` needs no
    ``math.isfinite`` (which would ``OverflowError`` on ``10**1000``
    converting to a C double); a ``float`` goes through ``math.isfinite``
    to reject ``NaN``/``±inf``; both are then bounded by :data:`TS_ABS_MAX`,
    a comparison that is exact for an ``int`` of any size. The one
    difference from the sibling: *allow_none* lets a caller demand a REAL
    timestamp — ``minted_ts`` here has no "legacy record" escape hatch, so
    :func:`validate_record` calls this with ``allow_none=False`` for it.
    """
    if value is None:
        return allow_none
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -TS_ABS_MAX <= value <= TS_ABS_MAX
    if isinstance(value, float):
        return math.isfinite(value) and -TS_ABS_MAX <= value <= TS_ABS_MAX
    return False


def _reject_constant(name: str):
    """``json``'s hook for its NON-STANDARD ``NaN`` / ``Infinity`` /
    ``-Infinity`` literals. JSON has no such values; Python's decoder
    accepts them by default. Raising here makes the envelope parse fail
    closed instead of admitting a value that can never round-trip through
    the canonical writer. Mirrors ``callback_attempts._reject_constant``."""
    raise ValueError(f"non-finite JSON constant {name!r}")


def parse_envelope(data: bytes) -> dict | None:
    """Bounded fail-closed envelope parse: ``None`` on ANY defect.

    Unlike ``callback_attempts.parse_envelope`` (v1/v2, an optional
    ``meta`` channel, unknown keys silently dropped for forward compat),
    this envelope has exactly one legal shape: ``{"v": 1}`` — no other key,
    known or not, is tolerated. An event delivery's mint marker carries no
    consumer-authored payload through this ledger, so there is nothing to
    echo and nothing worth a forward-compat allowance; a scribbled or
    future-versioned envelope is simply invalid.

    Defects: over ``ENVELOPE_MAX_BYTES``, non-UTF-8, non-JSON, non-object
    JSON, a non-finite JSON constant anywhere in the body (``NaN`` /
    ``Infinity`` / ``-Infinity``), any key set other than exactly ``{"v"}``,
    or ``v`` not equal to :data:`SCHEMA_VERSION`.
    """
    try:
        if len(data) > ENVELOPE_MAX_BYTES:
            return None
        obj = json.loads(data.decode("utf-8"),
                         parse_constant=_reject_constant)
        if not isinstance(obj, dict) or set(obj) != {"v"}:
            return None
        v = obj["v"]
        if isinstance(v, bool) or v != SCHEMA_VERSION:
            return None
        return {"v": SCHEMA_VERSION}
    except Exception:
        return None


def new_record(emitter: str, event: str, subscriber: str, gen: int,
               ack_token: str, now: float) -> dict:
    """A fresh delivery record with all schema fields.

    ``minted_ts`` is ``now`` — the instant this delivery is minted IS the
    anchor for the whole nudge ladder (unlike ``callback_attempts``, there
    is no separate "result published" moment to anchor a second phase on).
    ``next_nudge_ts`` starts at ``now + PHASE_OFFSETS[0]`` (== ``now``): the
    first dispatch is due immediately. ``gen`` and ``ack_token`` are opaque
    values the caller supplies (a plugin generation counter and a delivery
    ack token respectively) — this function stores them verbatim, and
    every transition below (``terminalize``, ``next_nudge_after_accept``,
    ``next_nudge_after_reject``) preserves them unchanged, never reading or
    interpreting either.
    """
    return {
        "v": SCHEMA_VERSION,
        "emitter": emitter,
        "event": event,
        "subscriber": subscriber,
        "gen": gen,
        "ack_token": ack_token,
        "minted_ts": now,
        "status": "pending",
        "outcome": None,
        "nudges": 0,
        "last_nudge_ts": None,
        "next_nudge_ts": now + PHASE_OFFSETS[0],
        "deferrals": 0,
        "noted": False,
        "ended_ts": None,
    }


def validate_record(obj, *, expect_emitter: str | None = None,
                     expect_event: str | None = None,
                     expect_subscriber: str | None = None) -> dict | None:
    """Total fail-closed validation: a copy of ``obj``, or ``None``.

    ``None`` unless ``obj`` is a dict with exactly the schema keys and
    every field type-checks: registry-name grammar for ``emitter`` and
    ``subscriber`` (:func:`_valid_plugin_ref`, plain or scoped
    ``slug.name`` form), event grammar for ``event`` (:func:`_valid_event`),
    a non-empty string for ``ack_token`` (opaque, not a path component — no
    grammar to restate), a non-negative int for ``gen`` (a bool-typed int
    rejected, like every other counter here), real bools for ``noted``,
    finite arithmetic-safe timestamps (:func:`_is_ts`) with ``minted_ts``
    REQUIRED (never ``None`` — an event record is always minted from a live
    clock, unlike a callback attempt's legacy allowance), status/outcome in
    their vocabularies, non-negative int counters. Never raises: a
    malformed (possibly scribbled) file must read as INVALID so the caller
    re-derives it from artifacts, never as an exception.

    **``emitter``/``event``/``subscriber`` are validated against a LOCAL
    grammar, not merely "any non-empty string".** These three become spool
    path components once the spool exists (``delivery/<event>--
    <subscriber>.json`` under an ``/data/events/<emitter>/`` directory) —
    a charset-only check would let a scribbled record with
    ``subscriber="../../../etc"`` validate today and reach path
    construction later. Mirrors ``callback_attempts``' own precedent
    (``_HASH_RE``, re-stated rather than imported because this is a leaf)
    one field further: three identity fields instead of one.

    Status and outcome must agree, in both directions: ``outcome`` is
    ``None`` for exactly ``"pending"`` and set (to one of :data:`OUTCOMES`)
    for exactly ``"done"``. Otherwise ``{status: pending, outcome: acked}``
    (an open record wearing a terminal outcome) and ``{status: done,
    outcome: null}`` (a terminal record with no outcome to act on) would
    both read as truth — mirrors ``callback_attempts.validate_attempt``'s
    consistency gate exactly.

    **Identity binding**, mirroring ``validate_attempt``'s ``expect_hash``:
    when the caller supplies ``expect_emitter``/``expect_event``/
    ``expect_subscriber``, a mismatch against the corresponding field is
    INVALID. Every authoritative read of a record comes from a slot whose
    NAME encodes its identity, and a grammar check alone would let a file
    read under slot A carry field values naming slot B — a record in the
    wrong slot is not a record. Bound here so no reader can forget to
    check; an unbound read (all three ``expect_*`` left ``None``) only
    applies the grammar gate, exactly like ``validate_attempt`` without
    ``expect_hash``.
    """
    try:
        if not isinstance(obj, dict) or set(obj) != _RECORD_KEYS:
            return None
        if isinstance(obj["v"], bool) or obj["v"] != SCHEMA_VERSION:
            return None
        if not _valid_plugin_ref(obj["emitter"]):
            return None
        if expect_emitter is not None and obj["emitter"] != expect_emitter:
            return None
        if not _valid_event(obj["event"]):
            return None
        if expect_event is not None and obj["event"] != expect_event:
            return None
        if not _valid_plugin_ref(obj["subscriber"]):
            return None
        if expect_subscriber is not None and obj["subscriber"] != expect_subscriber:
            return None
        if not isinstance(obj["ack_token"], str) or obj["ack_token"] == "":
            return None
        gen = obj["gen"]
        if isinstance(gen, bool) or not isinstance(gen, int) or gen < 0:
            return None
        if obj["status"] not in STATUSES:
            return None
        if obj["outcome"] is not None and obj["outcome"] not in OUTCOMES:
            return None
        if (obj["outcome"] is not None) != (obj["status"] == "done"):
            return None
        if not isinstance(obj["noted"], bool):
            return None
        for key in ("nudges", "deferrals"):
            n = obj[key]
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                return None
        if not _is_ts(obj["minted_ts"], allow_none=False):
            return None
        for key in ("last_nudge_ts", "next_nudge_ts", "ended_ts"):
            if not _is_ts(obj[key]):
                return None
        return dict(obj)
    except Exception:
        return None


def terminalize(rec: dict, outcome: str, *, now: float) -> dict:
    """A copy of ``rec`` made terminal: ``done``/``outcome``, ``ended_ts=now``,
    ``next_nudge_ts=None``.

    **Refuses outright when ``rec`` is already terminal**: if
    ``rec["status"] == "done"``, this returns ``rec`` UNCHANGED — not a
    fresh copy with a new outcome/clock stamped in, and not merely
    idempotent-when-the-outcome-matches (``callback_attempts.terminalize``'s
    weaker rule, which still allows a same-outcome re-stamp to fall through
    the general path and still allows a *different* outcome to overwrite a
    terminal record). Terminal immutability is enforced HERE, at the schema
    layer: once a delivery is acked/revoked/removed/exhausted, no later
    call — retried, racing, or malicious — can move its outcome or its
    ``ended_ts``. A single set of offsets has no second phase to switch
    into on terminalization the way ``callback_attempts``' outcome-phase
    does, so there is nothing left to schedule either way: ``next_nudge_ts``
    goes straight to ``None``.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    if rec.get("status") == "done":
        return dict(rec)
    out = dict(rec)
    out["status"] = "done"
    out["outcome"] = outcome
    out["ended_ts"] = now
    out["next_nudge_ts"] = None
    return out


def next_nudge_after_accept(rec: dict, *, now: float) -> float | None:
    """The next ``next_nudge_ts`` after a bus-ACCEPTED dispatch, or
    ``None`` when the record is already terminal or the accept spends the
    last unit of budget.

    The accept consumes one budget unit (the caller writes
    ``nudges + 1``); the k-th accepted dispatch (``nudges`` incremented)
    schedules the next at ``minted_ts + PHASE_OFFSETS[nudges]`` when
    ``nudges < MAX_NUDGES``, floored at ``now`` so a slot that fell behind
    (e.g. after a rejected-dispatch deferral pushed the wall clock past the
    ladder's natural cadence) is never scheduled in the past. Past the
    budget — ``nudges >= MAX_NUDGES`` — ``None``: the record has spent all
    six chances and waits for the caller to terminalize it as
    ``"exhausted"``.
    """
    if rec.get("status") == "done":
        return None
    new_nudges = rec["nudges"] + 1
    if new_nudges >= MAX_NUDGES:
        return None
    return max(now, rec["minted_ts"] + PHASE_OFFSETS[new_nudges])


def next_nudge_after_reject(rec: dict, *, now: float) -> float:
    """The deferred ``next_nudge_ts`` after a bus-REJECTED dispatch.

    ``now + min(DEFERRAL_CAP_S, DEFERRAL_BASE_S * 2**deferrals)`` — the
    escalating capped deferral (the caller writes ``deferrals + 1``), so an
    unavailable bus yields a bounded cadence, never a floored-timeout spin.
    Spends NO nudge budget — a rejection is the bus refusing the dispatch
    attempt itself, not the subscriber declining the delivery, so
    ``nudges`` is untouched by this function and must stay untouched by
    the caller too. A malformed ``deferrals`` reads as 0. Identical to
    ``callback_attempts.next_nudge_after_reject`` in every particular,
    including saturating BEFORE exponentiating: ``deferrals`` is read off
    a file whose value the caller doesn't fully control, and the
    validator's "non-negative int" is satisfied by values for which
    ``2 ** deferrals`` would itself be a memory/CPU bomb (or overflow the
    float multiply) long before the ``min`` would have discarded it.
    """
    deferrals = rec.get("deferrals")
    if isinstance(deferrals, bool) or not isinstance(deferrals, int) or deferrals < 0:
        deferrals = 0
    if deferrals >= DEFERRAL_MAX_SHIFT:
        return now + DEFERRAL_CAP_S
    return now + min(DEFERRAL_CAP_S, DEFERRAL_BASE_S * 2 ** deferrals)
