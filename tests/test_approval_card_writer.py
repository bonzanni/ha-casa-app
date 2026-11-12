"""The isolated model call that writes an approval card: budget, concurrency,
cache, bounded input and a fallback that never fails. The SDK is never
reached: every test replaces ``approval_card._ask``."""

import asyncio
import json
import time
from dataclasses import replace

import pytest

import approval_card
from approval_card import Card, CardRequest, CardLine, clear_cache, write_card

VALID_REPLY = json.dumps({
    "action": "send an email to Marco",
    "summary": "Sends the invoice for March.",
    "lines": [{"label": "To", "value": "marco@example.com", "sensitive": False}],
})
FALLBACK = Card(title="🔐 Allow Ellen to use gmail?", lines=(CardLine("To", "marco@example.com"),))


def req_n(i, **kw):
    return CardRequest(kind="tool", who="Ellen", facts={"tool": "send", "n": i},
                       fallback=Card(title=f"fallback {i}"), **kw)


REQ = CardRequest(kind="tool", who="Ellen", facts={"tool": "send", "to": "marco@example.com"},
                  fallback=FALLBACK)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


class Stub:
    def __init__(self, replies=(VALID_REPLY,), delay=0.0, hang=False):
        self.replies = list(replies)
        self.delay = delay
        self.hang = hang
        self.calls = 0
        self.running = 0
        self.max_concurrent = 0
        self._entered = asyncio.Event()
        self._want = 0

    async def __call__(self, system, user):
        self.calls += 1
        self.running += 1
        self.max_concurrent = max(self.max_concurrent, self.running)
        if self.running >= self._want:
            self._entered.set()
        try:
            if self.hang:
                await asyncio.Event().wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.replies[min(self.calls, len(self.replies)) - 1]
        finally:
            self.running -= 1

    async def entered(self, n):
        self._want = n
        if self.running >= n:
            return
        self._entered.clear()
        await asyncio.wait_for(self._entered.wait(), 5)


@pytest.fixture
def stub_ask(monkeypatch):
    stub = Stub()
    monkeypatch.setattr(approval_card, "_ask", stub)
    return stub


@pytest.fixture
def stub_ask_hangs(monkeypatch):
    stub = Stub(hang=True)
    monkeypatch.setattr(approval_card, "_ask", stub)
    return stub


@pytest.fixture
def stub_ask_invalid(monkeypatch):
    stub = Stub(replies=("not json",))
    monkeypatch.setattr(approval_card, "_ask", stub)
    return stub


@pytest.fixture
def stub_ask_slow_invalid(monkeypatch):
    stub = Stub(replies=("not json",), delay=2.0)
    monkeypatch.setattr(approval_card, "_ask", stub)
    return stub


async def test_a_valid_reply_becomes_the_card(stub_ask):
    card = await write_card(REQ)
    assert card.title == "🔐 Allow Ellen to send an email to Marco?"


async def test_timeout_returns_the_fallback(stub_ask_hangs):
    t0 = time.monotonic()
    card = await write_card(replace(REQ, budget_s=0.2))
    assert card is REQ.fallback and time.monotonic() - t0 < 0.5


async def test_one_retry_for_an_invalid_reply_then_fallback(stub_ask_invalid):
    assert await write_card(REQ) is REQ.fallback and stub_ask_invalid.calls == 2


async def test_a_retry_can_recover(monkeypatch):
    stub = Stub(replies=("not json", VALID_REPLY))
    monkeypatch.setattr(approval_card, "_ask", stub)
    card = await write_card(REQ)
    assert stub.calls == 2 and card.title.startswith("🔐 Allow Ellen to ")


async def test_no_retry_when_less_than_six_seconds_remain(stub_ask_slow_invalid):
    req = replace(REQ, budget_s=7.0)
    assert await write_card(req) is req.fallback and stub_ask_slow_invalid.calls == 1  # first reply after 2 s


async def test_a_backend_error_returns_the_fallback(monkeypatch):
    async def boom(system, user):
        raise RuntimeError("cli exploded")
    monkeypatch.setattr(approval_card, "_ask", boom)
    assert await write_card(REQ) is REQ.fallback


async def test_a_pathologically_nested_reply_is_invalid_and_retried(monkeypatch):
    nested = '{"a":' * 100_000 + "1" + "}" * 100_000
    with pytest.raises(RecursionError):
        json.loads(nested)                                           # the reply really is hostile
    stub = Stub(replies=(nested,))
    monkeypatch.setattr(approval_card, "_ask", stub)
    assert await write_card(REQ) is REQ.fallback and stub.calls == 2


async def test_a_fenced_reply_becomes_the_card(monkeypatch):
    stub = Stub(replies=(f"```json\n{VALID_REPLY}\n```",))
    monkeypatch.setattr(approval_card, "_ask", stub)
    card = await write_card(REQ)
    assert stub.calls == 1 and card.title == "🔐 Allow Ellen to send an email to Marco?"


async def test_a_reply_with_a_leading_sentence_becomes_the_card(monkeypatch):
    stub = Stub(replies=(f"Here is the card: {VALID_REPLY} Hope that helps.",))
    monkeypatch.setattr(approval_card, "_ask", stub)
    assert (await write_card(REQ)).title == "🔐 Allow Ellen to send an email to Marco?"


def test_a_reply_without_braces_is_not_a_card():
    assert approval_card.card_from_reply("no card here", who="Ellen") is None
    assert approval_card.card_from_reply("} backwards {", who="Ellen") is None


async def test_the_failure_log_carries_the_message(monkeypatch, caplog):
    async def boom(system, user):
        raise RuntimeError("cli exploded")
    monkeypatch.setattr(approval_card, "_ask", boom)
    with caplog.at_level("WARNING", logger="approval_card"):
        assert await write_card(REQ) is REQ.fallback
    assert "RuntimeError: cli exploded" in caplog.text


async def test_waiting_for_a_slot_counts_against_the_budget(stub_ask_hangs):
    holders = [asyncio.create_task(write_card(replace(req_n(i), budget_s=30))) for i in range(3)]
    await stub_ask_hangs.entered(3)                                  # all three slots now held
    late = replace(req_n(9), budget_s=0.3)
    t0 = time.monotonic()
    card = await write_card(late)
    assert card is late.fallback and time.monotonic() - t0 < 0.45    # its own budget, not after a slot frees
    assert stub_ask_hangs.max_concurrent == 3
    for h in holders:
        h.cancel()
    await asyncio.gather(*holders, return_exceptions=True)


async def test_identical_facts_reuse_the_cached_card(stub_ask):
    await write_card(REQ)
    await write_card(REQ)
    assert stub_ask.calls == 1


async def test_a_cache_hit_uses_this_requests_who_and_title(stub_ask):
    await write_card(REQ)
    other = replace(REQ, who="Alex", title="🔐 Uninstall gmail: erase its data too?")
    card = await write_card(other)
    assert stub_ask.calls == 1 and card.title == "🔐 Uninstall gmail: erase its data too?"
    card2 = await write_card(replace(REQ, who="Alex"))
    assert card2.title.startswith("🔐 Allow Alex to ")


async def test_a_failed_request_is_not_cached(monkeypatch):
    stub = Stub(replies=("not json", "not json", VALID_REPLY))
    monkeypatch.setattr(approval_card, "_ask", stub)
    assert await write_card(REQ) is REQ.fallback
    assert (await write_card(REQ)).title.startswith("🔐 Allow Ellen to ")


async def test_cache_key_includes_the_model(stub_ask, monkeypatch):
    await write_card(REQ)
    monkeypatch.setattr(approval_card, "MODEL_SHORTNAME", "haiku")
    await write_card(REQ)
    assert stub_ask.calls == 2


async def test_the_cache_is_bounded(stub_ask, monkeypatch):
    monkeypatch.setattr(approval_card, "CACHE_SIZE", 2)
    for i in range(3):
        await write_card(req_n(i))
    await write_card(req_n(0))                                      # evicted, asked again
    assert stub_ask.calls == 4


async def test_the_cache_expires(stub_ask, monkeypatch):
    monkeypatch.setattr(approval_card, "CACHE_TTL_S", 0.0)
    await write_card(REQ)
    await write_card(REQ)
    assert stub_ask.calls == 2


def test_facts_for_the_model_are_bounded():
    wide_schema = {"type": "object", "properties": {f"p{i}": {"type": "string", "description": "d" * 60}
                                                    for i in range(400)}}
    deep = cur = {}
    for _ in range(20):
        cur["n"] = {}
        cur = cur["n"]
    big = {"arguments": {"body": "x" * 50_000, "items": list(range(500)), "deep": deep},
           "tool_description": "d" * 9000, "input_schema": wide_schema}
    out = approval_card.facts_json(big)
    assert len(out) <= approval_card.FACTS_JSON_MAX
    parsed = json.loads(out)
    assert len(parsed["arguments"]["items"]) <= approval_card.FACTS_LIST_MAX + 1   # + one "…N more" marker


def test_a_wide_schema_alone_is_cut_as_text():
    wide = {"properties": {f"p{i}": {"type": "string"} for i in range(2000)}}
    parsed = json.loads(approval_card.facts_json({"input_schema": wide}))
    assert isinstance(parsed["input_schema"], str)
    assert len(parsed["input_schema"]) <= approval_card.SCHEMA_JSON_MAX + 30


def test_many_strings_at_the_per_string_cap_still_fit():
    args = {f"k{i}": "x" * 4000 for i in range(6)}                  # 24 075 characters before the final reduction
    out = approval_card.facts_json({"arguments": args})
    assert len(out) <= approval_card.FACTS_JSON_MAX and json.loads(out)


def test_an_accepted_huge_install_terminates_within_the_cap():
    plugins = [{"name": f"pl{i}", "version": "1.0.0",
                "protected_tools": [{"name": chr(97 + j % 26) + str(j), "summary": None} for j in range(50)]}
               for i in range(9)]
    t0 = time.monotonic()
    out = approval_card.facts_json({"kind": "specialist_install", "requested_by": "Ellen",
                                    "bundled_plugins": plugins})
    assert time.monotonic() - t0 < 1.0 and len(out) <= approval_card.FACTS_JSON_MAX and json.loads(out)


def test_a_wide_mapping_is_cut():
    parsed = json.loads(approval_card.facts_json({"arguments": {f"k{i:04d}": i for i in range(3000)}}))
    assert len(parsed["arguments"]) <= approval_card.FACTS_LIST_MAX + 1


def test_the_terminal_reduction_stays_valid_json_when_escaping_expands_the_text():
    # Quotes double in size once escaped; the last-resort cut must still fit.
    plugins = [{"name": '"' * 4000, "protected_tools": ['"' * 4000] * 50} for _ in range(9)]
    out = approval_card.facts_json({"kind": "specialist_install", "requested_by": "Ellen",
                                    "bundled_plugins": plugins})
    parsed = json.loads(out)
    assert len(out) <= approval_card.FACTS_JSON_MAX and parsed["kind"] == "specialist_install"


def test_lone_surrogates_do_not_reach_the_model():
    out = approval_card.facts_json({"arguments": {"body": "a\ud800b"}})
    out.encode("utf-8")                                              # must not raise
