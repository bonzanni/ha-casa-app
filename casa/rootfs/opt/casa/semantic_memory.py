# casa/rootfs/opt/casa/semantic_memory.py
"""Long-term semantic-memory seam (memory re-architecture spec §5).

A small interface shaped to a best-in-class backend (Hindsight), with a
NoOp degraded impl (recall unavailable, silent writes → the agent runs cold
on its SDK thread).
Reads return rendered markdown digests for the system prompt; ``retain``
is fire-and-forget (None). Short-term/recency is NOT here — that is owned
by the SDK session.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping

from personality_types import RecallHit, SensitivityTier
from recall_renderer import local_date_text

if TYPE_CHECKING:
    from timekeeping import RecallWindow


class RecallUnavailable(RuntimeError):
    """Semantic recall could NOT be performed — timeout, 5xx/429, transport
    failure, or a malformed response envelope.

    Three-outcome contract (v0.99.0): a recall is either (1) hits, (2) a
    genuine zero-hit '' from a well-formed 2xx response, or (3) this
    exception. Backends and bridges must never collapse a failure into '',
    which callers cannot tell from "searched and found nothing" — that is
    how agents end up truthfully-looking denying knowledge they have.

    ``reason`` is a stable slug (``timeout``, ``http_504``, ``transport``,
    ``malformed_envelope``, ``backend_error``) safe to log; it never carries
    query text or recalled content.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"recall unavailable: {reason}")


class RecallProtocolError(RecallUnavailable):
    """The recall backend answered with a 2xx envelope that could not be
    trusted as a structured result (personality Task 11).

    A subclass of :class:`RecallUnavailable` so every existing
    ``except RecallUnavailable`` still catches it and the three-outcome
    discipline is preserved: a malformed envelope, a hit that fails the
    per-field wire contract, or an all-hits-dropped-by-clearance response is
    NEVER reported as a genuine zero-hit — it means memory could not be read.

    ``reason`` is fixed to ``"protocol_error"``; ``detail`` is a bounded,
    enum-like tag (e.g. ``"results_missing_or_wrong_shape"``) that NEVER
    carries response content or query text."""

    def __init__(self, detail: str) -> None:
        super().__init__("protocol_error")
        self.detail = detail


class StoredTagsUnavailable(RuntimeError):
    """#1123: a document's stored tags could NOT be read — the answer was
    neither the document's tags nor the backend's positive "never saved".
    A save that needs the stored tier must not proceed on a guess, so the
    retain builder lets this (and every other read failure) fail the save.
    ``reason`` is bounded diagnostics (a status and the backend's detail),
    never memory content."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"stored tags unavailable: {reason}")


# #1126: the one line that opens a non-empty overlay. The summaries are written
# by the memory server from past conversations, on its own schedule, so they are
# leads to check, not live state — and a model covers only what its question
# asked, so nothing missing from it is evidence of absence.
MENTAL_MODEL_OVERLAY_LABEL = (
    "[memory-derived leads: summaries the memory server wrote from past "
    "conversations, each dated by its last refresh. Not live state — check "
    "anything that can change before relying on it — and not a complete list: "
    "something missing here is not evidence that it does not exist.]"
)


def parse_aware_timestamp(value: object) -> datetime | None:
    """A tz-aware ISO-8601 instant (``Z`` accepted), or ``None`` — never an
    error. A naive timestamp cannot be placed in the operator's day, so it is
    treated as absent."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def mental_model_refresh_paused(model: Mapping[str, Any]) -> bool:
    """#1126: the memory server's own predicate for "automatic refreshes of this
    model are paused" (Hindsight 0.10.2 ``_automatic_refresh_paused``): the last
    refresh failed AND (it never refreshed OR the failure is later than the last
    success). A model whose first refresh is still running (both null) is not
    paused. A failure time that cannot be read is not treated as one."""
    failed = parse_aware_timestamp(model.get("last_refresh_failed_at"))
    if failed is None:
        return False
    raw_refreshed = model.get("last_refreshed_at")
    if raw_refreshed is None:
        return True
    refreshed = parse_aware_timestamp(raw_refreshed)
    if refreshed is None:
        return False
    return failed > refreshed


def render_mental_models(response: dict[str, Any]) -> str:
    """Render a mental-model list response into the overlay digest (#1126).

    Each model with content renders under its name with the date of its last
    refresh in the operator's timezone, and says so when its automatic refreshes
    are paused by a failure. A model with no content (still on its first
    refresh) or no readable refresh date is skipped: the overlay never shows a
    summary whose age it cannot state. Total: odd items and fields are skipped,
    never raised on. Tolerant of the list key (``items`` is Hindsight's)."""
    resp = response if isinstance(response, dict) else {}
    models = resp.get("items") or resp.get("mental_models") or resp.get("models") or []
    if not isinstance(models, list):
        return ""
    blocks: list[str] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        refreshed = parse_aware_timestamp(m.get("last_refreshed_at"))
        if refreshed is None:
            continue
        name = next(
            (v.strip() for v in (m.get("name"), m.get("id"))
             if isinstance(v, str) and v.strip()),
            "mental model",
        )
        when = f"refreshed {local_date_text(refreshed)}"
        if mental_model_refresh_paused(m):
            failed = parse_aware_timestamp(m.get("last_refresh_failed_at"))
            when += (f"; its last refresh failed {local_date_text(failed)}, so it "
                     "may be out of date")
        blocks.append(f"### {name} ({when})\n{content.strip()}")
    if not blocks:
        return ""
    return "\n\n".join([MENTAL_MODEL_OVERLAY_LABEL, *blocks])


def render_recall(response: dict[str, Any]) -> str:
    """Render a Hindsight recall response into a markdown digest.

    Shape (spec §8 findings): ``{"results": [{"text": str, "type": str,
    "tags": [str], ...}, ...]}``. One bullet per fact; empty/missing →
    empty string (no placeholder lines)."""
    results = (response or {}).get("results") or []
    lines: list[str] = []
    for r in results:
        text = (r.get("text") or "").strip()
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)


@dataclass(frozen=True)
class MentalModelSpec:
    """#1126: one mental model Casa declares and reconciles on the backend. Tags
    are always empty (an untagged model reads the whole bank). ``trigger``
    holds ONLY the keys Casa declares: the backend stores a full trigger, and a
    key Casa does not declare is never compared or sent."""
    id: str
    name: str
    source_query: str
    max_tokens: int
    trigger: Mapping[str, Any] = field(default_factory=dict)


class SemanticMemory(ABC):
    """Long-term memory backend. Banks are addressed by id (see hindsight_ids)."""

    @abstractmethod
    async def retain(
        self, bank: str, items: list[dict[str, Any]], *, async_: bool = True,
    ) -> None:
        """Persist memory items into ``bank`` (LLM fact-extraction, async by
        default). Each item: ``{content(req), context, timestamp, tags,
        metadata, document_id}``."""

    @abstractmethod
    async def recall(
        self, bank: str, query: str, *, tags: list[str], max_tokens: int,
        types: tuple[str, ...] = ("world", "experience", "observation"),
        tags_match: str = "any", budget: str = "mid",
    ) -> str:
        """Return a rendered digest of facts relevant to ``query`` in ``bank``.
        ``types`` MUST keep ``world`` or raw facts are dropped (spec §8.9)."""

    @abstractmethod
    async def recall_items(
        self, bank: str, query: str, *, tags: list[str], max_tokens: int,
        clearance: SensitivityTier,
        types: tuple[str, ...] = ("world", "experience", "observation"),
        tags_match: str = "any", budget: str = "mid",
        window: RecallWindow | None = None,
    ) -> tuple[RecallHit, ...]:
        """Typed, attributed recall (personality Task 11): decode each hit's
        sensitivity tier + speaker provenance and return trustworthy,
        readable :class:`RecallHit`s at or below ``clearance``.

        ADDITIVE — ``recall`` above is untouched and remains available. Same
        three-outcome contract: an empty tuple is returned ONLY for a
        well-formed 2xx response whose ``results`` is an actual empty list;
        a malformed envelope or a response whose hits are all dropped by the
        clearance/wire contract raises :class:`RecallProtocolError`; every
        transport/HTTP failure raises :class:`RecallUnavailable`.

        #1120: ``window`` is a RANKING HINT for the backend and nothing more —
        the seam does not filter by it, so the contract above is unchanged and
        hits recorded outside the window may come back. A caller that promises
        "only from this period" filters the hits itself (``recall_memory``).
        Callers pass it only when they have one."""
        raise NotImplementedError

    @abstractmethod
    async def document_tags(self, bank: str, document_id: str) -> frozenset[str] | None:
        """#1123: the tags ``bank`` currently stores for ``document_id``, or
        None when the backend positively reports that document was never saved.
        Every other outcome RAISES (:class:`StoredTagsUnavailable` or the
        transport error): the retain builder reads the stored tier through this
        before each save, and an unknown answer read as "never saved" would
        let a save lower that tier. Abstract with no default on purpose — a
        default of None would be exactly that fail-open read."""

    @abstractmethod
    async def profile(self, bank: str) -> str:
        """Return the bank's mental-model overlay digest (cheap GET, no LLM)."""

    async def delete_bank(self, bank: str) -> bool:
        """#411: irreversibly delete ``bank`` and everything in it. Returns
        True when the backend performed a deletion, False when there was
        nothing to delete (NoOp). Like ``retain``, the seam itself enforces
        NO policy (INV-MEM-005 discipline) — operator consent and writer
        quiescing live entirely at the callers (:mod:`memory_wipe`); nothing
        but the wipe orchestrator may call this."""
        return False

    async def reconcile_mental_models(
        self, bank: str, declared: tuple[MentalModelSpec, ...],
    ) -> None:
        """#1126: make ``bank``'s Casa-owned mental models match ``declared`` —
        create the missing, re-define the drifted, delete undeclared ids under
        the reserved ``casa-`` prefix, never touch any other id. Best-effort:
        callers (:mod:`mental_models`) log a failure and carry on.

        Concrete no-op default on purpose (a backend without mental models has
        nothing to reconcile), unlike the abstract ``document_tags``, whose
        default would fail open. An abstract method here would also make every
        ABC test double unbuildable for a reason unrelated to what it tests."""
        return None

    async def close(self) -> None:
        """Release any backend resources (e.g. a pooled HTTP session).

        Concrete (non-abstract) no-op default so backends that hold nothing
        (NoOpSemanticMemory) need not override it; HTTP-backed backends
        override to close their shared client session on shutdown."""
        return None


class NoOpSemanticMemory(SemanticMemory):
    """Degraded backend: retain is silent, profile returns ''. The agent then
    runs on its SDK thread alone (cold long-term).

    ``recall`` raises :class:`RecallUnavailable` (v0.99.0): a NoOp cannot
    CHECK memory, and returning '' would fabricate a genuine-looking zero-hit
    search — agents would then claim "nothing found" where no search ever
    ran. The overlay (``profile``) stays a silent '' because nothing claims
    absence from a missing overlay."""

    async def retain(
        self, bank: str, items: list[dict[str, Any]], *, async_: bool = True,
    ) -> None:
        return None

    async def recall(
        self, bank: str, query: str, *, tags: list[str], max_tokens: int,
        types: tuple[str, ...] = ("world", "experience", "observation"),
        tags_match: str = "any", budget: str = "mid",
    ) -> str:
        raise RecallUnavailable("not_configured")

    async def recall_items(
        self, bank: str, query: str, *, tags: list[str], max_tokens: int,
        clearance: SensitivityTier,
        types: tuple[str, ...] = ("world", "experience", "observation"),
        tags_match: str = "any", budget: str = "mid",
        window: RecallWindow | None = None,
    ) -> tuple[RecallHit, ...]:
        raise RecallUnavailable("not_configured")

    async def document_tags(self, bank: str, document_id: str) -> frozenset[str] | None:
        # Nothing is ever stored here (``retain`` is silent), so "never saved"
        # is literally true — and a writer must not fail on it.
        return None

    async def profile(self, bank: str) -> str:
        return ""
