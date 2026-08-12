# casa/rootfs/opt/casa/hindsight_memory.py
"""Hindsight HTTP implementation of SemanticMemory (spec §4, verified §8).

Talks to the bank API at ``{base_url}/v1/default/banks/{bank}/...``. The
base URL is configurable (the add-on is reachable via its hassio network
alias / IP, NOT the literal host ``hindsight`` -- spec §8.8). API is
unauthenticated on the internal network (spec §8.4).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from hindsight_ids import bank_id as _validate_bank_id  # fail-fast on bad ids
from personality_types import RecallHit
from semantic_memory import (
    RecallProtocolError,
    RecallUnavailable,
    SemanticMemory,
    render_mental_models,
    render_recall,
)
from speaker_provenance import RESERVED_SOURCE_NAMESPACE, decode_provenance_from_tags

logger = logging.getLogger(__name__)

_DEFAULT_TYPES = ("world", "experience", "observation")

# Access ladder (design §2): a hit is readable iff its tier rank is <= the
# reader's clearance rank. Mirrors sensitivity.TIERS, kept local so the decode
# has no import cycle risk against the tools/agent graph.
_TIER_RANK = {"public": 0, "friends": 1, "family": 2, "private": 3}


def _decode_sensitivity(wire_tags: tuple[str, ...], *, clearance: str) -> str | None:
    """Return the hit's sensitivity tier, or None if it is unreadable.

    A trustworthy hit carries EXACTLY ONE tier token; zero or multiple tier
    tokens is ambiguous → drop (None). A tier above ``clearance`` is not
    readable by this context → drop (None). The single-occurrence rule is a
    provenance-integrity gate, not a leak-safe default: a dropped hit never
    surfaces, so it can never leak."""
    occurrences = [tag for tag in wire_tags if tag in _TIER_RANK]
    if len(occurrences) != 1:
        return None
    tier = occurrences[0]
    if _TIER_RANK[tier] > _TIER_RANK.get(clearance, -1):
        return None
    return tier


class HindsightSemanticMemory(SemanticMemory):
    def __init__(self, base_url: str, *, timeout_s: float = 20.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        # Lazily created inside a running event loop (tests construct this
        # object synchronously). One ClientSession is reused across calls, but
        # its connector uses ``force_close`` (see _new_session) so no TCP
        # connection is pooled between calls.
        self._session: aiohttp.ClientSession | None = None

    def _new_session(self) -> aiohttp.ClientSession:
        # D-3 (2026-07-11): the client previously pooled keep-alive
        # connections. Memory round-trips are sparse and bursty (1-2 per turn,
        # turns minutes+ apart), so a pooled connection was almost always idle
        # past Hindsight's keep-alive window; the FIRST round-trip of a turn
        # (the recall) then reused a half-closed socket and raised
        # ServerDisconnectedError on ``await protocol.read()``, silently
        # degrading memory for hours while the same-turn retain (fresh
        # connection) still succeeded. ``force_close`` opens one fresh
        # connection per call — correct for this traffic shape, and the
        # keep-alive it dropped was never actually reused between turns anyway.
        return aiohttp.ClientSession(
            timeout=self._timeout,
            connector=aiohttp.TCPConnector(force_close=True),
        )

    async def _roundtrip(
        self, method: str, url: str, payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        assert self._session is not None  # set by _request before calling
        async with self._session.request(method, url, json=payload) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One HTTP round-trip -> parsed JSON. Raises aiohttp errors to caller
        (callers degrade to '' / log per the existing memory-call rule)."""
        url = f"{self._base}{path}"
        if self._session is None or self._session.closed:
            self._session = self._new_session()
        try:
            return await self._roundtrip(method, url, payload)
        except aiohttp.ClientConnectionError:
            # Belt to force_close's root-cause fix: a genuine mid-call drop
            # (ServerDisconnectedError / ClientOSError, both subclasses) means
            # no response was received, so aiohttp has discarded the dead
            # transport and a single retry gets a fresh connection. Scoped to
            # connection errors ONLY: an HTTP 4xx/5xx (ClientResponseError, not
            # a ClientConnectionError) means the request WAS received, so a
            # retained write may have landed — retrying it could double-write.
            return await self._roundtrip(method, url, payload)

    async def close(self) -> None:
        """Close the shared client session (called on shutdown so aiohttp
        does not emit an 'Unclosed client session' warning)."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def retain(
        self, bank: str, items: list[dict[str, Any]], *, async_: bool = True,
    ) -> None:
        # ``bank`` is already a built id (e.g. "casa-assistant"); a single-part
        # bank_id() call re-validates charset + length and raises ValueError on
        # a malformed id before any HTTP (Hindsight silently accepts bad ids).
        _validate_bank_id(bank)
        await self._request(
            "POST", f"/v1/default/banks/{bank}/memories",
            {"async": async_, "items": items},
        )
        # E1 (observability): retains were previously silent on success, so a
        # working memory write left no trace in the logs — only failures logged.
        logger.info(
            "memory_retain bank=%s items=%d async=%s", bank, len(items), async_,
        )

    async def recall(
        self, bank: str, query: str, *, tags: list[str], max_tokens: int,
        types: tuple[str, ...] = _DEFAULT_TYPES,
        tags_match: str = "any", budget: str = "mid",
    ) -> str:
        """Three-outcome contract (v0.99.0): hits, zero hits, or
        RecallUnavailable. Only a well-formed 2xx envelope carrying an actual
        ``results`` list may mean zero hits; every failure (timeout, 5xx/429,
        transport drop, malformed envelope) raises so callers can tell
        "memory could not be checked" from "searched and found nothing".
        No synchronous retry on HTTP errors — a 504 means the reranker is
        overloaded and retrying makes it worse (_request already restricts
        its single retry to connection-level drops)."""
        _validate_bank_id(bank)
        t0 = time.monotonic()

        def _latency_ms() -> int:
            return int((time.monotonic() - t0) * 1000)

        def _unavailable(reason: str) -> RecallUnavailable:
            # E1 (observability): one distinguishable line per outcome, with
            # latency; never the query text (may be sensitive).
            logger.warning(
                "memory_recall bank=%s tags=%s outcome=unavailable reason=%s latency_ms=%d",
                bank, tags, reason, _latency_ms(),
            )
            return RecallUnavailable(reason)

        try:
            resp = await self._request(
                "POST", f"/v1/default/banks/{bank}/memories/recall",
                {
                    "query": query, "tags": tags, "tags_match": tags_match,
                    "max_tokens": max_tokens, "types": list(types), "budget": budget,
                },
            )
        except asyncio.TimeoutError as exc:
            raise _unavailable("timeout") from exc
        except aiohttp.ClientResponseError as exc:
            raise _unavailable(f"http_{exc.status}") from exc
        except (aiohttp.ClientError, ValueError) as exc:
            # ClientError: connection drops surviving the single reconnect
            # retry; ValueError: undecodable JSON body on a 2xx.
            raise _unavailable("transport") from exc

        results = resp.get("results") if isinstance(resp, dict) else None
        if not isinstance(results, list):
            raise _unavailable("malformed_envelope")
        try:
            digest = render_recall(resp)
        except Exception as exc:  # noqa: BLE001 — non-dict/odd items must not leak raw
            raise _unavailable("malformed_envelope") from exc
        if results and not digest:
            # Hits exist but none rendered ([{}], empty text, …): that is NOT
            # a genuine zero-hit — the memories cannot be read.
            raise _unavailable("malformed_envelope")
        logger.info(
            "memory_recall bank=%s tags=%s outcome=%s hits=%d latency_ms=%d",
            bank, tags, "hits" if results else "empty", len(results), _latency_ms(),
        )
        return digest

    async def recall_items(
        self, bank: str, query: str, *, tags: list[str], max_tokens: int,
        clearance: str,
        types: tuple[str, ...] = _DEFAULT_TYPES,
        tags_match: str = "any", budget: str = "mid",
    ) -> tuple[RecallHit, ...]:
        """Typed, attributed recall (personality Task 11). ADDITIVE — leaves
        :meth:`recall` and its reason strings untouched. The failure mapping
        below is byte-for-byte the SAME as :meth:`recall`
        (asyncio.TimeoutError→timeout; aiohttp.ClientResponseError→http_{status};
        (aiohttp.ClientError, ValueError)→transport). Three-outcome contract:
        an empty tuple is returned ONLY for a well-formed 2xx ``results: []``;
        a malformed envelope, a per-hit wire-contract violation, or an
        all-hits-dropped-by-clearance response raises RecallProtocolError (a
        RecallUnavailable subclass)."""
        _validate_bank_id(bank)
        t0 = time.monotonic()

        def _latency_ms() -> int:
            return int((time.monotonic() - t0) * 1000)

        def _unavailable(reason: str) -> RecallUnavailable:
            logger.warning(
                "memory_recall_items bank=%s tags=%s outcome=unavailable "
                "reason=%s latency_ms=%d",
                bank, tags, reason, _latency_ms(),
            )
            return RecallUnavailable(reason)

        try:
            raw = await self._request(
                "POST", f"/v1/default/banks/{bank}/memories/recall",
                {
                    "query": query, "tags": tags, "tags_match": tags_match,
                    "max_tokens": max_tokens, "types": list(types), "budget": budget,
                },
            )
        except asyncio.TimeoutError as exc:
            raise _unavailable("timeout") from exc
        except aiohttp.ClientResponseError as exc:
            raise _unavailable(f"http_{exc.status}") from exc
        except (aiohttp.ClientError, ValueError) as exc:
            raise _unavailable("transport") from exc

        if (not isinstance(raw, dict) or "results" not in raw
                or not isinstance(raw["results"], list)):
            raise RecallProtocolError("results_missing_or_wrong_shape")
        if not raw["results"]:
            logger.info(
                "memory_recall_items bank=%s tags=%s outcome=empty hits=0 latency_ms=%d",
                bank, tags, _latency_ms(),
            )
            return ()  # the sole successful-zero condition

        hits: list[RecallHit] = []
        for result in raw["results"]:
            if not isinstance(result, dict):
                raise RecallProtocolError("result_not_object")
            text = result.get("text")
            raw_tags = result.get("tags")
            if not isinstance(text, str) or not text.strip():
                raise RecallProtocolError("result_text_invalid")
            if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
                raise RecallProtocolError("result_tags_invalid")
            raw_metadata = result.get("metadata")
            if raw_metadata is not None and not isinstance(raw_metadata, dict):
                # #311: a non-mapping metadata used to escape as a raw
                # ValueError/TypeError out of freeze_metadata, outside the
                # documented failure contract.
                raise RecallProtocolError("result_metadata_invalid")
            wire_tags = tuple(raw_tags)
            sensitivity = _decode_sensitivity(wire_tags, clearance=clearance)
            if sensitivity is None:
                continue
            provenance, _reason = decode_provenance_from_tags(wire_tags)
            application_tags = tuple(
                t for t in wire_tags
                if t not in _TIER_RANK and not t.startswith(RESERVED_SOURCE_NAMESPACE)
            )
            source_fact_ids = (
                tuple(result["source_fact_ids"])
                if isinstance(result.get("source_fact_ids"), list) else None
            )
            score = (
                float(result["score"])
                if type(result.get("score")) in {int, float} else None
            )
            hits.append(RecallHit(
                text=text.strip(), memory_type=result.get("type") or "unknown",
                sensitivity=sensitivity, application_tags=application_tags,
                provenance=provenance, backend_id=result.get("id") or None,
                document_id=result.get("document_id") or None,
                chunk_id=result.get("chunk_id") or None,
                source_fact_ids=source_fact_ids,
                metadata=RecallHit.freeze_metadata(raw_metadata),
                context=result.get("context") or None, score=score,
            ))
        if not hits:
            # Every hit was dropped (clearance or ambiguous provenance): NOT a
            # genuine zero-hit — hits exist but none is trustworthy/readable.
            raise RecallProtocolError("no_trustworthy_readable_hit")
        logger.info(
            "memory_recall_items bank=%s tags=%s outcome=hits hits=%d latency_ms=%d",
            bank, tags, len(hits), _latency_ms(),
        )
        return tuple(hits)

    async def profile(self, bank: str) -> str:
        _validate_bank_id(bank)
        resp = await self._request(
            "GET", f"/v1/default/banks/{bank}/mental-models", None,
        )
        return render_mental_models(resp)

