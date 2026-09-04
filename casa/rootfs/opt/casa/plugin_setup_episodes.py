"""Durable per-artifact setup obligations (v0.112.0, v0.161.0 / #451).

A plugin that declares ``casa.setupTool`` gets its setup tool run
AUTOMATICALLY by CASA — never by an agent acting on a hand-back. Because
plugin MCP tools surface only on the plugin's target agents, Casa dispatches
a synthetic Casa-authored turn to the execution agent rather than calling the
tool itself.

v0.161.0 (#451) — ONE runner, released by a POSITIVE verdict
-----------------------------------------------------------
Until v0.160.0 two runners could execute a plugin's setup tool: this facility,
and an agent acting on the configurator's ``run_plugin_setup_tool`` hand-back.
Which one acted was classified at MUTATION time, and two attempts to make that
classification total failed adversarial review — because at mutation time there
is no third answer. A runner must be named *now*, and every hole both attempts
found was a case whose correct answer was "not yet".

So the hand-back is gone and this facility is the only runner. The model:

* **Obligation per (plugin, artifact_id)** — created LEVEL-TRIGGERED by the
  reconciler sweep for every resolved plugin declaring ``casa.setupTool``
  (:func:`ensure_obligation`). The generation key is the ``artifact_id``, so a
  new artifact is a new obligation; the three artifact-publishing sites
  (``plugin_add``, ``plugin_update``, bundled specialist plugins) need no hook
  of their own, and none can be missed.
* **Release requires a POSITIVE consent verdict for the exact artifact.** The
  reconciler seals one round per artifact whose membership is the union of the
  pending trigger and callback consent identities — possibly EMPTY, which is a
  real statement that this artifact needs no consent. An obligation with no
  sealed verdict HOLDS: absence of a round is never a permission (that was
  attempt 1's defect — it would dispatch before the reconcile opened the
  round). Holding is the third answer mutation-time routing could not express;
  it is visible in health and re-checked on every kick.
* **A denial refuses the obligation** (``status="refused"``) and never
  dispatches — the operator declined the endpoint. The way back is mechanical:
  while a consent for that artifact is pending again the sweep RE-ARMS the row
  (:func:`ensure_obligation`), so the operator's later Approve runs setup, as
  the denial note promises. That is also how a re-consent which re-mints a
  secret gets setup re-run on an unchanged artifact. ``refused`` is therefore a
  record of the last settlement rather than a permanent barrier — Casa's
  consent layer does not persist a denial either (an unacked consent stays
  pending and is re-prompted), and the barrier that matters is that nothing
  dispatches without a release.
* **The setup tool is resolved at DISPATCH time**, live from the current
  manifest — which is why an update that changes ``casa.setupTool`` while
  leaving ``casa.callbacks`` byte-identical still runs the NEW tool (attempt
  2's missed run) without binding the setup contract into a consent identity.

Design (Sol+Terra design round + implementation rounds 1-3, 2026-07-24):

* **Round ledger, not ack counting**: every prompted consent registers an
  OPEN member with a fresh per-prompt NONCE (:func:`open_round`); terminal
  decisions mark members. Settlement = ALL members decided.
* **All-approved gate** (impl r3): the obligation is released only when every
  member is APPROVED. Any denial settles the round WITHOUT a release and
  tells the operator — the argument-free setup tool cannot distinguish
  approved from denied triggers, so a mixed round must not wire blindly.
* **Crash-safe approval recording** (impl r3): approvals are recorded
  SYNCHRONOUSLY inside the consent ack commit callback
  (:func:`record_approval_sync` — same yield-free step that persists the
  ack), and a BOOT RECOVERY SWEEP (:func:`_recover_and_settle`, first act of the
  worker) marks any still-open member whose identity has a persisted ack
  as approved with that ack's generation — a crash anywhere between ack
  persistence and settlement recovers on restart.
* **Prompt nonces** (impl r3): denials/expiries apply only when their
  nonce matches the member's CURRENT nonce — a late expiry callback from a
  superseded keyboard (re-prompt of the same identity) cannot decide the
  fresh prompt. Approvals are exempt: the persisted ack is ground truth.
* **Stale-artifact fencing** (impl r2): the decision path never replaces
  an existing round with a different artifact_id — only the prompt path
  (which runs solely from a live reconcile) starts a new-artifact round.
* **Re-consent re-arms the obligation** (v0.161.0): a pending consent for an
  artifact whose obligation is terminal bumps its ``gen`` and returns it to
  ``pending``/``awaiting_verdict``. The decision is driven by the reconciler's
  positive pending set, NOT by whether a prompt happened to mint a fresh nonce
  — that coupling lost setup after a denial plus a restart (a promptless pass
  opened the member, so the prompted pass deduped onto it and minted nothing)
  and could also re-arm with no keyboard able to settle the round. This
  replaces the v0.112.0 scheme of
  minting a new episode keyed by ``identity#gen`` plus consumed-key
  tombstones: creation is now driven solely by the LIVE registry sweep, so
  there is no replay path a tombstone would need to fence, and the row's
  identity is simply ``(plugin, artifact_id)``.
* **Exact-artifact binding (TOCTOU)**: the worker re-resolves the registry
  at dispatch time and marks the episode ``stale`` when the plugin was
  removed or superseded. (The residual dispatch→agent-turn window is an
  ACCEPTED, disclosed risk: seconds wide, and the tool is idempotent
  wiring whose current-artifact consent round re-runs setup regardless.)
* **Unambiguous tool binding**: exactly one server grant or the episode
  fails with a clear reason; verify blocks ambiguous plugins upstream.
* **Worker survivability**: per-episode isolation + self re-kick.
* **Terminal-state hygiene**: supersession prunes a plugin's older
  episodes; ``failed``/``stale`` decay out of health after 72h.
* **Delivery semantics (disclosed)**: ``dispatched`` means the turn was
  accepted by the in-process bus and the target agent will report the
  actual outcome to the operator — the durable retry contract covers
  consent-to-dispatch, not tool execution. User-facing claims are worded
  accordingly.
* **No plugin prose**: fixed Casa-authored template; only grammar-
  validated identifiers interpolated. The ``synthetic`` marker is a
  RESERVED provenance key external ingress cannot spoof.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, NamedTuple

import plugin_dispatch

logger = logging.getLogger(__name__)

STORE_PATH = Path("/data/plugin-setup-episodes.json")
_SCHEMA_VERSION = 4
_MAX_DISPATCH_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 5.0)
# impl r9 (Terra): a dispatch-time resolution-unavailable deferral schedules
# its own delayed re-kick at this interval, so recovery never depends on a
# coalesced reconcile kick that already fired. Bounded overall by
# _MAX_RESOLVE_DEFERRALS (the obligation goes stale after that many attempts).
_RETRY_INTERVAL_S = 5.0
_HEALTH_DECAY_S = 72 * 3600.0
# impl r7 (Sol): a released obligation whose registry resolution is UNAVAILABLE
# at DISPATCH time is retained and retried on later kicks; after this many
# failed attempts the plugin is evidently gone (uninstalled) and the obligation
# goes stale so it can't retry forever. v0.161.0: settlement no longer resolves
# the registry at all, so this bounds only the dispatch path.
_MAX_RESOLVE_DEFERRALS = 10
# #521: bounded re-dispatch budget for a RESIDENT-execution episode whose
# dispatched turn produced no positive evidence of the setup tool (not run,
# not listed by the session init). Each failure returns the row to `pending`
# (gate stays released) and the next reload/reconcile kick re-dispatches;
# past the bound the obligation fails with an operator note rather than
# looping through operator-visible synthetic turns forever.
_MAX_EXECUTION_RETRIES = 3
# The only member states a round may carry. Anything else is unreadable, and an
# unreadable state must never be counted as a DECISION — settlement requires a
# positive "approved", it does not infer one from "neither open nor denied".
_MEMBER_STATES = ("open", "approved", "denied")
# The one `_compose` failure that a registry mutation can REPAIR, so it must not
# be terminal (see _run_episode). Matched as a substring of the reason
# `plugin_dispatch.compose` returns.
_MUTABLE_COMPOSE_FAILURE = "no resident or specialist target"

# Wired by casa_core at boot. All optional — absent seams degrade to logging.
_dispatch: Callable[[str, str, dict], Awaitable[bool]] | None = None
_notify_operator: Callable[[str], Awaitable[None]] | None = None
_resolve_registry_entry: Callable[[str], Any] | None = None
_ack_lookup: Callable[[str], str | None] | None = None
_routes_live: Callable[[str], bool] | None = None
# #803: ``() -> (published, generation) | None`` over the APPLIED routing
# overlays (None = no runtime registry bound). See ``_applied_routing_state``.
_applied_routing: "Callable[[], tuple[bool, int] | None] | None" = None
_secrets_ready: Callable[[str], bool] | None = None
_execution_ready: Callable[[str, str, str], bool] | None = None
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

_lock: asyncio.Lock | None = None
_worker_task: asyncio.Task | None = None
_retry_task: asyncio.Task | None = None
_kick: asyncio.Event | None = None


def _now() -> float:
    return time.time()


def configure(*, dispatch, notify_operator, resolve_registry_entry,
              ack_lookup=None, routes_live=None, applied_routing=None,
              secrets_ready=None, execution_ready=None,
              sleep=asyncio.sleep) -> None:
    """casa_core boot wiring. Idempotent. ``ack_lookup(identity)`` returns
    the persisted ack's approval generation (or None) — the boot recovery
    sweep's ground truth. ``routes_live(plugin)`` reports whether the
    plugin's triggers are ROUTED (impl r4, Sol): the worker holds a pending
    episode until the route overlay is live, so the external service is
    never pointed at an unrouted endpoint; reconciles call :func:`kick` to
    retry the gate. ``secrets_ready(plugin)`` (#423) reports whether every
    env var the plugin's ``.mcp.json`` references is resolved in the
    effective environment: the worker holds a pending episode until then,
    so the setup tool never runs against an MCP server spawned with literal
    ``${VAR}`` placeholders; plugin_env reloads call :func:`kick`.
    ``execution_ready(role, plugin, artifact_id)`` (#423 r2) reports whether
    the EXECUTING agent's next session build will carry the episode's exact
    artifact — an agent whose published binding was built while the plugin
    was env-withheld keeps excluding it until an agent reload, and a
    dispatch into that session would consume the episode against a session
    without the tool; agent reloads call :func:`kick`. ``applied_routing()``
    (#803) reports the APPLIED routing overlays as ``(published, generation)``
    or ``None`` when no runtime registry is bound: the worker reads it before
    the route recomputation and again, yield-free, before the send, and
    DEFERS (its own timer) on a standing unavailable marker, on any
    publication that landed in between, or on a read that raised."""
    global _dispatch, _notify_operator, _resolve_registry_entry
    global _ack_lookup, _routes_live, _applied_routing, _secrets_ready
    global _execution_ready, _sleep, _lock, _kick
    _dispatch = dispatch
    _notify_operator = notify_operator
    _resolve_registry_entry = resolve_registry_entry
    _ack_lookup = ack_lookup
    _routes_live = routes_live
    _applied_routing = applied_routing
    _secrets_ready = secrets_ready
    _execution_ready = execution_ready
    _sleep = sleep
    if _lock is None:
        _lock = asyncio.Lock()
    if _kick is None:
        _kick = asyncio.Event()


# ---------------------------------------------------------------------------
# Store: {"schema_version", "rounds": {plugin: {"artifact_id", "members":
#   {identity: {"state", "gen", "nonce"}}}}, "episodes": [...]}
#
# An "episode" row is the durable per-artifact SETUP OBLIGATION:
#   {"id", "plugin", "artifact_id", "gen": int,
#    "status": "pending"|"dispatched"|"failed"|"stale"|"refused",
#    "gate": "awaiting_verdict"|"released",
#    "attempts", "resolve_deferrals", "approved_identities",
#    "created_ts", "updated_ts", "last_error"}
# There is at most ONE row per plugin — its CURRENT artifact's obligation.
# ---------------------------------------------------------------------------

#: The damage classes a read can report, and the only ones a persisted
#: ``reset`` record may carry (#747). ``unreadable``: the file exists but the
#: bytes could not be read (a permission, a directory, an I/O error).
#: ``malformed``: the bytes were read but are not a valid store (parse, shape
#: or schema). ``incomplete``: the store was valid but at least one episode
#: row was not a readable episode — not a mapping, or a mapping without the
#: fields that make it one (:func:`_readable_episode`) — and was dropped; the
#: readable rows are kept.
_DAMAGE_CLASSES = ("unreadable", "malformed", "incomplete")


class StoreRead(NamedTuple):
    """One point-in-time read of the store (#747, INV-PLUG-015).

    ``data`` is exactly what :func:`_load` returns — the canonical store, reset
    to empty on ``unreadable``/``malformed`` damage, normalized on
    ``incomplete``. ``damage`` is ``None`` for a readable store and for an
    ABSENT one (absence is ordinary — a box that never ran a plugin setup — and
    must not start disclosing damage), one of :data:`_DAMAGE_CLASSES` while
    the damage stands, or ``"reset"`` when the store is readable and carries a
    well-formed, unexpired ``reset`` record. ``reset`` is that record.

    Every writer does ``_load()`` then ``_save()``, so the first writer after
    damage REPLACES the damaged file with a valid empty one — before any
    reporting surface may have observed it. The fact of that replacement
    therefore travels IN ``data``: on damage the returned store carries a
    ``reset`` record, and the writer's own save persists it as a side effect
    of its own write. The record is honoured for :data:`_HEALTH_DECAY_S` — the
    window a failed setup stays in health — and pruned by the next writer after
    that. No process-global "last load failed" flag: it would race unrelated
    reads by the worker and the status tool.
    """
    data: dict
    damage: str | None
    reset: dict | None


class EpisodesRead(NamedTuple):
    """:func:`read_episodes`'s result: the rows plus the read's damage."""
    rows: list
    damage: str | None
    reset: dict | None


def _empty(reset: dict | None = None) -> dict:
    data: dict = {"schema_version": _SCHEMA_VERSION, "rounds": {},
                  "episodes": []}
    if reset is not None:
        data["reset"] = reset
    return data


def _reset_record(damage: str) -> dict:
    return {"damage": damage, "ts": _now()}


def _finite(value: Any) -> float | None:
    """A store value as a FINITE float, or None — total: never raises. Every
    number read out of the store passes through here. Three review rounds each
    found one more conversion that could raise on a hand-edited value (a
    `float()` on a non-number, then an int too large for a float, then
    `math.isfinite()` on that same int), so there is now one conversion, and it
    accepts only a real int/float (never a bool) that becomes a finite float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        out = float(value)
    except Exception:  # noqa: BLE001 — OverflowError on a huge int, anything
        return None
    return out if math.isfinite(out) else None


def _iso_utc(ts: Any) -> str | None:
    """A renderable UTC timestamp, or None — never raises."""
    try:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _valid_reset(record: Any) -> dict | None:
    """The persisted ``reset`` record, normalized, when it is well-formed AND
    still inside the decay window; None otherwise (the caller drops it, and
    the next writer prunes it). Well-formed means exactly one of the generated
    damage classes and a non-boolean, finite, renderable timestamp — a record a
    hand edit or a partial write left in any other shape is ignored rather than
    rendered, because a renderer that raises inside the health merge would
    erase every setup row from the report."""
    if not isinstance(record, dict):
        return None
    damage = record.get("damage")
    if damage not in _DAMAGE_CLASSES:
        return None
    ts = _finite(record.get("ts"))
    if ts is None or _iso_utc(ts) is None:
        return None
    if ts < _now() - _HEALTH_DECAY_S:
        return None
    return {"damage": damage, "ts": ts}


def _read_store() -> StoreRead:
    """The one protected read every reader shares (#747). Never raises."""
    try:
        raw = STORE_PATH.read_bytes()
    except FileNotFoundError:
        return StoreRead(_empty(), None, None)
    except OSError:
        logger.exception("plugin-setup-episodes store unreadable — resetting")
        rec = _reset_record("unreadable")
        return StoreRead(_empty(rec), "unreadable", rec)
    try:
        # Decoding is INSIDE the malformed guard: UnicodeDecodeError is a
        # ValueError, not an OSError, so bytes that are not UTF-8 are a
        # malformed store, never a raise out of a reader that must not raise.
        data = json.loads(raw.decode("utf-8"))
        if (not isinstance(data, dict)
                or not isinstance(data.get("episodes"), list)):
            raise ValueError("malformed store")
        # Fail-closed on any other stored version (there is no migration
        # machinery pre-1.0): reset to empty and let the reconciler re-seal
        # rounds and re-derive obligations from live registry state.
        if int(data.get("schema_version") or 0) != _SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {data.get('schema_version')!r}")
        data.setdefault("rounds", {})
        before = len(data["episodes"])
        _normalize(data)
        dropped = before - len(data["episodes"])
    except Exception:  # noqa: BLE001 — a corrupt store must not brick boot
        logger.exception("plugin-setup-episodes store unreadable — resetting")
        rec = _reset_record("malformed")
        return StoreRead(_empty(rec), "malformed", rec)
    if dropped:
        # Row-level loss: the readable rows stay (exactly as _normalize left
        # them) and the loss is recorded. Round-ledger repairs are NOT damage:
        # a dropped round establishes nothing and the reconciler re-seals it
        # from live state — repair, not history loss — so they stay logged
        # rather than reported.
        rec = _reset_record("incomplete")
        data["reset"] = rec
        return StoreRead(data, "incomplete", rec)
    rec = _valid_reset(data.get("reset"))
    if rec is None:
        data.pop("reset", None)
        return StoreRead(data, None, None)
    data["reset"] = rec
    return StoreRead(data, "reset", rec)


def _load() -> dict:
    """Compatibility wrapper for the eleven store writers/readers: never
    raises, always the canonical store (plus the optional ``reset`` record,
    which every caller ignores and every writer carries forward)."""
    return _read_store().data


#: The statuses an obligation row may carry (the store comment above). A row
#: whose status is anything else has no readable STATE: every state-reading
#: consumer misclassifies it — `ensure_obligation` reads it as terminal and
#: settled (so setup is silently never owed again), `health_issues` and the
#: worker as nothing.
_EPISODE_STATUSES = ("pending", "dispatched", "failed", "stale", "refused")


def _readable_episode(row: Any) -> bool:
    """Whether a stored row is an episode the code can identify, classify and
    write back to (candidate review, sol S2). Three fields are essential, and
    only three — derived from what the consumers require, not from the row
    template:

    * ``plugin``, a non-empty string — the row's identity: `_row_for`,
      `retire_for_removed`, the supersession in `ensure_obligation`, the
      health merge's registered-name filter on `health_issues()`'s `plugin`,
      and ``ep["plugin"]`` in `_run_episode`. Without it a row is never
      matched, superseded or retired, is filtered out of the standing report,
      renders as "a plugin", and raises in the worker.
    * ``status``, one of :data:`_EPISODE_STATUSES` — the row's state: the
      ``pending`` filter, `health_issues`, `ensure_obligation`,
      `_settle_locked`, `report_dispatch_outcome`.
    * ``id``, a non-empty string — the handle every write-back is keyed by:
      `_update_episode`, ``ep["id"]`` throughout `_run_episode`,
      `report_dispatch_outcome`. A released row without one raises on every
      kick, and `_worker_pass`'s isolation arm calls
      ``_update_episode(ep.get("id") or "")``, which matches nothing — it is
      never repaired and never completes.

    A mapping missing any of them is not an episode: keeping it presented
    damage as history (``{}`` rendered as "a plugin: setup is unknown" with no
    disclosure) and the next writer persisted it. Every other field is
    tolerated or repaired — a malformed ``updated_ts`` reads as oldest
    (`_stamp`), a missing ``artifact_id`` is superseded by the next sweep, a
    missing ``gate`` holds — because dropping a valid pending obligation over
    a non-essential field would lose setup, which the gate review ruled
    against. Dropping one of the three loses nothing: `ensure_obligation`
    re-creates the obligation with a handle. Total: never raises."""
    return (isinstance(row, dict)
            and isinstance(row.get("plugin"), str) and bool(row["plugin"])
            and isinstance(row.get("id"), str) and bool(row["id"])
            and row.get("status") in _EPISODE_STATUSES)


def _normalize(data: dict) -> None:
    """Repair a structurally corrupt store in memory (the next :func:`_save`
    persists it). Corruption defense, not version migration — idempotent and
    version-independent."""
    # Normalise the WHOLE round structure, every level, on every load.
    #
    # Three consecutive review rounds found this one level deeper each time —
    # malformed episode rows, then the `rounds` container, then an individual
    # round's `members`. Every instance had the same two failure modes: an
    # attribute access raising inside a swallowing `except` (so nothing repaired
    # the shape, it persisted across loads, and EVERY plugin's settlement sweep
    # aborted), or a missing key defaulting permissively (a round with no
    # `verdict` read as AUTHORITATIVE and released an obligation the reconciler
    # never sealed). So this validates the shape structurally rather than
    # guarding each access site.
    #
    # A round that cannot be read is DROPPED, not repaired: it establishes
    # nothing, and the reconciler seals a fresh one on the next pass. Dropping is
    # also the fail-closed direction — a dropped round means "no verdict yet".
    rounds = data.get("rounds")
    if not isinstance(rounds, dict):
        if rounds is not None:
            logger.warning("setup-round container was %s, not a mapping — "
                           "resetting it", type(rounds).__name__)
        data["rounds"] = {}
    else:
        clean: dict = {}
        for plugin, rnd in rounds.items():
            if (not isinstance(plugin, str) or not isinstance(rnd, dict)
                    or not isinstance(rnd.get("artifact_id"), str)
                    or not isinstance(rnd.get("members"), dict)):
                logger.warning("dropping unreadable setup round (plugin=%r)",
                               plugin)
                continue
            # A member that cannot be read makes the WHOLE round unreadable.
            # Dropping just the member (the first attempt) was worse than
            # useless: it PRESERVED the round's authority while deleting its
            # membership, turning a members-bearing round into an authoritative
            # ZERO-member one — which is the positive assertion "this artifact
            # needs no consent", and released setup with nothing approved.
            # Normalisation must not manufacture a verdict; drop the round and
            # let the reconciler seal a fresh one from live state.
            if any(not isinstance(i, str) or not isinstance(m, dict)
                   for i, m in rnd["members"].items()):
                logger.warning("dropping setup round with an unreadable member "
                               "(plugin=%s)", plugin)
                continue
            members = {}
            for i, m in rnd["members"].items():
                if m.get("state") not in _MEMBER_STATES:
                    # An unreadable state becomes OPEN, never a decision. Open is
                    # the self-healing direction: it blocks settlement (so no
                    # release is inferred from it), and the reconciler either
                    # re-seals the member with a fresh nonce if the consent is
                    # still pending, or prunes it if it is not.
                    logger.warning("member state %r unreadable — treating as "
                                   "open (plugin=%s)", m.get("state"), plugin)
                    m["state"] = "open"
                members[i] = m
            rnd["members"] = members
            # A round whose authority cannot be read is NOT authoritative.
            # `is True` rather than `bool(...)`: coercion made every truthy value
            # authoritative, so a persisted `"verdict": "false"` released setup.
            # Only the exact boolean the sealer writes counts.
            rnd["verdict"] = rnd.get("verdict") is True
            clean[plugin] = rnd
        data["rounds"] = clean
    # DROP structurally corrupt rows, on every load and at every version. The
    # store-level guard only checks that `episodes` is a list, so one non-dict
    # element (a partial write, a hand-edit) used to survive and then raise on
    # the first `e.get(...)` in `_row_for` / `episodes()` / `health_issues()` —
    # stranding EVERY plugin's setup and breaking health regeneration, with the
    # "a corrupt store must not brick boot" recovery never reached because the
    # store parsed fine.
    # A MAPPING that is not an episode (candidate review, sol S2) is the same
    # class one level down: `{}` raised nowhere, so it was kept, read as an
    # undamaged store, rendered by the status tool as "a plugin: setup is
    # unknown" with no disclosure, and written back by the next writer. What
    # makes a row an episode is decided in ONE place, `_readable_episode`; the
    # count of dropped rows — mapping or not — is what `_read_store`
    # classifies as `incomplete`.
    rows = data.get("episodes")
    if isinstance(rows, list):
        kept = [r for r in rows if _readable_episode(r)]
        if len(kept) != len(rows):
            logger.warning("dropped %d malformed setup-obligation row(s)",
                           len(rows) - len(kept))
        data["episodes"] = kept


def _save(data: dict) -> None:
    # GHSA-569r-7crq-xr43: was a hand-rolled temp-file replace, which landed the
    # file at the umask default (0644) and so let every engagement uid read the
    # plugin setup transcripts. Routed through atomic_io both to get the private
    # mode and to gain the fsync/rename durability this path never had.
    from atomic_io import PRIVATE, atomic_write_json
    atomic_write_json(STORE_PATH, data, indent=1, mode=PRIVATE)


def read_episodes(status: str | None = None) -> EpisodesRead:
    """The rows plus the read's availability (#747). One read; the rows and
    the damage describe the same bytes."""
    read = _read_store()
    rows = [e for e in read.data["episodes"]
            if status is None or e.get("status") == status]
    return EpisodesRead(rows, read.damage, read.reset)


def episodes(status: str | None = None) -> list[dict]:
    return read_episodes(status).rows


def damage_sentence(read: "EpisodesRead | StoreRead", *,
                    precise: bool = True) -> str | None:
    """One operator-facing sentence for a read's damage, or None when there is
    none. The ONE renderer both reporting surfaces use. ``precise=False`` is
    the standing health row's form: identical for live damage and for a
    persisted reset, so the row a regeneration writes does not depend on
    whether the kicked worker has persisted the record yet. ``precise=True``
    is the status tool's form, read live at call time, and alone carries the
    class and the timestamp. Never raises."""
    if read.damage is None:
        return None
    if not precise:
        return ("the plugin setup history could not be read; entries recorded "
                "before the damage are lost")
    if read.damage == "reset":
        rec = read.reset or {}
        cls = rec.get("damage") or "earlier"
        when = _iso_utc(rec.get("ts"))
        at = f" at {when}" if when else ""
        return (f"the plugin setup history was reset after {cls} damage{at}, "
                "so entries recorded before then are lost")
    return f"the plugin setup history could not be read ({read.damage})"


def _stamp(value: Any) -> float:
    """``updated_ts`` as a number, tolerating anything a hand-edited row holds
    (the status tool's `_status_sort_key` rule). A malformed stamp reads as the
    OLDEST time rather than raising: a raise here used to be swallowed by the
    health merge, which then published no setup row at all — hiding a valid
    pending obligation sitting beside the bad row — and, since this function
    is the standing surface's damage disclosure, no global row either. Reading
    the stamp as oldest decays a terminal row and keeps a pending one, which is
    the direction that loses nothing an operator needs."""
    stamp = _finite(value) if value else 0.0
    return 0.0 if stamp is None else stamp


def health_issues() -> list[dict]:
    """Non-terminal-success obligations for plugin-health regeneration.
    ``failed``/``stale``/``refused`` rows decay after
    :data:`_HEALTH_DECAY_S`; ``pending`` never decays (actionable until
    dispatched — including an obligation still HOLDING for a consent verdict,
    which is precisely the state an operator needs to see when no DM was
    reachable to prompt them)."""
    out = []
    cutoff = _now() - _HEALTH_DECAY_S
    read = read_episodes()
    for e in read.rows:
        st = e.get("status")
        if st == "pending" or (
                st in ("failed", "stale", "refused")
                and _stamp(e.get("updated_ts")) >= cutoff):
            out.append({
                "kind": f"setup_episode_{st}",
                "plugin": e.get("plugin"),
                # #653 r1: which ARTIFACT's obligation this was. A terminal
                # `failed` row is never superseded and `retire_for_removed`
                # leaves it alone, so it outlives both the artifact and the
                # installation; the health merge needs this to tell a current
                # failure from a previous artifact's. Consumed for FILTERING
                # only — it is deliberately not carried onto the emitted issue,
                # because artifact_id is part of the health fingerprint and
                # adding it there would re-announce every already-notified
                # setup row exactly once.
                "artifact_id": e.get("artifact_id"),
                "episode": e.get("id"),
                "detail": e.get("last_error") or "",
            })
    if read.damage is not None:
        # #747 / INV-PLUG-015: one registry-GLOBAL row (``plugin="*"``, the
        # established spelling) so the standing report says the history is
        # unavailable rather than silently erasing every setup row. Its
        # fingerprint is the same for live damage and for a persisted reset,
        # so the live->reset transition re-announces nothing.
        out.append({
            "kind": "setup_history_unavailable",
            "plugin": "*",
            "artifact_id": None,
            "episode": None,
            "detail": damage_sentence(read, precise=False),
        })
    return out


# ---------------------------------------------------------------------------
# Obligations
# ---------------------------------------------------------------------------

def _row_for(data: dict, plugin: str,
             artifact_id: str | None = None) -> dict | None:
    """The plugin's obligation row, optionally required to be for a specific
    artifact. At most one row exists per plugin."""
    for e in data["episodes"]:
        if e.get("plugin") != plugin:
            continue
        if artifact_id is not None and e.get("artifact_id") != artifact_id:
            continue
        return e
    return None


def _new_row(plugin: str, artifact_id: str, gen: int) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "plugin": plugin,
        "artifact_id": artifact_id,
        "gen": gen,
        "status": "pending",
        "gate": "awaiting_verdict",
        "attempts": 0,
        "resolve_deferrals": 0,
        "approved_identities": [],
        "created_ts": _now(),
        "updated_ts": _now(),
    }


def ensure_obligation(*, plugin: str, artifact_id: str,
                      consent_pending: bool = False) -> bool:
    """Ensure a durable setup obligation exists for this EXACT artifact, and
    report whether one is now awaiting a consent verdict.

    Called by the reconciler sweep for every resolved plugin declaring
    ``casa.setupTool`` — level-triggered, so all three artifact-publishing
    sites are covered without a hook of their own. A row for a DIFFERENT
    artifact is superseded (the new artifact's own verdict owns it now).

    ``consent_pending`` says whether the reconciler currently sees an
    UNACKED consent for this exact artifact. A terminal row plus a pending
    consent means setup is owed again — the operator is about to be prompted
    afresh, and an approval must be able to run setup — so the row is RE-ARMED
    to ``pending`` with the next generation. That is what makes a
    revoked-then-reapproved trigger re-run setup on an unchanged artifact,
    whose secret is re-minted and would otherwise leave the external service
    holding a stale credential; and it is what lets an approval after a denial
    do what the operator note promises.

    Re-arming is decided HERE, from the reconciler's positive pending set —
    never from whether :func:`open_round` happened to mint a fresh nonce. That
    earlier coupling failed in both directions: a promptless pass would open a
    member and then a prompted pass would dedupe onto it, minting nothing and
    never re-arming (setup lost after a denial + restart); while a peer
    reconciler could re-arm and open a member with no keyboard to settle it.

    Returns True iff there is now a ``pending`` obligation for
    ``artifact_id``, i.e. the caller should seal a verdict for it. A terminal
    row with no pending consent returns False, so a settled artifact stops
    generating verdict churn on every reconcile.

    SYNCHRONOUS + yield-free. Never raises."""
    try:
        data = _load()
        row = _row_for(data, plugin)
        if row is not None and row.get("artifact_id") == artifact_id:
            stale_release = (row.get("status") == "pending"
                             and row.get("gate") == "released")
            if row.get("status") == "pending" and not stale_release:
                # Already owed and unconcluded — nothing to reset.
                return True
            # A `stale` row means the DISPATCH path exhausted its bounded retries
            # without resolving the plugin, and concluded it was gone. This call
            # refutes that: it comes from the reconciler sweep, which only asks
            # about a plugin it just RESOLVED and which still declares
            # `casa.setupTool`. Re-arm regardless of whether a consent is pending
            # — a transient registry outage during the dispatch window would
            # otherwise strand an already-released obligation for good, because
            # by then every consent is acked and no pending signal remains.
            if row.get("status") == "stale":
                consent_pending = True
            if not consent_pending:
                return True if stale_release else False
            # Either terminal, or RELEASED under a verdict that a newly pending
            # consent has made stale. The released case matters during the
            # dispatch window: the row is still `pending` while `_run_episode`
            # awaits the bus, so a revoke landing there used to hit the
            # early-return above and never re-arm — the settled re-approval then
            # found a `dispatched` row and declined, leaving the RE-MINTED secret
            # unprovisioned while the accepted turn had provisioned the old one.
            # Re-arming mints a new id, so the in-flight dispatch's own
            # `_update_episode` becomes a no-op against the superseded row.
            gen = int(row.get("gen") or 0) + 1
            fresh = _new_row(plugin, artifact_id, gen)
            fresh["created_ts"] = row.get("created_ts") or fresh["created_ts"]
            data["episodes"] = [e for e in data["episodes"]
                                if e.get("plugin") != plugin]
            data["episodes"].append(fresh)
            _save(data)
            logger.info("setup obligation re-armed (plugin=%s gen=%d): a "
                        "consent for this artifact is pending again", plugin,
                        gen)
            return True
        # No row, or a row for a superseded artifact.
        data["episodes"] = [e for e in data["episodes"]
                            if isinstance(e, dict)
                            and e.get("plugin") != plugin]
        data["episodes"].append(_new_row(plugin, artifact_id, 0))
        _save(data)
        return True
    except Exception:  # noqa: BLE001 — the reconcile path must never see a raise
        logger.exception("setup obligation ensure failed (plugin=%s)", plugin)
        return False


# ---------------------------------------------------------------------------
# Round ledger
# ---------------------------------------------------------------------------

def open_round(*, plugin: str, artifact_id: str, identities: list[str],
               verdict: bool = True) -> dict[str, str]:
    """SEAL a consent round's membership BEFORE any keyboard posts (impl
    r4): the reconciler declares the COMPLETE per-plugin batch it is about
    to prompt in ONE yield-free call, so a fast Approve on the first
    keyboard can never settle a round that is still registering its other
    members. Returns ``{identity: nonce}`` — the caller threads each nonce
    into that keyboard's decision callbacks (stale-expiry fencing).

    ``identities`` is the COMPLETE current membership for this plugin — the
    caller (``trigger_reconcile.seal_setup_state``) seals nothing unless it
    computed both consent kinds successfully. So merging into an existing
    same-artifact round keeps every DECIDED member (they are this round's
    settlement so far) but PRUNES a still-``open`` member that is no longer
    pending. Without that prune an obsolete member blocks settlement forever:
    unassign a trigger target and reassign to another without changing the
    artifact, and the old target's member stays open (role invalidation does
    not cancel a TriggerConsentKey), so approving the NEW target can never
    settle the round, and the old keyboard's eventual expiry refuses the
    obligation instead — with the new consent acked, nothing re-arms it.

    A different artifact starts a fresh round. SYNCHRONOUS + yield-free —
    cannot interleave with the locked async sections. Never raises (returns {}
    on failure: unfenced but never incorrect).

    ``verdict`` is what makes this round AUTHORITATIVE for setup, and it is the
    MOST RECENT seal that governs (see the assignment below for why sticky was
    wrong). A round serves
    two jobs — it fences the consent keyboards with per-prompt nonces, and it is
    the verdict that releases a setup obligation — and those jobs do not always
    permit the same answer. When the caller cannot establish the plugin's full
    consent position (a non-consent gap hides part of it, the pass spanned
    registry generations, a pending compute failed), the keyboards still need
    their nonces but NO setup conclusion may be drawn. Such a round is sealed
    with ``verdict=False``: settlement consumes it and leaves the obligation
    exactly as it was, holding. One flag, set from one conjunction at the seal
    site, replaces what were three separate call-site conditions — each of which
    was found wrong in a different combination.

    v0.161.0 (#451): ``identities`` may be EMPTY, which seals a POSITIVE
    verdict that this artifact needs no consent — the reconciler's way of
    saying "released" for an ungated plugin, or for one whose consent ack
    survived an update. That is deliberately distinct from the absence of a
    round, which means "no verdict yet" and holds.

    This function does NOT re-arm a terminal obligation — that decision lives in
    :func:`ensure_obligation`, driven by whether a consent is actually pending
    for the artifact. Deriving it here from "did I mint a fresh nonce" was wrong
    in both directions: a promptless pass opened the member, so the later
    prompted pass deduped onto it and minted nothing."""
    try:
        data = _load()
        rnd = data["rounds"].get(plugin)
        if not isinstance(rnd, dict) or rnd.get("artifact_id") != artifact_id:
            rnd = {"artifact_id": artifact_id, "members": {}}
            data["rounds"][plugin] = rnd
        # LAST WRITE WINS, deliberately. The flag answers "could the most recent
        # pass establish this plugin's full consent position?", and settlement
        # reads it at the moment it concludes — so the conclusion always rests on
        # the freshest knowledge. ANDing it across seals instead (the first
        # attempt) STRANDED the obligation: a round downgraded while a gap was
        # open keeps its members, so it does not settle-and-consume until every
        # member is decided, and by then no later pass can restore its authority.
        # A member's approval is an ACK — ground truth about that consent — and
        # stays valid when the unrelated gap that suppressed the verdict clears;
        # what must never happen is CONCLUDING while the position is unknown, and
        # that is exactly what the per-pass predicate prevents.
        rnd["verdict"] = bool(verdict)
        nonces: dict[str, str] = {}
        for identity in identities:
            existing = rnd["members"].get(identity)
            # impl r5 (Terra): a member that is ALREADY open keeps its nonce.
            # A reconcile re-firing a prompt while its keyboard is still live
            # DEDUPES onto that keyboard (coordinator.register_challenge
            # returns created=False, retaining the ORIGINAL finish callback
            # and its nonce) — minting a fresh nonce here would desync the
            # ledger from that retained callback, so the keyboard's eventual
            # deny/expiry would be rejected as stale and the member would
            # never decide. A NEW member, or one being RE-OPENED after a
            # terminal decision (its old keyboard is gone, a fresh keyboard
            # with our new callback+nonce posts), gets a fresh nonce.
            if isinstance(existing, dict) and existing.get("state") == "open" \
                    and existing.get("nonce"):
                nonces[identity] = existing["nonce"]
                continue
            nonce = uuid.uuid4().hex[:8]
            rnd["members"][identity] = {"state": "open", "nonce": nonce}
            nonces[identity] = nonce
        listed = set(identities)
        obsolete = [i for i, m in rnd["members"].items()
                    if i not in listed
                    and isinstance(m, dict) and m.get("state") == "open"]
        for i in obsolete:
            del rnd["members"][i]
        if obsolete:
            logger.info("pruned %d obsolete open member(s) from the setup "
                        "round (plugin=%s): no longer pending", len(obsolete),
                        plugin)
        _save(data)
        return nonces
    except Exception:  # noqa: BLE001 — prompt path must never see a raise
        logger.exception("setup-round open failed (plugin=%s)", plugin)
        return {}


def kick() -> None:
    """Wake the worker (episode created elsewhere, or a reconcile just ran
    and may have made a pending episode's routes live)."""
    if _kick is not None:
        _kick.set()


def open_member_nonce(plugin: str, identity: str) -> str:
    """READ-ONLY: the current OPEN round member's nonce for this consent, or
    ``""`` (no round, round for another artifact's identity set, member
    absent, or member already terminal).

    #494: the on-demand re-prompt path (`consent_reprompt` → each
    reconciler's ``reprompt_pending``) re-posts a keyboard for a consent the
    LAST lifecycle reconcile already sealed. Threading the SEALED member's
    own nonce into that keyboard keeps the stale-decision fence intact — the
    fresh keyboard decides exactly the member the reconciler opened. A
    ``""`` return degrades exactly to today's superseded-keyboard semantics:
    the keyboard can still Approve (approvals are ack-backed ground truth),
    but its deny/expiry decides no member. Mutates nothing; never raises."""
    try:
        data = _load()
        rnd = data["rounds"].get(plugin)
        if not isinstance(rnd, dict):
            return ""
        member = rnd.get("members", {}).get(identity)
        if isinstance(member, dict) and member.get("state") == "open":
            return str(member.get("nonce") or "")
        return ""
    except Exception:  # noqa: BLE001 — a read helper must never raise
        logger.exception("open-member nonce read failed (plugin=%s)", plugin)
        return ""


def _rearm_refused_locked(data: dict, plugin: str, artifact_id: str) -> bool:
    """#494 (design r3/r4): an approval landing AFTER the round that refused
    this artifact's obligation was consumed (expired keyboard → member denied
    → ``status="refused"``; then an on-demand re-prompt's keyboard approves)
    must re-arm the obligation — the refusal note promised "Approving the
    consent will run it", and ``ensure_obligation`` cannot re-arm on its own
    once the ack exists (``consent_pending`` is False from then on).

    Narrow by design: ``refused`` rows only (``failed``/``stale`` keep their
    existing recovery paths), for THIS exact artifact, and only while the
    plugin still RESOLVES in the registry — an approval racing a
    ``plugin_remove`` must not mint a ``pending`` row nothing can ever seal
    or release (a ``pending`` row never decays out of health). An
    unresolvable registry (``resolved_ok=False``) also declines: declining is
    the fail-safe direction, since the next lifecycle reconcile re-arms via
    ``ensure_obligation`` whenever a consent is genuinely pending again.

    Release still requires a fresh AUTHORITATIVE union seal — the approve
    path's reconcile_cb runs one immediately.

    Mutates ``data`` (caller saves). Returns True when it re-armed."""
    row = _row_for(data, plugin, artifact_id)
    if row is None or row.get("status") != "refused":
        return False
    resolved_ok, entry = _resolve_entry(plugin)
    if not resolved_ok or not isinstance(entry, dict):
        logger.info("approve-time re-arm declined (plugin=%s): plugin does "
                    "not resolve", plugin)
        return False
    gen = int(row.get("gen") or 0) + 1
    fresh = _new_row(plugin, artifact_id, gen)
    fresh["created_ts"] = row.get("created_ts") or fresh["created_ts"]
    data["episodes"] = [e for e in data["episodes"]
                        if e.get("plugin") != plugin]
    data["episodes"].append(fresh)
    logger.info("setup obligation re-armed on approval (plugin=%s gen=%d): "
                "a refused round's consent was approved", plugin, gen)
    return True


def retire_for_removed(plugin: str) -> None:
    """Durable setup-obligation teardown for a REMOVED plugin: mark its
    obligation row ``stale`` (which decays out of health, unlike ``pending``)
    and drop its round. Closes the approve-racing-removal window (#494 design
    r4, Terra): a re-armed ``pending`` row for a plugin the registry no
    longer resolves can never be sealed or released, so nothing may leave one
    behind. Best-effort; never raises."""
    try:
        data = _load()
        changed = False
        for e in data["episodes"]:
            if (e.get("plugin") == plugin
                    and e.get("status") in ("pending", "dispatched")):
                e.update({"status": "stale", "updated_ts": _now(),
                          "last_error": "plugin removed"})
                changed = True
        if plugin in data["rounds"]:
            del data["rounds"][plugin]
            changed = True
        if changed:
            _save(data)
            logger.info("setup obligation retired (plugin=%s): plugin "
                        "removed", plugin)
    except Exception:  # noqa: BLE001 — removal teardown must never raise
        logger.exception("setup obligation retire failed (plugin=%s)", plugin)


def rearm_refused_sync(*, plugin: str, artifact_id: str) -> bool:
    """#494 (Sol diff-gate r1): the approve-commit's re-arm, as its own
    yield-free step run BEFORE the ack is persisted. The re-arm and the ack
    live in different files, so their order is the crash contract:

    * crash after re-arm, before the ack — the consent is still pending, so
      the next lifecycle reconcile re-prompts, and the sweep keeps the
      re-armed row `pending` (``consent_pending=True``). Self-healing.
    * crash after the ack (re-arm already durable) — boot's recovery sweep
      approves the member from the persisted ack (or, with the round already
      consumed, the next reconcile seals a fresh authoritative round with the
      consent no longer pending) and the re-armed obligation releases.

    Re-arming AFTER the ack had a window with no exit: ack durable, obligation
    still ``refused``, and ``ensure_obligation`` unable to re-arm because no
    consent is pending anymore.

    Returns True when nothing was required or the required re-arm is durable;
    False ONLY when a REQUIRED re-arm failed to persist (Sol diff-gate r2) —
    the caller must then ABORT the commit before the ack write: proceeding
    would recreate exactly the no-exit window above (ack durable, obligation
    refused, no pending consent left to re-arm through). Never raises."""
    try:
        data = _load()
        if _rearm_refused_locked(data, plugin, artifact_id):
            _save(data)
        return True
    except Exception:  # noqa: BLE001 — commit callback must never see a raise
        logger.exception("pre-ack re-arm failed (plugin=%s)", plugin)
        return False


def record_approval_sync(*, plugin: str, artifact_id: str, identity: str,
                         gen: str) -> None:
    """Record an approval DURABLY in the same yield-free commit step that
    persists the consent ack (impl r3: a crash after the ack but before the
    async finish hook must not strand the round — this write happens
    first; the boot sweep covers a crash even earlier). Approvals are
    ack-backed ground truth, so no nonce check applies. Never raises."""
    try:
        data = _load()
        rnd = data["rounds"].get(plugin)
        if isinstance(rnd, dict) and rnd.get("artifact_id") != artifact_id:
            logger.info("stale approval record ignored (plugin=%s)", plugin)
            return
        if not isinstance(rnd, dict):
            # Synthesized, so NOT authoritative — see on_consent_decision.
            rnd = {"artifact_id": artifact_id, "members": {},
                   "verdict": False}
            data["rounds"][plugin] = rnd
        member = rnd["members"].get(identity) or {}
        member.update({"state": "approved", "gen": gen})
        rnd["members"][identity] = member
        # #494: this same yield-free step re-arms a refused obligation, so a
        # crash between the ack write and the async feed cannot strand the
        # "approved but refused-forever" state.
        _rearm_refused_locked(data, plugin, artifact_id)
        _save(data)
    except Exception:  # noqa: BLE001 — commit callback must never see a raise
        logger.exception("sync approval record failed (plugin=%s)", plugin)


async def on_consent_decision(*, plugin: str, artifact_id: str,
                              identity: str, approved: bool,
                              approval_gen: str = "",
                              nonce: str = "") -> None:
    """Feed ONE terminal consent decision. Approvals re-apply idempotently
    (the sync record already ran) and then settle; denials/expiries apply
    ONLY when ``nonce`` matches the member's current nonce (a superseded
    keyboard's late callback is ignored). Settlement runs under ONE lock
    acquisition; notes/kick happen after release. Never raises."""
    if _lock is None:
        return
    notes: list[str] = []
    created = False
    try:
        async with _lock:
            data = _load()
            rnd = data["rounds"].get(plugin)
            if isinstance(rnd, dict) and rnd.get("artifact_id") != artifact_id:
                logger.info(
                    "stale consent decision ignored (plugin=%s artifact=%s, "
                    "current round is %s)", plugin, artifact_id,
                    rnd.get("artifact_id"))
                return
            existing_round = isinstance(rnd, dict)
            if not existing_round:
                # Unknown round (store reset, or a round already consumed by an
                # earlier settlement) — synthesize so a live decision is never
                # dropped, but NEVER as authoritative. The reconciler never
                # sealed this round, so nothing established the plugin's consent
                # position for it; concluding from it was the flag's escape
                # hatch, and it let a delayed finish hook RESURRECT a
                # non-authoritative round as an authoritative release.
                rnd = {"artifact_id": artifact_id, "members": {},
                       "verdict": False}
                data["rounds"][plugin] = rnd
            member = rnd["members"].get(identity)
            if not approved and existing_round and member is None:
                # NOT this round's business. The reconciler's sealed membership
                # is authoritative about what the round is waiting on, and it
                # does not list this identity — so a deny/expiry arriving for it
                # comes from a keyboard the reconciler has already stopped
                # counting (its member was pruned as no longer pending, e.g. the
                # trigger's target lost its channel while the keyboard was
                # live). Recording it would add a member the round never had,
                # settle on a denial nobody made about a consent no longer
                # pending, and refuse the obligation with a spurious "you
                # declined" note. The nonce fence cannot catch this: it requires
                # a member to compare against.
                logger.info(
                    "deny/expiry for a non-member ignored (plugin=%s): the "
                    "sealed round does not list this consent", plugin)
                return
            if approved:
                m = member or {}
                m["state"] = "approved"
                # impl r4 (Terra): never overwrite a durably-recorded
                # generation with a blank from a feed whose acks.get failed.
                if approval_gen or not m.get("gen"):
                    m["gen"] = approval_gen
                rnd["members"][identity] = m
                # #494: idempotent mirror of record_approval_sync's re-arm,
                # for a feed whose sync commit step failed (the row is
                # already `pending` after a successful sync step, so this
                # no-ops there).
                _rearm_refused_locked(data, plugin, artifact_id)
            else:
                # Nonce fence (impl r3): a deny/expiry from a SUPERSEDED
                # keyboard (mismatching nonce) must not decide the current
                # prompt. Fencing needs both sides to carry a nonce — a
                # blank on either side degrades to unfenced acceptance.
                cur_nonce = (member or {}).get("nonce") or ""
                if (member is not None and member.get("state") == "open"
                        and nonce and cur_nonce and nonce != cur_nonce):
                    logger.info(
                        "stale deny/expiry ignored (plugin=%s identity "
                        "nonce mismatch)", plugin)
                    _save(data)
                    return
                m = member or {}
                if m.get("state") != "approved":  # an ack outranks an expiry
                    m["state"] = "denied"
                rnd["members"][identity] = m
            created, notes = _settle_locked(data, plugin)
            _save(data)
    except Exception:  # noqa: BLE001 — consent flow must never see a raise
        logger.exception("setup-episode decision handling failed (plugin=%s)",
                         plugin)
        return
    for n in notes:
        await _note(n)
    if created and _kick is not None:
        _kick.set()


def _resolve_entry(plugin: str) -> tuple[bool, dict | None]:
    """Three-state registry resolution (impl r7/r8): ``(resolved_ok,
    entry)``. ``resolved_ok=False`` means the registry could not be consulted
    (resolver absent, raised, or returned a non-dict / None) — the caller
    RETAINS state and retries; it must NEVER treat this as a confirmed
    removal. ``(True, {...})`` = a live resolved entry."""
    if _resolve_registry_entry is None:
        return False, None
    try:
        entry = _resolve_registry_entry(plugin)
    except Exception:  # noqa: BLE001
        logger.exception("registry resolve failed (plugin=%s)", plugin)
        return False, None
    if not isinstance(entry, dict):
        return False, None
    return True, entry


def _settle_locked(data: dict, plugin: str) -> tuple[bool, list[str]]:
    """Settlement body (caller holds the lock / is yield-free): once every
    member is decided the round is CONSUMED and its outcome applied to the
    obligation for the round's artifact —

    * zero members, or all approved ⇒ ``gate="released"`` (the worker may now
      dispatch). A zero-member round is a positive verdict that this artifact
      needs no consent, NOT an empty round to wait on;
    * any denial ⇒ ``status="refused"`` and one operator note. The operator
      declined the endpoint; provisioning it anyway would act against a
      decision they just made. While a consent for that artifact is pending
      again the sweep re-arms the obligation (:func:`ensure_obligation`),
      which is the way back.

    A round with NO obligation belongs to a plugin that declares no
    ``casa.setupTool`` — consume it silently (a denial must emit no spurious
    note). v0.161.0: settlement no longer resolves the registry, so it can no
    longer be deferred; the setup tool is resolved at dispatch instead.

    Returns ``(released, notes)``. Mutates ``data`` (caller saves)."""
    rnd = data["rounds"].get(plugin)
    if not isinstance(rnd, dict):
        return False, []
    members = rnd.get("members") or {}
    # POSITIVE completeness: every member must carry a terminal decision. Waiting
    # only on `state == "open"` inferred a decision by elimination, so a member
    # with an unreadable state (`{}`) counted as approved and released setup for a
    # consent nobody had answered.
    if any(m.get("state") not in ("approved", "denied")
           for m in members.values()):
        return False, []
    artifact_id = rnd.get("artifact_id") or ""
    del data["rounds"][plugin]
    if rnd.get("verdict") is not True:
        # Sealed for keyboard fencing only — the reconciler could not establish
        # this plugin's full consent position in the pass that opened it. The
        # decisions it collected say nothing about setup, so consume the round
        # and leave the obligation untouched: it holds until a pass that CAN
        # establish the position seals an authoritative verdict.
        logger.info("non-authoritative round consumed (plugin=%s): no setup "
                    "conclusion drawn", plugin)
        return False, []
    row = _row_for(data, plugin, artifact_id)
    if row is None or row.get("status") != "pending":
        return False, []
    denied = [i for i, m in members.items() if m.get("state") == "denied"]
    if denied:
        if row.get("gate") == "released":
            # A denial may only withhold a release, never REVOKE one already
            # earned. The nonce fence protects a live member, but it cannot
            # fence a decision whose round is GONE: a late deny/expiry from a
            # superseded keyboard synthesizes a fresh round in
            # `on_consent_decision`, where the member is absent and the fence
            # is skipped by construction. Refusing here would strand the
            # obligation for good — its ack exists, so no re-prompt re-arms it.
            logger.info(
                "late denial ignored (plugin=%s): the obligation for this "
                "artifact was already released by a settled round", plugin)
            return False, []
        row.update({"status": "refused", "updated_ts": _now(),
                    "last_error": f"{len(denied)} unapproved consent(s)"})
        return False, [
            f"Plugin {plugin}: consent settled with {len(denied)} unapproved "
            "consent(s), so its setup tool was NOT run — it is argument-free "
            "and cannot target a subset. Approving the consent will run it."]
    if row.get("gate") == "released":
        return False, []
    row.update({
        "gate": "released",
        "approved_identities": sorted(f"{i}#{m.get('gen', '')}"
                                      for i, m in members.items()),
        "updated_ts": _now(),
    })
    return True, []


async def _recover_and_settle() -> None:
    """Run on EVERY worker kick (impl r3 + r7): (1) recover rounds stranded
    by a crash between ack persistence and decision recording — any OPEN
    member whose identity has a persisted ack becomes approved with that
    ack's generation; (2) SETTLE every round, which is also how a
    zero-member verdict sealed by the reconciler releases its obligation
    (nothing calls :func:`on_consent_decision` in that case — there is no
    decision to feed). Never raises."""
    if _lock is None:
        return
    notes: list[str] = []
    created_any = False
    try:
        async with _lock:
            data = _load()
            for plugin in list(data["rounds"].keys()):
                rnd = data["rounds"][plugin]
                if _ack_lookup is not None:
                    for identity, m in (rnd.get("members") or {}).items():
                        if m.get("state") != "open":
                            continue
                        try:
                            gen = _ack_lookup(identity)
                        except Exception:  # noqa: BLE001
                            gen = None
                        if gen is not None:
                            m.update({"state": "approved", "gen": str(gen)})
                # Settle EVERY round, changed or not: covers the crash-window
                # recovery AND a freshly sealed zero-member verdict.
                created, n = _settle_locked(data, plugin)
                created_any = created_any or created
                notes.extend(n)
            _save(data)
    except Exception:  # noqa: BLE001
        logger.exception("setup-round recover/settle sweep failed")
        return
    for n in notes:
        await _note(n)
    if created_any and _kick is not None:
        _kick.set()




# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def start_worker() -> None:
    """Boot seam: start the supervised dispatch worker; it runs the boot
    recovery sweep first, then dispatches ``pending`` episodes."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.get_running_loop().create_task(
        _worker(), name="plugin-setup-episodes")
    if _kick is not None:
        _kick.set()


async def _worker_pass() -> bool:
    """One drain pass: recover/settle rounds, then dispatch pending episodes.
    Returns True if any episode DEFERRED on transient registry unavailability
    (impl r9, Terra) — the caller schedules a delayed self-kick so recovery
    does not depend on a future reconcile kick that may have coalesced with
    the one that already fired (resolver failure is internal, not tied to a
    reconcile that would kick again)."""
    await _recover_and_settle()
    retry_wanted = False
    for ep in episodes("pending"):
        try:
            if await _run_episode(ep):
                retry_wanted = True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — isolate per episode
            logger.exception("setup episode %s failed unexpectedly",
                             ep.get("id"))
            _update_episode(ep.get("id") or "", status="failed",
                            last_error="internal error (see log)")
    return retry_wanted


def _schedule_retry(delay: float) -> None:
    """Fire ONE delayed self-kick (impl r9). Coalesces: a live retry task is
    not duplicated, so a whole pass of deferred episodes costs one timer."""
    global _retry_task
    if _retry_task is not None and not _retry_task.done():
        return

    async def _later() -> None:
        try:
            await _sleep(delay)
        finally:
            if _kick is not None:
                _kick.set()

    _retry_task = asyncio.get_running_loop().create_task(
        _later(), name="plugin-setup-retry")


async def _worker() -> None:
    while True:
        try:
            assert _kick is not None
            await _kick.wait()
            _kick.clear()
            if await _worker_pass():
                _schedule_retry(_RETRY_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the worker must survive anything
            logger.exception("plugin-setup worker pass failed")
            await _sleep(5.0)
            if _kick is not None:
                _kick.set()  # self re-kick: never strand pending episodes


def _update_episode(episode_id: str, **fields) -> None:
    data = _load()
    for e in data["episodes"]:
        if e.get("id") == episode_id:
            e.update(fields, updated_ts=_now())
            break
    _save(data)


# #803: the applied-routing seam raised. Its OWN object: ``None`` means "no
# registry bound" (the recompute decides) and a tuple is a reading, so a raise
# collapsed into either would fail open or invent a generation.
_ROUTING_UNREADABLE = object()


def _applied_routing_state():
    """One read of the applied-routing seam, fail-closed: ``None`` (seam absent
    or no registry bound), a ``(published, generation)`` tuple, or
    :data:`_ROUTING_UNREADABLE` when the seam raised."""
    if _applied_routing is None:
        return None
    try:
        return _applied_routing()
    except Exception:  # noqa: BLE001
        logger.exception("applied routing read failed")
        return _ROUTING_UNREADABLE


async def _run_episode(ep: dict) -> bool:
    """Dispatch one pending episode. Returns True iff it DEFERRED on
    transient registry unavailability (the caller schedules a delayed
    re-kick); False/None on every terminal or route-gated outcome."""
    plugin = ep["plugin"]
    # #451 INV-PLUG-010: an obligation dispatches ONLY against a POSITIVELY
    # sealed consent verdict for its exact artifact. No verdict yet ⇒ HOLD —
    # never "nothing to wait for, so go". The obligation stays `pending` and
    # actionable in health, and every reconcile kicks a re-check. This is the
    # third answer that mutation-time routing could not express, and the
    # absence of it is what made both #443 attempts fail.
    if ep.get("gate") != "released":
        return
    # impl r8 (Sol+Terra): dispatch-time resolution is THREE-STATE, exactly
    # like settlement — a transient resolver failure must NOT permanently
    # mark a durably-settled episode stale (that would lose the setup after
    # approval). UNAVAILABLE ⇒ keep pending + bounded retry on later kicks;
    # a valid entry with a DIFFERENT artifact ⇒ confirmed supersession
    # (stale); the matching artifact ⇒ proceed.
    resolved_ok, entry = _resolve_entry(plugin)
    if not resolved_ok:
        deferrals = int(ep.get("resolve_deferrals") or 0) + 1
        if deferrals >= _MAX_RESOLVE_DEFERRALS:
            _update_episode(ep["id"], status="stale",
                            last_error="registry unresolvable after retries "
                            "(plugin gone?)")
            await _note(f"Plugin {plugin}: a queued setup run was dropped — "
                        "the plugin could not be resolved. Run its setup tool "
                        "manually if it is still installed.")
            return
        _update_episode(ep["id"], resolve_deferrals=deferrals,
                        last_error="waiting for registry resolution")
        return True  # deferred — caller schedules a delayed self-kick
    if entry.get("artifact_id") != ep["artifact_id"]:
        # Updated to a NEW artifact (whose own consent round mints its own
        # episode) — this one must never fire (confirmed TOCTOU supersession).
        _update_episode(ep["id"], status="stale",
                        last_error="artifact superseded")
        await _note(f"Plugin {plugin}: a queued setup run was dropped — the "
                    "plugin was updated since the consent. A new consent "
                    "round owns the current version.")
        return
    # #451: the setup tool is resolved HERE, live from the current manifest —
    # never captured at settlement. That is what makes an update which changes
    # `casa.setupTool` while leaving `casa.callbacks` byte-identical run the
    # NEW tool, without binding the setup contract into a consent identity.
    # The artifact matched above, so a missing declaration means the plugin
    # dropped it under us; say so rather than dispatching a blank tool name.
    tool = entry.get("setup_tool")
    if not tool:
        _update_episode(ep["id"], status="failed",
                        last_error="plugin no longer declares a setup tool")
        return
    # impl r4 (Sol): dispatch only against a LIVE route — the durable
    # approval/settlement happened regardless of the reconcile outcome (a
    # transient reconcile failure must not strand the round), but the setup
    # tool must not point the external service at an unrouted endpoint. The
    # episode stays pending; every reconcile kicks the worker to re-check.
    # #803: read the APPLIED overlays BEFORE the route recomputation. The
    # recompute below verifies durable artifacts — secret sidecars, the marker
    # pair — and reads no overlay, so it answers "live" while a reconcile's
    # unavailable marker has ingress shut (every paired producer kicks this
    # worker from its trigger half before its callback half has swapped, and
    # the four consent-tap reconciles heal one half only). A standing marker
    # DEFERS here without paying the recompute; the capture is compared
    # again, yield-free, before the send, so a publication landing DURING the
    # recompute is seen before the decision. Every refusal these reads produce
    # is a timed deferral (the caller's 5 s self-kick), never a hold: the
    # publication that would clear it may not kick (the revoke sweep kicks
    # only through reconciles that may raise, and a producer that raises
    # before it publishes kicks nothing at all), and a hold with no waker is a
    # lost setup. #825 closed one case of that rather than making the wake
    # dependable: a cancelled callback heal used to clear the marker with no
    # wake, both overlays then reading live so the recovery job stayed idle,
    # and now drains its writes and kicks before the cancellation propagates
    # (INV-CB-010). No registry bound reads ``None``: the recompute decides.
    routing_before = _applied_routing_state()
    if routing_before is _ROUTING_UNREADABLE:
        _update_episode(ep["id"], last_error="applied routing state unreadable")
        return True  # deferred — caller schedules a delayed self-kick
    if routing_before is not None and not routing_before[0]:
        _update_episode(ep["id"],
                        last_error="waiting for plugin routing to be published")
        return True  # deferred — caller schedules a delayed self-kick
    if _routes_live is not None:
        try:
            # #453: OFF THE EVENT LOOP. The gate re-derives both reconcilers'
            # issue sets and now also reads the durable artifacts behind them —
            # the per-trigger secret sidecars, and the callback marker pair
            # under the spool's process-wide lock, which the reconcile holds
            # across fsyncs from a worker thread. The reconcilers put these very
            # reads behind `to_thread` for that reason; this was the one caller
            # that did them inline.
            live = bool(await asyncio.to_thread(_routes_live, plugin))
        except Exception:  # noqa: BLE001
            logger.exception("episode %s: routes_live check failed", ep["id"])
            live = False
        if not live:
            _update_episode(ep["id"],
                            last_error="waiting for live trigger route")
            return
        # ...and RE-ESTABLISH the supersession check the await above broke.
        # Everything from `_resolve_entry` to the dispatch used to be yield-free,
        # which is what made "a superseded artifact must never fire" true rather
        # than likely. Moving this gate off the event loop inserted the first
        # yield into that window — and the awaited work is not short (two
        # registry resolutions plus the secret and marker reads), so a
        # `plugin_update` completing inside it left this episode dispatching the
        # OLD artifact's setup tool against the provider, from a captured
        # `entry`. The resident binding check downstream does not catch it (the
        # published binding still names the old artifact until a reload) and a
        # specialist target has no such check at all.
        resolved_ok, entry = _resolve_entry(plugin)
        if not resolved_ok or entry.get("artifact_id") != ep["artifact_id"]:
            # Deliberately NOT terminal here: re-run the whole ladder from the
            # top on the next kick, where the three-state resolution decides
            # between "unavailable, retry" and "confirmed supersession, stale".
            _update_episode(ep["id"],
                            last_error="artifact changed during the route check")
            return True  # deferred — caller schedules a delayed self-kick
        tool = entry.get("setup_tool")
        if not tool:
            _update_episode(ep["id"], status="failed",
                            last_error="plugin no longer declares a setup tool")
            return
    # #423: dispatch only when the plugin's required env vars are resolved
    # in the effective environment — the consent round can settle while the
    # installing engagement is still wiring secrets, and a setup MCP server
    # spawned before plugin_env reload runs with literal ${VAR} placeholders
    # (observed live: an OAuth URL with client_id=${GMAIL_CLIENT_ID}). The
    # episode stays pending; plugin_env reloads and reconciles kick a
    # re-check. Fails CLOSED on a raising seam, like the routes gate.
    if _secrets_ready is not None:
        try:
            ready = bool(_secrets_ready(plugin))
        except Exception:  # noqa: BLE001
            logger.exception("episode %s: secrets_ready check failed",
                             ep["id"])
            ready = False
        if not ready:
            _update_episode(ep["id"],
                            last_error="waiting for plugin secrets")
            return
    # #423 r2 (Sol 1 / Terra 1): secrets resolving is necessary but not
    # sufficient — a RESIDENT executes in a long-lived Agent that may still
    # hold a binding snapshot built while the plugin was env-withheld, and
    # bus acceptance marks the episode dispatched, permanently consuming the
    # automatic setup against a session without the tool. Hold until that
    # resident's next session build carries this exact artifact; agent
    # reloads wake a re-check. r3 (Terra r2-1): the SPECIALIST branch is
    # exempt — specialists are not boot-registered runtime agents; they
    # build options fresh per delegation against the current environment
    # (the specialist builder withholds env-unresolved plugins), so a
    # resident-binding check would strand a specialist-only episode
    # forever. Fails CLOSED on a raising seam.
    if _execution_ready is not None:
        exec_tier, exec_role = plugin_dispatch.execution_target(entry)
        if exec_tier == "resident":
            try:
                exec_ok = bool(_execution_ready(exec_role, plugin,
                                                ep["artifact_id"]))
            except Exception:  # noqa: BLE001
                logger.exception("episode %s: execution_ready check failed",
                                 ep["id"])
                exec_ok = False
            if not exec_ok:
                _update_episode(ep["id"],
                                last_error="waiting for target agent reload")
                return
    role, instruction = _compose(ep, entry, tool)
    if role is None:
        if _MUTABLE_COMPOSE_FAILURE in instruction:
            # #451 r13 (Sol): having no runnable target is an ASSIGNMENT state,
            # not a property of the artifact — `plugin_assign` repairs it. Making
            # it terminal stranded the obligation for good: with the hand-back
            # gone there is no manual path, and nothing re-arms a terminal row
            # once every consent is acked. So it HOLDS, like the route, secret,
            # binding and dispatch-acceptance gates; assignment reloads kick a
            # re-check. Only an artifact-intrinsic failure stays terminal.
            _update_episode(ep["id"], last_error=f"waiting: {instruction}")
            return
        _update_episode(ep["id"], status="failed", last_error=instruction)
        await _note(f"Plugin {plugin}: automatic setup could not run "
                    f"({instruction}). Run its setup tool manually.")
        return
    ok = False
    attempts = int(ep.get("attempts") or 0)
    while attempts < _MAX_DISPATCH_ATTEMPTS and not ok:
        # #803: the FINAL read, yield-free before the send — inside the loop
        # because the backoff sleep below yields between attempts, and before
        # the attempt counter so a refusal records no attempt that never
        # happened. Everything between the gate's thread and here is
        # synchronous, and ``_setup_dispatch`` reaches its bus enqueue with no
        # further await (pinned by source), so what this read sees is what the
        # bus accepts against. A standing marker, a publication of either
        # kind since the capture (generation), or a read that raised all
        # DEFER on the worker's own timer.
        if _applied_routing is not None:
            now = _applied_routing_state()
            if now is _ROUTING_UNREADABLE:
                reason = "applied routing state unreadable at dispatch"
            elif now is not None and not now[0]:
                reason = "waiting for plugin routing to be published"
            elif now != routing_before:
                reason = "plugin routing was republished during the dispatch check"
            else:
                reason = None
            if reason is not None:
                _update_episode(ep["id"], last_error=reason)
                return True  # deferred — caller schedules a delayed self-kick
        attempts += 1
        if _dispatch is not None:
            try:
                ok = await _dispatch(role, instruction, {
                    "synthetic": "plugin_setup",
                    "setup_episode": ep["id"],
                })
            except Exception:  # noqa: BLE001
                logger.exception("episode %s: dispatch raised", ep["id"])
                ok = False
        if not ok and attempts < _MAX_DISPATCH_ATTEMPTS:
            await _sleep(_RETRY_BACKOFF_S[min(attempts - 1,
                                              len(_RETRY_BACKOFF_S) - 1)])
    if ok:
        # Bus accepted — the agent's own reply reports the setup RESULT to
        # the operator; what Casa now also correlates (#521) is whether the
        # dispatched session could run the tool at all. For a RESIDENT
        # execution target the dispatched session itself must carry the
        # namespaced tool, so record it for `report_dispatch_outcome`; a
        # SPECIALIST target's courier session never carries it (the
        # specialist builds options fresh per delegation), so no
        # availability claim can be made about the dispatched session and
        # delivery-only semantics stand there, disclosed.
        # `last_error=""` clears a stale gate-hold message ("waiting for
        # live trigger route") that used to survive into the terminal row.
        exec_tier, _ = plugin_dispatch.execution_target(entry)
        expected_tool = (
            f"{sorted(entry.get('granted_tools') or [])[0]}__{tool}"
            if exec_tier == "resident" else "")
        _update_episode(ep["id"], status="dispatched", attempts=attempts,
                        last_error="", expected_tool=expected_tool)
    else:
        # #451 r4 (Sol): a rejected dispatch HOLDS; it is not terminal. Bus
        # rejection is transient by nature — the commonest cause is that no
        # operator DM is reachable yet (``_setup_dispatch`` returns False when
        # ``operator_identity`` is unavailable), which resolves when Telegram is
        # configured, possibly days later. Marking it ``failed`` made it
        # terminal, and with the hand-back gone there is no second runner to
        # compensate: `ensure_obligation` will not re-arm a terminal row without
        # a pending consent, so an ungated plugin's setup was lost for good. The
        # attempts counter still bounds the retry burst WITHIN a pass; the row
        # stays `pending` and every later kick tries again, visible in health
        # (where `pending` never decays) until it lands.
        _update_episode(ep["id"], attempts=0,
                        last_error="waiting to reach a target agent "
                        f"(last: {attempts} dispatch attempt(s) not accepted)")


def report_dispatch_outcome(episode_id: str, *, tools_used_ok: set,
                            tools_attempted: set,
                            available_tools: "set | None") -> None:
    """#521: correlate a dispatched setup turn's outcome with its episode.

    Called by the executing agent at the END of the turn that carried the
    ``setup_episode`` context marker — on success, on a raising turn, and on
    cancellation alike (the caller reports from a ``finally``). Evidence, in
    precedence order over the row's recorded ``expected_tool``:

    * ran (``tools_used_ok`` — at least one observed non-error result) ⇒ the
      episode stays consumed; the agent's own reply reports the result.
    * attempted with only error results (``tools_attempted`` without a
      ``used_ok`` entry) ⇒ NOT run, even when the session init listed the
      tool — a listed tool can still be categorically uncallable in the turn
      (Sol design r1: a denied protected tool; an erroring server).
    * not attempted, and the session init POSITIVELY listed the tool
      (``available_tools``) ⇒ consumed — "consumed ⇒ the tool was available
      to the turn" is the invariant, and an available tool the agent chose
      not to call is its reply's business, not a dispatch failure.
    * anything else — tool absent from the init list, or availability
      UNKNOWN (``available_tools is None``: a warm-reuse session replays no
      init; a turn that died before one) ⇒ NOT evidenced.

    A non-evidenced turn returns the row to ``pending`` (its released gate is
    kept — the verdict was earned) so the next kick re-dispatches: the
    post-reload kick is the expected healer, exactly the sequence observed
    live (#521: G-2 forced a ``casa_reload`` seconds after the toolless
    turn). Deliberately NO self-kick here — an immediate retry would land in
    the same broken warm session and burn the budget in seconds; the row is
    meanwhile visible in health (``pending`` never decays). Past
    :data:`_MAX_EXECUTION_RETRIES` the obligation fails with an operator
    note (best-effort, scheduled — this function stays synchronous so a
    cancelled turn's ``finally`` can call it).

    Rows keyed away by id (superseded by a re-arm or a new artifact), rows
    no longer ``dispatched``, and rows with no ``expected_tool`` (specialist
    courier) are all no-ops. SYNCHRONOUS + yield-free; never raises."""
    try:
        data = _load()
        row = next((e for e in data["episodes"]
                    if e.get("id") == episode_id), None)
        if row is None or row.get("status") != "dispatched":
            return
        expected = row.get("expected_tool") or ""
        if not expected:
            return
        if expected in tools_used_ok:
            return
        if (expected not in tools_attempted and available_tools is not None
                and expected in available_tools):
            return
        retries = int(row.get("execution_retries") or 0) + 1
        plugin = row.get("plugin")
        if retries >= _MAX_EXECUTION_RETRIES:
            row.update({
                "status": "failed", "execution_retries": retries,
                "updated_ts": _now(),
                "last_error": ("dispatched turn could not run the setup "
                               f"tool ({retries} execution attempt(s))"),
            })
            _save(data)
            logger.warning(
                "setup episode %s failed (plugin=%s): no dispatched turn "
                "evidenced the setup tool in %d attempts", episode_id,
                plugin, retries)
            note = (f"Plugin {plugin}: automatic setup was dispatched "
                    f"{retries} times but the agent's session could not run "
                    "the setup tool. Run it manually once the plugin's "
                    "tools load.")
            try:
                asyncio.get_running_loop().create_task(_note(note))
            except RuntimeError:
                pass
            return
        row.update({
            "status": "pending", "attempts": 0,
            "execution_retries": retries, "updated_ts": _now(),
            "last_error": ("dispatched turn could not run the setup tool "
                           f"(execution retry {retries}/"
                           f"{_MAX_EXECUTION_RETRIES}); the next agent "
                           "reload re-dispatches"),
        })
        _save(data)
        logger.info(
            "setup episode %s returned to pending (plugin=%s): dispatched "
            "turn did not evidence the setup tool (retry %d/%d)",
            episode_id, plugin, retries, _MAX_EXECUTION_RETRIES)
    except Exception:  # noqa: BLE001 — the turn path must never see a raise
        logger.exception("setup-episode outcome report failed (id=%s)",
                         episode_id)


async def _note(text: str) -> None:
    if _notify_operator is None:
        return
    try:
        await _notify_operator(text)
    except Exception:  # noqa: BLE001
        logger.exception("setup-episode operator note failed")


def _compose(ep: dict, entry: dict, tool: str) -> tuple[str | None, str]:
    """The fixed Casa-authored setup instruction + execution-target
    selection. Returns ``(role, instruction)`` or ``(None, reason)``.

    Tool binding is UNAMBIGUOUS or nothing: exactly one server-level grant
    is required — zero or several fail the episode (verify blocks such
    plugins upstream with ``setup_tool_ambiguous_server``).

    Target ORDER is the shared ``plugin_dispatch.compose`` (extracted so
    this and ``callback_episodes._compose`` can never drift apart):
    ``resident:assistant`` when targeted; else the lexicographically first
    resident; else the first specialist via assistant delegation.
    Executor-only/empty targets are refused upstream at verify. The
    resident/specialist-delegate INSTRUCTION WORDING differs from
    ``plugin_dispatch.compose``'s generic delegate wrap (it names the exact
    setup tool), so only the resident branches reuse ``compose``'s returned
    instruction verbatim; the specialist branch builds its own text off the
    same decision.
    """
    grants = sorted(entry.get("granted_tools") or [])
    if len(grants) != 1:
        return None, (f"ambiguous or missing MCP server binding "
                      f"({len(grants)} server grants)")
    namespaced = f"{grants[0]}__{tool}"
    targets = entry.get("targets") or []
    residents = sorted(t.split(":", 1)[1] for t in targets
                       if t.startswith("resident:"))
    specialists = sorted(t.split(":", 1)[1] for t in targets
                         if t.startswith("specialist:"))
    # #451: state only what is true of THIS obligation. A released obligation
    # with approved identities followed a consent the operator granted; one
    # released by a zero-member verdict followed no consent at all (an ungated
    # plugin, or an update whose declaration-bound ack survived). Asserting an
    # approval that never happened is the same class of invented fact as #443's
    # "the integration is dead" — see INV-TOOL-005.
    if ep.get("approved_identities"):
        preamble = (f"The operator approved the consent for plugin "
                    f"'{ep['plugin']}' and its secret was (re)minted. ")
    else:
        preamble = (f"Plugin '{ep['plugin']}' was installed or updated and "
                    "declares a setup tool; it needed no new consent. ")
    base = f"[casa plugin setup · episode {ep['id']}] {preamble}"
    tail = (
        " Call it with no arguments, take no other action, and report the "
        "outcome briefly."
    )
    resident_text = (
        base + f"Run the setup tool `{namespaced}` now to (re-)point the "
        "external service." + tail)
    role, instruction = plugin_dispatch.compose(entry, resident_text)
    if role is None:
        return None, instruction
    if residents:
        return role, instruction
    sp = specialists[0]
    return "assistant", (
        base + f"Delegate to the specialist '{sp}' with the instruction "
        f"to run its setup tool `{namespaced}` now — do not substitute "
        "another agent or tool." + tail)
