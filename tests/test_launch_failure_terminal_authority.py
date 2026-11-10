"""#757 — a named launch-failure arm acts only on a transition it won.

A launch-failure arm settles its job (the message journal) only on the
strength of a terminal transition this call both WON and durably persisted. A
transition another writer won, and a transition whose persist rolled back
leaving the record live, each leave the record to its owner. The arm still
answers its caller with its own named fault, and posts no notice.

**Every assertion here is on an OUTCOME**, never on an arrangement: the
journal settles and channel calls counted, and the status read back OUT OF THE
TOMBSTONE FILE. Nothing in this module configures ``mark_error`` as a mock.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import (
    AgentConfig, CharacterConfig, DelegateEntry, MemoryConfig, SessionConfig,
    ToolsConfig,
)

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio]


# --------------------------------------------------------------------------
# outcome probes — counted effects only
# --------------------------------------------------------------------------

class _Effects:
    """What a launch-failure arm can do beyond its record: settle the job's
    journal, and post through the channel."""

    def __init__(self) -> None:
        self.settled: list[str] = []
        self.channel: MagicMock | None = None

    @property
    def posts(self) -> list:
        """Every call on the channel that would put a message somewhere."""
        calls = self.channel.mock_calls if self.channel else []
        return [c for c in calls
                if c[0].lstrip("_").startswith(("send", "post", "deliver",
                                                 "notify", "tell"))]

    @property
    def counts(self) -> tuple[int, int]:
        return (len(self.settled), len(self.posts))


def _channel(effects: _Effects):
    ch = MagicMock()
    effects.channel = ch
    return ch


def _settle_spy(monkeypatch, effects: _Effects):
    import tools

    async def _settle(rec, driver):
        effects.settled.append(rec.id)

    monkeypatch.setattr(tools, "_settle_job_journal", _settle)


def _disk_rows(tombstone) -> list[dict]:
    """The tombstone as it actually is ON DISK — never the in-memory record."""
    return json.loads(tombstone.read_text())


def _disk_row(tombstone, engagement_id: str) -> dict:
    rows = [r for r in _disk_rows(tombstone) if r["id"] == engagement_id]
    assert len(rows) == 1, rows
    return rows[0]


def _fail_terminal_write(registry, tombstone):
    """Make ONLY the terminal-``error`` snapshot fail to persist.

    Creation still persists normally, so the record is durably live before the
    launch fails. This is the swallowed-write interleaving: the strict arm must
    roll back and leave memory == disk == live.
    """
    real = registry._write_tombstone

    def _write(snapshot):
        if any(row.get("status") == "error" for row in snapshot):
            raise OSError("tombstone volume is gone")
        return real(snapshot)

    registry._write_tombstone = _write


def _cancel_on_create(registry):
    """Another writer commits ``cancelled`` the instant the record exists.

    Models an operator ``/cancel`` landing between ``create()`` and the launch
    failure — the lost-race interleaving. Uses the registry's own strict
    primitive, so the loser really is refused by the production gate rather
    than by a stub.
    """
    real_create = registry.create

    async def _create(*a, **kw):
        rec = await real_create(*a, **kw)
        won = await registry.try_transition_terminal(
            rec.id, "cancelled", strict=True)
        if not won:                     # arrangement, not an outcome
            raise AssertionError(
                "harness precondition: the concurrent writer must win")
        return rec

    registry.create = _create


# --------------------------------------------------------------------------
# a specialist-hosted job's engagement launch arms
# --------------------------------------------------------------------------

def _assistant_cfg():
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="assistant")
    cfg.delegates = [DelegateEntry(agent="finance", purpose="p", when="w")]
    return cfg


def _finance_cfg():
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="finance")
    cfg.character = CharacterConfig(name="Alex", archetype="finance",
                                    card="", prompt="You are Alex.")
    cfg.enabled = True
    cfg.model = "sonnet"
    cfg.tools = ToolsConfig(allowed=["Read"], disallowed=[],
                            permission_mode="acceptEdits", max_turns=20)
    cfg.mcp_server_names = ["casa-framework"]
    cfg.memory = MemoryConfig(token_budget=0)
    cfg.session = SessionConfig(strategy="ephemeral", idle_timeout=0)
    cfg.channels = []
    cfg.system_prompt = "You are Alex."
    return cfg


def _build_job(tmp_path, monkeypatch, effects, *, arm):
    import agent as agent_mod
    from engagement_registry import EngagementRegistry
    import tools
    from tools import init_tools

    tombstone = tmp_path / "engagements.json"
    registry = EngagementRegistry(tombstone_path=str(tombstone))
    channel = _channel(effects)

    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)
    specialist_reg = MagicMock()
    specialist_reg.get = MagicMock(return_value=_finance_cfg())

    driver = MagicMock()
    driver.open = AsyncMock(side_effect=RuntimeError("boom"))
    driver.cancel = AsyncMock()
    monkeypatch.setattr(
        agent_mod, "active_engagement_driver",
        None if arm.endswith("no_driver") else driver, raising=False)

    init_tools(
        channel_manager=cm, bus=MagicMock(),
        specialist_registry=specialist_reg, mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=registry,
        agent_role_map={"assistant": _assistant_cfg()},
    )

    async def _launch(args):
        # as ``_start_job_on_host`` hands a specialist-hosted job over
        import background_jobs as jobs
        job = jobs.JobDecl("finplan:plan", "finplan", "plan", "finplan:plan",
                           args["task"], None)
        origin = dict(agent_mod.origin_var.get())
        origin["plugin_job"] = {"plugin": job.plugin}
        return await tools._launch_interactive_engagement(
            args["agent"], args["task"], args["context"], origin, job=job)

    return SimpleNamespace(handler=_launch), registry, tombstone, channel


# --------------------------------------------------------------------------
# the arms
# --------------------------------------------------------------------------

#: (case id, handler family, arm key, the kind the caller MUST still be told).
#: The configurator's six executor arms went with ``engage_executor``: the
#: configurator is a delegated run now, and the job launch owner is the one
#: engagement launcher left. ``start_failed`` is the job session failing to
#: open.
ARMS = [
    ("job_no_driver",           "job", "no_driver",
     "no_driver"),
    ("job_start_failed",        "job", "start_failed",
     "driver_start_failed"),
]


async def _run(family, tmp_path, monkeypatch, effects, *, arm, sabotage=None):
    """Drive one launch-failure arm end to end and return its payload."""
    import agent as agent_mod

    assert family == "job"
    handler, registry, tombstone, channel = _build_job(
        tmp_path, monkeypatch, effects, arm=arm)
    args = {"agent": "finance", "task": "Plan Q2", "context": ""}

    if sabotage is not None:
        sabotage(registry, tombstone)

    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram",
        "chat_id": "c1", "cid": "x", "user_text": "hi", "scope": "business",
    })
    try:
        res = await handler.handler(args)
    finally:
        agent_mod.origin_var.reset(token)
    return json.loads(res["content"][0]["text"]), registry, tombstone


# --------------------------------------------------------------------------
# the red cases
# --------------------------------------------------------------------------

class TestOnlyADurableWinSettlesTheJob:
    """The two arms of a specialist-hosted job's launch."""

    @pytest.mark.parametrize(
        "case,family,arm,kind", ARMS, ids=[a[0] for a in ARMS])
    async def test_a_lost_terminal_race_leaves_the_record_to_its_owner(
        self, case, family, arm, kind, tmp_path, monkeypatch,
    ):
        """Another writer committed the terminal first.

        The winner owns every terminal side effect (INV-ENG-001), so this arm
        settles nothing and posts nothing — while still answering its caller
        with its own named fault.
        """
        effects = _Effects()
        _settle_spy(monkeypatch, effects)
        payload, registry, tombstone = await _run(
            family, tmp_path, monkeypatch, effects,
            arm=arm, sabotage=lambda reg, _t: _cancel_on_create(reg))

        assert payload["status"] == "error", payload
        assert payload["kind"] == kind, payload

        # The outcome, not the arrangement.
        assert effects.counts == (0, 0), effects.counts

        rows = _disk_rows(tombstone)
        assert [r["status"] for r in rows] == ["cancelled"], rows
        assert "error_kind" not in rows[0]["origin"], rows[0]["origin"]

    @pytest.mark.parametrize(
        "case,family,arm,kind", ARMS, ids=[a[0] for a in ARMS])
    async def test_a_rolled_back_persist_leaves_the_record_live(
        self, case, family, arm, kind, tmp_path, monkeypatch,
    ):
        """The terminal mark never reached disk.

        The strict arm restores the full snapshot and re-raises, so the record
        is still LIVE and memory equals disk; its job is not settled.
        """
        effects = _Effects()
        _settle_spy(monkeypatch, effects)
        payload, registry, tombstone = await _run(
            family, tmp_path, monkeypatch, effects,
            arm=arm, sabotage=_fail_terminal_write)

        assert payload["status"] == "error", payload
        assert payload["kind"] == kind, payload

        assert effects.counts == (0, 0), effects.counts

        rows = _disk_rows(tombstone)
        assert [r["status"] for r in rows] == ["active"], rows
        assert "error_kind" not in rows[0]["origin"], rows[0]["origin"]
        # memory == disk is the whole point of the strict arm
        live = [r for r in registry._records.values()]
        assert [r.status for r in live] == ["active"], live

    @pytest.mark.parametrize(
        "case,family,arm,kind", ARMS, ids=[a[0] for a in ARMS])
    async def test_a_durable_win_ends_the_record_and_settles_the_job(
        self, case, family, arm, kind, tmp_path, monkeypatch,
    ):
        """The regression control for every arm: the arm's own transition
        wins and persists, the record is ``error`` on disk, its job is
        settled once — and still no notice."""
        effects = _Effects()
        _settle_spy(monkeypatch, effects)
        payload, registry, tombstone = await _run(
            family, tmp_path, monkeypatch, effects, arm=arm)

        assert payload["status"] == "error", payload
        assert payload["kind"] == kind, payload

        rows = _disk_rows(tombstone)
        assert effects.counts == (1, 0), effects.counts
        assert effects.settled == [rows[0]["id"]]
        assert [r["status"] for r in rows] == ["error"], rows
        assert rows[0]["origin"]["error_kind"] == kind, rows[0]["origin"]


class TestTheAuthorityOutlivesItsLauncher:
    """A cancellation after the durable commit must not cut the abort's
    teardown short: the abort is anchored and shielded."""

    async def test_cancelling_the_awaiter_does_not_cancel_the_abort(
        self, tmp_path, monkeypatch,
    ):
        """The launcher is cancelled after the durable commit, while the
        abort is settling the job's message journal.

        Deterministic, not timed: the settle SETS an event and blocks on a
        second one, so the cancellation is delivered at a known point rather
        than after a fixed number of scheduler yields.
        """
        import tools as tools_mod

        effects = _Effects()
        reached_ledger = asyncio.Event()
        release_paint = asyncio.Event()
        settled: list = []

        handler, registry, tombstone, channel = _build_job(
            tmp_path, monkeypatch, effects, arm="start_failed")

        real_settle = tools_mod._settle_job_journal

        async def _slow_settle(rec, drv):
            reached_ledger.set()
            await release_paint.wait()
            await real_settle(rec, drv)
            settled.append(rec.id)

        monkeypatch.setattr(tools_mod, "_settle_job_journal", _slow_settle)

        import agent as agent_mod
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            caller = asyncio.ensure_future(handler.handler({
                "agent": "finance", "task": "Plan Q2", "context": "",
            }))
            await asyncio.wait_for(reached_ledger.wait(), 10)

            # Past the durable terminal commit, blocked in the settle.
            assert settled == []
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller

            release_paint.set()
            await asyncio.wait_for(
                tools_mod.drain_launch_death_reports(), 30)
        finally:
            release_paint.set()
            agent_mod.origin_var.reset(token)

        # The abort outlived its launcher: the journal was settled.
        assert len(settled) == 1, settled
        assert effects.posts == []
        assert [r["status"] for r in _disk_rows(tombstone)] == ["error"]
