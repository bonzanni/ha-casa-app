"""#1117 red cases: recall shows when a memory was recorded and whether a
scheduled turn last saved it; scheduled-turn output is saved dated and marked
(the operator's answers and delegated results stay ordinary); a busy memory
server (503) is retried once; the assistant's doctrine carries the live-state
rule.

Every test here reaches new parameters ADAPTIVELY (``inspect.signature``) or
seeds persisted state in its on-disk form, so on the pre-fix tree each one fails
on the behaviour it pins, never on an import error or an unexpected keyword.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import aiohttp
import pytest

import hindsight_memory
import session_saver
from hindsight_memory import HindsightSemanticMemory
from personality_types import SpeakerProvenance
from recall_renderer import render_recall
from semantic_memory import RecallUnavailable
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV
from session_registry import SessionRegistry
from speaker_provenance import encode_provenance_tag
from timekeeping import compose_time_envelope, resolve_tz
from trait_renderer import estimate_tokens_v1

MARK = "casa-scheduled"
AMS = ZoneInfo("Europe/Amsterdam")
SYSTEM = SpeakerProvenance(speaker_kind="system")
LIVE_STATE_RULE = (
    "Before stating changeable household or device state as current, read it "
    "with a live tool in this turn or say plainly that you have not checked "
    "it; a recalled memory, however recent its date and whether or not it was "
    "saved by a scheduled turn, is history, not verification."
)


@pytest.fixture
def amsterdam(monkeypatch):
    monkeypatch.setenv("CASA_TZ", "Europe/Amsterdam")
    resolve_tz.cache_clear()
    yield
    resolve_tz.cache_clear()


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _accepts(fn, name: str) -> bool:
    return name in inspect.signature(fn).parameters


class _Msg:
    def __init__(self, type_, text):
        self.type = type_
        self.message = {"role": type_, "content": text}


def _env(local: datetime) -> str:
    return compose_time_envelope(local)


def _ts(local: datetime) -> str:
    return local.isoformat(timespec="seconds")


def _is_marked(item) -> int:
    return int(MARK in item["tags"])


def _tier_and_source_counts(item) -> tuple[int, int]:
    tiers = sum(1 for t in item["tags"] if t in {"public", "friends", "family", "private"})
    sources = sum(1 for t in item["tags"] if t.startswith("casa-source-"))
    return tiers, sources


async def _private(_text: str) -> str:
    return "private"


def _synth_completion(**kw) -> str:
    """The REAL delegation-completion user-turn text (``_synthesize_delegation_turn``)."""
    import agent
    from bus import BusMessage, MessageType
    from specialist_registry import DelegationComplete

    complete = DelegationComplete(
        delegation_id="abcdef1234567890", agent="finance",
        origin={"user_text": "This is your weekday morning briefing.",
                "chat_id": "cron-morning-briefing", "_scheduled_delivery": True},
        **kw,
    )
    msg = BusMessage(
        type=MessageType.NOTIFICATION, source="finance", target="assistant",
        content=complete, channel="telegram",
        context={"chat_id": "cron-morning-briefing"},
    )
    return str(agent.Agent._synthesize_delegation_turn(types.SimpleNamespace(), msg).content)


def _terminal(kind: str) -> str:
    import scheduled_asks
    return scheduled_asks._terminal_text("r1", kind, "casa_shutdown", "Submit it")


T0 = datetime(2026, 9, 30, 8, 0, 0, tzinfo=AMS)
T1 = datetime(2026, 9, 30, 8, 5, 0, tzinfo=AMS)
T2 = datetime(2026, 9, 30, 8, 9, 0, tzinfo=AMS)
T3 = datetime(2026, 9, 30, 9, 0, 0, tzinfo=AMS)


def _scheduled_transcript() -> list[_Msg]:
    """Eight distinct turns of one scheduled session (RC4/RC6)."""
    return [
        _Msg("user", _env(T0) + "This is your weekday morning briefing."),
        _Msg("assistant", "- Invoice INV-7 is still a draft."),
        _Msg("user", _env(T1) + _terminal("answered")),
        _Msg("assistant", "Submitted INV-7 as you asked."),
        _Msg("user", _env(T2) + _synth_completion(status="ok", text="INV-7 submitted.")),
        _Msg("assistant", "Finance confirms INV-7 went out."),
        _Msg("user", _env(T3) + _terminal("no_answer")),
        _Msg("assistant", "<silent/>"),
    ]


async def _items(messages, *, scheduled, user_provenance=SYSTEM):
    kw = {"scheduled": scheduled} if _accepts(session_saver.transcript_to_items, "scheduled") else {}
    return await session_saver.transcript_to_items(
        messages, speaker_provenance=STUB_SPEAKER_PROV,
        user_provenance=user_provenance, **kw,
    )


# ---------------------------------------------------------------------------
# RC1 — R1: a recalled memory shows when it was recorded
# ---------------------------------------------------------------------------


def _result(text: str, *, tier: str = "private", extra_tags=(), **fields) -> dict:
    return {"text": text, "type": "world",
            "tags": [tier, encode_provenance_tag(STUB_SPEAKER_PROV), *extra_tags],
            **fields}


async def test_rc1_recorded_date_is_local_optional_and_budgeted(amsterdam):
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"results": [
        _result("fact dated", mentioned_at="2026-09-29T23:30:00Z"),
        _result("fact absent"),
        _result("fact garbage", mentioned_at="garbage"),
        _result("fact naive", mentioned_at="2026-09-29T23:30:00"),
    ]})
    hits = await mem.recall_items("casa", "q", tags=["private"], max_tokens=500,
                                  clearance="private")
    assert len(hits) == 4
    rendered = render_recall(hits, current_speaker=STUB_SPEAKER_PROV,
                             surface="text", clearance="private", token_budget=10_000)
    # 23:30 UTC on Tue 29 Sep is Wed 30 Sep 01:30 in Amsterdam: local, absolute.
    assert rendered.splitlines().count("  [recorded Wed 30 Sep 2026]") == 1
    assert rendered.count("[recorded ") == 1

    # The label counts inside the budget: the independently built entry fits at
    # exactly its own estimate and not one token below it.
    single = [h for h in hits if h.text == "fact dated"]
    expected = "\n".join([
        "- Tester previously said: fact dated",
        "  [source: resident:assistant, casa/tester@0.1.0]",
        "  [recorded Wed 30 Sep 2026]",
    ])
    budget = estimate_tokens_v1(expected)
    assert render_recall(single, current_speaker=STUB_SPEAKER_PROV, surface="text",
                         clearance="private", token_budget=budget) == expected
    assert render_recall(single, current_speaker=STUB_SPEAKER_PROV, surface="text",
                         clearance="private", token_budget=budget - 1) == ""


# ---------------------------------------------------------------------------
# RC2 — both recall payloads carry the same local query_timestamp
# ---------------------------------------------------------------------------


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 9, 29, 23, 30, 0, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)


async def test_rc2_both_recall_payloads_have_local_query_timestamp(amsterdam, monkeypatch):
    monkeypatch.setattr(hindsight_memory, "datetime", _FrozenDatetime, raising=False)
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"results": []})
    await mem.recall("casa", "q", tags=["public"], max_tokens=100)
    await mem.recall_items("casa", "q", tags=["public"], max_tokens=100, clearance="public")
    payloads = [call.args[2] for call in mem._request.await_args_list]
    assert len(payloads) == 2
    assert [p.get("query_timestamp") for p in payloads] == [
        "2026-09-30T01:30:00+02:00",
        "2026-09-30T01:30:00+02:00",
    ]
    assert sum("prefer_observations" in p for p in payloads) == 0


# ---------------------------------------------------------------------------
# RC3 — R7: a 503 is retried exactly once, after a capped injected wait
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, *, status=200, headers=None, body=None):
        self._status = status
        self._headers = headers or {}
        self._body = body if body is not None else {}

    def raise_for_status(self):
        if self._status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self._status,
                headers=self._headers)

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.payloads: list = []
        self.closed = False

    def request(self, method, url, json=None):
        self.payloads.append(json)
        return self._outcomes[len(self.payloads) - 1]

    async def close(self):
        self.closed = True


_HIT_BODY = {"results": [{"text": "fact", "type": "world",
                          "tags": ["public", encode_provenance_tag(STUB_SPEAKER_PROV)]}]}


async def _run_retry_case(method: str, outcomes):
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    kw = {"sleep": _sleep} if _accepts(HindsightSemanticMemory.__init__, "sleep") else {}
    mem = HindsightSemanticMemory(base_url="http://hs:8888", **kw)
    session = _Session(outcomes)
    mem._session = session
    try:
        if method == "recall":
            out = await mem.recall("casa", "q", tags=["public"], max_tokens=100)
            outcome = ("ok", out)
        else:
            hits = await mem.recall_items("casa", "q", tags=["public"], max_tokens=100,
                                          clearance="public")
            outcome = ("ok", len(hits))
    except RecallUnavailable as exc:
        outcome = ("unavailable", exc.reason)
    return session, sleeps, outcome


@pytest.mark.parametrize("method", ["recall", "recall_items"])
async def test_rc3_recall_retries_one_503_with_bounded_injected_sleep(method):
    ok = ("ok", "- fact") if method == "recall" else ("ok", 1)

    session, sleeps, outcome = await _run_retry_case(
        method, [_Resp(status=503, headers={"Retry-After": "9"}), _Resp(body=_HIT_BODY)])
    assert (len(session.payloads), sleeps, outcome) == (2, [1.0], ok)
    assert session.payloads[0] == session.payloads[1]

    session, sleeps, outcome = await _run_retry_case(
        method, [_Resp(status=503), _Resp(body=_HIT_BODY)])
    assert (len(session.payloads), len(sleeps), outcome) == (2, 1, ok)
    assert 0 < sleeps[0] <= 1.0

    session, sleeps, outcome = await _run_retry_case(
        method, [_Resp(status=503), _Resp(status=503), _Resp(body=_HIT_BODY)])
    assert (len(session.payloads), len(sleeps), outcome) == (2, 1, ("unavailable", "http_503"))


# ---------------------------------------------------------------------------
# RC4 — R2/R3/R4: a scheduled session's items are marked per class and dated
# ---------------------------------------------------------------------------


async def _seed_entry(reg: SessionRegistry, key: str, sid: str, *, scheduled) -> None:
    await reg.register(key, "resident:assistant", sid, binding_digest=STUB_BINDING_DIGEST,
                       speaker_provenance=STUB_SPEAKER_PROV, user_provenance=SYSTEM)
    # The persisted on-disk form of the marker (absent = written before it existed).
    if scheduled is None:
        reg._data[key].pop("scheduled", None)
    else:
        reg._data[key]["scheduled"] = scheduled


async def _save_via_registry(tmp_path, scheduled) -> list[dict]:
    reg = SessionRegistry(str(tmp_path / f"s-{scheduled}.json"))
    session_label = "telegram-v2-scheduled-label"
    await _seed_entry(reg, session_label, "sid-sched", scheduled=scheduled)
    sem = AsyncMock()
    with patch("session_saver.get_session_messages", return_value=_scheduled_transcript()):
        await session_saver.save_session(
            session_label, reg, sem, directory="/config/agent-home/assistant", channel="telegram")
    assert sem.retain.await_count == 1
    return sem.retain.await_args.args[1]


async def test_rc4_save_marks_turn_classes_and_dates_assistant_items(tmp_path, monkeypatch, amsterdam):
    monkeypatch.setattr(session_saver, "classify_tier", _private)
    stamps = [_ts(T0), _ts(T0), _ts(T1), _ts(T1), _ts(T2), _ts(T2), _ts(T3), _ts(T3)]

    items = await _save_via_registry(tmp_path, True)
    assert len(items) == 8
    assert [_is_marked(i) for i in items] == [1, 1, 0, 1, 0, 1, 1, 1]
    assert [i.get("timestamp") for i in items] == stamps
    assert [_tier_and_source_counts(i) for i in items] == [(1, 1)] * 8
    # The operator's answer and the delegated result keep today's form.
    assert items[2]["content"] == _terminal("answered")
    assert items[2]["tags"][:2] == ["private", encode_provenance_tag(SYSTEM)]

    items = await _save_via_registry(tmp_path, False)
    assert len(items) == 8
    assert [_is_marked(i) for i in items] == [0] * 8
    assert [i.get("timestamp") for i in items] == stamps

    # Every real synthesizer branch stays unmarked; the model's reply to it is marked.
    variants = [
        dict(status="ok", text="done."),
        dict(status="ok", text="done.", replayed_after_restart=True,
             terminal_at="2026-09-30T06:00:00+00:00"),
        dict(status="ok", result_available=False),
        dict(status="ok", result_available=False, replayed_after_restart=True,
             terminal_at="2026-09-30T06:00:00+00:00"),
        dict(status="error", kind="restart_orphan"),
        dict(status="error", kind="restart_orphan", replayed_after_restart=True,
             terminal_at="2026-09-30T06:00:00+00:00"),
        dict(status="error", kind="launch_outcome_uncommitted"),
        dict(status="error", kind="timeout", message="took too long"),
        dict(status="error", kind="timeout", message="took too long",
             replayed_after_restart=True, terminal_at="2026-09-30T06:00:00+00:00"),
    ]
    texts = [_synth_completion(**kw) for kw in variants]
    with patch("authz_grants.take_delegation_awaiting_approval", return_value=True):
        texts.append(_synth_completion(status="ok", text="needs approval"))
    assert len(texts) == 10
    for n, text in enumerate(texts):
        got = await _items([_Msg("user", _env(T0) + text),
                            _Msg("assistant", f"reply {n}")], scheduled=True)
        assert [_is_marked(i) for i in got] == [0, 1], text.splitlines()[0]

    # The three continuation outcomes: only the operator's answer is exempt.
    for kind, expected in (("answered", [0, 1]), ("no_answer", [1, 1]), ("cancelled", [1, 1])):
        got = await _items([_Msg("user", _env(T0) + _terminal(kind)),
                            _Msg("assistant", f"after {kind}")], scheduled=True)
        assert [_is_marked(i) for i in got] == expected, kind

    # Near-misses of either signature are ordinary scheduled prompts: marked.
    for text in (
        "[answer to r9] something that is not a tap",
        "[System notification: nothing else follows]",
        "Heartbeat. Reply to the user via their original channel. Be concise.",
    ):
        got = await _items([_Msg("user", _env(T0) + text),
                            _Msg("assistant", "noted")], scheduled=True)
        assert [_is_marked(i) for i in got] == [1, 1], text

    # Time inheritance: an envelope-only user turn still dates the next reply; an
    # undated textual user turn clears the inherited time.
    got = await _items([
        _Msg("user", _env(T0) + "first prompt"),
        _Msg("user", _env(T1)),
        _Msg("assistant", "reply after the envelope-only turn"),
        _Msg("user", "a user turn that carries no envelope"),
        _Msg("assistant", "reply after the undated turn"),
    ], scheduled=True)
    assert [(i["content"], i.get("timestamp")) for i in got] == [
        ("first prompt", _ts(T0)),
        ("reply after the envelope-only turn", _ts(T1)),
        ("a user turn that carries no envelope", None),
        ("reply after the undated turn", None),
    ]


# ---------------------------------------------------------------------------
# RC5 — the marker is written at both register sites, sticky, and proves a
# superseded pre-upgrade session scheduled only from a marked incoming turn
# ---------------------------------------------------------------------------


def _turn(kind: str, chat_id: str, *, marked: bool, operator: bool = False, extra=None):
    from bus import BusMessage, MessageType
    context = {"chat_id": chat_id, **({"_scheduled_delivery": True} if marked else {}),
               **(extra or {})}
    trusted = None
    if operator:
        from ingress_identity import ingress_identity
        trusted = ingress_identity("telegram", sender_id=chat_id,
                                   sender_display_name="Op", sender_is_operator=True)
    return BusMessage(
        type=getattr(MessageType, kind), source="scheduler" if kind == "SCHEDULED" else "telegram",
        target="assistant", content=f"{kind.lower()} turn", channel="telegram",
        context=context, trusted_user_origin=trusted,
    )


async def test_rc5_process_persists_marker_and_proves_stale_scheduled_origin(tmp_path, monkeypatch):
    import agent as agent_mod
    from session_registry import build_scoped_session_key
    from test_agent_process import FakeClient, _make_agent_with_registry

    path = tmp_path / "sessions.json"
    reg = SessionRegistry(str(path))
    agent = _make_agent_with_registry(reg, role="assistant")
    turns = [
        ("interval-heartbeat", _turn("SCHEDULED", "interval-heartbeat", marked=True)),   # bypass site
        ("cron-briefing", _turn("REQUEST", "cron-briefing", marked=True)),               # pooled, completion-first
        ("123", _turn("CHANNEL_IN", "123", marked=False, operator=True)),                # operator DM
        ("456", _turn("CHANNEL_IN", "456", marked=False,
                      extra={"synthetic": "event_wake", "emitter": "p", "event": "e"})),  # event wake
    ]
    FakeClient.reset()
    with patch("sdk_client_pool._default_make_client", FakeClient):
        for _chat, msg in turns:
            await agent._process(msg)
    stored = json.loads(path.read_text(encoding="utf-8"))
    keys = [build_scoped_session_key("telegram", "assistant", chat) for chat, _ in turns]
    assert [stored[k].get("scheduled", "<absent>") for k in keys] == [True, True, False, False]
    assert stored[keys[3]]["user_provenance"]["speaker_kind"] == "system"

    # Sticky: an unmarked re-registration of a scheduled entry keeps it scheduled.
    kw = {"scheduled": False} if _accepts(SessionRegistry.register, "scheduled") else {}
    await reg.register(keys[0], "resident:assistant", "sid-again",
                       binding_digest=STUB_BINDING_DIGEST,
                       speaker_provenance=STUB_SPEAKER_PROV, user_provenance=SYSTEM, **kw)
    assert reg.get(keys[0]).get("scheduled", "<absent>") is True

    # P1: stale pre-upgrade entries (no field), superseded by the next turn.
    olds: list = []

    async def _capture_cold(old, **_kw):
        olds.append(old)

    monkeypatch.setattr(agent_mod, "retain_cold_session", _capture_cold)
    reg2 = SessionRegistry(str(tmp_path / "stale.json"))
    agent2 = _make_agent_with_registry(reg2, role="assistant")
    from session_reg_helpers import RESIDENT_DIGEST, resident_prov
    stale = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    p1 = [
        ("interval-heartbeat", _turn("SCHEDULED", "interval-heartbeat", marked=True)),
        ("cron-briefing", _turn("REQUEST", "cron-briefing", marked=True)),
        ("123", _turn("CHANNEL_IN", "123", marked=False, operator=True)),
    ]
    for chat, _msg in p1:
        key = build_scoped_session_key("telegram", "assistant", chat)
        await reg2.register(key, "resident:assistant", f"old-{chat}",
                            binding_digest=RESIDENT_DIGEST,
                            speaker_provenance=resident_prov("assistant"),
                            user_provenance=SYSTEM)
        reg2._data[key].pop("scheduled", None)
        reg2._data[key]["last_active"] = stale
    counts = []
    FakeClient.reset()
    with patch("sdk_client_pool._default_make_client", FakeClient):
        for _chat, msg in p1:
            before = len(olds)
            await agent2._process(msg)
            if agent2._bg_tasks:
                await asyncio.gather(*list(agent2._bg_tasks), return_exceptions=True)
            counts.append(len(olds) - before)
    assert counts == [1, 1, 1]
    assert [getattr(o, "scheduled", "<absent>") for o in olds] == [True, True, None]


# ---------------------------------------------------------------------------
# RC6 — the spool carries the marker to the retry
# ---------------------------------------------------------------------------


async def test_rc6_failed_cold_retain_spools_and_replays_scheduled_marker(tmp_path, monkeypatch, amsterdam):
    from agent import snapshot_session_entry
    from speaker_provenance import provenance_mapping

    monkeypatch.setattr(session_saver, "classify_tier", _private)
    spool = tmp_path / "spool"
    entry = {"agent": "resident:assistant", "sdk_session_id": "sid-spool",
             "last_active": None, "binding_digest": STUB_BINDING_DIGEST,
             "speaker_provenance": provenance_mapping(STUB_SPEAKER_PROV),
             "user_provenance": provenance_mapping(SYSTEM), "scheduled": True}
    old = snapshot_session_entry(entry)
    failing = AsyncMock()
    failing.retain.side_effect = RuntimeError("hindsight down")
    with patch("session_saver.get_session_messages", return_value=_scheduled_transcript()):
        await session_saver.retain_cold_session(
            old, directory="/config/agent-home/assistant", channel="telegram",
            semantic_memory=failing, retry_dir=spool)
    records = sorted(spool.glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text()).get("scheduled", "<absent>") is True

    ok = AsyncMock()
    with patch("session_saver.get_session_messages", return_value=_scheduled_transcript()):
        await session_saver.retry_spooled_cold_retains(ok, retry_dir=spool)
    assert ok.retain.await_count == 1
    items = ok.retain.await_args.args[1]
    assert len(items) == 8
    assert sum(_is_marked(i) for i in items) == 6
    assert sum(1 for i in items[1::2] if i.get("timestamp")) == 4
    assert sorted(spool.glob("*.json")) == []

    # A record written before the marker existed replays in today's form.
    legacy = {"sdk_session_id": "sid-legacy", "directory": "/config/agent-home/assistant",
              "channel": "telegram",
              "speaker_provenance": provenance_mapping(STUB_SPEAKER_PROV),
              "user_provenance": provenance_mapping(SYSTEM), "attempts": 0}
    spool.mkdir(exist_ok=True)
    (spool / "sid-legacy.json").write_text(json.dumps(legacy))
    ok2 = AsyncMock()
    with patch("session_saver.get_session_messages", return_value=_scheduled_transcript()):
        await session_saver.retry_spooled_cold_retains(ok2, retry_dir=spool)
    items = ok2.retain.await_args.args[1]
    assert len(items) == 8
    assert sum(_is_marked(i) for i in items) == 0
    assert sum(1 for i in items[1::2] if i.get("timestamp")) == 0


# ---------------------------------------------------------------------------
# RC7 — the mark reaches recall; under last-save-wins tags and first-save-wins
# dates the two render as independent facts
# ---------------------------------------------------------------------------


class _M5Memory:
    """Hindsight 0.10.2 as measured: an identical re-retain under one
    document_id keeps the FIRST date and REPLACES the tag set with the new
    save's complete set. An item sent without a timestamp is dated by the save."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.clock = "2026-10-01T12:00:00+00:00"

    async def retain(self, bank, items, *, async_=True):
        for item in items:
            doc = self.docs.get(item["document_id"])
            if doc is not None and doc["content"] == item["content"]:
                doc["tags"] = list(item["tags"])
                continue
            self.docs[item["document_id"]] = {
                "content": item["content"], "tags": list(item["tags"]),
                "mentioned_at": item.get("timestamp") or self.clock,
            }

    def as_results(self, text: str) -> list[dict]:
        return [{"text": d["content"], "type": "experience", "tags": d["tags"],
                 "mentioned_at": d["mentioned_at"], "document_id": doc_id}
                for doc_id, d in self.docs.items() if d["content"] == text]


LINE = "The gap in the hallway schedule remains unfilled."
DAY1 = datetime(2026, 9, 28, 23, 30, 0, tzinfo=timezone.utc).astimezone(AMS)  # Tue 29 Sep local
DAY2 = DAY1 + timedelta(days=1)


async def _recall_rendered(results, *, budget=10_000) -> str:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"results": results})
    hits = await mem.recall_items("casa", "q", tags=["private"], max_tokens=500,
                                  clearance="private")
    return render_recall(hits, current_speaker=STUB_SPEAKER_PROV, surface="text",
                         clearance="private", token_budget=budget)


async def test_rc7_last_save_mark_and_first_recorded_date_remain_separate(monkeypatch, amsterdam):
    monkeypatch.setattr(session_saver, "classify_tier", _private)
    ordinary = [_Msg("user", _env(DAY1) + "how is the schedule?"), _Msg("assistant", LINE)]
    scheduled = [_Msg("user", _env(DAY2) + "Heartbeat check."), _Msg("assistant", LINE)]
    expected_head = [
        f"- Tester previously said: {LINE}",
        "  [source: resident:assistant, casa/tester@0.1.0]",
    ]

    m5 = _M5Memory()
    await m5.retain("casa", await _items(ordinary, scheduled=False))
    await m5.retain("casa", await _items(scheduled, scheduled=True))
    assert len(m5.as_results(LINE)) == 1
    rendered = await _recall_rendered(m5.as_results(LINE))
    assert rendered.splitlines() == [
        *expected_head, "  [recorded Tue 29 Sep 2026]", "  [last saved by a scheduled turn]",
    ]

    # The reverse order: the unmarked save came last, so the mark is gone, while
    # the first save's date stays.
    m5 = _M5Memory()
    await m5.retain("casa", await _items(scheduled, scheduled=True))
    await m5.retain("casa", await _items(ordinary, scheduled=False))
    rendered = await _recall_rendered(m5.as_results(LINE))
    assert rendered.splitlines() == [*expected_head, "  [recorded Wed 30 Sep 2026]"]

    # Backend order is kept: a marked first hit is not demoted behind an unmarked
    # second, and a budget admitting only the first complete entry renders it alone.
    marked_first = [
        {"text": "first, marked", "type": "experience", "mentioned_at": "2026-09-29T06:00:00Z",
         "tags": ["private", encode_provenance_tag(STUB_SPEAKER_PROV), MARK]},
        {"text": "second, unmarked", "type": "experience", "mentioned_at": "2026-09-28T06:00:00Z",
         "tags": ["private", encode_provenance_tag(STUB_SPEAKER_PROV)]},
    ]
    first_entry = "\n".join([
        "- Tester previously said: first, marked",
        "  [source: resident:assistant, casa/tester@0.1.0]",
        "  [recorded Tue 29 Sep 2026]",
        "  [last saved by a scheduled turn]",
    ])
    full = await _recall_rendered(marked_first)
    assert full.startswith(first_entry + "\n- Tester previously said: second, unmarked")
    assert await _recall_rendered(marked_first, budget=estimate_tokens_v1(first_entry)) == first_entry


# ---------------------------------------------------------------------------
# RC8 — R6: the live-state rule reaches exactly the assistant's carriers
# ---------------------------------------------------------------------------


def test_rc8_live_state_rule_reaches_exact_assistant_carriers():
    from test_assistant_prompts import (
        _BACKGROUND_EXCEPTION, _collapse_ws, _compiled_resident_carriers,
    )

    carriers = _compiled_resident_carriers()
    assert len(carriers) == 9
    rule = _collapse_ws(LIVE_STATE_RULE)
    counts = {name: _collapse_ws(body).count(rule) for name, body in carriers}
    assert counts == {name: int(name.startswith("assistant:")) for name, _ in carriers}
    assert _collapse_ws(dict(carriers)["assistant:text"]).count(
        _collapse_ws(_BACKGROUND_EXCEPTION)) == 1
