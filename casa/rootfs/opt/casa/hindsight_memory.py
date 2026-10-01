# casa/rootfs/opt/casa/hindsight_memory.py
"""Hindsight HTTP implementation of SemanticMemory (spec §4, verified §8).

Talks to the bank API at ``{base_url}/v1/default/banks/{bank}/...``. The
base URL is configurable (the add-on is reachable via its hassio network
alias / IP, NOT the literal host ``hindsight`` -- spec §8.8). API is
unauthenticated on the internal network (spec §8.4).
"""
from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import aiohttp

from hindsight_ids import bank_id as _validate_bank_id  # fail-fast on bad ids
from personality_types import RecallHit
from semantic_memory import (
    RecallProtocolError,
    RecallUnavailable,
    SemanticMemory,
    StoredTagsUnavailable,
    render_mental_models,
    render_recall,
)
from speaker_provenance import RESERVED_SOURCE_NAMESPACE, decode_provenance_from_tags
from timekeeping import RecallWindow, resolve_tz

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


# #1117: one retry of a recall answered 503 ("server busy"). The wait honours
# ``Retry-After`` but never exceeds a second, because auto-recall runs inside a
# 5 s deadline; without a usable header it waits this long.
_RETRY_503_CAP_S = 1.0
_RETRY_503_DEFAULT_S = 0.5


def _retry_after_s(headers: object) -> float:
    """Seconds to wait before retrying a 503: ``Retry-After`` as
    delta-seconds or an HTTP-date, clamped to ``[0, _RETRY_503_CAP_S]``; the
    default when the header is absent or unparseable."""
    try:
        raw = headers.get("Retry-After")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — a missing/odd headers object is "absent"
        raw = None
    if not isinstance(raw, str) or not raw.strip():
        return _RETRY_503_DEFAULT_S
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return _RETRY_503_DEFAULT_S
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds):
        return _RETRY_503_DEFAULT_S
    return min(max(seconds, 0.0), _RETRY_503_CAP_S)


# #1123: the one answer to a document read that means "never saved". Measured
# on Hindsight 0.10.2 (the route is present since v0.0.8): an unknown document
# AND a missing bank both answer 404 with a JSON object whose ``detail`` is
# exactly this string, while an unknown ROUTE answers 404 "Not Found". Every
# other answer fails the read — the bare string as plain text or as a JSON
# scalar included — so a server whose wording differs stops first saves rather
# than disabling the floor.
_DOCUMENT_NOT_FOUND = "Document not found"
_DETAIL_LOG_CHARS = 120
_NO_DETAIL = object()


def _error_detail(body: str) -> object:
    """The ``detail`` of an error body that decodes to a JSON object carrying
    one, else :data:`_NO_DETAIL` — never the raw body, so no other shape can
    equal the measured detail."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return _NO_DETAIL
    if not isinstance(parsed, dict) or "detail" not in parsed:
        return _NO_DETAIL
    return parsed["detail"]


def _parse_mentioned_at(value: object) -> datetime | None:
    """#1117: a hit's recorded date, or ``None`` — never an error. Only a
    tz-aware ISO-8601 string is a date; a naive one cannot be placed in the
    operator's day, so it is treated as absent."""
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


class HindsightSemanticMemory(SemanticMemory):
    def __init__(
        self, base_url: str, *, timeout_s: float = 20.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        # #1117: the wait before the one 503 retry. Injected so tests never
        # patch the shared ``asyncio.sleep``.
        self._sleep = sleep
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

    @staticmethod
    def _recall_payload(
        query: str, *, tags: list[str], tags_match: str, max_tokens: int,
        types: tuple[str, ...], budget: str, window: RecallWindow | None = None,
    ) -> dict[str, Any]:
        """The ONE recall request body, shared by :meth:`recall` and
        :meth:`recall_items` so the two can never drift. #1117: carries the
        client's own "now" in the operator's timezone, so the server anchors
        "today" and recency on Casa's clock. #1120: a ``window`` becomes the
        server's ``temporal_window`` (inclusive, timezone-aware bounds), which
        RANKS memories dated in it higher and filters nothing; without one the
        key is absent and the body is exactly what it was."""
        payload = {
            "query": query, "tags": tags, "tags_match": tags_match,
            "max_tokens": max_tokens, "types": list(types), "budget": budget,
            "query_timestamp": datetime.now(resolve_tz()).isoformat(),
        }
        if window is not None:
            payload["temporal_window"] = {
                "start": window.start.isoformat(), "end": window.end.isoformat(),
            }
        return payload

    async def _recall_request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """A recall round-trip with ONE retry of a 503 (#1117). Only a 503 —
        "busy, try again" — is retried, once, after :func:`_retry_after_s`;
        every other status stays single-shot (a 504 means the reranker is
        overloaded, and retrying makes it worse). Recall only: a retain never
        comes through here, because a retried write may double-write. The same
        payload is re-sent."""
        try:
            return await self._request("POST", path, payload)
        except aiohttp.ClientResponseError as exc:
            if exc.status != 503:
                raise
            await self._sleep(_retry_after_s(exc.headers))
        return await self._request("POST", path, payload)

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

    async def document_tags(self, bank: str, document_id: str) -> frozenset[str] | None:
        """#1123: ``GET /v1/default/banks/{bank}/documents/{document_id}`` —
        the document's current tags, or None only for a 404 whose body is a
        JSON object with ``detail`` exactly "Document not found" (see
        ``_DOCUMENT_NOT_FOUND``). Any other 404 — plain text, a JSON scalar or
        list, an object without that detail — any other status, and a 200
        without a JSON object holding a list of string ``tags`` raise :class:`StoredTagsUnavailable`; a timeout or transport failure
        propagates. Not ``_roundtrip``: that raises on the status before the
        body — the only thing that tells "never saved" from a wrong route —
        can be read. A GET is safe to retry once after a dropped connection."""
        _validate_bank_id(bank)
        url = f"{self._base}/v1/default/banks/{bank}/documents/{document_id}"
        if self._session is None or self._session.closed:
            self._session = self._new_session()

        async def _get_once() -> tuple[int, str]:
            async with self._session.request("GET", url) as resp:
                return resp.status, await resp.text()

        try:
            status, body = await _get_once()
        except aiohttp.ClientConnectionError:
            status, body = await _get_once()
        if status == 404:
            detail = _error_detail(body)
            if isinstance(detail, str) and detail == _DOCUMENT_NOT_FOUND:
                return None
            shown = repr(body if detail is _NO_DETAIL else detail)[:_DETAIL_LOG_CHARS]
            logger.warning(
                "memory stored-tier read got an unrecognised 404 bank=%s "
                "detail=%s — not read as never-saved; the save is skipped",
                bank, shown,
            )
            raise StoredTagsUnavailable(f"http_404 detail={shown}")
        if status != 200:
            raise StoredTagsUnavailable(f"http_{status}")
        try:
            payload = json.loads(body)
        except ValueError:
            raise StoredTagsUnavailable("malformed_document") from None
        tags = payload.get("tags") if isinstance(payload, dict) else None
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise StoredTagsUnavailable("malformed_document")
        return frozenset(tags)

    async def delete_bank(self, bank: str) -> bool:
        """#411: whole-bank delete (``DELETE /v1/default/banks/{bank}``).
        Policy-free like ``retain`` — consent and writer quiescing live in
        :mod:`memory_wipe`, the only permitted caller. Raises on transport/
        HTTP failure (the wipe reports it truthfully rather than claiming a
        deletion that did not happen)."""
        _validate_bank_id(bank)
        url = f"{self._base}/v1/default/banks/{bank}"
        if self._session is None or self._session.closed:
            self._session = self._new_session()

        async def _delete_once() -> None:
            # Not _roundtrip: a DELETE may legitimately return an empty body
            # (the provenance-contract probe tolerates exactly that), and
            # _roundtrip's unconditional .json() would raise on success.
            async with self._session.request("DELETE", url) as resp:
                resp.raise_for_status()

        try:
            await _delete_once()
        except aiohttp.ClientConnectionError:
            # Same single connection-drop retry as _request; DELETE is
            # idempotent so a retry after a mid-call drop is safe.
            await _delete_once()
        logger.warning("memory bank DELETED bank=%s", bank)
        return True

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
        No synchronous retry on HTTP errors except one: a 503 ("busy") is
        retried once after at most a second (#1117). A 504 means the reranker
        is overloaded and retrying makes it worse (_request already restricts
        its own single retry to connection-level drops)."""
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
            resp = await self._recall_request(
                f"/v1/default/banks/{bank}/memories/recall",
                self._recall_payload(
                    query, tags=tags, tags_match=tags_match,
                    max_tokens=max_tokens, types=types, budget=budget,
                ),
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
        window: RecallWindow | None = None,
    ) -> tuple[RecallHit, ...]:
        """Typed, attributed recall (personality Task 11). ADDITIVE — leaves
        :meth:`recall` and its reason strings untouched. The failure mapping
        below is byte-for-byte the SAME as :meth:`recall`
        (asyncio.TimeoutError→timeout; aiohttp.ClientResponseError→http_{status};
        (aiohttp.ClientError, ValueError)→transport). Three-outcome contract:
        an empty tuple is returned ONLY for a well-formed 2xx ``results: []``;
        a malformed envelope, a per-hit wire-contract violation, or an
        all-hits-dropped-by-clearance response raises RecallProtocolError (a
        RecallUnavailable subclass). #1120: ``window`` only ranks (see
        :meth:`_recall_payload`); it never changes which outcome is returned."""
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
            raw = await self._recall_request(
                f"/v1/default/banks/{bank}/memories/recall",
                self._recall_payload(
                    query, tags=tags, tags_match=tags_match,
                    max_tokens=max_tokens, types=types, budget=budget,
                    window=window,
                ),
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
                mentioned_at=_parse_mentioned_at(result.get("mentioned_at")),
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

