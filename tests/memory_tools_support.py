"""Shared helpers for the memory tools' tests: a real store wired as the
process's store, events stored on chosen days at chosen tiers through
capture's own append, the calling turn's origin, and a tool call's
payload. No model is called."""
from __future__ import annotations

import dataclasses
import json
import secrets
from datetime import datetime
from pathlib import Path

import pytest

import agent as agent_mod
from memory import capture
from memory.store import MemoryStore
from timekeeping import resolve_tz


def new_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(str(tmp_path / "memory"))
    assert store.open() is True, store.unavailable_reason
    return store


@pytest.fixture
def mem(tmp_path, monkeypatch) -> MemoryStore:
    """A real store, wired as the process's memory store."""
    store = new_store(tmp_path)
    monkeypatch.setitem(capture._wiring, "store", store)
    monkeypatch.setitem(capture._wiring, "notify", None)
    return store


@pytest.fixture
def turn():
    """Set the calling turn's origin: ``turn(clearance, **origin)``. Each
    async test runs in a context of its own, so nothing outlives it."""

    def _set(clearance: str = "private", **extra) -> dict:
        origin = {"role": "assistant", "channel": "telegram",
                  "chat_id": "4242", "_origin_route": "telegram",
                  "_origin_clearance": clearance, **extra}
        agent_mod.origin_var.set(origin)
        return origin

    return _set


def canary() -> str:
    return secrets.token_hex(8)


def store_event(store: MemoryStore, text: str, *, tier: str = "private",
                day: str | None = None, at: str = "12:00",
                speaker: str = "operator", role: str = "assistant",
                channel: str = "telegram", scheduled: bool = False,
                classified: bool = True, **extra) -> int:
    """Store one event said on local *day* at *at* (operator's zone), at
    *tier*; returns its seq."""
    tz = resolve_tz()
    when = datetime.now(tz)
    if day is not None:
        hour, minute = (int(p) for p in at.split(":"))
        when = datetime.fromisoformat(day).replace(
            hour=hour, minute=minute, tzinfo=tz)
    item = capture.event(
        role=role, channel=channel, speaker_kind=speaker,
        origin_kind="operator", text=text, eligibility="write_trusted_turn",
        origin_tier=None, scheduled=scheduled, **extra)
    item = dataclasses.replace(
        item, occurred_at=when.astimezone().isoformat(timespec="microseconds"),
        tz=tz.key, local_day=when.date().isoformat(), tier=tier,
        tier_status="classified" if classified else "provisional")
    receipt = capture.append(store, item)
    assert receipt is not None and receipt.outcome == capture.STORED
    with store.transaction() as conn:
        return conn.execute("SELECT seq FROM events WHERE source_key = ?",
                            (item.source_key,)).fetchone()[0]


def payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])
