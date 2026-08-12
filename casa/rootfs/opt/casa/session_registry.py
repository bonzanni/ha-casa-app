"""Persistent session registry backed by a JSON file."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import itertools
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from atomic_io import PRIVATE, atomic_write_json
from personality_types import SpeakerProvenance
from speaker_provenance import provenance_mapping, validate_speaker_provenance

logger = logging.getLogger(__name__)

_SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SESSION_KEY_MAX = 100  # mirrors the server-side max_length from the former upstream dependency

# Sentinel distinguishing "remove unconditionally" (argument omitted) from
# "remove only while the entry still has NO session id" (explicit None) —
# needed because a guarded caller's snapshot may itself lack a sid (#353).
_UNCONDITIONAL = object()

# #526: process-local registration-generation allocator. Generations are
# NEVER persisted — a restarted process's counter restarts, so a persisted
# value could collide with a fresh allocation and re-open the same-sid ABA
# this closes. An entry loaded from disk therefore reads generation ``None``
# (see :meth:`SessionRegistry.generation`), which no live ``register()`` can
# ever re-produce. Module-level so every registry instance in a process
# allocates from one strictly-increasing sequence.
_GENERATION_COUNTER = itertools.count(1)

# v2 scoped-key schema (spec A2): channel-role-scope identity, collision-safe
# across residents sharing a device/channel. See build_scoped_session_key.
SESSION_KEY_SCHEMA_V2 = "v2"


def _is_uuid_scope(scope_id: str) -> bool:
    """True when `scope_id` parses as a UUID (any version).

    Distinguishes a webhook one-shot (random chat_id fabricated by
    `build_invoke_message`) from a deliberately-pinned webhook session
    (e.g. `webhook-ha-automation-daily`). Only the former qualifies for
    the short `webhook_session_ttl_days` (spec A2). The classification
    is made on the RAW pre-hash scope (a v2 key's hashed remainder is
    never uuid-shaped) and persisted as ``scope_class`` at register()."""
    try:
        uuid.UUID(scope_id)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _build_key(*parts: str) -> str:
    """Join ``parts`` with ``-`` into a resource-name-safe key.

    Each part must be a non-empty ``str`` matching ``[A-Za-z0-9_-]+``; the
    joined result must be ≤ 100 chars. Raises ``ValueError`` otherwise.
    Silent sanitization is intentionally NOT supported (a ``:`` or any other
    out-of-charset char fails fast). Replaces the former upstream session-id
    builder dependency; output format is unchanged.
    """
    if not parts:
        raise ValueError("_build_key requires at least one part")
    for i, part in enumerate(parts):
        if not isinstance(part, str):
            raise ValueError(f"part {i} must be str, got {type(part).__name__}")
        if not part:
            raise ValueError(f"part {i} is empty")
        if not _SESSION_KEY_RE.fullmatch(part):
            raise ValueError(
                f"part {i}={part!r} contains characters outside [A-Za-z0-9_-]"
            )
    joined = "-".join(parts)
    if len(joined) > _SESSION_KEY_MAX:
        raise ValueError(f"session key {joined!r} is {len(joined)} chars; max {_SESSION_KEY_MAX}")
    return joined


def build_session_key(channel: str, scope_id: str | int | None) -> str:
    """Build a canonical channel key of the form ``{channel}-{scope_id}``.

    Used internally by :class:`SessionRegistry` (JSON-on-disk dict
    keyed by this string) AND as the channel-key prefix for session ids
    (see :func:`_build_key`).

    ``scope_id`` may be ``int`` (Telegram ``chat_id``) or ``str``
    (voice ``scope_id``); coerced to ``str``. ``None`` or falsy values
    map to ``"default"``.

    Raises ``ValueError`` when ``channel`` is empty or when either
    part contains characters outside ``[A-Za-z0-9_-]``. The ``:``
    separator is forbidden in inputs to prevent silent regression to
    invalid ids.
    """
    if not channel:
        raise ValueError("channel is required")
    sid = scope_id if scope_id else "default"
    return _build_key(channel, str(sid))


def build_scoped_session_key(channel: str, role: str, scope_id: str | int | None) -> str:
    """Collision-safe (channel, role, scope) identity, channel-FIRST so
    existing ``key.partition('-')[0]`` consumers still read the channel
    (spec A2). Two residents sharing one device/channel scope (e.g. voice
    butler + concierge on the same kitchen satellite) get DISTINCT keys —
    the v1 ``build_session_key`` collided on ``(channel, scope)`` alone.

    Format: ``{channel}-v2-{sha256(channel\\x00role\\x00scope)[:24]}``.
    """
    if not channel:
        raise ValueError("channel is required")
    if not role:
        raise ValueError("role is required")
    sid = str(scope_id) if scope_id else "default"
    digest = hashlib.sha256(f"{channel}\x00{role}\x00{sid}".encode()).hexdigest()[:24]
    return _build_key(channel, SESSION_KEY_SCHEMA_V2, digest)


class SessionRegistry:
    """Maps channel keys to session metadata and persists to disk.

    All disk I/O is offloaded to a thread via :func:`asyncio.to_thread`
    to avoid blocking the event loop. A single :class:`asyncio.Lock`
    serialises every mutate+save so concurrent bus tasks cannot clobber
    each other's writes (spec 5.1 §3).
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._reset_listeners: list = []
        # #526: in-memory only — an entry loaded from disk has NO generation
        # (reads ``None``), and no live registration can allocate ``None``.
        self._generations: dict[str, int] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if not isinstance(loaded, dict):
                    raise ValueError(f"expected dict, got {type(loaded).__name__}")
                # #345: validate each ENTRY too — a structurally corrupt value
                # (list/str/None) would otherwise crash register()'s
                # deepcopy+update, abort every sweeper pass, and break
                # _save_locked's per-entry dict() copy for ALL entries. Losing
                # the bad pointer is recoverable (that scope starts a fresh
                # session); a wedged registry is not.
                corrupt = [k for k, v in loaded.items() if not isinstance(v, dict)]
                for key in corrupt:
                    loaded.pop(key)
                if corrupt:
                    logger.error(
                        "sessions.json: quarantined %d structurally corrupt "
                        "entr(y|ies) %s — their sessions start fresh",
                        len(corrupt), corrupt,
                    )
                self._data = loaded
            except (json.JSONDecodeError, OSError, ValueError):
                # Corrupt/unreadable registry (e.g. truncated by power loss or
                # OOM-kill mid-write). Losing session pointers is recoverable —
                # the fleet just starts fresh sessions; a boot crash-stop is
                # not. Quarantine the bad file for diagnosis and start empty.
                logger.error(
                    "sessions.json is corrupt or unreadable; moving it to "
                    "%s.corrupt and starting with an empty registry", path,
                )
                try:
                    os.replace(path, f"{path}.corrupt")
                except OSError:
                    pass
                self._data = {}

    async def register(
        self,
        channel_key: str,
        agent: str,
        sdk_session_id: str,
        scope_class: str | None = None,
        *,
        binding_digest: str,
        speaker_provenance: SpeakerProvenance,
        user_provenance: SpeakerProvenance,
    ) -> None:
        """Register (or overwrite) a session entry and persist.

        The session ID is *not* tracked here in the v0.17.1
        topology: it is derived at call time via :func:`_build_key`.

        ``scope_class`` persists a caller-determined classification (spec
        A2) — currently only ``"webhook_oneshot"`` — onto the entry so
        :mod:`session_sweeper` can read it back at sweep time instead of
        re-deriving it from the (now-hashed, never-uuid-shaped) v2 key.

        Personality Task 9: ``binding_digest``/``speaker_provenance``/
        ``user_provenance`` are REQUIRED keyword-only additive fields —
        every session-resume path now gates on
        ``{stable_agent_id, role_checksum, binding_digest}``, and an entry
        with an unset provenance could not later be distinguished from a
        legitimate system/anonymous turn (the bug this closes). Both
        provenances are validated (fail-fast on a malformed identity) and
        stored as their canonical v1 mappings. Merged onto a deep copy of any
        prior entry so a concurrent in-flight ``SpeakerProvenance``/dict can
        never mutate the persisted snapshot.
        """
        validate_speaker_provenance(speaker_provenance)
        validate_speaker_provenance(user_provenance)
        async with self._lock:
            entry: dict[str, Any] = copy.deepcopy(
                self._data.get(channel_key) or {},
            )
            entry.update({
                "agent": agent,
                "sdk_session_id": sdk_session_id,
                "last_active": datetime.now(timezone.utc).isoformat(),
                "binding_digest": binding_digest,
                "speaker_provenance": provenance_mapping(speaker_provenance),
                "user_provenance": provenance_mapping(user_provenance),
            })
            # A fresh registration supersedes any in-flight save claim.
            entry.pop("consolidated_at", None)
            if scope_class is None:
                entry.pop("scope_class", None)
            else:
                entry["scope_class"] = scope_class
            self._data[channel_key] = entry
            # #526: stamp a fresh registration generation (in-memory only —
            # never serialized) so the conditional guards below can tell a
            # same-sid re-registration from the snapshot their caller took.
            self._generations[channel_key] = next(_GENERATION_COUNTER)
            await self._save_locked()

    def purge_webhook_sessions(self) -> int:
        """Release A / Layer 4: PURGE every persisted webhook session entry
        at boot, returning how many were dropped. A restarted process cannot
        know a stored webhook session's origin route (operator-signed
        ``invoke`` vs untrusted ``webhook_trigger``), so it must never be
        resumed or treated as trusted; fresh dispatches start clean. (New
        webhook one-shots still acquire scope_class + the short TTL at
        register().) Synchronous — the caller is expected to ``await save()``
        afterwards if anything changed; this runs once at boot before
        concurrent access begins."""
        dropped = 0
        for key in list(self._data):
            if key.startswith("webhook-"):
                self._data.pop(key)
                self._generations.pop(key, None)
                dropped += 1
        return dropped

    def get(self, channel_key: str) -> dict[str, Any] | None:
        """Return the entry for *channel_key*, or ``None``."""
        return self._data.get(channel_key)

    def generation(self, channel_key: str) -> int | None:
        """Return the entry's registration generation (#526).

        ``None`` for a missing key AND for an entry loaded from disk that no
        in-process ``register()`` has touched — generations are process-local
        and never persisted, so "no generation" is itself meaningful: a
        guarded caller that snapshotted ``None`` declines the moment any real
        registration lands. Read this in the SAME no-await block as the
        :meth:`get` snapshot it accompanies."""
        return self._generations.get(channel_key)

    async def touch(self, channel_key: str) -> None:
        """Update ``last_active`` for an existing entry and persist."""
        async with self._lock:
            entry = self._data.get(channel_key)
            if entry is not None:
                entry["last_active"] = datetime.now(timezone.utc).isoformat()
                await self._save_locked()

    def _generation_moved(
        self, channel_key: str, expected_generation: object,
    ) -> bool:
        """#526 shared guard: True when the caller supplied a generation and
        the entry's current generation differs — i.e. a registration landed
        after the caller's snapshot (even one carrying the SAME sid, the ABA
        the sid guards cannot see). Caller must hold ``self._lock``."""
        return (
            expected_generation is not _UNCONDITIONAL
            and self._generations.get(channel_key) != expected_generation
        )

    async def remove(
        self, channel_key: str, expected_sid: object = _UNCONDITIONAL,
        *, expected_generation: object = _UNCONDITIONAL,
    ) -> None:
        """Remove an entry and persist.

        With ``expected_sid``, remove only while the entry still carries that
        session id — like ``finish_save``/``clear_save_claim``, a newer
        registration (a turn that landed between the caller's snapshot and
        this call) is left intact instead of being silently deleted (#317,
        #353). ``expected_sid=None`` means the snapshotted entry had NO
        session id, so the removal declines once any real session registers.
        ``expected_generation`` (#526) additionally declines when ANY
        registration — same sid included — landed after the caller's
        snapshot. Omitting either argument keeps that guard unconditional."""
        async with self._lock:
            if self._generation_moved(channel_key, expected_generation):
                return  # re-registered after the snapshot; leave it alone
            if expected_sid is not _UNCONDITIONAL:
                entry = self._data.get(channel_key)
                if entry is None or entry.get("sdk_session_id") != expected_sid:
                    return  # re-registered by a newer session; leave it alone
            self._data.pop(channel_key, None)
            self._generations.pop(channel_key, None)
            await self._save_locked()

    async def clear_sdk_session(
        self, channel_key: str, expected_sid: object = _UNCONDITIONAL,
        *, expected_generation: object = _UNCONDITIONAL,
    ) -> None:
        """Drop the ``sdk_session_id`` field for a key; keep other metadata.

        Used by the resume-failure recovery path in :mod:`agent` when
        claude CLI rejects a ``--resume <sid>`` with ``ProcessError``
        (spec 5.8 §3.1). The entry itself is NOT removed —
        ``last_active`` stays so the session sweeper still gates on
        age, and subsequent turns on the same key see an entry without
        a session id and start a fresh SDK conversation.

        With ``expected_sid``, clear only while the entry still carries that
        session id — like ``remove``, a newer registration (a concurrent
        same-key turn that landed between the resume failure and this call)
        is left intact instead of being silently dropped (#349, same
        check-then-act family as #317/#353). ``expected_generation`` (#526)
        closes the residual that guard could not: a concurrent turn that
        re-registered the SAME sid still moved the generation, so the clear
        declines and that session's continuity + save-time consolidation
        survive. Omitting an argument keeps that guard unconditional.

        No-op when the key does not exist, or when the entry has no
        ``sdk_session_id`` field (idempotent).
        """
        async with self._lock:
            if self._generation_moved(channel_key, expected_generation):
                return  # re-registered after the snapshot; leave it alone
            entry = self._data.get(channel_key)
            if entry is None:
                return
            if (
                expected_sid is not _UNCONDITIONAL
                and entry.get("sdk_session_id") != expected_sid
            ):
                return  # re-registered by a newer session; leave it alone
            if "sdk_session_id" in entry:
                entry.pop("sdk_session_id", None)
                await self._save_locked()

    async def try_begin_save(
        self, channel_key: str,
        *, expected_generation: object = _UNCONDITIONAL,
    ) -> bool:
        """Atomically claim a session for saving. Returns True for the first
        caller (sets ``consolidated_at``), False if missing or already claimed.
        The ``consolidated_at`` marker persists (it is saved to disk); a save
        that crashes after claiming but before ``finish_save`` leaves the marker
        set — the FreshnessReaper's stale-claim recovery (a later task) detects a
        marker older than its retry window and re-opens the claim. ``finish_save``
        removes the entry only on success.

        ``expected_generation`` (#526, Sol design-r2): the claim must be
        generation-gated BEFORE it mutates — an unguarded claim can land on a
        registration NEWER than the caller's snapshot, and the guarded
        clear/remove downstream would then decline forever, stranding the
        fresh entry claimed until stale-claim recovery."""
        async with self._lock:
            if self._generation_moved(channel_key, expected_generation):
                return False  # re-registered after the snapshot; don't claim
            entry = self._data.get(channel_key)
            if entry is None or entry.get("consolidated_at"):
                return False
            entry["consolidated_at"] = datetime.now(timezone.utc).isoformat()
            await self._save_locked()
            return True

    async def finish_save(
        self, channel_key: str, sdk_session_id: str | None = None,
        *, expected_generation: object = _UNCONDITIONAL,
    ) -> None:
        """Remove the entry after a successful retain (the session is now
        long-term) — but only if it still belongs to the session that was
        saved. If register() replaced the entry with a NEW sid mid-save (a
        user turn landed during a slow reaper save), leave the new
        registration intact; otherwise we would silently delete a fresh
        session's pointer (M24). ``expected_generation`` (#526) extends that
        to a mid-save re-registration of the SAME sid. Passing
        ``sdk_session_id=None`` preserves the old unconditional behavior.
        Idempotent."""
        async with self._lock:
            if self._generation_moved(channel_key, expected_generation):
                return  # re-registered after the snapshot; leave it alone
            entry = self._data.get(channel_key)
            if entry is None:
                return
            if (
                sdk_session_id is not None
                and entry.get("sdk_session_id") != sdk_session_id
            ):
                return  # re-registered by a newer session; leave it alone
            self._data.pop(channel_key, None)
            self._generations.pop(channel_key, None)
            await self._save_locked()

    async def clear_save_claim(
        self, channel_key: str, sdk_session_id: str | None = None,
        *, expected_generation: object = _UNCONDITIONAL,
    ) -> None:
        """Release a save claim after a FAILED retain so the next reaper cycle
        retries. Keeps the entry. Like ``finish_save``, only clears the claim
        if the entry still belongs to the saved session — a newer
        registration (which already wiped ``consolidated_at``) is left
        untouched (M24); ``expected_generation`` (#526) extends that to a
        same-sid re-registration."""
        async with self._lock:
            if self._generation_moved(channel_key, expected_generation):
                return  # re-registered after the snapshot; leave it alone
            entry = self._data.get(channel_key)
            if entry is None or "consolidated_at" not in entry:
                return
            if (
                sdk_session_id is not None
                and entry.get("sdk_session_id") != sdk_session_id
            ):
                return
            entry.pop("consolidated_at", None)
            await self._save_locked()

    async def save(self) -> None:
        """Public save: acquires the lock.

        For out-of-band callers (tests, future background snapshotters).
        Internal mutators call :meth:`_save_locked` while already
        holding the lock.
        """
        async with self._lock:
            await self._save_locked()

    async def _save_locked(self) -> None:
        """Persist current data. Caller MUST hold ``self._lock``."""
        # Per-entry copy, not a shallow dict() of the outer map: the write runs
        # in a thread (the lock is released while we await it), so a concurrent
        # mutator that acquires the lock could otherwise mutate an inner entry
        # dict mid-serialize. The save model relies on sdk_session_id/consolidated_at
        # persisting atomically (crash-safety, spec §4.2 / C3). Entries are flat
        # str→str|None maps, so a one-level copy is sufficient and cheap.
        data = {k: dict(v) for k, v in self._data.items()}
        await asyncio.to_thread(self._write, data)

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        """Synchronous write helper, runs in thread pool.

        Atomic (temp-file + fsync + ``os.replace`` via :mod:`atomic_io`) so a
        crash mid-write can never leave a truncated sessions.json — a corrupt
        file used to crash-stop the add-on on boot (H12).
        """
        atomic_write_json(self._path, data, indent=2, mode=PRIVATE)

    def all_entries(self) -> dict[str, dict[str, Any]]:
        """Return a shallow copy of all entries."""
        return dict(self._data)

    def add_reset_listener(self, cb):
        """Register an async callback(channel_key) fired by notify_reset
        BEFORE an explicit reset saves the transcript (AR-4: gives the SDK
        client pool a chance to close — and thereby flush — the warm
        subprocess for that key). Returns an unsubscribe callable."""
        self._reset_listeners.append(cb)

        def _unsub() -> None:
            try:
                self._reset_listeners.remove(cb)
            except ValueError:
                pass
        return _unsub

    async def notify_reset(self, channel_key: str) -> None:
        """Best-effort fan-out; a listener failure must never block a reset."""
        for cb in list(self._reset_listeners):
            try:
                await cb(channel_key)
            except Exception:  # noqa: BLE001
                logger.exception("reset listener failed for %s", channel_key)
