"""Tests for topic_ledger — the terminal-engagement topic retention ledger.

Covers the 2026-07-10 topic-retention-cleanup design: append/load/remove
round-trips, idempotent append, corrupt-file `.casabak` archiving, the
[AR-5] delete-error classification contract, and the [AR-2]/[AR-5]/[AR-6]
sweep behaviours (chat_id guard, dry-run, RetryAfter honoring, serialized
deletions, never-drop-on-unknown-error).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import pytest

import telegram.error as tg_err

import topic_ledger


# ---------------------------------------------------------------------------
# telegram.error stubs — conftest installs a minimal telegram.error stub
# (TelegramError / NetworkError / TimedOut only). classify_delete_error also
# needs BadRequest, Forbidden and RetryAfter; attach them here mirroring the
# REAL python-telegram-bot hierarchy (BadRequest and TimedOut subclass
# NetworkError!) so the BadRequest-before-NetworkError classification order
# is pinned by these tests. hasattr-guarded: no-ops against the real library
# or if another test file attached them first.
# ---------------------------------------------------------------------------


def _ensure_stub_error_classes() -> None:
    if not hasattr(tg_err, "BadRequest"):
        class BadRequest(tg_err.NetworkError):
            pass

        tg_err.BadRequest = BadRequest
    if not hasattr(tg_err, "Forbidden"):
        class Forbidden(tg_err.TelegramError):
            pass

        tg_err.Forbidden = Forbidden
    if not hasattr(tg_err, "RetryAfter"):
        class RetryAfter(tg_err.TelegramError):
            def __init__(self, retry_after):
                super().__init__(
                    f"Flood control exceeded. Retry in {retry_after} seconds"
                )
                self.retry_after = retry_after

        tg_err.RetryAfter = RetryAfter


_ensure_stub_error_classes()


CHAT = -1001234567890
OTHER_CHAT = -1009999999999
NOW = 2_000_000_000.0
DAY = 86400.0

# Captured at import time, before the autouse fixture repoints it to tmp_path.
_SHIPPED_LEDGER_PATH = topic_ledger.LEDGER_PATH


@pytest.fixture(autouse=True)
def _fresh_module_state(monkeypatch, tmp_path):
    """Isolate every test: a fresh module lock (asyncio primitives cache the
    first loop they're contended on; pytest-asyncio gives each test its own
    loop) and a tmp default LEDGER_PATH so nothing can ever touch /data."""
    monkeypatch.setattr(topic_ledger, "_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        topic_ledger, "_SWEEP_LOCK", asyncio.Lock(), raising=False,
    )
    monkeypatch.setattr(
        topic_ledger, "LEDGER_PATH", str(tmp_path / "default-topic-ledger.json")
    )


def _entry(
    eid: str,
    *,
    chat_id=CHAT,
    topic_id: int = 613,
    outcome: str = "completed",
    delete_after: float = NOW - 1,
) -> dict:
    return {
        "engagement_id": eid,
        "chat_id": chat_id,
        "topic_id": topic_id,
        "outcome": outcome,
        "closed_at": delete_after - topic_ledger.TOPIC_RETENTION_DAYS * DAY,
        "delete_after": delete_after,
    }


def _seed(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps(entries), encoding="utf-8")


def _expected(**overrides) -> dict:
    """Full sweep-result shape with zeroed counters; keeps the exact-dict
    assertions exact as the shape grows."""
    base = {
        "deleted": 0,
        "kept": 0,
        "dropped_mismatched": 0,
        "dropped_stuck": 0,
        "dropped_malformed": 0,
        "failures": [],
        "needs_permission": False,
        "dry_run": False,
        "targets": [],
    }
    base.update(overrides)
    return base


def _tl_warnings(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "topic_ledger" and r.levelno >= logging.WARNING
    ]


class StubChannel:
    """Duck-typed channel: records delete_topic calls, raises scripted
    exceptions. ``script`` maps topic_id -> list of actions consumed one per
    call; an exception instance is raised, anything else (or an exhausted /
    absent script) is success."""

    def __init__(self, script: dict[int, list] | None = None):
        self.calls: list[int] = []
        self._script = {k: list(v) for k, v in (script or {}).items()}

    async def delete_topic(self, thread_id):
        self.calls.append(thread_id)
        queue = self._script.get(thread_id)
        if queue:
            action = queue.pop(0)
            if isinstance(action, BaseException):
                raise action


@pytest.fixture
def recorded_sleeps(monkeypatch):
    calls: list[float] = []

    async def fake_sleep(seconds, *args, **kwargs):
        calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return calls


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_retention_matches_workspace_retention():
    """TOPIC_RETENTION_DAYS is intentionally equal to the workspace window
    (topics and workspaces expire together); the constant lives in
    topic_ledger so telegram.py and tools.py import it without a cycle."""
    import tools

    assert topic_ledger.TOPIC_RETENTION_DAYS == 7
    assert topic_ledger.TOPIC_RETENTION_DAYS == tools._WORKSPACE_RETENTION_DAYS


def test_module_constants():
    assert topic_ledger.DELETE_SPACING_SECONDS == pytest.approx(0.3)
    assert topic_ledger.STUCK_ENTRY_MAX_AGE_SECONDS == 90 * 86400
    assert topic_ledger.RETRY_AFTER_CAP_SECONDS == pytest.approx(60.0)
    assert _SHIPPED_LEDGER_PATH == "/data/topic-ledger.json"


# ---------------------------------------------------------------------------
# append / load / remove
# ---------------------------------------------------------------------------


async def test_append_load_round_trip_records_all_fields(tmp_path):
    path = str(tmp_path / "ledger.json")
    await topic_ledger.append(
        engagement_id="e1", chat_id=CHAT, topic_id=613,
        outcome="completed", closed_at=1_000_000.0, path=path,
    )
    await topic_ledger.append(
        engagement_id="e2", chat_id=CHAT, topic_id=614,
        outcome="error", closed_at=1_000_100.0, path=path,
    )

    entries = await topic_ledger.load(path=path)
    assert [e["engagement_id"] for e in entries] == ["e1", "e2"]
    first = entries[0]
    assert set(first) == {
        "engagement_id", "chat_id", "topic_id", "outcome",
        "closed_at", "delete_after",
    }
    assert first["chat_id"] == CHAT
    assert first["topic_id"] == 613
    assert first["outcome"] == "completed"
    assert first["closed_at"] == 1_000_000.0
    assert first["delete_after"] == 1_000_000.0 + 7 * 86400


async def test_append_defaults_closed_at_to_now(tmp_path):
    path = str(tmp_path / "ledger.json")
    before = time.time()
    await topic_ledger.append(
        engagement_id="e1", chat_id=CHAT, topic_id=613,
        outcome="completed", path=path,
    )
    after = time.time()

    (entry,) = await topic_ledger.load(path=path)
    assert before <= entry["closed_at"] <= after
    assert entry["delete_after"] == pytest.approx(
        entry["closed_at"] + topic_ledger.TOPIC_RETENTION_DAYS * DAY
    )


async def test_append_is_idempotent_by_engagement_id(tmp_path):
    path = str(tmp_path / "ledger.json")
    await topic_ledger.append(
        engagement_id="e1", chat_id=CHAT, topic_id=613,
        outcome="completed", closed_at=1_000_000.0, path=path,
    )
    # Re-append of a known id is a no-op — the first record wins.
    await topic_ledger.append(
        engagement_id="e1", chat_id=OTHER_CHAT, topic_id=999,
        outcome="error", closed_at=2_000_000.0, path=path,
    )

    (entry,) = await topic_ledger.load(path=path)
    assert entry["topic_id"] == 613
    assert entry["outcome"] == "completed"
    assert entry["closed_at"] == 1_000_000.0


async def test_append_uses_module_default_path(tmp_path):
    # The autouse fixture points LEDGER_PATH into tmp_path; calling without
    # path= must resolve the module constant at call time (monkeypatchable).
    await topic_ledger.append(
        engagement_id="e1", chat_id=CHAT, topic_id=613, outcome="completed",
    )
    assert Path(topic_ledger.LEDGER_PATH).exists()
    (entry,) = await topic_ledger.load()
    assert entry["engagement_id"] == "e1"


async def test_load_missing_file_returns_empty_list(tmp_path):
    assert await topic_ledger.load(path=str(tmp_path / "absent.json")) == []


async def test_load_corrupt_json_archives_casabak(tmp_path, caplog):
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        entries = await topic_ledger.load(path=str(path))

    assert entries == []
    bak = tmp_path / "ledger.json.casabak"
    assert bak.exists(), "corrupt ledger must be archived aside"
    assert bak.read_text(encoding="utf-8") == "{not json"
    # Archived ASIDE — moved, never silently truncated in place [AR-9]; a
    # subsequent load sees a missing file, not the same corruption again.
    assert not path.exists()
    assert _tl_warnings(caplog), "corruption must be warned about"
    assert await topic_ledger.load(path=str(path)) == []


async def test_load_non_list_json_is_treated_as_corrupt(tmp_path, caplog):
    path = tmp_path / "ledger.json"
    path.write_text('{"engagement_id": "e1"}', encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert await topic_ledger.load(path=str(path)) == []

    assert (tmp_path / "ledger.json.casabak").exists()
    assert _tl_warnings(caplog)


async def test_remove_drops_named_ids_and_ignores_unknown(tmp_path):
    path = tmp_path / "ledger.json"
    _seed(path, [_entry("e1"), _entry("e2"), _entry("e3")])

    await topic_ledger.remove({"e1", "e3", "never-existed"}, path=str(path))

    entries = await topic_ledger.load(path=str(path))
    assert [e["engagement_id"] for e in entries] == ["e2"]

    await topic_ledger.remove(set(), path=str(path))  # no-op, no error
    assert len(await topic_ledger.load(path=str(path))) == 1


async def test_append_writes_through_atomic_write_json(tmp_path, monkeypatch):
    """Crash-safety pin: ledger persistence must route through
    atomic_io.atomic_write_json — a plain open("w") regression could leave
    a truncated ledger after a power loss.

    Pins INV-STATE-003. Red case demonstrated: replacing the ledger's atomic_write_json call with a direct write fails this test.
    """
    real = topic_ledger.atomic_write_json
    calls: list[str] = []

    def spy(path, data, **kwargs):
        calls.append(path)
        return real(path, data, **kwargs)

    monkeypatch.setattr(topic_ledger, "atomic_write_json", spy)
    path = str(tmp_path / "ledger.json")

    await topic_ledger.append(
        engagement_id="e1", chat_id=CHAT, topic_id=613,
        outcome="completed", path=path,
    )

    assert calls == [path]
    (entry,) = await topic_ledger.load(path=path)
    assert entry["engagement_id"] == "e1"


async def test_concurrent_appends_are_lock_serialized(tmp_path):
    path = str(tmp_path / "ledger.json")
    await asyncio.gather(*[
        topic_ledger.append(
            engagement_id=f"e{i}", chat_id=CHAT, topic_id=600 + i,
            outcome="completed", path=path,
        )
        for i in range(8)
    ])

    entries = await topic_ledger.load(path=path)
    assert sorted(e["engagement_id"] for e in entries) == sorted(
        f"e{i}" for i in range(8)
    ), "interleaved read-modify-write must not lose appends"


# ---------------------------------------------------------------------------
# classify_delete_error [AR-5]
# ---------------------------------------------------------------------------


def test_classify_transient_classes():
    assert topic_ledger.classify_delete_error(tg_err.RetryAfter(3)) == "transient"
    assert topic_ledger.classify_delete_error(tg_err.TimedOut()) == "transient"
    assert (
        topic_ledger.classify_delete_error(tg_err.NetworkError("boom"))
        == "transient"
    )


def test_classify_forbidden_is_permission():
    assert (
        topic_ledger.classify_delete_error(tg_err.Forbidden("bot was kicked"))
        == "permission"
    )


def test_classify_bad_request_not_found_case_insensitive():
    assert (
        topic_ledger.classify_delete_error(
            tg_err.BadRequest("Message thread not found")
        )
        == "not_found"
    )
    assert (
        topic_ledger.classify_delete_error(
            tg_err.BadRequest("MESSAGE THREAD NOT FOUND")
        )
        == "not_found"
    )


@pytest.mark.parametrize(
    "message",
    [
        "Not enough rights to manage topics",
        "need administrator rights in the channel chat",
        "CHAT_ADMIN_REQUIRED: can_delete_messages",
    ],
)
def test_classify_bad_request_rights_is_permission(message):
    assert (
        topic_ledger.classify_delete_error(tg_err.BadRequest(message))
        == "permission"
    )


def test_classify_bad_request_chat_not_found_is_not_found():
    """A deleted engagement supergroup means the topic is unreachable
    forever — classify as gone so the entry resolves instead of retrying
    every sweep until end of time."""
    assert (
        topic_ledger.classify_delete_error(tg_err.BadRequest("Chat not found"))
        == "not_found"
    )
    assert (
        topic_ledger.classify_delete_error(tg_err.BadRequest("CHAT NOT FOUND"))
        == "not_found"
    )


def test_classify_unrecognized_bad_request_is_unknown_not_transient():
    """BadRequest subclasses NetworkError in python-telegram-bot: a naive
    NetworkError-first isinstance ladder would misclassify every BadRequest
    as transient. An unrecognized BadRequest must be 'unknown' (kept)."""
    exc = tg_err.BadRequest("Chat not modified")
    assert isinstance(exc, tg_err.NetworkError)  # the hazard being pinned
    assert topic_ledger.classify_delete_error(exc) == "unknown"


def test_classify_non_telegram_errors_are_unknown():
    assert topic_ledger.classify_delete_error(ValueError("boom")) == "unknown"
    assert topic_ledger.classify_delete_error(RuntimeError("weird")) == "unknown"


# ---------------------------------------------------------------------------
# sweep_topics
# ---------------------------------------------------------------------------


async def test_sweep_due_scope_deletes_only_due_entries(tmp_path):
    path = tmp_path / "ledger.json"
    _seed(path, [
        _entry("due", topic_id=613, delete_after=NOW - 1),
        _entry("future", topic_id=614, delete_after=NOW + DAY),
    ])
    channel = StubChannel()

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, scope="due", now=NOW, path=str(path),
    )

    assert channel.calls == [613]
    assert result == _expected(
        deleted=1, kept=1,
        targets=[{"engagement_id": "due", "topic_id": 613}],
    )
    entries = await topic_ledger.load(path=str(path))
    assert [e["engagement_id"] for e in entries] == ["future"]


async def test_sweep_all_terminal_scope_ignores_delete_after(
    tmp_path, recorded_sleeps,
):
    path = tmp_path / "ledger.json"
    _seed(path, [
        _entry("due", topic_id=613, delete_after=NOW - 1),
        _entry("future", topic_id=614, delete_after=NOW + DAY),
    ])
    channel = StubChannel()

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, scope="all_terminal", now=NOW, path=str(path),
    )

    assert channel.calls == [613, 614]
    assert result["deleted"] == 2
    assert result["kept"] == 0
    assert await topic_ledger.load(path=str(path)) == []


async def test_sweep_dry_run_deletes_nothing_and_mutates_nothing(tmp_path, caplog):
    path = tmp_path / "ledger.json"
    seeded = [
        _entry("due", topic_id=613, delete_after=NOW - 1),
        _entry("future", topic_id=614, delete_after=NOW + DAY),
        _entry("migrated", chat_id=OTHER_CHAT, topic_id=615, delete_after=NOW - 1),
    ]
    _seed(path, seeded)
    channel = StubChannel()

    with caplog.at_level(logging.WARNING):
        result = await topic_ledger.sweep_topics(
            channel, chat_id=CHAT, scope="due", dry_run=True, now=NOW,
            path=str(path),
        )

    assert channel.calls == []
    assert result == _expected(
        deleted=1, kept=1, dropped_mismatched=1, dry_run=True,
        targets=[{"engagement_id": "due", "topic_id": 613}],
    )
    assert await topic_ledger.load(path=str(path)) == seeded
    # A dry run mutates nothing — its mismatch warning must not claim a
    # drop that never happened.
    mismatch = [r for r in _tl_warnings(caplog) if "would drop" in r.getMessage()]
    assert mismatch, "dry-run mismatch warning must say it WOULD drop"


@pytest.mark.parametrize("scope", ["due", "all_terminal"])
async def test_sweep_dry_run_echoes_targets_for_both_scopes(tmp_path, scope):
    """dry_run identities: ``targets`` lists exactly the would-be-deleted
    entries for the given scope; no channel call, no ledger mutation."""
    path = tmp_path / "ledger.json"
    seeded = [
        _entry("due", topic_id=613, delete_after=NOW - 1),
        _entry("future", topic_id=614, delete_after=NOW + DAY),
    ]
    _seed(path, seeded)
    channel = StubChannel()

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, scope=scope, dry_run=True, now=NOW,
        path=str(path),
    )

    expected_targets = [{"engagement_id": "due", "topic_id": 613}]
    if scope == "all_terminal":
        expected_targets.append({"engagement_id": "future", "topic_id": 614})

    assert channel.calls == [], "dry run must never call delete_topic"
    assert result["dry_run"] is True
    assert result["targets"] == expected_targets
    assert result["deleted"] == len(expected_targets)
    assert await topic_ledger.load(path=str(path)) == seeded


async def test_sweep_drops_mismatched_chat_without_deleting(tmp_path, caplog):
    """[AR-2] topic ids restart low in a fresh supergroup — a stale entry
    from a previous group must never delete an innocent topic in the new
    one. Dropped from the ledger, never sent to the channel."""
    path = tmp_path / "ledger.json"
    _seed(path, [
        _entry("migrated", chat_id=OTHER_CHAT, topic_id=615, delete_after=NOW - 1),
        _entry("due", topic_id=613, delete_after=NOW - 1),
    ])
    channel = StubChannel()

    with caplog.at_level(logging.WARNING):
        result = await topic_ledger.sweep_topics(
            channel, chat_id=CHAT, now=NOW, path=str(path),
        )

    assert channel.calls == [613], "mismatched entry must never be deleted"
    assert result["dropped_mismatched"] == 1
    assert result["deleted"] == 1
    assert await topic_ledger.load(path=str(path)) == []
    assert _tl_warnings(caplog), "mismatch drop must be warned about"


@pytest.mark.parametrize("dry_run", [False, True])
async def test_sweep_keeps_null_chat_entries_one_aggregate_warning(
    tmp_path, caplog, dry_run,
):
    """Null-chat entries are kept forever — warned about as ONE aggregate
    line per sweep (not per entry), in dry-run too (it's informational)."""
    path = tmp_path / "ledger.json"
    _seed(path, [
        _entry("no-chat-1", chat_id=None, topic_id=613, delete_after=NOW - 1),
        _entry("no-chat-2", chat_id=None, topic_id=614, delete_after=NOW - 1),
    ])
    channel = StubChannel()

    with caplog.at_level(logging.WARNING):
        result = await topic_ledger.sweep_topics(
            channel, chat_id=CHAT, dry_run=dry_run, now=NOW, path=str(path),
        )

    assert channel.calls == []
    assert result == _expected(kept=2, dry_run=dry_run)
    assert len(await topic_ledger.load(path=str(path))) == 2
    warnings = _tl_warnings(caplog)
    assert len(warnings) == 1, "one aggregate warning, not per-entry spam"
    assert "2 entries have no chat_id" in warnings[0].getMessage()


async def test_sweep_with_unconfigured_chat_skips_cleanly(tmp_path, caplog):
    """chat_id param None (telegram unconfigured): delete nothing, drop
    nothing, kept = all, and — [AR-8] — no per-entry warning spam."""
    path = tmp_path / "ledger.json"
    seeded = [
        _entry("due", topic_id=613, delete_after=NOW - 1),
        _entry("no-chat", chat_id=None, topic_id=614, delete_after=NOW - 1),
    ]
    _seed(path, seeded)
    channel = StubChannel()

    with caplog.at_level(logging.WARNING):
        result = await topic_ledger.sweep_topics(
            channel, chat_id=None, now=NOW, path=str(path),
        )

    assert channel.calls == []
    assert result == _expected(kept=2)
    assert await topic_ledger.load(path=str(path)) == seeded
    assert _tl_warnings(caplog) == []


async def test_sweep_not_found_removes_entry(tmp_path):
    """An already-gone topic (manual deletion, or a crash between delete and
    entry-removal) resolves the entry — counted in 'deleted'."""
    path = tmp_path / "ledger.json"
    _seed(path, [_entry("gone", topic_id=613, delete_after=NOW - 1)])
    channel = StubChannel(
        script={613: [tg_err.BadRequest("Message thread not found")]},
    )

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    )

    assert result["deleted"] == 1
    assert result["failures"] == []
    assert result["needs_permission"] is False
    # not_found resolutions are deletions from the ledger's point of view —
    # they appear in targets like an actual delete.
    assert result["targets"] == [{"engagement_id": "gone", "topic_id": 613}]
    assert await topic_ledger.load(path=str(path)) == []


async def test_sweep_permission_failure_keeps_entry_and_flags(tmp_path):
    path = tmp_path / "ledger.json"
    _seed(path, [
        _entry("forb", topic_id=613, delete_after=NOW - 1),
        _entry("rights", topic_id=614, delete_after=NOW - 1),
    ])
    channel = StubChannel(script={
        613: [tg_err.Forbidden("bot is not a member")],
        614: [tg_err.BadRequest("not enough rights")],
    })

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    )

    assert result["deleted"] == 0
    assert result["kept"] == 2
    assert result["needs_permission"] is True
    assert result["failures"] == [
        {"engagement_id": "forb", "topic_id": 613, "reason": "permission"},
        {"engagement_id": "rights", "topic_id": 614, "reason": "permission"},
    ]
    assert len(await topic_ledger.load(path=str(path))) == 2, (
        "entries must be retained for retry after the operator grants rights"
    )


async def test_sweep_unknown_error_keeps_entry(tmp_path):
    """NEVER remove an entry on an unrecognized error [AR-5]."""
    path = tmp_path / "ledger.json"
    _seed(path, [_entry("odd", topic_id=613, delete_after=NOW - 1)])
    channel = StubChannel(script={613: [RuntimeError("weird")]})

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    )

    assert result["deleted"] == 0
    assert result["kept"] == 1
    assert result["needs_permission"] is False
    assert result["failures"] == [
        {"engagement_id": "odd", "topic_id": 613, "reason": "unknown"},
    ]
    assert len(await topic_ledger.load(path=str(path))) == 1


# ---------------------------------------------------------------------------
# stuck-entry aging — bounded retry under permanent failure
# ---------------------------------------------------------------------------


_STUCK_DEADLINE = NOW - topic_ledger.STUCK_ENTRY_MAX_AGE_SECONDS - 1


async def test_sweep_drops_stuck_unknown_failure_with_warning(tmp_path, caplog):
    """An entry that keeps failing past STUCK_ENTRY_MAX_AGE_SECONDS beyond
    its deadline is dropped (warned) instead of kept — a permanent failure
    must not grow the ledger and the failures list forever."""
    path = tmp_path / "ledger.json"
    _seed(path, [_entry("stuck", topic_id=613, delete_after=_STUCK_DEADLINE)])
    channel = StubChannel(script={613: [RuntimeError("weird")]})

    with caplog.at_level(logging.WARNING):
        result = await topic_ledger.sweep_topics(
            channel, chat_id=CHAT, now=NOW, path=str(path),
        )

    assert channel.calls == [613], "stuck entries are still attempted"
    assert result["dropped_stuck"] == 1
    assert result["kept"] == 0
    assert result["failures"] == []
    assert await topic_ledger.load(path=str(path)) == []
    assert _tl_warnings(caplog), "the stuck drop must be warned about"


async def test_sweep_drops_stuck_permission_failure_but_still_flags(tmp_path):
    """A permission-class stuck entry is dropped too, but needs_permission
    still fires so the operator nag survives the drop."""
    path = tmp_path / "ledger.json"
    _seed(path, [_entry("stuck", topic_id=613, delete_after=_STUCK_DEADLINE)])
    channel = StubChannel(script={613: [tg_err.Forbidden("no rights")]})

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    )

    assert result["dropped_stuck"] == 1
    assert result["needs_permission"] is True
    assert result["failures"] == []
    assert await topic_ledger.load(path=str(path)) == []


async def test_sweep_failure_just_under_stuck_age_is_kept(tmp_path):
    """Strict boundary: at exactly delete_after + STUCK_ENTRY_MAX_AGE_SECONDS
    the entry is still kept-with-failure, not dropped."""
    path = tmp_path / "ledger.json"
    _seed(path, [_entry(
        "aging", topic_id=613,
        delete_after=NOW - topic_ledger.STUCK_ENTRY_MAX_AGE_SECONDS,
    )])
    channel = StubChannel(script={613: [RuntimeError("weird")]})

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    )

    assert result["dropped_stuck"] == 0
    assert result["kept"] == 1
    assert result["failures"] == [
        {"engagement_id": "aging", "topic_id": 613, "reason": "unknown"},
    ]
    assert len(await topic_ledger.load(path=str(path))) == 1


async def test_sweep_transient_error_keeps_entry(tmp_path):
    path = tmp_path / "ledger.json"
    _seed(path, [_entry("flaky", topic_id=613, delete_after=NOW - 1)])
    channel = StubChannel(script={613: [tg_err.TimedOut()]})

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    )

    assert result["failures"] == [
        {"engagement_id": "flaky", "topic_id": 613, "reason": "transient"},
    ]
    assert len(await topic_ledger.load(path=str(path))) == 1


async def test_sweep_honors_retry_after_once_then_keeps(
    tmp_path, recorded_sleeps,
):
    """[AR-6] one RetryAfter per entry is honored (sleep retry_after, retry
    once); a second flood error keeps the entry for the next sweep."""
    path = tmp_path / "ledger.json"
    _seed(path, [_entry("flood", topic_id=613, delete_after=NOW - 1)])
    channel = StubChannel(
        script={613: [tg_err.RetryAfter(2), tg_err.RetryAfter(5)]},
    )

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    )

    assert channel.calls == [613, 613], "exactly one retry per entry"
    assert recorded_sleeps == [2.0], "retry_after honored once, not twice"
    assert result["kept"] == 1
    assert result["failures"] == [
        {"engagement_id": "flood", "topic_id": 613, "reason": "transient"},
    ]
    assert len(await topic_ledger.load(path=str(path))) == 1


async def test_sweep_retry_after_then_success_removes(tmp_path, recorded_sleeps):
    path = tmp_path / "ledger.json"
    _seed(path, [_entry("flood", topic_id=613, delete_after=NOW - 1)])
    channel = StubChannel(script={613: [tg_err.RetryAfter(1)]})  # then success

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    )

    assert channel.calls == [613, 613]
    assert recorded_sleeps == [1.0]
    assert result["deleted"] == 1
    assert await topic_ledger.load(path=str(path)) == []


async def test_sweep_retry_after_over_cap_is_transient_without_sleep(
    tmp_path, recorded_sleeps,
):
    """Telegram can demand waits of hours; a sweep (or a live tool call)
    must never stall that long — an over-cap RetryAfter is classified
    transient with NO sleep and the entry retries at the next sweep."""
    path = tmp_path / "ledger.json"
    _seed(path, [_entry("flood", topic_id=613, delete_after=NOW - 1)])
    channel = StubChannel(script={613: [tg_err.RetryAfter(3600)]})

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    )

    assert channel.calls == [613], "no in-sweep retry after an over-cap flood"
    assert recorded_sleeps == [], "the demanded 3600s must never be slept"
    assert result["kept"] == 1
    assert result["failures"] == [
        {"engagement_id": "flood", "topic_id": 613, "reason": "transient"},
    ]
    assert len(await topic_ledger.load(path=str(path))) == 1


# ---------------------------------------------------------------------------
# malformed delete_after tolerance
# ---------------------------------------------------------------------------


async def test_sweep_malformed_delete_after_falls_back_to_closed_at_due(tmp_path):
    """Garbage delete_after + valid closed_at: due-ness is recomputed as
    closed_at + retention — here past due, so the topic is deleted."""
    path = tmp_path / "ledger.json"
    entry = _entry("fallback", topic_id=613)
    entry["delete_after"] = "garbage"
    entry["closed_at"] = NOW - topic_ledger.TOPIC_RETENTION_DAYS * DAY - 1
    _seed(path, [entry])
    channel = StubChannel()

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, scope="due", now=NOW, path=str(path),
    )

    assert channel.calls == [613]
    assert result["deleted"] == 1
    assert result["dropped_malformed"] == 0
    assert await topic_ledger.load(path=str(path)) == []


async def test_sweep_malformed_delete_after_falls_back_to_closed_at_not_due(
    tmp_path,
):
    """Same fallback, not-due case: closed_at is recent, so the entry is
    kept — a malformed delete_after must not make it prematurely due."""
    path = tmp_path / "ledger.json"
    entry = _entry("fallback", topic_id=613)
    entry["delete_after"] = None
    entry["closed_at"] = NOW - 1  # recomputed deadline: NOW - 1 + 7 days
    _seed(path, [entry])
    channel = StubChannel()

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, scope="due", now=NOW, path=str(path),
    )

    assert channel.calls == []
    assert result["kept"] == 1
    assert result["dropped_malformed"] == 0
    assert len(await topic_ledger.load(path=str(path))) == 1


async def test_sweep_drops_malformed_beyond_use_and_continues(tmp_path, caplog):
    """Both timestamps garbage: dropped (dropped_malformed) with a warning,
    no delete attempt — and the NEXT entry is still processed; one bad
    entry must never abort the sweep."""
    path = tmp_path / "ledger.json"
    bad = _entry("bad", topic_id=613)
    bad["delete_after"] = "junk"
    bad["closed_at"] = "also-junk"
    _seed(path, [bad, _entry("good", topic_id=614, delete_after=NOW - 1)])
    channel = StubChannel()

    with caplog.at_level(logging.WARNING):
        result = await topic_ledger.sweep_topics(
            channel, chat_id=CHAT, now=NOW, path=str(path),
        )

    assert channel.calls == [614], "no delete attempt for the malformed entry"
    assert result["dropped_malformed"] == 1
    assert result["deleted"] == 1
    assert await topic_ledger.load(path=str(path)) == []
    assert _tl_warnings(caplog), "the malformed drop must be warned about"


async def test_sweep_serializes_deletions_with_spacing_sleep(
    tmp_path, recorded_sleeps,
):
    """[AR-6] bulk-rate safety: an inter-call sleep between deletions (not
    before the first)."""
    path = tmp_path / "ledger.json"
    _seed(path, [
        _entry("e1", topic_id=611, delete_after=NOW - 1),
        _entry("e2", topic_id=612, delete_after=NOW - 1),
        _entry("e3", topic_id=613, delete_after=NOW - 1),
    ])
    channel = StubChannel()

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    )

    assert channel.calls == [611, 612, 613]
    assert recorded_sleeps == [topic_ledger.DELETE_SPACING_SECONDS] * 2
    assert result["deleted"] == 3
    assert await topic_ledger.load(path=str(path)) == []


async def test_sweep_counters_partition_the_ledger(tmp_path, recorded_sleeps):
    """deleted + kept + dropped_mismatched + dropped_stuck + dropped_malformed
    == entries seen, one bucket each."""
    path = tmp_path / "ledger.json"
    malformed = _entry("malformed", topic_id=617)
    malformed["delete_after"] = "junk"
    malformed["closed_at"] = None
    _seed(path, [
        _entry("ok", topic_id=611, delete_after=NOW - 1),
        _entry("future", topic_id=612, delete_after=NOW + DAY),
        _entry("migrated", chat_id=OTHER_CHAT, topic_id=613, delete_after=NOW - 1),
        _entry("no-chat", chat_id=None, topic_id=614, delete_after=NOW - 1),
        _entry("denied", topic_id=615, delete_after=NOW - 1),
        _entry("stuck", topic_id=616, delete_after=_STUCK_DEADLINE),
        malformed,
    ])
    channel = StubChannel(script={
        615: [tg_err.Forbidden("no")],
        616: [RuntimeError("weird")],
    })

    result = await topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    )

    assert result["deleted"] == 1
    assert result["kept"] == 3  # future + no-chat + permission-denied
    assert result["dropped_mismatched"] == 1
    assert result["dropped_stuck"] == 1
    assert result["dropped_malformed"] == 1
    assert (
        result["deleted"] + result["kept"] + result["dropped_mismatched"]
        + result["dropped_stuck"] + result["dropped_malformed"]
    ) == 7
    remaining = {e["engagement_id"] for e in await topic_ledger.load(path=str(path))}
    assert remaining == {"future", "no-chat", "denied"}


# ---------------------------------------------------------------------------
# #311: sweep serialization + per-entry checkpointing
# ---------------------------------------------------------------------------


class _GatedChannel:
    """delete_topic parks on an event so a second sweep can be interleaved
    deterministically while the first is mid-delete."""

    def __init__(self):
        self.calls: list[int] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def delete_topic(self, thread_id):
        self.calls.append(thread_id)
        self.entered.set()
        await self.release.wait()


async def test_concurrent_sweeps_do_not_double_delete(tmp_path):
    """#311: the scheduled sweep and the configurator sweep tool could both
    load the same due entry before either removed it, double-issuing the
    Telegram deletion (the loser folded into `deleted` as not_found). Sweeps
    must be serialized: one delete_topic call per topic, ever."""
    path = tmp_path / "ledger.json"
    _seed(path, [_entry("dup", topic_id=613, delete_after=NOW - 1)])
    channel = _GatedChannel()

    sweep1 = asyncio.ensure_future(topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    ))
    await asyncio.wait_for(channel.entered.wait(), 1)  # sweep1 mid-delete
    sweep2 = asyncio.ensure_future(topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    ))
    for _ in range(10):
        await asyncio.sleep(0)  # give sweep2 every chance to (wrongly) load
    channel.release.set()
    r1, r2 = await asyncio.gather(sweep1, sweep2)

    assert channel.calls == [613], (
        f"the same topic was deleted {len(channel.calls)} times: "
        f"{channel.calls!r}"
    )
    assert r1["deleted"] + r2["deleted"] == 1
    assert await topic_ledger.load(path=str(path)) == []


async def test_cancelled_sweep_checkpoints_completed_deletions(tmp_path):
    """#311 (design r1): a sweep cancelled mid-run must not forget deletions
    it already performed — each resolved entry is removed from the ledger
    before the next cancellable await, so the next sweep re-issues at most
    the single in-flight entry."""
    path = tmp_path / "ledger.json"
    _seed(path, [
        _entry("done", topic_id=613, delete_after=NOW - 1),
        _entry("in-flight", topic_id=614, delete_after=NOW - 1),
    ])

    class _SecondCallParks:
        def __init__(self):
            self.calls: list[int] = []
            self.second_entered = asyncio.Event()

        async def delete_topic(self, thread_id):
            self.calls.append(thread_id)
            if len(self.calls) >= 2:
                self.second_entered.set()
                await asyncio.Event().wait()  # park forever

    channel = _SecondCallParks()
    task = asyncio.ensure_future(topic_ledger.sweep_topics(
        channel, chat_id=CHAT, now=NOW, path=str(path),
    ))
    await asyncio.wait_for(channel.second_entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    remaining = {
        e["engagement_id"] for e in await topic_ledger.load(path=str(path))
    }
    assert remaining == {"in-flight"}, (
        "the completed deletion was not checkpointed before cancellation: "
        f"ledger still holds {remaining!r}"
    )
