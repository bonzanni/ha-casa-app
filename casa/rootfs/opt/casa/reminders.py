"""Reminder entries as data (#396, folded into ``triggers.yaml`` by #398).

A reminder IS a trigger. This module owns the parts of that which are about
*data*: generating reminder names, deriving a schedule from a resolved instant
plus a repeat rule, adding/removing the agent's own entries in a role's
``triggers.yaml``, and answering which reminders are overdue. It knows about
files and time; it knows nothing about APScheduler or MCP — so
``trigger_registry`` never learns to write YAML and ``tools`` never learns cron.

Design points worth keeping in view:

* **Cron has no year field.** A dated one-shot written as cron (``55 7 3 8 *``)
  is an ANNUAL trigger with a self-delete instruction stapled on. ``type:
  date`` exists to remove that trap.
* **Presence is the ledger.** A one-shot reminder still sitting in the file
  with a past fire time *is* the record that delivery is owed. Delivery removes
  the entry. There is no second store to keep in sync.
* **One file, ownership per entry.** Reminders live in the operator's own
  ``triggers.yaml``. #396 kept them apart because ``config_sync`` resolved an
  edited image-owned file against a changed shipped default as "image wins",
  which would have deleted every pending reminder on such an update; #398
  release 1 made that file reconcile PER ENTRY, so a locally-added name is
  preserved and the separate file lost its only purpose.
* **Ownership is DATA, never inferred.** An entry is the agent's iff it carries
  ``managed_by: agent``. The schema permits an operator to author ``name:
  reminder-bins / type: date / one_shot: true``, so neither the reserved name
  prefix, nor the type, nor the flag identifies ownership — three review rounds
  of inferring it each found a new way to delete a live operator trigger.
* **:func:`remove_entry` must never refuse for a reason :func:`past_due`
  tolerates.** The sweep delivers what ``past_due`` selects and then removes
  it; any *check* present in one and absent from the other means the sweep can
  deliver an entry it cannot clean up, and redelivers it every five minutes
  forever. This is why the writer carries no whole-file duplicate-name check
  and why removal skips schema validation: a duplicate elsewhere in the file
  parses fine, so ``past_due`` succeeds, and a running process can hold a valid
  snapshot while the on-disk file is invalid (``config_git_commit`` refuses such
  a commit but leaves the working tree edited). Sharing :func:`_read_doc` is
  what keeps their READ tolerance identical.

  It does not make them *identical in every respect*, and the difference is
  worth naming because it was mistaken for one once: reading a document and
  re-emitting it can fail independently (see :func:`_emit`). What is guaranteed
  is that both failure directions land inside the handled ``(OSError,
  ValueError)`` contract, so the worst case is the ordinary at-least-once one —
  delivered, not cleaned up, logged, and the remaining roles still swept —
  never an aborted pass.

* **A pre-existing SCHEMA defect in a sibling entry never blocks either
  operation.** The file on disk can already be invalid while Casa runs on the
  snapshot it booted with, and refusing then protects nothing while making
  reminders unavailable. So creation schema-validates only the entry it is adding
  (see :func:`_validate_candidate`), and removal validates nothing at all.

  "Schema" is the operative word: a sibling can still block both operations by
  being unreadable or un-re-emittable, because there is then no way to write the
  file at all (see :func:`_read_doc` and :func:`_emit`). Only the *judgment* is
  scoped to our own entry; the mechanics are necessarily whole-file.

  The qualifier is exact, not hedging: the added entry is judged under the
  document's REAL top level, because ``schema_version`` decides what is legal.
  So a pre-existing defect in the top level itself — an unknown root key, an
  out-of-range ``schema_version`` — does still refuse creation. That is the
  intended boundary rather than a leak: the top level is not separable from the
  judgment, and such a file cannot boot either way, so neither refusing nor
  writing helps the operator.

* **No AGENT writes this file except through here** (#403). This module began
  as the reminder writer, and the document layer it grew — read, judge, re-emit,
  refuse — is what any writer of that file needs. It now serves the
  configurator's trigger edits too, through :func:`upsert_entry` /
  :func:`delete_entry`, because the alternative was fatal: the configurator runs
  in a SEPARATE CLI process, so its read-modify-write spans model thinking time
  and no lock may be held across that. A reminder set inside that window was
  discarded by the stale rewrite, silently, and ``config_git_commit``'s
  ``git add -A`` committed the loss. Routing the edit here makes it a single
  synchronous step on Casa's loop, where it interleaves with the reminder writer
  not at all; ``hooks.trigger_file_write_guard`` denies the hand edit that used
  to be the recipe. The general mutators are strictly weaker than the reminder
  ones in what they may TOUCH — a ``managed_by: agent`` entry is the resident's
  and they refuse it, in the entry submitted AND in the entry being replaced —
  and identical in how they judge what they write.

  The bound is "no agent", not "no writer". ``config_sync`` rewrites this file
  too, from a worker thread; #458 closed the window in which its stale rewrite
  could discard a reminder written here meanwhile, by serializing both under
  ``trigger_write_lock.PASS_LOCK`` (see :func:`_under_pass_lock`). The
  operator's own editor is still unbound, deliberately — the guard binds
  agents, not the human.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta

import yaml

import trigger_write_lock
from atomic_io import atomic_write_text
from config import (_ENV_RE, dump_yaml_declared_text,
                    load_yaml_declared_text, text_has_lone_placeholder)
from provenance import scheduled_delivery_markers
import scheduled_asks


def _under_pass_lock(fn):
    """Hold ``trigger_write_lock.PASS_LOCK`` for the whole read → write of a
    ``triggers.yaml`` mutator (#458).

    ``config_sync`` holds the same lock across its entire reconcile pass, so a
    decorated mutator can only run before or after the pass — never between the
    pass's read and its write, which is where a reminder was silently lost. The
    lock BLOCKS, so a caller on the event loop MUST invoke the mutator via
    ``asyncio.to_thread`` (never directly), or a running pass would stall the
    loop; off-loop callers (boot, tests) may call directly.
    """
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        with trigger_write_lock.PASS_LOCK:
            return fn(*args, **kwargs)
    return _wrapped


logger = logging.getLogger(__name__)

REMINDER_PREFIX = "reminder-"

REPEATS: tuple[str, ...] = ("none", "daily", "weekdays", "weekly", "monthly")

# datetime.weekday(): 0=Monday. Cron day NAMES are emitted deliberately — a
# numeric day-of-week reintroduces #343 (standard cron numbers Sunday 0,
# APScheduler 3.x numbers Monday 0), which trigger_registry._translate_cron_dow
# exists to defuse. Deriving the name from the already-resolved date also means
# the weekday can never disagree with the time the agent read back to the user.
_DOW_BY_WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def new_reminder_name(taken: "set[str] | None" = None) -> str:
    """A fresh reminder trigger name, matching the schema's name pattern.

    *taken* is the set of names already in use. A collision would make
    registration fail on a duplicate job id, and the caller's rollback would
    then delete the PRE-EXISTING reminder of the same name along with its
    own — losing a reminder the user had already been promised. Avoiding the
    collision outright is the fix; the widened entropy just makes retries
    vanishingly rare.
    """
    taken = taken or set()
    for _ in range(10):
        name = f"{REMINDER_PREFIX}{secrets.token_hex(4)}"
        if name not in taken:
            return name
    raise ValueError("could not generate an unused reminder name")


def existing_names(path: str) -> set[str]:
    """EVERY trigger name in the file at *path*, whatever its owner.

    Deliberately not limited to the agent's own entries: ``register_agent``
    raises on a duplicate name and that is uncaught at boot, so a generated
    reminder name colliding with an OPERATOR trigger would be a crash loop.
    Generating against the whole namespace is what prevents it.
    """
    try:
        _, doc = _read_doc(path)
        return {e["name"] for e in doc["triggers"]
                if isinstance(e, dict) and isinstance(e.get("name"), str)}
    except (OSError, ValueError):
        return set()


# ---------------------------------------------------------------------------
# Time and schedule derivation
# ---------------------------------------------------------------------------


def parse_at(value: str) -> datetime:
    """Parse an ISO-8601 instant that MUST carry a UTC offset.

    A naive datetime is refused rather than assumed to be local: "08:00" with
    no offset is not a point in time, and guessing one is how a reminder
    quietly fires an hour out.
    """
    if not value:
        raise ValueError("reminder time is required")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not an ISO-8601 time: {value!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"reminder time must carry a UTC offset; got {value!r}"
        )
    return dt


def validate_recurring(at: datetime, repeat: str, tz=None) -> None:
    """Raise ValueError if *at* cannot be honoured EXACTLY as *repeat*.

    Refusing beats approximating. A cron expression has minute resolution and
    a fixed day-of-month, so a sub-minute anchor or a day that does not exist
    in every month can only be delivered by silently changing what the user
    asked for — and then the time they were told is not the time that fires.
    The caller surfaces this so the agent can ask for something expressible.
    """
    if repeat == "none":
        return
    if at.second or at.microsecond:
        raise ValueError(
            "a repeating reminder must fall on a whole minute; "
            f"{at.isoformat()} has seconds"
        )
    local = at.astimezone(tz) if tz is not None else at
    if repeat == "weekdays" and local.weekday() >= 5:
        raise ValueError(
            "a weekdays reminder cannot start on a Saturday or Sunday: the "
            "first occurrence would silently be the following Monday. Give "
            "the first weekday occurrence instead."
        )
    if repeat == "monthly" and local.day > 28:
        raise ValueError(
            f"a monthly reminder cannot fall on day {local.day}: that day is "
            "missing from some months, so the reminder would skip them. Use "
            "day 28 or earlier, or ask the user for a different day."
        )


def derive_schedule(at: datetime, repeat: str, tz=None) -> dict[str, str]:
    """Trigger fields for *repeat* anchored at *at*.

    ``repeat="none"`` keeps the absolute instant — a single point in time has
    no recurrence to drift, and the offset is what pins it.

    Every recurring rule persists DERIVED WALL-CLOCK fields and DISCARDS the
    offset, so a reminder set at 07:00 in summer still fires at 07:00 local in
    winter (spec §7.1). APScheduler evaluates the cron expression in the
    scheduler's own timezone; persisting the supplied ``+02:00`` and applying
    it literally would shift the reminder by an hour across a DST boundary.
    """
    if repeat not in REPEATS:
        raise ValueError(f"repeat must be one of {REPEATS}; got {repeat!r}")
    if repeat == "none":
        return {"type": "date", "at": at.isoformat()}

    # This function does NO rounding and NO clamping. Three review rounds
    # produced a finding here every time — truncating seconds, then rounding
    # them up, then mapping day>28 to end-of-month — each fix creating the
    # next defect, because approximating a request silently makes the time the
    # user was told differ from the time that fires. Anything a cron cannot
    # express EXACTLY is refused by the caller instead (see
    # ``validate_recurring``), so what is promised is always what happens.
    #
    # The one transformation that remains is a conversion, not an
    # approximation: the wall-clock fields are read in the SCHEDULER's
    # timezone. The caller's offset pins which instant is meant; the cron is
    # evaluated in the scheduler's zone, so deriving the fields from the
    # caller's offset would misschedule by the difference whenever the two
    # disagree — and would drift across a DST boundary.
    local = at.astimezone(tz) if tz is not None else at

    minute, hour = local.minute, local.hour
    if repeat == "daily":
        schedule = f"{minute} {hour} * * *"
    elif repeat == "weekdays":
        schedule = f"{minute} {hour} * * mon-fri"
    elif repeat == "weekly":
        schedule = f"{minute} {hour} * * {_DOW_BY_WEEKDAY[local.weekday()]}"
    else:  # monthly
        schedule = f"{minute} {hour} {local.day} * *"
    # ``at`` is the FIRST occurrence and becomes the scheduler's start_date.
    # Without it, "every Thursday from the 20th" set on the 3rd would fire on
    # the 6th and 13th — two occurrences the user never asked for. It does NOT
    # drive recurrence: the cron fields above do, evaluated in the scheduler's
    # timezone, which is what keeps the series DST-correct.
    #
    # Callers must report THIS value back to the user rather than the one they
    # passed in: the two differ whenever the caller's offset and the
    # scheduler's timezone render different wall-clock times.
    return {"type": "cron", "schedule": schedule, "at": local.isoformat()}


# ---------------------------------------------------------------------------
# The entry store — the agent's own entries inside a role's triggers.yaml
# ---------------------------------------------------------------------------

# The one value ``managed_by`` may carry. Absent means the entry is the
# operator's, which is why the schema's enum is single-valued: an "operator"
# marker would invert the default.
OWNER_AGENT = "agent"


def triggers_path(agents_dir: str, role: str) -> str:
    """Absolute path to *role*'s triggers.yaml. Residents only.

    One file (#398 release 2). Reminders are ordinary entries in the operator's
    own trigger file, distinguished by ``managed_by: agent`` rather than by
    living somewhere else. ``role_configs`` holds residents only, so this is
    never called for a specialist or executor — where the file is FORBIDDEN and
    creating one would be boot-fatal.
    """
    return os.path.join(agents_dir, role, "triggers.yaml")


# Every field this module reads, and the type it must have. An entry failing
# this is SKIPPED BY SELECTION but never removed from the document — see
# _read_doc.
_FIELD_TYPES = {
    "name": str, "type": str, "at": str, "schedule": str,
    "channel": str, "prompt": str, "one_shot": bool, "managed_by": str,
}


def _wellformed(entry) -> bool:
    """True if *entry* is safe for every consumer in this module."""
    if not isinstance(entry, dict):
        return False
    if not isinstance(entry.get("name"), str):
        return False
    for field, expected in _FIELD_TYPES.items():
        value = entry.get(field)
        if value is None:
            continue
        # bool is a subclass of int; require an exact match for one_shot so a
        # stray integer does not read as True.
        if expected is bool:
            if not isinstance(value, bool):
                return False
        elif not isinstance(value, expected):
            return False
    return True


def _read_doc(path: str) -> "tuple[str | None, dict]":
    """``(raw_text, document)`` for *path*, folding parse failure into
    ``ValueError``. ``raw_text`` is ``None`` when the file does not exist.

    This is the STRUCTURAL parse: the document that will be mutated and
    re-emitted. It is deliberately NOT env-substituted — substituting before
    dumping would bake resolved values into the file and destroy the operator's
    ``${VAR}`` placeholders permanently.

    It records one thing a plain ``safe_load`` throws away: which lone
    placeholders the file declared as TEXT, so :func:`_emit` can re-emit them
    quoted (#512). Everything else about the parse is ``safe_load``'s.

    It also does NOT drop malformed entries. Under #396 this file was written
    only by this module, so dropping corruption was safe; it is now the
    OPERATOR's file, and silently dropping an entry from the document we write
    back would destroy their configuration. Malformed entries are skipped by
    SELECTION instead (:func:`_wellformed`) and re-emitted untouched.

    Every failure folds into ``ValueError`` because both this module's readers
    and its writers catch exactly ``(OSError, ValueError)``: an unfolded
    ``yaml.YAMLError`` would escape and abort the whole sweep, so later roles'
    overdue reminders would go undelivered.
    """
    if not os.path.exists(path):
        # A role's first reminder creates the file. schema_version 2 is what
        # the shipped assistant default declares (#654), and it keeps #402's
        # future tightening from being boot-fatal on a file this writer
        # created. (The add recipe writes no version at all — it hands this
        # module an entry, never a document.)
        return None, {"schema_version": 2, "triggers": []}
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        doc = load_yaml_declared_text(text) or {}
    except (Exception, RecursionError) as exc:  # noqa: BLE001
        raise ValueError(f"{path}: cannot parse: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: triggers.yaml is not a mapping")
    if not isinstance(doc.get("triggers"), list):
        # NOT coerced to []: against a shared file that would silently erase
        # every operator trigger on the next write.
        raise ValueError(f"{path}: 'triggers' is not a list")
    return text, doc


def _agent_owned(entry) -> bool:
    """True iff *entry* is one the reminder tools may touch."""
    return (isinstance(entry, dict)
            and entry.get("managed_by") == OWNER_AGENT)


def _emit(doc: dict, path: str) -> str:
    """Serialize *doc*, folding an emission failure into ``ValueError``.

    Quoting survives (#512): a scalar :func:`_read_doc` saw declared as text and
    consisting only of ``${VAR}`` is re-emitted quoted, so the rewrite does not
    change what it means to the loader — and does not erase the very property
    ``text_has_lone_placeholder`` tests, which is what made one cleanup
    permanently disarm the guard for every later writer. Nothing else about the
    file's form is preserved; see the module docstring.

    Parsing tolerance and EMISSION tolerance are not the same (Sol, impl r1):
    with the pinned PyYAML, a document nested a few hundred levels deep
    ``safe_load``s fine but makes ``safe_dump`` raise ``RecursionError`` — which
    is a ``RuntimeError``, so it escapes every ``except (OSError, ValueError)``
    in this module and in the sweep. Unfolded it aborted the whole sweep pass
    after a delivery, so later roles' overdue reminders went undelivered AND the
    delivered entry was redelivered on every subsequent pass.

    Folding it here puts such a file on the ordinary at-least-once path instead:
    the reminder is delivered, cleanup fails loudly, the entry stays, and the
    remaining roles are still swept. Delivery is deliberately NOT withheld — a
    duplicate nudge is a better failure than a missed reminder (spec §8).
    """
    try:
        return dump_yaml_declared_text(doc, sort_keys=False, allow_unicode=True)
    except (Exception, RecursionError) as exc:  # noqa: BLE001
        raise ValueError(f"{path}: cannot be re-emitted: {exc}") from exc


# --- #513: placeholder-bearing rewrites owed to the operator ---------------
# remove_entry runs OFF the loop (a thread, under trigger_write_lock.PASS_LOCK)
# and cannot await a send, so it records the path here and the notifier on the
# loop delivers it. A plain lock, not asyncio's — the writer is a thread.
_PLACEHOLDER_LOCK = threading.Lock()
_placeholder_pending: set[str] = set()


def peek_placeholder_notices() -> list[str]:
    """Snapshot of files rewritten despite ``${...}`` interpolation (#513).

    A SNAPSHOT, never a drain. The caller removes a path only once its notice
    is CONFIRMED delivered — draining here would consume the pending work on an
    unconfirmed send, which is exactly the defect #556 reports about the
    plugin-health notice. Sorted so the notifier's order is deterministic.
    """
    with _PLACEHOLDER_LOCK:
        return sorted(_placeholder_pending)


def clear_placeholder_notice(path: str) -> None:
    """Drop *path* once its notice has been confirmed delivered (or suppressed
    as already-notified in this process)."""
    with _PLACEHOLDER_LOCK:
        _placeholder_pending.discard(path)


def _save(path: str, doc: dict) -> None:
    atomic_write_text(path, _emit(doc, path))


def _schema_error(doc: dict, path: str) -> "str | None":
    """The triggers-schema complaint about *doc*, or None if it validates.

    ``agent_loader._validate`` raises ``LoadError``, a direct ``Exception``
    subclass that no caller in this module catches (Sol r2 #2), so it is folded
    into a return value here rather than escaping past ``set_reminder``'s
    ``(OSError, ValueError)`` handler as an unstructured crash.

    Only ``LoadError`` is folded, because only ``LoadError`` is a VERDICT ABOUT
    THE DOCUMENT. Anything else — an unreadable or malformed schema file, an
    invalid schema — means validation could not be performed at all, and that
    must fail closed rather than be reported as "this document is fine". A broken
    schema would otherwise disable the one check that stops a boot-breaking file
    being written.
    """
    import agent_loader

    try:
        agent_loader._validate(doc, "triggers", path)
    except agent_loader.LoadError as exc:
        return str(exc)
    except (Exception, RecursionError) as exc:  # noqa: BLE001
        raise ValueError(
            f"{path}: cannot validate against the triggers schema: {exc}"
        ) from exc
    return None


def _resolve(text: str, path: str) -> dict:
    """The document as the LOADER sees it, through the LOADER'S OWN function.

    Not a second copy of the loader's pipeline: this module used to substitute
    into the text and parse the result, which is what the loader did until #409
    moved resolution into the YAML constructor. Two copies of "what the loader
    sees" disagree the moment one of them changes, and this one decides whether
    an entry is safe to write — so it calls the real thing.

    Distinct from :func:`_read_doc`'s structural parse, which must stay
    unresolved so placeholders survive the rewrite.
    """
    import agent_loader

    try:
        return agent_loader.parse_yaml_text(text, path)
    except (Exception, RecursionError) as exc:  # noqa: BLE001
        raise ValueError(f"{path}: does not parse: {exc}") from exc


def _validate_candidate(candidate: str, path: str, name: str) -> None:
    """Refuse *candidate* unless the entry named *name* validates on its own.

    Validation runs through the loader's own pipeline — ``_substitute_env``,
    parse, then the triggers schema — because a file this writer considers fine
    but the loader rejects is a boot failure at the next restart, the one
    outcome worse than a refused reminder.

    **Only the new entry is judged**, under the document's real top level (the
    ``schema_version`` branch changes what is legal). That is the whole rule.
    Two earlier versions tried to be cleverer and each drew a finding:

    * validating the WHOLE candidate refused a reminder because some unrelated
      entry was already invalid — protecting nothing, since such a file already
      fails to load, while making reminders unavailable and naming an entry the
      user never touched. A running Casa holds the snapshot it booted with, so
      the file on disk can already be invalid while everything works
      (``config_git_commit`` refuses a commit failing boot-parity but leaves the
      working tree edited).
    * comparing the whole candidate against the whole PRIOR document then let
      any pre-existing complaint wave through any new one — including an invalid
      reminder, a fresh boot defect this writer introduced.

    Judging the added entry alone answers the actual question directly, and it is
    sound rather than approximate: the schema carries no cross-entry constraints
    (no ``uniqueItems``, no ``contains``, no ``minItems``), so an entry that
    validates in isolation cannot be what makes the document fail. Duplicate
    names — the one genuine cross-entry hazard — are refused separately in
    :func:`add_entry`, where the check belongs.
    """
    resolved = _resolve(candidate, path)
    solo = {k: v for k, v in resolved.items() if k != "triggers"}
    solo["triggers"] = [e for e in resolved.get("triggers") or []
                        # Picked out of the RESOLVED document so the entry is
                        # judged exactly as the loader will see it.
                        if isinstance(e, dict) and e.get("name") == name]
    problem = _schema_error(solo, path)
    if problem is not None:
        raise ValueError(problem)


@_under_pass_lock
def add_entry(path: str, entry: dict) -> None:
    """Add one agent-owned *entry*, leaving every other trigger untouched.

    Raises ``ValueError`` (or ``OSError``) and writes NOTHING on any refusal.
    Refusing an add is always safe: the caller reports it and the user simply
    has no reminder. That is why the checks here are strict while
    :func:`remove_entry`'s are not — see the module docstring.
    """
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"a reminder entry needs a name; got {name!r}")
    if not _agent_owned(entry):
        raise ValueError(
            f"refusing to write {name!r} without managed_by={OWNER_AGENT!r}: "
            f"provenance is what bounds this writer"
        )
    # The one placeholder the writer controls. A stored ``${VAR}`` would be
    # substituted by the loader at boot, so the reminder would not say what the
    # user asked for; refusing is honest and keeps the file placeholder-free.
    for field, value in entry.items():
        if isinstance(value, str) and _ENV_RE.search(value):
            raise ValueError(
                f"a reminder's {field} cannot contain a ${{...}} placeholder"
            )

    text, doc = _read_doc(path)
    # A scalar that is nothing but `${VAR}` is re-emitted UNQUOTED by safe_dump,
    # and QUOTING is what tells the loader such a scalar is text (#409): quoted
    # it is a string whatever it holds, plain it has its value read back. So an
    # entry authored as `prompt: "${DETAIL}"` means something different once a
    # rewrite drops the quotes and the value looks like a number, a flag or a
    # list. #409 expected this door to close with it; it does not, because the
    # hazard was never only truncation — it is that a rewrite cannot preserve a
    # style it can no longer see. Decided on the TEXT, hence independent of the
    # current environment: a comparison of resolved values would pass with a
    # benign value today and still change the entry's meaning tomorrow. The
    # predicate is shared with config_sync, which rewrites the same files.
    if text is not None and text_has_lone_placeholder(text):
        raise ValueError(
            f"{path} uses ${{...}} interpolation, which this writer cannot "
            f"rewrite without risking the meaning of an existing trigger"
        )
    if name in {e["name"] for e in doc["triggers"]
                if isinstance(e, dict) and isinstance(e.get("name"), str)}:
        # A duplicate makes register_agent raise, uncaught at boot: a crash
        # loop. This is the ONLY duplicate check, and it is scoped to the name
        # being added — a whole-file check would refuse removals too, which is
        # how a delivered reminder gets stranded (Sol r2 #1).
        raise ValueError(f"a trigger named {name!r} already exists")

    candidate = dict(doc)
    candidate["triggers"] = list(doc["triggers"]) + [entry]
    text_out = _emit(candidate, path)
    _validate_candidate(text_out, path, name)
    atomic_write_text(path, text_out)


@_under_pass_lock
def remove_entry(path: str, name: str) -> str:
    """Remove the agent-owned entry called *name*.

    Returns ``"removed"``, ``"not_found"``, or ``"not_owned"`` — three outcomes
    rather than a bool so the canceller can tell the model "that is operator
    configuration" instead of a misleading "no such reminder", decided by the
    one authoritative field.

    Deliberately does NOT schema-validate the document and carries no
    whole-file duplicate check: removal must never refuse for a reason
    :func:`past_due` tolerates, or the sweep delivers an entry it cannot clean
    up and redelivers it every five minutes forever.
    """
    text, doc = _read_doc(path)
    matches = [e for e in doc["triggers"]
               if isinstance(e, dict) and e.get("name") == name]
    if not matches:
        return "not_found"
    if not any(_agent_owned(e) for e in matches):
        return "not_owned"

    placeholder_rewrite = text is not None and text_has_lone_placeholder(text)
    if placeholder_rewrite:
        # Warn and PROCEED. Refusing here is what strands a delivered reminder.
        logger.warning(
            "reminders: %s uses ${...} interpolation; rewriting it to remove "
            "%s may change an existing trigger's resolved value", path, name,
        )
    candidate = dict(doc)
    candidate["triggers"] = [e for e in doc["triggers"]
                             if not (_agent_owned(e)
                                     and e.get("name") == name)]
    text_out = _emit(candidate, path)
    disarmed = placeholder_rewrite and not text_has_lone_placeholder(text_out)
    atomic_write_text(path, text_out)
    if disarmed:
        # The residual bound of #512's fix, made LOUD rather than silent. The
        # rewrite carries the declared-text form of every scalar the document
        # KEEPS, so this can only happen when the file's declared-text
        # placeholders were all discarded by construction — a duplicate key's
        # loser, a merge donor an explicit key overrides. Nothing surviving can
        # have been retyped, but the guard those consumers share is off for
        # this file from here on, and that is worth a line in the log.
        #
        # AFTER the write, for the same reason the notice below is (#513): the
        # write can raise, leaving the file untouched, and a log line saying
        # the guard is off would then be false about the operator's own file.
        logger.warning(
            "reminders: %s no longer declares any ${...} scalar as text after "
            "this rewrite; the declared-text guard is off for this file, "
            "because the quoting it matched was on configuration the document "
            "itself discards (a duplicate key, or an overridden merge key)",
            path,
        )
    if placeholder_rewrite:
        # AFTER the write, never at the warning site (#513): _save can raise
        # before os.replace, leaving the original file untouched, and a notice
        # claiming "Updated <file>" for a file that was never written is a
        # false report about the operator's own configuration.
        with _PLACEHOLDER_LOCK:
            _placeholder_pending.add(path)
    return "removed"


# ---------------------------------------------------------------------------
# The general entry mutators — the configurator's trigger edits (#403)
# ---------------------------------------------------------------------------
#
# Same document layer, opposite ownership bound: these refuse an
# ``managed_by: agent`` entry, where the reminder writer above requires one.
# Between them, every entry in the file has exactly one writer that may touch
# it, and both of them run in this process.


def _refuse_placeholder_rewrite(text: "str | None", path: str, verb: str) -> None:
    """Refuse when re-emitting *path* would strip a lone-``${VAR}`` quote.

    The reminder writer's own refusal, reused verbatim — see :func:`add_entry`
    for why a rewrite cannot preserve a style it can no longer see. Unlike
    :func:`remove_entry`, which warns and PROCEEDS because a stranded delivered
    reminder redelivers forever, these refuse in both directions: nothing here
    is owed to anyone, and the operator can still edit such a file by hand. The
    ``PreToolUse`` guard binds executors, not the human.
    """
    if text is not None and text_has_lone_placeholder(text):
        raise ValueError(
            f"{path} uses ${{...}} interpolation, which this writer cannot "
            f"rewrite without risking the meaning of an existing trigger; "
            f"{verb} it by hand instead"
        )


def _carry_forward_secret_owner(previous: dict, entry: dict) -> dict:
    """Keep a declared ``auth`` that the replacement omits (#609).

    This is a WHOLE-ENTRY replacement, so a field the caller does not send is a
    field that disappears. For every other key that is harmless — the operator
    sees the diff and can put it back. For ``secret_owner`` it is not:
    ``provider`` means Casa must never mint here because the operator placed
    that credential by hand and Casa can neither regenerate nor import it.
    Losing the word flips ownership to the ``casa`` default, and since #609
    mints at registration, the next reload then OCCUPIES the slot. Because a
    casa-minted token also satisfies the provider validation rule, the report
    reads the route healthy while every request signed with the operator's
    real credential is refused.

    Omission therefore means "leave it alone", not "make it mine". That holds
    for the whole ``auth`` block as well as for ``secret_owner`` inside it:
    ``config_trigger_upsert`` projects only the fields it was handed, so an
    edit that touches nothing but ``clearance`` arrives with no ``auth`` at
    all, and dropping it re-defaults the trigger to ``hmac_body``/``casa`` —
    the route then verifies against the global secret while the integrator's
    existing signatures are refused, and the report calls it healthy.

    Changing the mode or the owner stays available by sending an ``auth``
    block that says so.
    """
    prior = previous.get("auth") if isinstance(previous, dict) else None
    if not isinstance(prior, dict):
        return entry
    # The replacement's own `type` decides: a webhook turned into a schedule
    # must not keep an auth block, and the schema would refuse one anyway.
    kind = entry.get("type", previous.get("type") if isinstance(previous, dict) else None)
    if kind != "webhook":
        return entry
    auth = entry.get("auth")
    if auth is None:
        # The whole block was omitted — the shape `config_trigger_upsert`
        # produces for an edit that never mentioned auth at all, since it
        # projects only the fields it was handed. Dropping it silently
        # re-defaults the trigger to `hmac_body`/`casa`, which changes what
        # verifies the route while the report reads healthy.
        return {**entry, "auth": dict(prior)}
    if not isinstance(auth, dict):
        return entry
    if "secret_owner" in auth or "secret_owner" not in prior:
        return entry
    return {**entry, "auth": {**auth, "secret_owner": prior["secret_owner"]}}


@_under_pass_lock
def upsert_entry(path: str, entry: dict) -> str:
    """Add *entry*, or replace the existing entry of the same ``name``.

    Returns ``"added"`` or ``"replaced"``. Raises ``ValueError`` (or
    ``OSError``) and writes NOTHING on any refusal — refusing is always safe
    here, exactly as it is for :func:`add_entry`: the caller reports it and the
    operator's configuration is untouched.

    The replaced entry keeps its POSITION in the document. Order is not
    semantically load-bearing, but a diff the operator reads is, and an update
    that silently moved the entry to the end would make every trigger edit look
    like a delete plus an add in the config repo's history.
    """
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"a trigger entry needs a name; got {name!r}")
    if _agent_owned(entry):
        raise ValueError(
            f"refusing to write {name!r} with managed_by={OWNER_AGENT!r}: that "
            f"provenance marks an entry the resident's own reminder tools own, "
            f"and only they may write one"
        )
    for field, value in entry.items():
        # `_ENV_RE.fullmatch(value.strip())` is the loader's OWN test for the
        # form whose meaning depends on quoting (config.py:158) — the same
        # predicate, not a second copy of the idea.
        if isinstance(value, str) and _ENV_RE.fullmatch(value.strip()):
            # NOT add_entry's blanket ${...} refusal (Sol + Terra both said so,
            # and they are right): interpolation is legitimate operator
            # configuration, and only ONE form is unwritable here. safe_dump
            # re-emits a scalar that is nothing but ${VAR} UNQUOTED, and quoting
            # is what tells the loader such a scalar is text (#409) — so the
            # value's meaning would depend on what the variable happens to hold.
            # An EMBEDDED placeholder has surrounding text, dumps plain, and
            # reads back as the string it is.
            #
            # add_entry keeps the wider refusal for a different reason: a
            # REMINDER must say what the user asked for, so a placeholder there
            # is wrong even when it round-trips.
            raise ValueError(
                f"a trigger's {field} cannot be exactly a ${{...}} placeholder "
                f"when written through this tool — re-emitting it would drop "
                f"the quoting that makes it text. Embed it in surrounding "
                f"text, or edit the file by hand."
            )

    text, doc = _read_doc(path)
    _refuse_placeholder_rewrite(text, path, "edit")

    matches = [i for i, e in enumerate(doc["triggers"])
               if isinstance(e, dict) and e.get("name") == name]
    if len(matches) > 1:
        # register_agent raises on a duplicate, uncaught at boot: a crash loop.
        # The file is ALREADY in that state; silently collapsing the duplicates
        # would hide it behind a successful edit.
        raise ValueError(
            f"{path} already contains {len(matches)} triggers named {name!r}; "
            f"that file cannot boot — resolve the duplicate by hand first"
        )
    if matches and _agent_owned(doc["triggers"][matches[0]]):
        raise ValueError(
            f"{name!r} is a reminder the resident owns (managed_by="
            f"{OWNER_AGENT!r}); ask the resident to change or cancel it"
        )

    candidate = dict(doc)
    entries = list(doc["triggers"])
    if matches:
        entry = _carry_forward_secret_owner(doc["triggers"][matches[0]], entry)
        entries[matches[0]] = entry
        outcome = "replaced"
    else:
        entries.append(entry)
        outcome = "added"
    candidate["triggers"] = entries
    text_out = _emit(candidate, path)
    # Judge the WRITTEN entry alone, under the document's real top level —
    # see _validate_candidate for the two cleverer versions this replaced.
    _validate_candidate(text_out, path, name)
    atomic_write_text(path, text_out)
    return outcome


@_under_pass_lock
def delete_entry(path: str, name: str) -> str:
    """Remove the non-agent-owned entry called *name*.

    ``"removed"``, ``"not_found"``, or ``"not_owned"`` — the same three-outcome
    shape as :func:`remove_entry`, with ownership inverted: an entry carrying
    ``managed_by: agent`` is the resident's, and the answer is "ask the resident
    to cancel it", not a misleading success.

    Deliberately does NOT schema-validate, for the same reason removal never
    does: a pre-existing defect elsewhere in the file must not make an entry
    undeletable.
    """
    text, doc = _read_doc(path)
    matches = [e for e in doc["triggers"]
               if isinstance(e, dict) and e.get("name") == name]
    if not matches:
        return "not_found"
    if any(_agent_owned(e) for e in matches):
        return "not_owned"
    _refuse_placeholder_rewrite(text, path, "edit")
    candidate = dict(doc)
    candidate["triggers"] = [
        e for e in doc["triggers"]
        if not (isinstance(e, dict) and e.get("name") == name
                and not _agent_owned(e))]
    _save(path, candidate)
    return "removed"


def agent_entries(path: str) -> "list[dict] | None":
    """Every agent-owned, well-formed entry in the file at *path*.

    An entry carrying the marker but failing :func:`_wellformed` is skipped —
    consumers here read its fields by type, and it is never dropped from the
    document itself.

    Returns ``None`` when the file cannot be read — NOT an empty list. An empty
    list means "the agent owns nothing here", which authorises reverse
    reconciliation to drop every reminder job; a transient read error must never
    be allowed to say that, or one bad read would unschedule every recurring
    reminder until the next successful sweep.
    """
    try:
        _, doc = _read_doc(path)
        return [e for e in doc["triggers"]
                if _agent_owned(e) and _wellformed(e)]
    except (OSError, ValueError):
        logger.warning("reminders: cannot read %s; skipping reconciliation",
                       path, exc_info=True)
        return None


def past_due(path: str, now: datetime) -> list[dict]:
    """One-shot reminder entries whose instant has passed and which are still
    present. Presence IS the record that delivery is owed.

    An unparseable ``at`` is skipped with a warning rather than raised: one
    corrupt entry must not stop the sweep delivering the others.
    """
    out: list[dict] = []
    try:
        _, doc = _read_doc(path)
    except (OSError, ValueError):
        logger.warning("reminders: cannot read %s; skipping", path,
                       exc_info=True)
        return out
    for entry in doc["triggers"]:
        # Ownership first, and by the field alone. An operator's own dated
        # one-shot is a shape the schema permits, and delivering theirs — or
        # deleting it afterwards — is exactly what inference used to do.
        if not _agent_owned(entry) or not _wellformed(entry):
            continue
        # A date trigger is one-shot BY DEFINITION, so membership is decided
        # on the type alone. Requiring the ``one_shot`` flag here as well
        # would mean an entry that somehow lacked it was skipped at
        # registration (past) AND skipped by the sweep — silently never
        # delivered. The schema forbids that shape; this does not depend on it.
        if entry.get("type") != "date":
            continue
        try:
            when = parse_at(entry.get("at", ""))
        except ValueError:
            logger.warning("reminder %s has an unparseable 'at'; skipping",
                           entry.get("name"))
            continue
        if when <= now:
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# The sweep — INV-TRIG-008
# ---------------------------------------------------------------------------


async def sweep_reminders(runtime, now: datetime) -> int:
    """Deliver every overdue one-shot reminder and remove it. Returns the
    number delivered.

    This is the backstop for the gap ``docs/architecture/triggers.md`` names
    outright: the scheduler is configured with no persistent job store, so an
    occurrence whose fire time fell while the process was down was never
    recorded anywhere and is otherwise simply lost. The misfire grace period
    bounds lateness for a RUNNING process; it cannot resurrect what was never
    recorded.

    Delivery happens BEFORE removal, so a removal failure redelivers on the
    next pass. That is at-least-once by choice (spec §8) — a duplicate nudge
    is a far better failure than a missed reminder. Conversely a FAILED
    delivery must not remove the entry: the reminder is still owed.
    """
    from bus import BusMessage, MessageType
    from log_cid import new_cid

    registry = getattr(runtime, "trigger_registry", None)

    delivered = 0
    for role in list(getattr(runtime, "role_configs", {}) or {}):
        # #398 release 2: triggers.yaml, NOT reminders.yaml. This is a change of
        # FILE, not merely of selector: the registry deliberately leaves a
        # past-dated date trigger unregistered for this sweep to deliver, so a
        # sweep left on the old path would find nothing and the reminder would
        # be silently never delivered — the exact failure #396 exists to
        # prevent.
        path = triggers_path(runtime.agents_dir, role)
        for entry in past_due(path, now):
            name = entry["name"]

            # Exclusive ownership. If the scheduler still holds a live job for
            # this reminder it WILL deliver it, so the sweep must not: for a
            # reminder whose time has only just passed, both are otherwise
            # eligible and the user gets it twice. After a restart there is no
            # job (they are memory-only and a past-dated one is never
            # registered), which is exactly when the sweep should act.
            if registry is not None and registry.has_job(role, name):
                continue

            # The sweep delivers the stored prompt verbatim — it has no
            # agent_dir to resolve a prompt_file against. The schema forbids
            # that combination, so an empty prompt here means a hand-edited or
            # corrupt entry. Refuse rather than send an empty message and
            # delete the evidence: a loud no-op beats silent loss.
            content = (entry.get("prompt") or "").strip()
            if not content:
                logger.warning(
                    "reminder sweep: %s has no prompt; leaving it in place "
                    "rather than delivering an empty message", name,
                )
                continue

            logger.info(
                "reminder sweep: delivering overdue %s for %s (due %s)",
                name, role, entry.get("at"),
            )
            try:
                await runtime.bus.send(BusMessage(
                    type=MessageType.SCHEDULED,
                    source="reminder-sweep",
                    target=role,
                    content=content,
                    channel=entry.get("channel", ""),
                    context={
                        "chat_id": f"date-{name}",
                        "trigger": name,
                        "cid": new_cid(),
                        "late": True,
                        # #485: the SAME rule the scheduler applies — a
                        # reminder delivered late (the restart case this sweep
                        # exists for) must be able to send exactly what it
                        # could have sent on time. #573: and under the same
                        # trigger-lifecycle epoch.
                        **scheduled_delivery_markers(
                            entry.get("channel", ""),
                            scheduled_asks.epoch_for(
                                role, f"date-{name}")),
                    },
                ))
            except Exception:  # noqa: BLE001
                # Still owed — leave the entry for the next pass.
                logger.warning(
                    "reminder sweep: delivery of %s failed; leaving it queued",
                    name, exc_info=True,
                )
                continue
            delivered += 1
            try:
                # Off the loop: remove_entry takes trigger_write_lock.PASS_LOCK,
                # which a config_sync pass may hold, and this sweep runs on the
                # scheduler loop — a blocking acquire here would stall it (#458).
                outcome = await asyncio.to_thread(remove_entry, path, name)
            except (OSError, ValueError):
                logger.warning(
                    "reminder sweep: could not remove %s after delivery; it "
                    "will be redelivered next pass", name, exc_info=True,
                )
            else:
                if outcome != "removed":
                    logger.warning(
                        "reminder sweep: %s reported %s after delivery; it "
                        "will be redelivered next pass", name, outcome,
                    )

        _reconcile_registrations(runtime, registry, role, path, now)

    return delivered


def _reconcile_registrations(runtime, registry, role: str, path: str,
                             now: datetime) -> None:
    """Re-register any agent-owned reminder that has no live job.

    The file is the truth; the scheduler is a cache of it. Anything that can
    make the two diverge — a reload re-registering a role from a snapshot
    taken before a reminder was written, a registration that failed
    transiently — is healed here rather than needing its own lock. Without
    this a recurring reminder lost that way would never fire again until the
    next restart, because only one-shots are recoverable by delivery.

    Both directions are bounded to ``managed_by: agent``. The operator's own
    triggers share this file now, and neither registering nor dropping one is
    this sweep's business.
    """
    if registry is None:
        return
    from config import TriggerSpec

    channels = list(getattr(
        getattr(runtime, "role_configs", {}).get(role), "channels", []) or [])
    if not channels:
        return

    entries = agent_entries(path)
    if entries is None:
        # File unreadable: neither direction is safe. Dropping would
        # unschedule live reminders; registering would work from nothing.
        return

    # Direction 1: an agent-owned job with no entry left must go. A
    # cancellation that raced a reload — which re-registers the role from a
    # snapshot taken before the cancellation — would otherwise leave the
    # reminder firing forever, even though cancel_reminder reported success.
    live_names = {e.get("name", "") for e in entries}
    # Only jobs whose spec RECORDS agent ownership are candidates for removal.
    # Provenance is carried as data, never inferred from the name — an operator
    # may legitimately author a `reminder-`-prefixed dated one-shot, and three
    # rounds of inference each found a new way to delete a live operator
    # trigger.
    try:
        registered = registry.agent_owned_job_names(role)
    except Exception:  # noqa: BLE001 - older registry without the accessor
        registered = []
    for name in registered:
        if name not in live_names:
            logger.info(
                "reminder sweep: dropping job %s for %s — no longer in "
                "triggers.yaml", name, role,
            )
            registry.remove_job_for(role, name)
            # #573: the entry is gone, so any question that job raised is
            # revoked with it (settled: keyboard retired, session told).
            scheduled_asks.revoke_trigger(role, name, "trigger_removed")

    # Direction 2: an entry with no job must be registered.
    for entry in entries:
        name = entry.get("name", "")
        if registry.has_job(role, name):
            continue
        if entry.get("type") == "date":
            # A past-dated one-shot is the sweep's to deliver, not to
            # register; a future one genuinely needs a job.
            try:
                if parse_at(entry.get("at", "")) <= now:
                    continue
            except ValueError:
                continue
        try:
            registry.register_agent(role, [TriggerSpec(
                name=name, type=entry.get("type", ""),
                schedule=entry.get("schedule", ""), at=entry.get("at", ""),
                one_shot=bool(entry.get("one_shot", False)),
                channel=entry.get("channel", ""),
                prompt=entry.get("prompt", ""),
                managed_by=OWNER_AGENT,
            )], channels)
            logger.info(
                "reminder sweep: re-registered %s for %s (no live job)",
                name, role,
            )
        except Exception:  # noqa: BLE001 - one bad entry must not stop the rest
            logger.warning(
                "reminder sweep: could not re-register %s for %s",
                name, role, exc_info=True,
            )
