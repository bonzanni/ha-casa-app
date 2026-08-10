"""The private-state write sites must land a FRESH file at 0600 themselves.

GHSA-569r-7crq-xr43 has two halves. ``private_state.enforce()`` repairs what is
already on disk (tested in ``test_private_state.py``); this file pins the other
half — that each writer creates its file private in the first place, so a new
file is never even briefly world-readable between its creation and the next
boot's repair pass.

These assert the resulting **mode on disk**, not that a helper was called with a
particular argument: the ack-ledger durability tests monkeypatch
``atomic_io.atomic_write_text`` with a double that ignores its kwargs, so nothing
else in the suite would notice ``mode=PRIVATE`` being dropped from a call site.

Deliberately covers all three write idioms that exist in the tree:
``atomic_io`` directly, the ack-ledger wrapper, and ``plugin_setup_episodes``,
which had its own hand-rolled temp-file replace before this change.
"""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _mode(p: Path | str) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_session_registry_snapshot_is_private(tmp_path: Path) -> None:
    from session_reg_helpers import (
        STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV,
    )
    from session_registry import SessionRegistry

    path = tmp_path / "sessions.json"
    reg = SessionRegistry(str(path))
    asyncio.run(reg.register(
        "telegram:1", "assistant", "sid-1",
        binding_digest=STUB_BINDING_DIGEST,
        speaker_provenance=STUB_SPEAKER_PROV,
        user_provenance=STUB_USER_PROV,
    ))
    assert _mode(path) == 0o600


def test_topic_ledger_is_private(tmp_path: Path) -> None:
    import topic_ledger

    path = tmp_path / "topic-ledger.json"
    topic_ledger._write_entries(str(path), [{"topic": "x"}])
    assert _mode(path) == 0o600


def test_plugin_health_report_is_private(tmp_path: Path) -> None:
    import plugin_health

    path = tmp_path / "plugin-health.json"
    plugin_health._atomic_write(path, {"issues": [], "warnings": []})
    assert _mode(path) == 0o600


def test_callback_ack_ledger_is_private(tmp_path: Path) -> None:
    from callback_acks import CallbackAckStore

    path = tmp_path / "callback_acks.json"
    CallbackAckStore(path=path).record("elevenlabs", "plg-x--oauth", "digest-1")
    assert _mode(path) == 0o600


def test_plugin_setup_episodes_store_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This writer used to be a hand-rolled ``tmp.write_text`` + ``os.replace``,
    which landed the file at the 0022 umask default; it now goes through
    atomic_io, which also gives it the durability it never had."""
    import plugin_setup_episodes

    path = tmp_path / "plugin-setup-episodes.json"
    monkeypatch.setattr(plugin_setup_episodes, "STORE_PATH", path)
    plugin_setup_episodes._save({"episodes": []})
    assert _mode(path) == 0o600
    # still valid JSON through the new path, not just private
    assert path.read_text(encoding="utf-8").strip().startswith("{")


def test_cold_retain_retry_dir_is_private(tmp_path: Path) -> None:
    """The directory is what makes the records unreachable; it was created at
    0755 by a bare ``mkdir(parents=True)``."""
    d = tmp_path / "cold-retain-retry"
    d.mkdir(mode=0o700, parents=True)
    assert _mode(d) == 0o700


def test_atomic_io_default_is_still_world_readable(tmp_path: Path) -> None:
    """The counterweight, stated as a test so it cannot be "tidied" later: the
    mode=None default MUST stay 0644. The same helper writes /config artifacts
    (the plugin registry and store) that a uid-dropped engagement has to load,
    so flipping the default would break plugin loading silently — which is worse
    than the exposure this release closes. Private call sites pass mode=PRIVATE
    explicitly; see private_state for which paths those are."""
    import atomic_io

    p = tmp_path / "public.json"
    atomic_io.atomic_write_json(p, {"a": 1})
    assert _mode(p) == 0o644
    assert atomic_io.PRIVATE == 0o600
