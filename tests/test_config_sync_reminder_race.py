"""#458: a reminder written on the event loop while ``config_sync`` reconciles
the same ``triggers.yaml`` on a worker thread must not be discarded.

The fix is one process-wide lock (``trigger_write_lock.PASS_LOCK``) held by
``config_sync`` across the whole reconcile pass and taken by every ``reminders``
mutator around its read → write. These tests pin both halves: that the mutators
actually take the lock (deterministic), and that the end-to-end interleave which
loses a reminder without it is closed with it.

Design attack: Sol/Terra rounds 1-3 (2026-08-08) — the mechanism was cut from
per-write-site locking to one whole-pass lock after each round found another
uncovered write site.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

import config_sync
import reminders
import trigger_write_lock

pytestmark = pytest.mark.unit


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class _FakeGit:
    """No git repo: snapshots are unavailable, as on a fresh /config."""
    available = False

    def snapshot(self, message: str):
        return None

    def head(self):
        return None


def _no_schema(_rel: str):
    return None


# A minimal, schema-valid triggers.yaml (schema_version 2, a triggers list).
_TRIGGERS_V1 = (
    "schema_version: 2\n"
    "triggers: []\n"
)
# The image ships a changed copy of the same file — enough to make the
# "untouched live == baseline, image changed" fast path (config_sync.py ~1067)
# copy the default over live, clobbering anything added meanwhile.
_TRIGGERS_IMAGE = (
    "schema_version: 2\n"
    "triggers:\n"
    "  - name: shipped\n"
    "    type: cron\n"
    "    schedule: '0 8 * * *'\n"
    "    channel: telegram\n"
    "    prompt: shipped\n"
)

_REL = "agents/butler/triggers.yaml"


def _reminder_entry(name: str) -> dict:
    return {
        "name": name,
        "type": "date",
        "at": "2099-01-01T08:00:00+00:00",
        "one_shot": True,
        "channel": "telegram",
        "managed_by": reminders.OWNER_AGENT,
        "prompt": "hello",
    }


def test_reminder_mutator_takes_the_pass_lock(tmp_path: Path) -> None:
    """Deterministic: while PASS_LOCK is held, a mutator on another thread
    cannot complete; it proceeds only once the lock is released."""
    # #778: a managed path. This used to be
    # `pytest.importorskip("tempfile").mkdtemp()` — an importorskip on a stdlib
    # module — whose directory was never removed.
    path = tmp_path / "triggers.yaml"
    path.write_text(_TRIGGERS_V1, encoding="utf-8")

    done = threading.Event()

    def _add() -> None:
        reminders.add_entry(str(path), _reminder_entry("reminder-aaaaaa"))
        done.set()

    with trigger_write_lock.PASS_LOCK:
        t = threading.Thread(target=_add, daemon=True)
        t.start()
        # The mutator must block on the lock we hold — not finish.
        assert not done.wait(timeout=1.0), (
            "add_entry completed while PASS_LOCK was held — it did not take "
            "the lock, so config_sync can still clobber a concurrent write"
        )
    # Released: the mutator now completes and the reminder lands.
    assert done.wait(timeout=5.0)
    t.join(timeout=5.0)
    doc = reminders._read_doc(str(path))[1]
    assert [e["name"] for e in doc["triggers"]] == ["reminder-aaaaaa"]
    # #778: the one file this test wrote is inside the managed root.
    assert [q.name for q in tmp_path.iterdir()] == ["triggers.yaml"]


def test_reminder_survives_concurrent_reconcile_clobber(tmp_path: Path) -> None:
    """End-to-end interleave against the byte-clobber fast path.

    reconcile decides "untouched, image changed" and will copy the default over
    live. A reminder is added on another thread during the pass. With the
    whole-pass lock the add waits for the pass, then appends to the post-sync
    file, so it survives; without it the copy discards the reminder.
    """
    _write(tmp_path / "baseline", _REL, _TRIGGERS_V1)
    _write(tmp_path / "live", _REL, _TRIGGERS_V1)      # == baseline: untouched
    _write(tmp_path / "defaults", _REL, _TRIGGERS_IMAGE)  # image changed
    live_path = tmp_path / "live" / _REL

    reminder_written = threading.Event()
    let_copy_proceed = threading.Event()
    real_copy = config_sync._copy

    def _adder() -> None:
        # Blocks on PASS_LOCK until the pass releases it (with the fix).
        reminders.add_entry(str(live_path), _reminder_entry("reminder-bbbbbb"))
        reminder_written.set()

    adder = threading.Thread(target=_adder, daemon=True)

    def _copy_spy(src_root, rel, dst_root):
        if rel == _REL:
            # We are inside the pass, right before it clobbers this file.
            # Kick off the concurrent reminder write and give it a real chance
            # to run. With the lock it stays blocked; without it, it writes
            # here and the copy below destroys it.
            adder.start()
            reminder_written.wait(timeout=0.5)
        return real_copy(src_root, rel, dst_root)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(config_sync, "_copy", _copy_spy)
    try:
        config_sync.run(
            defaults_dir=tmp_path / "defaults",
            config_dir=tmp_path / "live",
            baseline_dir=tmp_path / "baseline",
            report_path=str(tmp_path / "report.json"),
            image_version="v9.9.9",
        )
    finally:
        monkeypatch.undo()

    assert reminder_written.wait(timeout=5.0)
    adder.join(timeout=5.0)

    names = [e["name"] for e in reminders._read_doc(str(live_path))[1]["triggers"]]
    assert "reminder-bbbbbb" in names, (
        f"reminder was discarded by the concurrent reconcile; file has {names}"
    )
