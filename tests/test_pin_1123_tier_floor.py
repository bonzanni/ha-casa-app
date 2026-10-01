# tests/test_pin_1123_tier_floor.py
"""#1123 red cases: a save never lowers a memory's stored privacy tier.

Every case drives a REAL writer (``retain_cold_session``, ``retry_spooled_cold_retains``,
``save_session``, ``retain_delegated``) into one fake bank with the memory server's
measured semantics (Hindsight 0.10.2): a retain under an existing ``document_id``
REPLACES that document's tag set, and ``document_tags`` returns the CURRENT set, or None
for a document never saved. A ``private`` that comes from a classifier FAILURE is
produced by the real ``classify_tier`` over a fake SDK module, never by a new symbol.
The server itself is not reached: its replace-and-read semantics are pinned only
through this fake."""
from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock

import pytest

import delegated_memory
import session_saver
import tier_classifier
from hindsight_ids import content_document_id
from personality_types import RetainedTurn
from sensitivity import TIER_FORMAT_REMINDER, TIERS, parse_tier, tier_evidence
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV
from speaker_provenance import RESERVED_SOURCE_NAMESPACE, encode_provenance_tag

pytestmark = [pytest.mark.unit]

MARK = "casa-tier-unverified"
USER_LINE = "The garage code is 4417."
MODEL_LINE = "Noted."
_UNSET = object()


class _Bank:
    """M-5 replace on retain; M-6/M-7 read of one document's current tags."""

    def __init__(self) -> None:
        self.docs: dict[str, list[str]] = {}
        self.reads = 0
        self.retains = 0
        self.read_error: BaseException | None = None
        self.fail_ids: set[str] = set()   # empty = every read fails when read_error is set
        self.read_value: object = _UNSET

    async def document_tags(self, bank, document_id):
        self.reads += 1
        if self.read_error is not None and (not self.fail_ids or document_id in self.fail_ids):
            raise self.read_error
        if self.read_value is not _UNSET:
            return self.read_value
        tags = self.docs.get(document_id)
        return None if tags is None else frozenset(tags)

    async def retain(self, bank, items, *, async_=True):
        self.retains += 1
        for item in items:
            self.docs[item["document_id"]] = list(item["tags"])

    def tiers(self) -> list[list[str]]:
        return [[t for t in tags if t in TIERS] for tags in self.docs.values()]


class _Msg:
    def __init__(self, mtype: str, text: str) -> None:
        self.type = mtype
        self.message = {"role": mtype, "content": text}


def _transcript(_sid, _directory):
    return [_Msg("user", USER_LINE), _Msg("assistant", MODEL_LINE)]


def _snapshot():
    from agent import snapshot_session_entry
    from speaker_provenance import provenance_mapping
    return snapshot_session_entry({
        "agent": "resident:assistant", "sdk_session_id": "s1",
        "speaker_provenance": provenance_mapping(STUB_SPEAKER_PROV),
        "user_provenance": provenance_mapping(STUB_USER_PROV),
    })


def _fixed(tier: str):
    async def classify(_text: str) -> str:
        return tier
    return classify


class _Counting:
    def __init__(self, tier: str) -> None:
        self.tier = tier
        self.calls = 0

    async def __call__(self, _text: str) -> str:
        self.calls += 1
        return self.tier


def _install_sdk(monkeypatch, reply_for):
    """A fake ``claude_agent_sdk``: ``reply_for(prompt)`` returns the reply text, or
    raises to model a backend failure."""
    fake = types.ModuleType("claude_agent_sdk")

    class _Text:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:  # noqa: N801 — mirrors the SDK name
        def __init__(self, text):
            self.content = [_Text(text)]

    class ClaudeAgentOptions:  # noqa: N801
        def __init__(self, **kw):
            self.kw = kw

    async def query(*, prompt, options):  # noqa: ARG001
        yield AssistantMessage(reply_for(prompt))

    fake.AssistantMessage = AssistantMessage
    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    monkeypatch.setattr(tier_classifier, "_RETRY_BACKOFF_S", 0)


def _backend_down(_prompt):
    raise RuntimeError("classifier backend down")


async def _cold_save(monkeypatch, bank, classify, retry_dir):
    monkeypatch.setattr(session_saver, "classify_tier", classify)
    monkeypatch.setattr(session_saver, "get_session_messages", _transcript)
    await session_saver.retain_cold_session(
        _snapshot(), directory="/tmp", channel="telegram",
        semantic_memory=bank, retry_dir=retry_dir,
    )


async def _delegated_save(monkeypatch, bank, classify, *, application_tags=(), turns=None):
    monkeypatch.setattr(delegated_memory, "classify_tier", classify)
    await delegated_memory.retain_delegated(
        bank, origin_channel="telegram",
        turns=turns if turns is not None else [
            RetainedTurn(USER_LINE, STUB_USER_PROV),
            RetainedTurn(MODEL_LINE, STUB_SPEAKER_PROV),
        ],
        application_tags=application_tags,
    )


async def test_rc1_stored_real_tier_never_lowers(monkeypatch, tmp_path):
    cold = _Bank()
    await _cold_save(monkeypatch, cold, _fixed("private"), tmp_path)
    for later in ("public", "friends"):
        await _cold_save(monkeypatch, cold, _fixed(later), tmp_path)
        assert len(cold.docs) == 2
        assert cold.tiers() == [["private"], ["private"]]
    assert cold.retains == 3
    assert cold.reads >= 2

    delegated = _Bank()
    await _delegated_save(monkeypatch, delegated, _fixed("private"))
    for later in ("public", "friends"):
        await _delegated_save(monkeypatch, delegated, _fixed(later))
        assert len(delegated.docs) == 2
        assert delegated.tiers() == [["private"], ["private"]]
    assert delegated.retains == 3


async def test_rc2_unmarked_existing_document_has_floor(monkeypatch, tmp_path):
    bank = _Bank()
    user_doc = content_document_id(STUB_USER_PROV.user_peer, USER_LINE)
    bank.docs[user_doc] = [
        "private", encode_provenance_tag(STUB_USER_PROV), "casa-scheduled"]

    await _cold_save(monkeypatch, bank, _fixed("public"), tmp_path)

    assert len(bank.docs) == 2
    stored = bank.docs[user_doc]
    assert [t for t in stored if t in TIERS] == ["private"]
    assert MARK not in stored
    assert stored.count("casa-scheduled") == 0   # the mark stays last-save-wins


@pytest.mark.parametrize("real", ["friends", "family"])
async def test_rc3_fallback_preserves_real_floor_unmarked(monkeypatch, tmp_path, real):
    bank = _Bank()
    await _cold_save(monkeypatch, bank, _fixed(real), tmp_path)

    _install_sdk(monkeypatch, _backend_down)
    await _cold_save(monkeypatch, bank, tier_classifier.classify_tier, tmp_path)
    assert len(bank.docs) == 2
    assert bank.tiers() == [[real], [real]]
    assert all(MARK not in tags for tags in bank.docs.values())

    await _cold_save(monkeypatch, bank, _fixed("public"), tmp_path)
    assert len(bank.docs) == 2
    assert bank.tiers() == [[real], [real]]
    assert all(MARK not in tags for tags in bank.docs.values())


def _break(bank: _Bank, how: str) -> None:
    if how == "raises":
        bank.read_error = RuntimeError("memory store unreachable")
    else:
        bank.read_value = MagicMock()   # neither None nor a set of strings


@pytest.mark.parametrize("how", ["raises", "malformed"])
async def test_rc4_unreadable_floor_prevents_entire_save(monkeypatch, tmp_path, how):
    # W3 (gap) / W2 (reset) cold retain: nothing retained, one spool record.
    cold = _Bank()
    _break(cold, how)
    await _cold_save(monkeypatch, cold, _fixed("public"), tmp_path)
    assert cold.retains == 0
    assert cold.docs == {}
    records = list(tmp_path.glob("*.json"))
    assert len(records) == 1

    # W4 spool retry: nothing retained, the attempt is counted.
    await session_saver.retry_spooled_cold_retains(cold, retry_dir=tmp_path)
    assert cold.retains == 0
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert json.loads(records[0].read_text())["attempts"] == 1

    # W1 freshness save: False, nothing retained, entry kept with its claim released.
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "sessions.json"))
    await reg.register(
        "telegram-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST,
        speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    swept = _Bank()
    _break(swept, how)
    monkeypatch.setattr(session_saver, "get_session_messages", _transcript)
    ok = await session_saver.save_session(
        "telegram-r1", reg, swept, directory="/d", channel="telegram")
    assert ok is False
    assert swept.retains == 0
    entry = reg.get("telegram-r1")
    assert entry is not None
    assert not entry.get("consolidated_at")

    # W5-W7 delegated: returns normally, nothing retained.
    delegated = _Bank()
    _break(delegated, how)
    await _delegated_save(monkeypatch, delegated, _fixed("public"))
    assert delegated.retains == 0

    # One failed read among two withholds the whole batch.
    mixed = _Bank()
    mixed.read_error = RuntimeError("memory store busy")
    mixed.fail_ids = {content_document_id(STUB_USER_PROV.user_peer, USER_LINE)}
    await _delegated_save(monkeypatch, mixed, _fixed("public"))
    assert mixed.retains == 0
    assert mixed.docs == {}


async def test_rc5_callers_cannot_supply_tier_namespace(monkeypatch):
    bank = _Bank()
    classify = _Counting("public")
    await _delegated_save(monkeypatch, bank, classify, application_tags=(MARK,))
    assert (classify.calls, bank.reads, bank.retains) == (0, 0, 0)

    bank = _Bank()
    classify = _Counting("public")
    await _delegated_save(monkeypatch, bank, classify, turns=[
        RetainedTurn(USER_LINE, STUB_USER_PROV),
        RetainedTurn(MODEL_LINE, STUB_SPEAKER_PROV),
        # a duplicate that dedup would collapse still carries the forged tag
        RetainedTurn(USER_LINE, STUB_USER_PROV, application_tags=("casa-tier-anything",)),
    ])
    assert (classify.calls, bank.reads, bank.retains) == (0, 0, 0)


_CONFLICT_FIRST = "This must stay confidential.\nprivate"


async def test_rc6_cross_ask_conflict_establishes_real_floor(monkeypatch, tmp_path):
    assert parse_tier(_CONFLICT_FIRST) is None
    assert tier_evidence(_CONFLICT_FIRST) == ["private"]

    def reply_for(prompt):
        return "public" if TIER_FORMAT_REMINDER in prompt else _CONFLICT_FIRST

    bank = _Bank()
    _install_sdk(monkeypatch, reply_for)
    await _cold_save(monkeypatch, bank, tier_classifier.classify_tier, tmp_path)
    assert len(bank.docs) == 2
    assert bank.tiers() == [["private"], ["private"]]
    assert all(MARK not in tags for tags in bank.docs.values())

    await _cold_save(monkeypatch, bank, _fixed("public"), tmp_path)
    assert len(bank.docs) == 2
    assert bank.tiers() == [["private"], ["private"]]


async def test_rc7_first_fallback_is_provisional(monkeypatch, tmp_path):
    bank = _Bank()
    _install_sdk(monkeypatch, _backend_down)
    await _cold_save(monkeypatch, bank, tier_classifier.classify_tier, tmp_path)

    assert len(bank.docs) == 2
    for tags in bank.docs.values():
        assert [t for t in tags if t in TIERS] == ["private"]
        assert tags.count(MARK) == 1
        assert sum(1 for t in tags if t.startswith(RESERVED_SOURCE_NAMESPACE)) == 1
