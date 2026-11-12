"""An approval across a restart: one tap is one attempt.

A real Casa process is killed with ``os._exit`` at each point of an approval's
life, and the next process reads what the first one left:

* killed between Approve and the attempt, the approval is still ``approved``:
  asking again finds it already approved (no new card) and it may still start;
* killed after the attempt began — before or after the library write — the
  approval is ``used``: asking again posts a new card, and nothing starts it
  again.

Nothing runs at boot: the store is read when first asked.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import approval_records as ar
import private_state
import tools

CASA_ROOT = str(Path(__file__).resolve().parent.parent / "casa" / "rootfs" / "opt" / "casa")
KEY = "casa-test/mtg:mtg"
IDENTITY = "identity-of-mtg-with-these-values"
VALUES = {"config": {"API_URL": "https://example.test"}, "secrets": []}

# The first process: ask, post the real card, tap Approve, then stop at STOP.
_CHILD = textwrap.dedent('''
    import asyncio, json, os, sys
    from types import SimpleNamespace

    import approval_records as ar
    import configurator_continue
    import tools
    import verdict_broker
    from authz_grants import ChallengeCoordinator
    from specialist_install_consent import approval_card_hooks

    ar.PATH, STOP, KEY, IDENTITY, VALUES, WRITTEN = (
        sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], json.loads(sys.argv[5]), sys.argv[6])

    class Channel:
        bot_id = 111
        async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               entities=None):
            return 55
        async def edit_dm_message(self, chat_id, message_id, text, literal=False):
            return True

    starts = []
    def start(continuation, *, key, approval=None, asked="", on_outcome=None):
        starts.append(approval)
        return "started"
    configurator_continue.start = start

    async def main():
        async with tools._plugin_tools_guard():
            record, outcome, _ = ar.request("specialist_install", KEY, IDENTITY, VALUES)
        assert outcome == "new", outcome
        channel, coord = Channel(), ChallengeCoordinator()
        on_commit, finish_factory = approval_card_hooks(
            record=record, channel=channel, chat_id=100, continuation="carry on",
            name="mtg", noun="install", record_ack=lambda: True,
            not_recorded_text="not recorded")
        handle = coord.register_challenge(
            ("card", record.rid), bot_id=111, chat_id=100, operator_id=100,
            channel=channel, challenge_text="approve?", options=["Approve", "Deny"],
            on_commit_sync=on_commit, finish_factory=finish_factory,
            kind="specialist_install_consent", meta_extra={"approval_rid": record.rid})
        assert await handle.settled_post() == "posted"
        broker = verdict_broker.BROKER
        [rid] = broker.pending(namespace="resident_ask", scope="authz:111:100")
        claim = broker.claim(namespace="resident_ask", scope="authz:111:100",
                             request_id=rid, option_index=0, actor_id=100)
        assert broker.commit(claim)
        claim.req.meta["on_commit_sync"](0)
        for _ in range(50):
            await asyncio.sleep(0)
            if starts:
                break
        assert starts and starts[0].state == "approved", starts
        if STOP == "approved":
            os._exit(0)
        origin = {"rid": record.rid, "generation": record.generation}
        async with tools._plugin_tools_guard():
            assert ar.attempt(origin, kind="specialist_install",
                              consent_identity=IDENTITY) is None
            if STOP == "attempted":
                os._exit(0)
            with open(WRITTEN, "w") as fh:     # the library's write
                fh.write("installed")
            os._exit(0)

    asyncio.run(main())
    sys.exit(3)                                  # never reached: the process was killed
''')


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "data" / "approval-records.json"
    path.parent.mkdir()
    monkeypatch.setattr(ar, "PATH", str(path))
    monkeypatch.setattr(tools, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    ar.reset()
    yield path
    ar.reset()


def _run_until(stop: str, store: Path, tmp_path: Path) -> Path:
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")
    written = tmp_path / "written"
    env = {**os.environ, "PYTHONPATH": CASA_ROOT}
    proc = subprocess.run(
        [sys.executable, str(script), str(store), stop, KEY, IDENTITY, json.dumps(VALUES),
         str(written)],
        env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return written


def _boot() -> "list[ar.ApprovalRecord]":
    """The next process: nothing in memory, the store read from disk."""
    ar.reset()
    return [ar.current(KEY)]


async def _ask_again():
    async with tools._plugin_tools_guard():
        return ar.request("specialist_install", KEY, IDENTITY, VALUES)


async def test_killed_between_approve_and_the_attempt_the_approval_still_starts(
        store, tmp_path):
    _run_until("approved", store, tmp_path)
    [r] = _boot()
    assert (r.state, r.used_at, r.card is not None) == ("approved", None, True)
    assert ar.startable(r.rid, r.generation) == "ok"
    again, outcome, superseded = await _ask_again()
    # already approved: no new record, no card; Casa carries the run on with it
    assert (outcome, again.rid, superseded) == ("already_approved", r.rid, None)
    assert [x.rid for x in _boot()] == [r.rid]


@pytest.mark.parametrize("stop", ["attempted", "written"])
async def test_killed_after_the_attempt_the_approval_is_used_and_never_replayed(
        store, tmp_path, stop):
    written = _run_until(stop, store, tmp_path)
    assert written.exists() is (stop == "written")
    [r] = _boot()
    assert r.state == "used" and isinstance(r.used_at, float)
    # nothing starts it again, and a governed call refuses it
    assert ar.startable(r.rid, r.generation) == "used"
    origin = {"rid": r.rid, "generation": r.generation}
    assert ar.fence(origin, kind="specialist_install",
                    consent_identity=IDENTITY)["kind"] == "approval_used"
    # asking again is a new request: a new card
    again, outcome, superseded = await _ask_again()
    assert (outcome, again.state, superseded) == ("new", "pending", None)
    assert again.rid != r.rid and again.generation == r.generation + 1
    assert ar.get(r.rid).state == "used"


def test_the_records_are_private_state():
    [entry] = [e for e in private_state.FILES if e.path == "/data/approval-records.json"]
    assert (entry.mode, entry.kind) == (0o600, private_state.PRIVATE)
