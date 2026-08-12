"""Personality Phase A, Task 14: the lean, privacy-safe explanation store.

Holds ephemeral per-turn "why did the agent answer this way" records —
role identity, binding provenance, the compiled-prompt digest that was
actually sent, memory attributions, tool calls, and denials — so an
operator can inspect a single correlation id's provenance without
re-deriving it from logs.

Privacy is enforced at the STORE boundary, not by the caller:

* :meth:`ExplanationStore.record` REJECTS (raises ``ValueError``) any
  record whose JSON encoding contains a reserved ``casa-source-``
  provenance tag (spec: reserved tags are Hindsight-internal and must
  never leak into an operator-facing surface).
* :meth:`ExplanationStore.get` strips ``system_prompt``/``memory_text``
  AND the ``memory_tiers`` sensitivity-tier metadata (GH #202) by default;
  only ``show_sensitive=True`` (gated by the admin route's ``confirmed=true``
  requirement, and by ``casactl``'s interactive ``SHOW`` confirmation)
  returns them. ``memory_attributions`` stay visible by default — they are
  already clearance/surface-gated identity labels (never tier tokens, never
  reserved ``casa-source-`` tags).

Storage is atomic (temp file + chmod 0600 + ``os.replace``), TTL-pruned
(24h), and capped at 1000 records — this is a debugging aid, not durable
storage; restart or TTL expiry losing a record is fine.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

EXPLANATION_TTL_SECONDS = 86400
EXPLANATION_MAX_RECORDS = 1000
_CID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SENSITIVE = {"system_prompt", "memory_text"}
# GH #202: the memory sensitivity-tier tokens are metadata that reveals which
# clearance tiers a turn's recall touched — gated behind the SAME explicit
# show_sensitive confirmation as the prompt/memory prose (attribution labels,
# which are already clearance-gated, stay visible).
_SENSITIVE_TIER_META = {"memory_tiers"}


@dataclass(frozen=True, slots=True)
class ExplanationRecord:
    correlation_id: str
    role_id: str
    kind: str
    resolved_model: str
    persona_ref: str | None
    role_checksum: str
    binding_digest: str | None
    dependency_digests: tuple[str, ...]
    effective_config_digest: str | None
    lifecycle_state: str | None
    projection: str
    static_prompt_digest: str
    static_prompt_estimated_tokens: int
    memory_tiers: tuple[str, ...]
    memory_attributions: tuple[str, ...]
    tool_calls: tuple[str, ...]
    denials: tuple[str, ...]
    system_prompt: str | None = None
    memory_text: str | None = None


class ExplanationStore:
    """One JSON file per correlation id under ``root``.

    ``now`` is injectable (defaults to :func:`time.time`) so tests can
    control TTL/prune behavior deterministically without patching the
    module-global ``time.time`` or any ``asyncio.sleep`` (memory-cage
    rule — see CLAUDE.md).

    F5 (whole-branch review, round 2): :meth:`record`/:meth:`get`/:meth:`prune`
    now run on WORKER THREADS concurrently (``agent.py`` offloads the per-turn
    write via ``asyncio.to_thread``). Two guards make that safe: (1) an
    instance-level ``threading.Lock`` serializes every file operation, so a
    prune racing a record can never unlink a half-published file or read a
    partially-written one; (2) each write stages through a PER-WRITE UNIQUE
    temp filename (``<cid>.<uuid>.tmp``), so two concurrent ``record()`` calls
    for the SAME correlation id never collide on one shared temp path — each
    still finishes with the atomic ``os.replace`` publish (last writer wins,
    both readable-or-latest, never a torn file).
    """

    def __init__(self, root: Path = Path("/data/explanations"), *, now: Callable[[], float] = time.time) -> None:
        self._root = root
        self._now = now
        self._lock = threading.Lock()

    def _path(self, correlation_id: str) -> Path:
        if not isinstance(correlation_id, str) or not _CID.fullmatch(correlation_id):
            raise ValueError("invalid correlation id")
        return self._root / f"{correlation_id}.json"

    def record(self, record: ExplanationRecord) -> None:
        payload = asdict(record)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if "casa-source-" in encoded:
            raise ValueError("reserved provenance tags cannot enter explanations")
        path = self._path(record.correlation_id)
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            # F5: a per-write unique temp name (never the shared
            # `<cid>.json.tmp`) so concurrent writes for the same cid don't
            # clobber each other's staging file mid-flight. `.tmp`-suffixed, so
            # prune's `*.json` glob never sees it.
            temporary = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(encoded + "\n", encoding="utf-8")
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
            except BaseException:
                # GH #356: a failure between staging and publish must not
                # leave the temp file behind — it carries the full sensitive
                # record (system_prompt/memory_text) and the prune sweep is
                # what bounds this directory. After a successful os.replace
                # the temp no longer exists and this unlink is a no-op.
                temporary.unlink(missing_ok=True)
                raise
            # TTL/prune read the file's mtime as the record's age. Force it to
            # the injectable `now` (not whatever the real OS clock says) so a
            # test-supplied `now` callable drives TTL/prune deterministically —
            # os.utime accepts any epoch float, real or test-fictional.
            now = self._now()
            os.utime(path, (now, now))
            self._prune_locked()

    def get(self, correlation_id: str, *, show_sensitive: bool = False) -> dict[str, object]:
        path = self._path(correlation_id)
        with self._lock:
            try:
                mtime = path.stat().st_mtime
            except OSError as exc:
                raise KeyError(correlation_id) from exc
            if self._now() - mtime > EXPLANATION_TTL_SECONDS:
                raise KeyError(correlation_id)
            value = json.loads(path.read_text(encoding="utf-8"))
        if not show_sensitive:
            for key in (*_SENSITIVE, *_SENSITIVE_TIER_META):
                value.pop(key, None)
        return value

    def prune(self) -> None:
        with self._lock:
            self._prune_locked()

    def _prune_locked(self) -> None:
        """Prune body; the caller MUST already hold ``self._lock`` (``record``
        calls this while holding the lock, so a non-reentrant Lock stays
        deadlock-free — never re-acquire here)."""
        if not self._root.is_dir():
            return
        # GH #356: sweep orphaned staging files. All writers stage under
        # ``self._lock`` and publish (os.replace) or unlink before releasing
        # it, so any ``*.tmp`` visible while HOLDING the lock is an orphan —
        # a crashed process or a pre-fix failure — carrying sensitive
        # content: delete immediately, no TTL grace.
        for orphan in self._root.glob("*.tmp"):
            orphan.unlink(missing_ok=True)
        files = sorted(self._root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        cutoff = self._now() - EXPLANATION_TTL_SECONDS
        for index, path in enumerate(files):
            if index >= EXPLANATION_MAX_RECORDS or path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
