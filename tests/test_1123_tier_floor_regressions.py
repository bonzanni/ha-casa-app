# tests/test_1123_tier_floor_regressions.py
"""#1123 regression tests and structural pins around the stored-tier floor.

The red cases are ``tests/test_pin_1123_tier_floor.py``; these cover what was
already true at the base (and must stay true) and the new code's own contracts:
the classifier's fallback signal, the stored-tag decode, the Hindsight read's
404 discrimination, and that a failed read never leaves a sibling read running."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import delegated_memory
import session_saver
import tier_classifier
from hindsight_memory import HindsightSemanticMemory
from memory_provenance import UNVERIFIED_TIER_MARK, build_retain_items
from personality_types import RetainedTurn
from semantic_memory import NoOpSemanticMemory, SemanticMemory, StoredTagsUnavailable
from sensitivity import TIERS
from session_reg_helpers import STUB_SPEAKER_PROV, STUB_USER_PROV
from test_pin_1123_tier_floor import (
    _Bank, _backend_down, _cold_save, _fixed, _install_sdk, _transcript,
)

pytestmark = [pytest.mark.unit]


# --- regression: green at the base, kept by the floor ----------------------


async def test_fallback_private_then_real_public_lowers_and_drops_the_marker(
        monkeypatch, tmp_path):
    bank = _Bank()
    _install_sdk(monkeypatch, _backend_down)
    await _cold_save(monkeypatch, bank, tier_classifier.classify_tier, tmp_path)
    await _cold_save(monkeypatch, bank, _fixed("public"), tmp_path)
    assert len(bank.docs) == 2
    assert bank.tiers() == [["public"], ["public"]]
    assert all(UNVERIFIED_TIER_MARK not in tags for tags in bank.docs.values())


async def test_real_public_then_real_private_is_raised(monkeypatch, tmp_path):
    bank = _Bank()
    await _cold_save(monkeypatch, bank, _fixed("public"), tmp_path)
    await _cold_save(monkeypatch, bank, _fixed("private"), tmp_path)
    assert bank.tiers() == [["private"], ["private"]]


async def _scheduled_cold_save(monkeypatch, bank, classify, tmp_path, *, scheduled):
    from agent import snapshot_session_entry
    from speaker_provenance import provenance_mapping
    monkeypatch.setattr(session_saver, "classify_tier", classify)
    monkeypatch.setattr(session_saver, "get_session_messages", _transcript)
    old = snapshot_session_entry({
        "agent": "resident:assistant", "sdk_session_id": "s1",
        "speaker_provenance": provenance_mapping(STUB_SPEAKER_PROV),
        "user_provenance": provenance_mapping(STUB_USER_PROV),
        "scheduled": scheduled,
    })
    await session_saver.retain_cold_session(
        old, directory="/tmp", channel="telegram", semantic_memory=bank,
        retry_dir=tmp_path)


async def test_scheduled_mark_follows_the_last_save_while_the_tier_floors(
        monkeypatch, tmp_path):
    bank = _Bank()
    await _scheduled_cold_save(monkeypatch, bank, _fixed("private"), tmp_path, scheduled=True)
    assert all("casa-scheduled" in tags for tags in bank.docs.values())
    await _scheduled_cold_save(monkeypatch, bank, _fixed("public"), tmp_path, scheduled=False)
    assert len(bank.docs) == 2
    assert bank.tiers() == [["private"], ["private"]]
    assert all("casa-scheduled" not in tags for tags in bank.docs.values())

    # And the reverse order gains the mark, still at the stored tier.
    await _scheduled_cold_save(monkeypatch, bank, _fixed("friends"), tmp_path, scheduled=True)
    assert bank.tiers() == [["private"], ["private"]]
    assert all(tags.count("casa-scheduled") == 1 for tags in bank.docs.values())


async def test_a_never_saved_document_takes_its_own_tier(monkeypatch, tmp_path):
    bank = _Bank()
    await _cold_save(monkeypatch, bank, _fixed("friends"), tmp_path)
    assert bank.tiers() == [["friends"], ["friends"]]
    assert bank.reads == 2
    assert all(UNVERIFIED_TIER_MARK not in tags for tags in bank.docs.values())


async def test_a_real_private_needs_no_read(monkeypatch, tmp_path):
    bank = _Bank()
    bank.read_error = RuntimeError("must not be read")
    await _cold_save(monkeypatch, bank, _fixed("private"), tmp_path)
    assert bank.reads == 0
    assert bank.retains == 1
    assert bank.tiers() == [["private"], ["private"]]


async def test_a_bare_asyncmock_memory_never_reads_as_never_saved(monkeypatch):
    monkeypatch.setattr(delegated_memory, "classify_tier", _fixed("public"))
    sem = AsyncMock()
    await delegated_memory.retain_delegated(
        sem, origin_channel="telegram",
        turns=[RetainedTurn("a line", STUB_USER_PROV)])
    sem.retain.assert_not_awaited()


# --- the classifier's fallback signal ---------------------------------------


async def test_failure_arms_return_a_fallback_and_verdicts_stay_plain(monkeypatch):
    assert isinstance(await tier_classifier.classify_tier("   "), tier_classifier.FallbackTier)

    _install_sdk(monkeypatch, _backend_down)
    failed = await tier_classifier.classify_tier("the garage code")
    assert failed == "private" and isinstance(failed, tier_classifier.FallbackTier)

    _install_sdk(monkeypatch, lambda prompt: "I am not sure")
    unparseable = await tier_classifier.classify_tier("the garage code")
    assert unparseable == "private"
    assert isinstance(unparseable, tier_classifier.FallbackTier)

    _install_sdk(monkeypatch, lambda prompt: "private")
    real = await tier_classifier.classify_tier("the garage code")
    assert real == "private" and not isinstance(real, tier_classifier.FallbackTier)


async def test_the_conflict_arm_is_a_plain_verdict_and_still_counts_as_defaulted(monkeypatch):
    from sensitivity import TIER_FORMAT_REMINDER
    _install_sdk(monkeypatch, lambda prompt: (
        "public" if TIER_FORMAT_REMINDER in prompt
        else "This must stay confidential.\nprivate"))
    with tier_classifier.classify_stats() as stats:
        tier = await tier_classifier.classify_tier("the garage code")
    assert tier == "private"
    assert not isinstance(tier, tier_classifier.FallbackTier)
    assert (stats.total, stats.defaulted) == (1, 1)


# --- the stored-tag decode, through the builder -----------------------------


async def _build_one(stored, *, verdict="public"):
    async def classify(_text):
        return verdict

    async def stored_tags(_document_id):
        return stored

    (item,) = await build_retain_items(
        [RetainedTurn("a line", STUB_USER_PROV)], classify=classify,
        stored_tags=stored_tags)
    return item["tags"]


@pytest.mark.parametrize("stored, sent", [
    (["friends", "family"], "family"),                       # several: the strictest
    (["friends", UNVERIFIED_TIER_MARK], "friends"),          # marker beside a non-private
    (["private", UNVERIFIED_TIER_MARK], "public"),           # provisional: no floor
    (["casa-scheduled"], "public"),                          # no tier stored: no floor
    (frozenset({"private"}), "private"),                     # a set reads like a list
    (("family",), "family"),
])
async def test_stored_tag_decode(stored, sent):
    tags = await _build_one(stored)
    assert [t for t in tags if t in TIERS] == [sent]
    assert UNVERIFIED_TIER_MARK not in tags


@pytest.mark.parametrize("stored", [
    "private", {"tags": ["private"]}, ["private", 3], 7, object(),
])
async def test_an_unusable_stored_value_fails_the_build(stored):
    with pytest.raises(ValueError):
        await _build_one(stored)


async def test_a_failed_read_is_raised_only_after_every_sibling_read_finished():
    release = asyncio.Event()
    finished: list[str] = []
    order: list[str] = []

    async def reader(document_id):
        order.append(document_id)
        if len(order) == 1:
            raise RuntimeError("memory store busy")
        await release.wait()
        finished.append(document_id)
        return None

    async def classify(_text):
        return "public"

    task = asyncio.create_task(build_retain_items(
        [RetainedTurn("one line", STUB_USER_PROV),
         RetainedTurn("another line", STUB_USER_PROV)],
        classify=classify, stored_tags=reader))
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(order) == 2
    assert not task.done()          # the failure waits for the pending sibling
    release.set()
    with pytest.raises(RuntimeError, match="busy"):
        await task
    assert len(finished) == 1


# --- the seam ----------------------------------------------------------------


def test_document_tags_is_abstract_on_the_seam():
    class _NoRead(SemanticMemory):
        async def retain(self, bank, items, *, async_=True):
            return None

        async def recall(self, bank, query, **kw):
            return ""

        async def recall_items(self, bank, query, **kw):
            return ()

        async def profile(self, bank):
            return ""

    with pytest.raises(TypeError):
        _NoRead()


async def test_the_noop_backend_reads_never_saved():
    assert await NoOpSemanticMemory().document_tags("casa", "m-0") is None


class _Resp:
    def __init__(self, status, body="", exc=None):
        self.status, self._body, self._exc = status, body, exc

    async def text(self):
        return self._body

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[tuple] = []
        self.closed = False

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        return self._outcomes[len(self.calls) - 1]


def _memory(*outcomes):
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._session = _Session(outcomes)
    return mem


async def test_hindsight_read_returns_the_stored_tag_set():
    mem = _memory(_Resp(200, '{"id": "m-1", "tags": ["friends", "casa-source-x"]}'))
    assert await mem.document_tags("casa", "m-1") == frozenset({"friends", "casa-source-x"})
    assert mem._session.calls == [("GET", "http://hs:8888/v1/default/banks/casa/documents/m-1")]


async def test_hindsight_read_only_the_document_not_found_404_is_never_saved():
    mem = _memory(_Resp(404, '{"detail":"Document not found"}'))
    assert await mem.document_tags("casa", "m-1") is None


@pytest.mark.parametrize("resp", [
    _Resp(404, '{"detail":"Not Found"}'),        # an unknown route (FastAPI default)
    _Resp(404, ""),
    _Resp(404, "<html>gone</html>"),
    _Resp(404, '{"detail":"document not found"}'),
    # Only a decoded JSON OBJECT whose detail is the measured string is
    # "never saved" (M-7); the bare string, as text or as a JSON scalar, is not.
    _Resp(404, "Document not found"),
    _Resp(404, '"Document not found"'),
    _Resp(404, '["Document not found"]'),
    _Resp(404, '{"message":"Document not found"}'),
    _Resp(404, '{"detail":{"detail":"Document not found"}}'),
    _Resp(404, "{not json"),
    _Resp(500, '{"detail":"boom"}'),
    _Resp(503, ""),
    _Resp(200, "not json"),
    _Resp(200, '{"id": "m-1"}'),
    _Resp(200, '{"tags": "private"}'),
    _Resp(200, '{"tags": ["private", 1]}'),
    _Resp(200, '["private"]'),
    _Resp(200, '"private"'),
    _Resp(200, "null"),
    _Resp(200, '{"tags": null}'),
    _Resp(200, '{"tags": {"private": true}}'),
    _Resp(200, '{"tags": [null]}'),
    _Resp(200, '{"tags": [["private"]]}'),
])
async def test_hindsight_read_fails_on_every_other_answer(resp):
    with pytest.raises(StoredTagsUnavailable):
        await _memory(resp).document_tags("casa", "m-1")


async def test_hindsight_read_logs_an_unrecognised_404(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="hindsight_memory"):
        with pytest.raises(StoredTagsUnavailable):
            await _memory(_Resp(404, '{"detail":"Not Found"}')).document_tags("casa", "m-1")
    assert any("unrecognised 404" in r.getMessage() and "Not Found" in r.getMessage()
               for r in caplog.records)


@pytest.mark.parametrize("body", ["Document not found", '"Document not found"'])
async def test_hindsight_read_logs_a_404_without_a_json_detail(caplog, body):
    import logging
    with caplog.at_level(logging.WARNING, logger="hindsight_memory"):
        with pytest.raises(StoredTagsUnavailable):
            await _memory(_Resp(404, body)).document_tags("casa", "m-1")
    assert any("unrecognised 404" in r.getMessage() and "Document not found" in r.getMessage()
               for r in caplog.records)


async def test_hindsight_read_retries_one_dropped_connection_only():
    import aiohttp
    mem = _memory(_Resp(0, exc=aiohttp.ServerDisconnectedError()),
                  _Resp(200, '{"tags": ["public"]}'))
    assert await mem.document_tags("casa", "m-1") == frozenset({"public"})
    assert len(mem._session.calls) == 2

    mem = _memory(_Resp(0, exc=aiohttp.ServerDisconnectedError()),
                  _Resp(0, exc=aiohttp.ServerDisconnectedError()))
    with pytest.raises(aiohttp.ClientConnectionError):
        await mem.document_tags("casa", "m-1")
    assert len(mem._session.calls) == 2


async def test_hindsight_read_propagates_a_timeout_without_retrying():
    mem = _memory(_Resp(0, exc=asyncio.TimeoutError()))
    with pytest.raises(asyncio.TimeoutError):
        await mem.document_tags("casa", "m-1")
    assert len(mem._session.calls) == 1


async def test_hindsight_read_validates_the_bank_before_any_request():
    mem = _memory()
    with pytest.raises(ValueError):
        await mem.document_tags("Bad Bank!", "m-1")
    assert mem._session.calls == []


class _HttpBank:
    """The real adapter's HTTP seam over a bank with Hindsight 0.10.2's measured
    semantics: a retain REPLACES a document's tag set (M-5); a GET returns the
    document's current tags (M-6), and a document never saved answers 404 with
    the measured ``{"detail":"Document not found"}`` (M-7). ``drifted`` set to a
    body makes EVERY read answer 404 with it — a server (or anything in front of
    it) whose answers differ from the measured ones."""

    def __init__(self) -> None:
        self.docs: dict[str, list[str]] = {}
        self.retains = 0
        self.drifted: str | None = None
        self.closed = False

    def request(self, method, url, json=None, **kw):  # noqa: A002 — aiohttp's name
        bank = self

        class _R:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def raise_for_status(self):
                return None

            async def json(self):
                return {"success": True}

            async def text(self):
                return self.body

        resp = _R()
        if method == "POST" and url.endswith("/memories"):
            bank.retains += 1
            for item in json["items"]:
                bank.docs[item["document_id"]] = list(item["tags"])
            resp.status, resp.body = 200, '{"success": true}'
        elif method == "GET" and "/documents/" in url:
            tags = bank.docs.get(url.rsplit("/", 1)[1])
            if bank.drifted is not None:
                resp.status, resp.body = 404, bank.drifted
            elif tags is None:
                resp.status, resp.body = 404, '{"detail":"Document not found"}'
            else:
                import json as _json
                resp.status, resp.body = 200, _json.dumps({"tags": tags})
        else:  # pragma: no cover — no other route is used
            raise AssertionError((method, url))
        return resp


@pytest.mark.parametrize("drifted", ["Document not found", '"Document not found"'])
async def test_a_malformed_404_never_lets_a_delegated_save_lower_a_stored_tier(
        monkeypatch, drifted):
    """The bank holds a document saved ``private``; the server then answers the
    read of it with a 404 whose body is not the measured JSON object. That answer
    is not "never saved": the save is skipped, so the stored ``private`` stands."""
    http = _HttpBank()
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._session = http
    turns = [RetainedTurn("The garage code is 4417.", STUB_USER_PROV)]
    monkeypatch.setattr(delegated_memory, "classify_tier", _fixed("private"))
    await delegated_memory.retain_delegated(mem, origin_channel="telegram", turns=turns)
    assert http.retains == 1
    assert [[t for t in tags if t in TIERS] for tags in http.docs.values()] == [["private"]]

    http.drifted = drifted
    monkeypatch.setattr(delegated_memory, "classify_tier", _fixed("public"))
    await delegated_memory.retain_delegated(mem, origin_channel="telegram", turns=turns)
    assert http.retains == 1
    assert [[t for t in tags if t in TIERS] for tags in http.docs.values()] == [["private"]]


async def test_the_measured_404_still_saves_through_the_real_adapter(monkeypatch):
    http = _HttpBank()
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._session = http
    monkeypatch.setattr(delegated_memory, "classify_tier", _fixed("friends"))
    await delegated_memory.retain_delegated(
        mem, origin_channel="telegram",
        turns=[RetainedTurn("The garage code is 4417.", STUB_USER_PROV)])
    assert http.retains == 1
    assert [[t for t in tags if t in TIERS] for tags in http.docs.values()] == [["friends"]]
