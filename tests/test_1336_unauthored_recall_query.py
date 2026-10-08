"""#1336: a session no person opened recalls with a short topical query.

Automatic recall searches with the opening turn's text. When no person
authored that text at ingress (no ``trusted_user_origin``) it is Casa's own:
a trigger's instruction prompt, a delegation notice, an event. Sent as the
recall query, the backend's CPU reranker scored hundreds of tokens against
every candidate (12–20 s measured) and the recall missed its deadline every
time. Such a turn now recalls with a fixed topical query under a longer
deadline; a turn a person typed keeps its own text and the short deadline.

Reuses the real-``_process`` harness of
tests/test_agent_auto_recall_unavailable.py.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

import agent as agent_mod
from bus import BusMessage, MessageType
from ingress_identity import ingress_identity
from semantic_memory import SemanticMemory

try:
    from tests.test_agent_auto_recall_unavailable import _CaptureClient, _agent
except ImportError:
    from test_agent_auto_recall_unavailable import _CaptureClient, _agent

pytestmark = [pytest.mark.unit]

TRIGGER_PROMPT = (
    "Silent heartbeat self-check. The DEFAULT ACTION is to produce zero "
    "output text and end the turn. Most heartbeats produce no message."
)
NOTICE = "[System notification: your delegation to configurator finished.]"
PERSON_TEXT = "what did the plumber say about the boiler?"


class _RecordingSem(SemanticMemory):
    """Records each recall's query; optionally takes ``delay`` seconds.
    ``completed`` counts recalls that ran to the end (not cancelled)."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.queries: list[str] = []
        self.completed = 0

    async def retain(self, bank, items, *, async_=True):
        return None

    async def recall(self, bank, query, *, tags, max_tokens,
                     types=("world", "experience", "observation"),
                     tags_match="any", budget="mid"):
        return ""

    async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                           types=("world", "experience", "observation"),
                           tags_match="any", budget="mid"):
        self.queries.append(query)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.completed += 1
        return ()

    async def document_tags(self, bank, document_id):
        return None

    async def profile(self, bank):
        return ""


def _scheduled(chat_id: str, **extra) -> BusMessage:
    return BusMessage(
        type=MessageType.SCHEDULED, source="scheduler", target="assistant",
        content=TRIGGER_PROMPT, channel="telegram",
        context={"chat_id": chat_id, "trigger": "heartbeat",
                 "_scheduled_delivery": True, **extra},
    )


def _notice(chat_id: str) -> BusMessage:
    """A delegation-completion narration turn: synthesized, never scheduled."""
    return BusMessage(
        type=MessageType.REQUEST, source="telegram", target="assistant",
        content=NOTICE, channel="telegram", context={"chat_id": chat_id},
    )


def _person(chat_id: str, *, operator: bool = True) -> BusMessage:
    """A message a person typed: Telegram ingress stamps a trusted origin."""
    return BusMessage(
        type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
        content=PERSON_TEXT, channel="telegram",
        context={"chat_id": chat_id},
        trusted_user_origin=ingress_identity(
            "telegram", sender_id="4242", sender_display_name="someone",
            sender_is_operator=operator),
    )


async def _queries(tmp_path, msg, sem=None) -> _RecordingSem:
    sem = sem or _RecordingSem()
    agent = _agent(tmp_path, sem)
    with patch("sdk_client_pool._default_make_client", _CaptureClient):
        await agent._process(msg)
    return sem


def test_the_fixed_query_is_short_and_topical():
    """A short topical phrase, nowhere near a trigger prompt's size (the
    length is what made the rerank slow)."""
    q = agent_mod._UNAUTHORED_RECALL_QUERY
    assert isinstance(q, str) and q.strip()
    assert len(q) <= 80
    assert agent_mod._UNAUTHORED_AUTO_RECALL_TIMEOUT_S > agent_mod._AUTO_RECALL_TIMEOUT_S


async def test_scheduled_fresh_turn_recalls_with_the_fixed_query(tmp_path):
    sem = await _queries(tmp_path, _scheduled("interval-heartbeat"))
    assert sem.queries == [agent_mod._UNAUTHORED_RECALL_QUERY]


async def test_notice_fresh_turn_recalls_with_the_fixed_query(tmp_path):
    """Prod's 21:58Z miss: a delegation notice opened a fresh session and was
    sent whole as the query. It is not scheduled; no person wrote it."""
    sem = await _queries(tmp_path, _notice("c-notice"))
    assert sem.queries == [agent_mod._UNAUTHORED_RECALL_QUERY]


@pytest.mark.parametrize("operator", [True, False])
async def test_a_person_typed_opening_recalls_with_its_own_text(tmp_path, operator):
    """The operator, or another person Telegram admits, keeps their words."""
    sem = await _queries(tmp_path, _person("c-person", operator=operator))
    assert len(sem.queries) == 1
    assert sem.queries[0].endswith(PERSON_TEXT)
    assert sem.queries[0] != agent_mod._UNAUTHORED_RECALL_QUERY


async def test_a_context_key_cannot_claim_an_author(tmp_path):
    """The stamp is computed from the trusted ingress, not copied from the
    message context: a context saying otherwise changes nothing."""
    sem = await _queries(
        tmp_path, _scheduled("interval-ctx", _unauthored_opening=False))
    assert sem.queries == [agent_mod._UNAUTHORED_RECALL_QUERY]


async def test_unauthored_turn_waits_past_the_person_deadline(tmp_path, monkeypatch):
    """A recall slower than the short deadline but inside the longer one
    completes on a turn no person opened and is cancelled on a person's."""
    monkeypatch.setattr(agent_mod, "_AUTO_RECALL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(agent_mod, "_UNAUTHORED_AUTO_RECALL_TIMEOUT_S", 2.0)
    (tmp_path / "s").mkdir()
    (tmp_path / "p").mkdir()

    sched = await _queries(tmp_path / "s", _scheduled("interval-slow"),
                           _RecordingSem(delay=0.3))
    assert sched.completed == 1

    person = await _queries(tmp_path / "p", _person("c-slow"),
                            _RecordingSem(delay=0.3))
    assert len(person.queries) == 1
    assert person.completed == 0


async def test_the_longer_deadline_still_bounds_the_turn(tmp_path, monkeypatch):
    """A hung backend on a turn no person opened is cancelled and the turn
    proceeds without a memory block."""
    monkeypatch.setattr(agent_mod, "_UNAUTHORED_AUTO_RECALL_TIMEOUT_S", 0.1)
    t0 = time.monotonic()
    sem = await _queries(tmp_path, _scheduled("interval-hung"),
                         _RecordingSem(delay=30))
    assert time.monotonic() - t0 < 5.0
    assert sem.completed == 0
    assert "<memory_context>" not in (_CaptureClient.captured_options.system_prompt or "")
