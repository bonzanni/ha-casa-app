"""#1126 red case — INV-MEM-020, half 2: every model the mental-model overlay
renders carries its name and the date of its last refresh in the operator's
timezone.

Drives the REAL ``HindsightSemanticMemory.profile`` with only ``_request``
replaced. The fake projects each stored model by the ``detail`` the request
asked for, the way Hindsight 0.10.2's list route does (``detail`` defaults to
``metadata``, whose projection carries no ``content``), so a fix that corrects
only the envelope while still omitting ``detail=content`` stays red.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest

from hindsight_memory import HindsightSemanticMemory
from timekeeping import resolve_tz

_METADATA_KEYS = (
    "id", "bank_id", "name", "tags", "last_refreshed_at",
    "last_memory_seen_at", "last_refresh_failed_at", "created_at",
)
_CONTENT_KEYS = ("source_query", "max_tokens", "trigger", "content")

_COMMON = {
    "bank_id": "casa-assistant",
    "tags": [],
    "created_at": "2026-09-01T12:00:00Z",
    "last_memory_seen_at": "2026-09-20T12:00:00Z",
    "last_refresh_failed_at": "2026-09-21T12:00:00Z",
    "source_query": "q",
    "max_tokens": 1024,
    "trigger": {"refresh_cron": "0 5 * * *"},
}
_STORED = (
    {**_COMMON, "id": "model-alpha", "name": "Household rhythm",
     "content": "BODY_ALPHA", "last_refreshed_at": "2026-10-01T00:30:00Z"},
    {**_COMMON, "id": "model-beta", "name": "Garden routine",
     "content": "BODY_BETA", "last_refreshed_at": "2026-09-29T00:30:00+00:00"},
)


@pytest.fixture
def los_angeles(monkeypatch):
    monkeypatch.setenv("CASA_TZ", "America/Los_Angeles")
    monkeypatch.setenv("TZ", "UTC")
    resolve_tz.cache_clear()
    yield
    resolve_tz.cache_clear()


async def test_profile_renders_each_model_name_and_local_refresh_date(los_angeles):
    calls: list[tuple[str, str, object]] = []

    async def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        detail = parse_qs(urlsplit(path).query).get("detail", ["metadata"])[0]
        keys = _METADATA_KEYS + (_CONTENT_KEYS if detail in ("content", "full") else ())
        rows = [{k: row[k] for k in keys} for row in _STORED]
        return {"items": rows, "total": 2, "limit": 100, "offset": 0}

    mem = HindsightSemanticMemory("http://hs:8888")
    mem._request = fake_request
    out = await mem.profile("casa-assistant")

    assert len(calls) == 1
    assert sum(
        method == "GET"
        and urlsplit(path).path == "/v1/default/banks/casa-assistant/mental-models"
        and parse_qs(urlsplit(path).query).get("detail") == ["content"]
        and payload is None
        for method, path, payload in calls
    ) == 1

    assert tuple(out.count(value) for value in (
        "BODY_ALPHA", "BODY_BETA",
        "Household rhythm", "Garden routine",
        "Wed 30 Sep 2026", "Mon 28 Sep 2026",
    )) == (1, 1, 1, 1, 1, 1)

    # Association: each model's name and date precede its own content without
    # crossing another model's name or content, so swapped dates cannot pass.
    tokens = ("Household rhythm", "Garden routine", "BODY_ALPHA", "BODY_BETA")
    forbidden = "|".join(map(re.escape, tokens))
    gap = rf"(?:(?!(?:{forbidden}))[\s\S])*?"
    for name, date, body in (
        ("Household rhythm", "Wed 30 Sep 2026", "BODY_ALPHA"),
        ("Garden routine", "Mon 28 Sep 2026", "BODY_BETA"),
    ):
        pattern = re.escape(name) + gap + re.escape(date) + gap + re.escape(body)
        assert len(re.findall(pattern, out)) == 1
