"""#1120 red cases: Ellen's memory search can be limited to a time period.

``recall_memory`` gains one optional ``period`` input. The memory server's
``temporal_window`` only RANKS (Hindsight 0.10.2), so "only from that period"
is Casa's own filter on each hit's recorded date (``mentioned_at``, the first
date for identical text); undated hits are left out. The double below returns
every hit whatever window it is sent, which is exactly that ranking-only
behaviour; a double that filtered would make every case here vacuous.

Every test drives symbols that exist before the change (the tool's handler
takes an args dict, so an extra ``period`` key is accepted and ignored there),
so on the pre-fix tree each one fails on the behaviour it pins, never on an
import error or an unexpected keyword.
"""
from __future__ import annotations

import ast
import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from hindsight_memory import HindsightSemanticMemory
from session_reg_helpers import STUB_SPEAKER_PROV
from speaker_provenance import encode_provenance_tag
from timekeeping import resolve_tz

pytestmark = [pytest.mark.unit]

KTM = ZoneInfo("Asia/Kathmandu")  # +05:45, no DST: a local day is never a UTC day
CODE = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"


@pytest.fixture
def kathmandu(monkeypatch):
    monkeypatch.setenv("CASA_TZ", "Asia/Kathmandu")
    resolve_tz.cache_clear()
    yield
    resolve_tz.cache_clear()


def _hit(text: str, mentioned_at=None, *, tier: str = "public") -> dict:
    row = {"text": text, "type": "world",
           "tags": [tier, encode_provenance_tag(STUB_SPEAKER_PROV)]}
    if mentioned_at is not None:
        row["mentioned_at"] = mentioned_at
    return row


def _wire(monkeypatch, results: list[dict], *, clearance: str = "private"):
    """Install a real Hindsight backend whose transport returns ``results``
    for every request (ranking-only: any window sent is ignored)."""
    import agent as agent_mod
    import tools
    from types import SimpleNamespace

    mem = HindsightSemanticMemory("http://hindsight.invalid")
    mem._request = AsyncMock(return_value={"results": list(results)})
    monkeypatch.setattr(agent_mod, "active_semantic_memory", mem, raising=False)
    cfg = SimpleNamespace(memory=SimpleNamespace(token_budget=4000))
    monkeypatch.setattr(tools, "_agent_role_map", {"assistant": cfg}, raising=False)
    agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram",
        "_origin_route": "telegram", "_origin_clearance": clearance,
    })
    return mem


async def _call(args) -> dict:
    import json
    import tools
    res = await tools.recall_memory.handler(args)
    return json.loads(res["content"][0]["text"])


def _payloads(mem) -> list[dict]:
    return [c.args[2] for c in mem._request.await_args_list]


# RC1's rows, in backend order. Index 0, 4, 5, 6 lie outside 2026-04-04 (Kathmandu).
ROWS = [
    ("OUT-before-start", "2026-04-03T23:59:59+05:45"),
    ("IN-exactly-start", "2026-04-04T00:00:00+05:45"),
    ("IN-utc-previous-day", "2026-04-03T18:20:00Z"),        # 00:05 local on the 4th
    ("IN-exactly-end", "2026-04-04T23:59:59.999999+05:45"),
    ("OUT-after-end", "2026-04-05T00:00:00+05:45"),
    ("OUT-undated", None),
    ("OUT-naive", "2026-04-04T12:00:00"),                   # naive -> no recorded date
]
EXPECTED = [0, 1, 1, 1, 0, 0, 0]


# ---------------------------------------------------------------------------
# RC1 — only hits first recorded inside the period; undated hits are left out
# ---------------------------------------------------------------------------


async def test_rc1_windowed_recall_returns_only_hits_recorded_in_the_period(
    monkeypatch, kathmandu,
):
    mem = _wire(monkeypatch, [_hit(t, m) for t, m in ROWS])
    out = await _call({"query": "boiler service", "period": "2026-04-04"})
    assert out["status"] == "ok"
    counts = [out["memory"].count(t) for t, _ in ROWS]
    assert counts == EXPECTED
    assert mem._request.await_count == 1


# ---------------------------------------------------------------------------
# RC2 — nothing in the period: the empty-in-window arm, never absence
# ---------------------------------------------------------------------------


async def test_rc2_all_out_of_period_is_the_bounded_empty_arm(monkeypatch, kathmandu):
    mem = _wire(monkeypatch, [_hit(*ROWS[i]) for i in (0, 4, 5, 6)])
    out = await _call({"query": "boiler service", "period": "2026-04-04"})
    assert out["status"] == "ok"
    assert out["memory"] == ""
    msg = out["message"].lower()
    assert "not proof of absence" in msg
    assert "render budget" not in msg
    assert out.get("period") == {
        "start": "2026-04-04T00:00:00+05:45",
        "end": "2026-04-04T23:59:59.999999+05:45",
    }
    assert mem._request.await_count == 1


# ---------------------------------------------------------------------------
# RC3 — an invalid or inverted period is refused before any request
# ---------------------------------------------------------------------------

INVALID = ["2026-04-05..2026-04-04", "20260404", "2026-W14-6", "2026-02-30",
           "next_fortnight", "", 7]


async def test_rc3_invalid_or_inverted_period_is_refused_with_zero_requests(
    monkeypatch, kathmandu,
):
    mem = _wire(monkeypatch, [_hit(t, m) for t, m in ROWS])
    outs = [await _call({"query": "boiler service", "period": p}) for p in INVALID]
    assert [o["status"] for o in outs] == ["error"] * len(INVALID)
    assert [o.get("kind") for o in outs] == ["invalid_period"] * len(INVALID)
    assert mem._request.await_count == 0


# ---------------------------------------------------------------------------
# RC4 — the period is published, and optional, on both routes
# ---------------------------------------------------------------------------


def _admits_string_and_null(prop: dict) -> bool:
    kinds: set = set()
    t = prop.get("type")
    if isinstance(t, list):
        kinds.update(t)
    elif isinstance(t, str):
        kinds.add(t)
    for alt in prop.get("anyOf", []) or []:
        if isinstance(alt, dict) and isinstance(alt.get("type"), str):
            kinds.add(alt["type"])
    return {"string", "null"} <= kinds


def test_rc4_period_is_published_and_optional_on_both_routes():
    import mcp.types as mcp_types
    import tools
    from claude_agent_sdk import create_sdk_mcp_server
    from mcp_envelope import _tool_schema

    envelope = _tool_schema(tools.recall_memory)["inputSchema"]
    server = create_sdk_mcp_server(name="pin-1120", tools=[tools.recall_memory])
    handler = server["instance"].request_handlers[mcp_types.ListToolsRequest]
    listed = asyncio.run(handler(mcp_types.ListToolsRequest(method="tools/list")))
    sdk = listed.root.tools[0].inputSchema

    for schema in (envelope, sdk):
        props = schema.get("properties", {})
        assert "period" in props
        assert schema.get("required") == ["query"]
        assert _admits_string_and_null(props["period"])


# ---------------------------------------------------------------------------
# RC5 — named periods resolve in the operator's zone; the window is sent as
# the server's ranking hint
# ---------------------------------------------------------------------------


def _first_day(name: str, today: date) -> tuple[date, date]:
    if name == "today":
        return today, today
    if name == "this_week":
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=6)
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return last_prev.replace(day=1), last_prev


def _local_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=KTM)


async def test_rc5_named_periods_resolve_in_the_operator_zone_and_reach_the_server(
    monkeypatch, kathmandu,
):
    mem = _wire(monkeypatch, [])
    names = ["today", "this_week", "last_month"]
    for name in names:
        before = datetime.now(KTM).date()
        await _call({"query": "boiler service", "period": name})
        after = datetime.now(KTM).date()
        payload = _payloads(mem)[-1]
        assert "temporal_window" in payload
        win = payload["temporal_window"]
        start = datetime.fromisoformat(win["start"])
        end = datetime.fromisoformat(win["end"])
        assert start.utcoffset() == timedelta(hours=5, minutes=45)
        assert end.utcoffset() == timedelta(hours=5, minutes=45)
        candidates = {_first_day(name, d) for d in {before, after}}
        assert any(
            start == _local_midnight(first)
            and end + timedelta(microseconds=1) == _local_midnight(last + timedelta(days=1))
            for first, last in candidates
        ), (name, win)
    assert mem._request.await_count == len(names)


# ---------------------------------------------------------------------------
# Seam-round addition: the period's end is an instant, not a wall-clock time
# ---------------------------------------------------------------------------


async def test_period_end_at_timezone_transition(monkeypatch):
    monkeypatch.setenv("CASA_TZ", "America/Nuuk")
    resolve_tz.cache_clear()
    try:
        last_valid = "LAST-VALID-instant"
        next_day = "NEXT-DAY-instant"
        mem = _wire(monkeypatch, [
            _hit(last_valid, "2026-03-29T00:59:59.999999Z"),
            _hit(next_day, "2026-03-29T01:30:00Z"),
        ])
        out = await _call({"query": "boiler service", "period": "2026-03-28"})
        assert [out["memory"].count(last_valid), out["memory"].count(next_day)] == [1, 0]
        assert mem._request.await_count == 1
    finally:
        resolve_tz.cache_clear()


# ---------------------------------------------------------------------------
# Regression tests (green before the change; they keep what must not move)
# ---------------------------------------------------------------------------

UNWINDOWED_KEYS = {"query", "tags", "tags_match", "max_tokens", "types", "budget",
                   "query_timestamp"}


async def test_unwindowed_call_sends_the_same_request_as_before(monkeypatch, kathmandu):
    mem = _wire(monkeypatch, [_hit(t, m) for t, m in ROWS])
    out = await _call({"query": "boiler service"})
    assert set(_payloads(mem)[0]) == UNWINDOWED_KEYS
    assert "period" not in out
    # without a period nothing is filtered by date, undated hits included
    assert [out["memory"].count(t) for t, _ in ROWS] == [1] * len(ROWS)


async def test_unwindowed_call_keeps_the_old_seam_keywords(monkeypatch):
    """A backend with the pre-change ``recall_items`` keyword set (no
    ``**kwargs``) still serves a call without a period."""
    import agent as agent_mod
    import tools
    from types import SimpleNamespace
    from personality_types import RecallHit
    from semantic_memory import SemanticMemory

    calls = []

    class _Strict(SemanticMemory):
        async def retain(self, bank, items, *, async_=True):
            return None

        async def recall(self, bank, query, *, tags, max_tokens,
                         types=("world",), tags_match="any", budget="mid"):
            return ""

        async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                               types=("world", "experience", "observation"),
                               tags_match="any", budget="mid"):
            calls.append(query)
            return (RecallHit(
                text="Strict backend fact.", memory_type="world",
                sensitivity="public", application_tags=(), provenance=None,
                backend_id="s1", document_id=None, chunk_id=None,
                source_fact_ids=None, metadata=None, context=None, score=None,
            ),)

        async def document_tags(self, bank, document_id):
            return None

        async def profile(self, bank):
            return ""

    monkeypatch.setattr(agent_mod, "active_semantic_memory", _Strict(), raising=False)
    monkeypatch.setattr(tools, "_agent_role_map",
                        {"assistant": SimpleNamespace(memory=SimpleNamespace(token_budget=500))},
                        raising=False)
    agent_mod.origin_var.set({"role": "assistant", "channel": "telegram",
                              "_origin_route": "telegram", "_origin_clearance": "private"})
    out = await _call({"query": "boiler service"})
    assert out["status"] == "ok"
    assert out["memory"].count("Strict backend fact.") == 1
    assert calls == ["boiler service"]


async def test_period_with_nothing_readable_is_still_unavailable(monkeypatch, kathmandu):
    """INV-MEM-002 is not amended: every hit above the caller's clearance is
    'could not be checked', with or without a period — never an empty period."""
    mem = _wire(monkeypatch, [_hit(t, m, tier="private") for t, m in ROWS[1:4]],
                clearance="public")
    out = await _call({"query": "boiler service", "period": "2026-04-04"})
    assert out["status"] == "unavailable"
    assert mem._request.await_count == 1


class _CountingHit:
    """Proxy a RecallHit, counting reads of its recorded date."""

    def __init__(self, hit):
        object.__setattr__(self, "_hit", hit)
        object.__setattr__(self, "date_reads", 0)

    def __getattr__(self, name):
        if name == "mentioned_at":
            object.__setattr__(self, "date_reads", self.date_reads + 1)
        return getattr(self._hit, name)


async def test_period_filter_runs_after_the_clearance_refilter(monkeypatch, kathmandu):
    """A clearance downgrade that lands while the recall is in flight drops the
    unreadable hit BEFORE the period filter ever looks at its date (#369)."""
    import tools

    mem = _wire(monkeypatch, [
        _hit("PRIVATE-out-of-period", ROWS[0][1], tier="private"),
        _hit("PUBLIC-in-period", ROWS[1][1]),
    ])
    real = mem.recall_items
    proxies: list[_CountingHit] = []

    async def counting_recall_items(*a, **kw):
        hits = await real(*a, **kw)
        proxies.extend(_CountingHit(h) for h in hits)
        return tuple(proxies)

    mem.recall_items = counting_recall_items
    markers = iter([("telegram", "private"), ("telegram", "public")])
    resolutions = []

    def fake_markers(origin):
        resolutions.append(1)
        return next(markers)

    monkeypatch.setattr(tools, "_origin_clearance_markers", fake_markers)
    out = await _call({"query": "boiler service", "period": "2026-04-04"})
    assert len(resolutions) == 2
    assert mem._request.await_count == 1
    assert [out["memory"].count("PRIVATE-out-of-period"),
            out["memory"].count("PUBLIC-in-period")] == [0, 1]
    private = [p for p in proxies if p.text == "PRIVATE-out-of-period"]
    assert len(private) == 1
    assert private[0].date_reads == 0


def _recall_items_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text())
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "recall_items"]


def test_auto_recall_and_delegated_recall_send_no_period():
    for name in ("agent.py", "delegated_memory.py"):
        calls = _recall_items_calls(CODE / name)
        assert len(calls) == 1, name
        assert [k.arg for k in calls[0].keywords if k.arg == "window"] == [], name
        assert all(k.arg is not None for k in calls[0].keywords), name  # no **kw splat
