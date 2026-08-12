# tests/test_hindsight_memory.py
"""HindsightSemanticMemory HTTP client. _request is patched so retain/recall/
render logic is tested without live HTTP (verified shapes in spec §8 findings)."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from hindsight_memory import HindsightSemanticMemory
from semantic_memory import RecallUnavailable, SemanticMemory

pytestmark = [pytest.mark.unit]


def test_is_semantic_memory() -> None:
    assert issubclass(HindsightSemanticMemory, SemanticMemory)


async def test_retain_posts_verified_shape() -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"success": True, "items_count": 1})
    items = [{"content": "Nicola keeps the thermostat at 20C.",
              "tags": ["house"], "metadata": {"speaker": "nicola"},
              "document_id": "voice-1:0"}]
    await mem.retain("casa-assistant", items, async_=True)
    mem._request.assert_awaited_once()
    method, path, payload = mem._request.await_args.args
    assert method == "POST"
    assert path == "/v1/default/banks/casa-assistant/memories"
    assert payload["async"] is True          # top-level, not per-item (spec §8)
    assert payload["items"] == items


async def test_retain_validates_bank_id() -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock()
    with pytest.raises(ValueError):
        await mem.retain("casa/finance", [{"content": "x"}])  # bad bank id
    mem._request.assert_not_awaited()


async def test_recall_posts_verified_shape_and_renders() -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"results": [
        {"text": "Nicola keeps the thermostat at 20C.", "type": "world", "tags": ["house"]},
    ]})
    out = await mem.recall("casa-assistant", "thermostat?", tags=["house"], max_tokens=512, budget="low")
    method, path, payload = mem._request.await_args.args
    assert method == "POST"
    assert path == "/v1/default/banks/casa-assistant/memories/recall"
    assert payload["query"] == "thermostat?"
    assert payload["tags"] == ["house"]
    assert payload["tags_match"] == "any"
    assert payload["max_tokens"] == 512
    assert payload["budget"] == "low"
    assert "world" in payload["types"]        # spec §8.9 — must not drop world
    assert "thermostat at 20C" in out         # rendered digest


async def test_retain_logs_success(caplog) -> None:
    """E1: a successful retain emits an INFO trace (was previously silent)."""
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"success": True})
    items = [{"content": "a", "tags": ["public"], "document_id": "m-x"},
             {"content": "b", "tags": ["public"], "document_id": "m-y"}]
    with caplog.at_level(logging.INFO, logger="hindsight_memory"):
        await mem.retain("casa", items, async_=True)
    line = "".join(r.getMessage() for r in caplog.records)
    assert "memory_retain" in line
    assert "bank=casa" in line
    assert "items=2" in line


async def test_recall_logs_hit_count_not_query(caplog) -> None:
    """E1: recall logs the hit count + clearance tags, never the query text."""
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"results": [
        {"text": "one", "type": "world", "tags": ["private"]},
        {"text": "two", "type": "experience", "tags": ["private"]},
    ]})
    with caplog.at_level(logging.INFO, logger="hindsight_memory"):
        await mem.recall(
            "casa", "what is my secret salary", tags=["private"],
            max_tokens=512, budget="low",
        )
    line = "".join(r.getMessage() for r in caplog.records)
    assert "memory_recall" in line
    assert "bank=casa" in line
    assert "hits=2" in line
    assert "salary" not in line  # query text must NOT be logged


async def test_profile_gets_mental_models() -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"mental_models": [
        {"content": "Nicola: terse, prefers metric units."},
    ]})
    out = await mem.profile("casa-assistant")
    method, path, payload = mem._request.await_args.args
    assert method == "GET"
    assert path == "/v1/default/banks/casa-assistant/mental-models"
    assert payload is None
    assert "terse" in out



async def test_request_reuses_one_client_session(monkeypatch) -> None:
    """L32: _request must reuse one ClientSession across calls (keep-alive
    pooling), lazily replacing it only after close()."""
    created = []

    class FakeResp:
        def raise_for_status(self):
            pass

        async def json(self):
            return {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def __init__(self, *a, **kw):
            created.append(self)
            self.closed = False

        def request(self, *a, **kw):
            return FakeResp()

        async def close(self):
            self.closed = True

    monkeypatch.setattr("hindsight_memory.aiohttp.ClientSession", FakeSession)
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    await mem._request("GET", "/v1/default/banks/casa-assistant/mental-models")
    await mem._request(
        "POST", "/v1/default/banks/casa-assistant/memories/recall", {"query": "x"},
    )
    assert len(created) == 1, "ClientSession must be created once and reused"
    await mem.close()
    assert created[0].closed, "close() must close the shared session"
    await mem._request("GET", "/v1/default/banks/casa-assistant/mental-models")
    assert len(created) == 2, "a closed session must be lazily replaced, not reused"
    await mem.close()

# --- D-3 (2026-07-11): stale keep-alive connection resilience --------------
# Recall is the FIRST memory round-trip of a turn, after a long idle gap
# (hourly heartbeats, sparse messages). The reused pooled connection was
# reliably idle past Hindsight's keep-alive window, so recall reused a
# half-closed socket and raised ServerDisconnectedError on `await
# protocol.read()` — silently degrading memory for hours while the later
# same-turn retain (fresh connection) succeeded.


class _SeqResp:
    """One request outcome: raise ``exc`` on enter, else return status/body."""

    def __init__(self, *, exc=None, raise_for_status_exc=None, body=None):
        self._exc = exc
        self._rfs_exc = raise_for_status_exc
        self._body = body if body is not None else {}

    def raise_for_status(self):
        if self._rfs_exc is not None:
            raise self._rfs_exc

    async def json(self):
        return self._body

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self

    async def __aexit__(self, *exc):
        return False


class _SeqSession:
    """ClientSession stub replaying a fixed sequence of request outcomes."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.closed = False

    def request(self, *a, **kw):
        out = self._outcomes[self.calls]
        self.calls += 1
        return out

    async def close(self):
        self.closed = True


async def test_request_retries_once_on_server_disconnect() -> None:
    import aiohttp
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._session = _SeqSession([
        _SeqResp(exc=aiohttp.ServerDisconnectedError()),
        _SeqResp(body={"ok": 1}),
    ])
    out = await mem._request("POST", "/p", {"q": "x"})
    assert out == {"ok": 1}
    assert mem._session.calls == 2, "must retry a dropped connection once"


async def test_request_gives_up_after_one_retry() -> None:
    import aiohttp
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._session = _SeqSession([
        _SeqResp(exc=aiohttp.ServerDisconnectedError()),
        _SeqResp(exc=aiohttp.ServerDisconnectedError()),
    ])
    with pytest.raises(aiohttp.ServerDisconnectedError):
        await mem._request("POST", "/p", {"q": "x"})
    assert mem._session.calls == 2, "exactly one retry, not an unbounded loop"


async def test_request_does_not_retry_http_error() -> None:
    """A 5xx means the request WAS received (a retained write may have
    landed) — retrying could double-write. Only connection-level drops retry."""
    import aiohttp
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    http_err = aiohttp.ClientResponseError(
        request_info=None, history=(), status=500,
    )
    mem._session = _SeqSession([_SeqResp(raise_for_status_exc=http_err)])
    with pytest.raises(aiohttp.ClientResponseError):
        await mem._request("POST", "/p", {"q": "x"})
    assert mem._session.calls == 1, "HTTP errors must not be retried"


# --- Three-outcome recall contract (v0.99.0) -------------------------------
# A recall has exactly three outcomes: hits, zero hits, UNAVAILABLE. Only a
# well-formed 2xx envelope with an actual (possibly empty) `results` list may
# mean zero hits; timeouts, 5xx/429, transport failures and malformed
# envelopes raise RecallUnavailable instead of collapsing to ''.


def _http_err(status: int):
    import aiohttp
    return aiohttp.ClientResponseError(request_info=None, history=(), status=status)


async def test_recall_504_raises_unavailable() -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._session = _SeqSession([_SeqResp(raise_for_status_exc=_http_err(504))])
    with pytest.raises(RecallUnavailable) as ei:
        await mem.recall("casa", "q", tags=["public"], max_tokens=100)
    assert ei.value.reason == "http_504"


async def test_recall_429_raises_unavailable() -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._session = _SeqSession([_SeqResp(raise_for_status_exc=_http_err(429))])
    with pytest.raises(RecallUnavailable) as ei:
        await mem.recall("casa", "q", tags=["public"], max_tokens=100)
    assert ei.value.reason == "http_429"


async def test_recall_does_not_retry_5xx() -> None:
    """A 504 means the reranker is overloaded — a synchronous retry makes it
    worse. Exactly one request, then UNAVAILABLE."""
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._session = _SeqSession([
        _SeqResp(raise_for_status_exc=_http_err(504)),
        _SeqResp(body={"results": []}),
    ])
    with pytest.raises(RecallUnavailable):
        await mem.recall("casa", "q", tags=["public"], max_tokens=100)
    assert mem._session.calls == 1


async def test_recall_timeout_raises_unavailable() -> None:
    import asyncio
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._session = _SeqSession([_SeqResp(exc=asyncio.TimeoutError())])
    with pytest.raises(RecallUnavailable) as ei:
        await mem.recall("casa", "q", tags=["public"], max_tokens=100)
    assert ei.value.reason == "timeout"


async def test_recall_connection_error_raises_unavailable() -> None:
    """Transport failure surviving the single reconnect retry → UNAVAILABLE."""
    import aiohttp
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._session = _SeqSession([
        _SeqResp(exc=aiohttp.ServerDisconnectedError()),
        _SeqResp(exc=aiohttp.ServerDisconnectedError()),
    ])
    with pytest.raises(RecallUnavailable):
        await mem.recall("casa", "q", tags=["public"], max_tokens=100)


@pytest.mark.parametrize("body", [
    {},                       # missing results key
    {"results": "nope"},      # wrong-shaped results
    {"memories": []},         # wrong envelope key
    "not a dict",             # non-dict body
])
async def test_recall_malformed_envelope_raises_unavailable(body) -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value=body)
    with pytest.raises(RecallUnavailable) as ei:
        await mem.recall("casa", "q", tags=["public"], max_tokens=100)
    assert ei.value.reason == "malformed_envelope"


async def test_recall_wellformed_empty_is_zero_hits() -> None:
    """{"results": []} on a 2xx is the ONLY shape that means zero hits."""
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"results": []})
    assert await mem.recall("casa", "q", tags=["public"], max_tokens=100) == ""


@pytest.mark.parametrize("results", [
    [{}],                       # item with no text
    [{"text": ""}],             # item with empty text
    ["not a dict"],             # non-dict item (render would raise)
    [{"text": None}],           # null text
])
async def test_recall_unrenderable_results_are_unavailable_not_zero_hits(results) -> None:
    """Non-empty results that render to nothing are NOT a genuine zero-hit —
    hits exist but cannot be read, so the recall is UNAVAILABLE.

    Pins INV-MEM-001. Red case demonstrated: replacing the unrenderable-results
    raise in HindsightSemanticMemory.recall with `return ""` fails this test.
    """
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"results": results})
    with pytest.raises(RecallUnavailable) as ei:
        await mem.recall("casa", "q", tags=["public"], max_tokens=100)
    assert ei.value.reason == "malformed_envelope"


async def test_recall_logs_unavailable_with_latency_not_query(caplog) -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._session = _SeqSession([_SeqResp(raise_for_status_exc=_http_err(504))])
    with caplog.at_level(logging.INFO, logger="hindsight_memory"):
        with pytest.raises(RecallUnavailable):
            await mem.recall(
                "casa", "what is my secret salary", tags=["private"],
                max_tokens=100,
            )
    line = "".join(r.getMessage() for r in caplog.records)
    assert "outcome=unavailable" in line
    assert "reason=http_504" in line
    assert "latency_ms=" in line
    assert "salary" not in line   # query text must NOT be logged


async def test_recall_logs_success_outcomes_with_latency(caplog) -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"results": [
        {"text": "one", "type": "world", "tags": ["public"]},
    ]})
    with caplog.at_level(logging.INFO, logger="hindsight_memory"):
        await mem.recall("casa", "q", tags=["public"], max_tokens=100)
    mem._request = AsyncMock(return_value={"results": []})
    with caplog.at_level(logging.INFO, logger="hindsight_memory"):
        await mem.recall("casa", "q", tags=["public"], max_tokens=100)
    lines = [r.getMessage() for r in caplog.records if "memory_recall" in r.getMessage()]
    assert any("outcome=hits" in ln and "latency_ms=" in ln for ln in lines)
    assert any("outcome=empty" in ln and "latency_ms=" in ln for ln in lines)


async def test_session_uses_force_close_connector() -> None:
    """Root cause: never reuse a keep-alive connection for this sparse,
    bursty traffic — a pooled connection is almost always idle-expired."""
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    session = mem._new_session()
    try:
        assert session.connector.force_close is True
    finally:
        await session.close()


# --- Personality Task 11: recall_items (typed, attributed, three-outcome) ---


@pytest.fixture
def memory() -> HindsightSemanticMemory:
    return HindsightSemanticMemory(base_url="http://hs:8888")


def _valid_payload() -> str:
    """The base64 payload of a valid resident provenance tag (sans namespace)."""
    from personality_types import SpeakerProvenance
    from speaker_provenance import RESERVED_SOURCE_PREFIX, encode_provenance_tag

    tag = encode_provenance_tag(SpeakerProvenance(
        speaker_kind="resident", role_id="resident:butler",
        persona_id="casa.personas/tina", persona_version="1.0.0",
        display_name="Tina", binding_digest="sha256:" + "5" * 64,
    ))
    return tag[len(RESERVED_SOURCE_PREFIX):]


async def test_recall_items_maps_hindsight_result_to_typed_hit(memory) -> None:
    memory._request = AsyncMock(return_value={
        "results": [{
            "id": "backend-1", "text": " Fact ", "type": "world",
            "tags": ["friends", "casa-source-v1." + _valid_payload()],
            "document_id": None, "chunk_id": "chunk-1", "source_fact_ids": ["fact-1"],
            "metadata": {}, "context": "context", "score": 0.75,
        }],
    })
    hits = await memory.recall_items(
        "casa", "query", tags=["friends"], max_tokens=100, clearance="friends",
    )
    assert hits[0].text == "Fact"
    assert hits[0].sensitivity == "friends"
    assert hits[0].backend_id == "backend-1"
    assert hits[0].provenance is not None
    assert hits[0].provenance.display_name == "Tina"
    assert hits[0].chunk_id == "chunk-1"
    assert hits[0].source_fact_ids == ("fact-1",)
    assert hits[0].context == "context"
    assert hits[0].score == 0.75
    # Step 8: reserved + tier tags never survive into application_tags.
    assert "casa-source-v1." + _valid_payload() not in hits[0].application_tags
    for token in ("public", "friends", "family", "private"):
        assert token not in hits[0].application_tags


async def test_recall_items_posts_the_same_wire_shape_as_recall(memory) -> None:
    memory._request = AsyncMock(return_value={"results": []})
    await memory.recall_items(
        "casa-assistant", "thermostat?", tags=["house"], max_tokens=512,
        clearance="private", budget="low",
    )
    method, path, payload = memory._request.await_args.args
    assert method == "POST"
    assert path == "/v1/default/banks/casa-assistant/memories/recall"
    assert payload["query"] == "thermostat?"
    assert payload["tags"] == ["house"]
    assert payload["tags_match"] == "any"
    assert payload["max_tokens"] == 512
    assert payload["budget"] == "low"
    assert "world" in payload["types"]


async def test_zero_wire_hits_is_the_only_empty_tuple_case(memory) -> None:
    memory._request = AsyncMock(return_value={"results": []})
    hits = await memory.recall_items(
        "casa", "query", tags=["friends"], max_tokens=100, clearance="friends",
    )
    assert hits == ()


@pytest.mark.parametrize("bad_metadata", [["unexpected"], "text", 5, True])
async def test_recall_items_non_mapping_metadata_is_protocol_error(
    memory, bad_metadata,
) -> None:
    """#311: a non-mapping `metadata` used to escape the failure contract as
    a raw ValueError/TypeError out of freeze_metadata; the wire-contract
    violation must surface as RecallProtocolError like its siblings
    (result_not_object / result_text_invalid / result_tags_invalid)."""
    from semantic_memory import RecallProtocolError

    memory._request = AsyncMock(return_value={
        "results": [{
            "id": "b1", "text": "fact", "type": "world",
            "tags": ["friends"], "metadata": bad_metadata,
        }],
    })
    with pytest.raises(RecallProtocolError):
        await memory.recall_items(
            "casa", "query", tags=["friends"], max_tokens=100, clearance="friends",
        )


async def test_all_hits_dropped_by_clearance_is_protocol_error_not_empty(memory) -> None:
    from semantic_memory import RecallProtocolError

    memory._request = AsyncMock(return_value={
        "results": [{"id": "b1", "text": "secret", "type": "world", "tags": ["private"]}],
    })
    with pytest.raises(RecallProtocolError):
        await memory.recall_items(
            "casa", "query", tags=["friends"], max_tokens=100, clearance="friends",
        )


async def test_recall_items_protocol_error_is_a_recall_unavailable(memory) -> None:
    """RecallProtocolError subclasses RecallUnavailable so every existing
    `except RecallUnavailable` still catches it."""
    from semantic_memory import RecallProtocolError

    assert issubclass(RecallProtocolError, RecallUnavailable)
    memory._request = AsyncMock(return_value={"nope": []})
    with pytest.raises(RecallUnavailable):
        await memory.recall_items(
            "casa", "q", tags=["public"], max_tokens=10, clearance="public",
        )


@pytest.mark.parametrize("body", [
    {},                       # missing results key
    {"results": "nope"},      # wrong-shaped results
    "not a dict",             # non-dict body
])
async def test_recall_items_malformed_envelope_is_protocol_error(memory, body) -> None:
    from semantic_memory import RecallProtocolError

    memory._request = AsyncMock(return_value=body)
    with pytest.raises(RecallProtocolError):
        await memory.recall_items(
            "casa", "q", tags=["public"], max_tokens=10, clearance="public",
        )


async def test_recall_items_maps_failures_exactly_like_recall(memory) -> None:
    import asyncio

    import aiohttp

    memory._request = AsyncMock(side_effect=asyncio.TimeoutError())
    with pytest.raises(RecallUnavailable) as ei:
        await memory.recall_items("casa", "q", tags=["public"], max_tokens=10, clearance="public")
    assert ei.value.reason == "timeout"

    memory._request = AsyncMock(side_effect=_http_err(504))
    with pytest.raises(RecallUnavailable) as ei:
        await memory.recall_items("casa", "q", tags=["public"], max_tokens=10, clearance="public")
    assert ei.value.reason == "http_504"

    memory._request = AsyncMock(side_effect=_http_err(429))
    with pytest.raises(RecallUnavailable) as ei:
        await memory.recall_items("casa", "q", tags=["public"], max_tokens=10, clearance="public")
    assert ei.value.reason == "http_429"

    memory._request = AsyncMock(side_effect=aiohttp.ClientError())
    with pytest.raises(RecallUnavailable) as ei:
        await memory.recall_items("casa", "q", tags=["public"], max_tokens=10, clearance="public")
    assert ei.value.reason == "transport"

    memory._request = AsyncMock(side_effect=ValueError("bad json"))
    with pytest.raises(RecallUnavailable) as ei:
        await memory.recall_items("casa", "q", tags=["public"], max_tokens=10, clearance="public")
    assert ei.value.reason == "transport"


async def test_recall_items_validates_bank_id(memory) -> None:
    memory._request = AsyncMock()
    with pytest.raises(ValueError):
        await memory.recall_items(
            "casa/finance", "q", tags=["public"], max_tokens=10, clearance="public",
        )
    memory._request.assert_not_awaited()
