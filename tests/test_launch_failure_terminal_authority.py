"""#757 red case — INV-ENG-019: what authorizes an irreversible topic close.

Specified by **sol** in the drive redcase round (MODE: SPECIFY) against
``f414c4c6a149aefa08c097b1fbf98ee771cc937e``, with a convergent specification
from terra. Accepted by **terra**.

The invariant this module pins:

    A launch-failure arm aborts its engagement's topic only on the strength of a
    terminal transition this call both WON and durably persisted. A transition
    another writer won, and a transition whose persist rolled back leaving the
    record live, each leave the topic open and the record to its owner. The arm
    still answers its caller with its own named fault, and still posts no notice.

Pre-fix terminus, read at ``f414c4c6``:

  - ``engagement_registry.py:1325-1420`` — the strict arm snapshots every
    mutated field and, on a persist failure, restores the FULL snapshot and
    re-raises, so the record is left LIVE with memory equal to disk; a
    non-winner is refused with ``False``.
  - ``tools.py:8129-8143`` — ``LaunchDeathResult`` is three-way ON PURPOSE
    because collapsing these to one value "conflates a LOST terminal race …
    with a ROLLED-BACK persist".
  - ``tools.py:8488-8499`` — ``_abort_engagement_topic``, the irreversible act
    performed unconditionally after the discarded mark.
  - ``docs/architecture/engagement-failure-and-restart.md:104-108`` — the
    corpus states in terms that whether the best-effort mark "may authorize an
    irreversible topic close" is UNSETTLED and is #757.

**Every assertion here is on an OUTCOME**, never on an arrangement: painted /
closed / ledger-appended counts, and the status read back OUT OF THE TOMBSTONE
FILE. Nothing in this module configures ``mark_error`` as a mock, so the truthy
``MagicMock`` default that keeps ``test_engage_executor_tool.py:1362`` green
with the defect live cannot reach it.
"""

from __future__ import annotations

import asyncio
import json
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
    """Every operator-visible effect a launch-failure arm can produce."""

    def __init__(self) -> None:
        self.painted: list[tuple] = []
        self.closed: list[int] = []
        self.ledger: list[tuple] = []
        self.notices = 0

    @property
    def counts(self) -> tuple[int, int, int, int]:
        return (len(self.painted), len(self.closed), len(self.ledger),
                self.notices)


def _channel(effects: _Effects, topic_id: int):
    ch = MagicMock()
    ch.engagement_permission_ok = True
    ch.engagement_supergroup_id = -1001
    ch.open_engagement_topic = AsyncMock(return_value=topic_id)
    ch.send_to_topic = AsyncMock()

    async def _paint(*, engagement_id, new_state):
        effects.painted.append((engagement_id, new_state))

    async def _close(*, thread_id):
        effects.closed.append(thread_id)

    async def _notice(*a, **kw):
        effects.notices += 1

    ch.update_topic_state = AsyncMock(side_effect=_paint)
    ch.close_topic = AsyncMock(side_effect=_close)
    ch._post_engagement_notice = AsyncMock(side_effect=_notice)
    return ch


def _ledger_spy(monkeypatch, effects: _Effects):
    import topic_ledger

    async def _append(*, engagement_id, chat_id, topic_id, outcome,
                      closed_at=None, path=None):
        effects.ledger.append((engagement_id, topic_id, outcome))

    monkeypatch.setattr(topic_ledger, "append", _append)


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
# executor arms — nine of the thirteen
# --------------------------------------------------------------------------

def _executor_def(tmp_path, *, driver="in_casa", prompt_exists=True):
    from config import ExecutorDefinition

    p = tmp_path / "prompt.md"
    if prompt_exists:
        p.write_text("do {task} / {context} / {world_state_summary}")
    return ExecutorDefinition(
        role_artifact=STUB_ROLE_ARTIFACT,
        type="configurator",
        description="Configurator for the #757 launch-authority red case.",
        model="claude-sonnet-4-6",
        driver=driver,
        enabled=True,
        tools_allowed=["Read"],
        tools_disallowed=[],
        permission_mode="acceptEdits",
        mcp_server_names=["casa-framework"],
        idle_reminder_days=7,
        prompt_template_path=str(p),
        hooks_path=None,
        observer_policy_path=None,
        doctrine_dir="",
    )


def _build_executor(tmp_path, monkeypatch, effects, *, arm):
    """Real registry + real ``engage_executor.handler``, one failure arm armed."""
    import agent as agent_mod
    from engagement_registry import EngagementRegistry
    from tools import engage_executor, init_tools

    tombstone = tmp_path / "engagements.json"
    registry = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    channel = _channel(effects, 42)
    _ledger_spy(monkeypatch, effects)

    driver_kind = "claude_code" if arm.startswith("cc_") else "in_casa"
    defn = _executor_def(
        tmp_path, driver=driver_kind,
        prompt_exists=(arm != "prompt_template_missing"))

    exec_reg = MagicMock()
    exec_reg.get = MagicMock(return_value=defn)
    exec_reg.list_types = MagicMock(return_value=["configurator"])

    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)

    driver = MagicMock()
    driver.start = AsyncMock(side_effect=_start_effect(arm))
    driver.cancel = AsyncMock()

    attr = ("active_claude_code_driver" if driver_kind == "claude_code"
            else "active_engagement_driver")
    other = ("active_engagement_driver" if driver_kind == "claude_code"
             else "active_claude_code_driver")
    monkeypatch.setattr(agent_mod, other, MagicMock(), raising=False)
    monkeypatch.setattr(
        agent_mod, attr, None if arm.endswith("no_driver") else driver,
        raising=False)

    if arm == "plugin_superseded":
        import tools as tools_mod

        # STABLE across the bind loop (sample → resolve → verify), then BUMPED
        # once the record exists — which is exactly the manual
        # `casa_reload(scope="full")` seam this arm exists for: it moves the
        # snapshot without the plugin-tools lock, so the post-create recheck
        # is the only thing that catches it.
        def _gen():
            return 2 if registry._records else 1

        monkeypatch.setattr(
            tools_mod.plugin_registry, "snapshot_generation", _gen,
            raising=False)

    init_tools(
        channel_manager=cm, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=registry,
        executor_registry=exec_reg,
    )
    return engage_executor, registry, tombstone, channel


def _start_effect(arm: str):
    from drivers.driver_protocol import StaleLaunchError
    from error_kinds import ApiErrorTurn, ErrorKind

    if arm.endswith("clearance_changed_during_launch"):
        return StaleLaunchError("clamped", record_live=False)
    if arm.endswith("api_error"):
        return ApiErrorTurn(ErrorKind.RATE_LIMIT, "rate limited")
    return RuntimeError("boom")


# --------------------------------------------------------------------------
# delegate_to_agent interactive arms — the other four
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


def _build_delegate(tmp_path, monkeypatch, effects, *, arm):
    import agent as agent_mod
    from engagement_registry import EngagementRegistry
    from tools import delegate_to_agent, init_tools

    tombstone = tmp_path / "engagements.json"
    registry = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    channel = _channel(effects, 555)
    _ledger_spy(monkeypatch, effects)

    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)
    specialist_reg = MagicMock()
    specialist_reg.get = MagicMock(return_value=_finance_cfg())

    driver = MagicMock()
    driver.start = AsyncMock(side_effect=_start_effect(arm))
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
    return delegate_to_agent, registry, tombstone, channel


# --------------------------------------------------------------------------
# the thirteen arms
# --------------------------------------------------------------------------

#: (case id, handler family, arm key, the kind the caller MUST still be told)
ARMS = [
    ("delegate_no_driver",           "delegate", "no_driver",
     "no_driver"),
    ("delegate_stale",               "delegate", "clearance_changed_during_launch",
     "clearance_changed_during_launch"),
    ("delegate_api_error",           "delegate", "api_error",
     "rate_limit"),
    ("delegate_start_failed",        "delegate", "start_failed",
     "driver_start_failed"),
    ("executor_plugin_superseded",   "executor", "plugin_superseded",
     "plugin_superseded"),
    ("executor_prompt_missing",      "executor", "prompt_template_missing",
     "prompt_template_missing"),
    ("executor_cc_no_driver",        "executor", "cc_no_driver",
     "no_driver"),
    ("executor_cc_stale",            "executor", "cc_clearance_changed_during_launch",
     "clearance_changed_during_launch"),
    ("executor_cc_start_failed",     "executor", "cc_start_failed",
     "driver_start_failed"),
    ("executor_incasa_no_driver",    "executor", "no_driver",
     "no_driver"),
    ("executor_incasa_stale",        "executor", "clearance_changed_during_launch",
     "clearance_changed_during_launch"),
    ("executor_incasa_api_error",    "executor", "api_error",
     "rate_limit"),
    ("executor_incasa_start_failed", "executor", "start_failed",
     "driver_start_failed"),
]


async def _run(family, tmp_path, monkeypatch, effects, *, arm, sabotage=None):
    """Drive one launch-failure arm end to end and return its payload."""
    import agent as agent_mod

    if family == "delegate":
        handler, registry, tombstone, channel = _build_delegate(
            tmp_path, monkeypatch, effects, arm=arm)
        args = {"agent": "finance", "task": "Plan Q2", "context": "",
                "mode": "interactive"}
    else:
        handler, registry, tombstone, channel = _build_executor(
            tmp_path, monkeypatch, effects, arm=arm)
        args = {"executor_type": "configurator", "task": "do it",
                "context": ""}

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

class TestOnlyADurableWinMayCloseTheTopic:
    """INV-ENG-019, the two arms the base gets wrong."""

    @pytest.mark.parametrize(
        "case,family,arm,kind", ARMS, ids=[a[0] for a in ARMS])
    async def test_a_lost_terminal_race_leaves_the_topic_open(
        self, case, family, arm, kind, tmp_path, monkeypatch,
    ):
        """Another writer committed the terminal first.

        The winner owns every terminal side effect (INV-ENG-001), so this arm
        may paint nothing, close nothing and append nothing — while still
        answering its caller with its own named fault.
        """
        effects = _Effects()
        payload, registry, tombstone = await _run(
            family, tmp_path, monkeypatch, effects,
            arm=arm, sabotage=lambda reg, _t: _cancel_on_create(reg))

        assert payload["status"] == "error", payload
        assert payload["kind"] == kind, payload

        # The outcome, not the arrangement.
        assert effects.counts == (0, 0, 0, 0), effects.counts

        rows = _disk_rows(tombstone)
        assert [r["status"] for r in rows] == ["cancelled"], rows
        assert "error_kind" not in rows[0]["origin"], rows[0]["origin"]

    @pytest.mark.parametrize(
        "case,family,arm,kind", ARMS, ids=[a[0] for a in ARMS])
    async def test_a_rolled_back_persist_leaves_the_topic_open(
        self, case, family, arm, kind, tmp_path, monkeypatch,
    ):
        """The terminal mark never reached disk.

        The strict arm restores the full snapshot and re-raises, so the record
        is still LIVE and memory equals disk. An irreversible close here would
        orphan a record boot replay will resume into a topic that is gone.
        """
        effects = _Effects()
        payload, registry, tombstone = await _run(
            family, tmp_path, monkeypatch, effects,
            arm=arm, sabotage=_fail_terminal_write)

        assert payload["status"] == "error", payload
        assert payload["kind"] == kind, payload

        assert effects.counts == (0, 0, 0, 0), effects.counts

        rows = _disk_rows(tombstone)
        assert [r["status"] for r in rows] == ["active"], rows
        assert "error_kind" not in rows[0]["origin"], rows[0]["origin"]
        # memory == disk is the whole point of the strict arm
        live = [r for r in registry._records.values()]
        assert [r.status for r in live] == ["active"], live

    @pytest.mark.parametrize(
        "case,family,arm,kind", ARMS, ids=[a[0] for a in ARMS])
    async def test_a_durable_win_still_paints_closes_and_appends_once(
        self, case, family, arm, kind, tmp_path, monkeypatch,
    ):
        """The regression control for all thirteen arms.

        Without this, "never abort" passes the two cases above. Exactly one
        paint, one close and one ledger append, and still no notice — the
        launch-failure notice exemption is a committed ruling.
        """
        effects = _Effects()
        payload, registry, tombstone = await _run(
            family, tmp_path, monkeypatch, effects, arm=arm)

        assert payload["status"] == "error", payload
        assert payload["kind"] == kind, payload

        assert effects.counts == (1, 1, 1, 0), effects.counts
        assert effects.painted[0][1] == "failed", effects.painted

        rows = _disk_rows(tombstone)
        assert [r["status"] for r in rows] == ["error"], rows
        assert rows[0]["origin"]["error_kind"] == kind, rows[0]["origin"]
        assert effects.closed == [rows[0]["topic_id"]], effects.closed
        assert effects.ledger[0][0] == rows[0]["id"], effects.ledger


class TestTheAuthorityOutlivesItsLauncher:
    """A cancellation after the durable commit must not strand an open topic
    over a terminal record: the abort is anchored and shielded."""

    async def test_cancelling_the_awaiter_does_not_cancel_the_abort(
        self, tmp_path, monkeypatch,
    ):
        """The launcher is cancelled after the durable commit and after the
        ledger append, while the paint is still in flight.

        Deterministic, not timed: the ledger spy SETS an event, and the paint
        blocks on a second event, so the cancellation is delivered at a known
        point rather than after a fixed number of scheduler yields.
        """
        import tools as tools_mod

        effects = _Effects()
        reached_ledger = asyncio.Event()
        release_paint = asyncio.Event()

        handler, registry, tombstone, channel = _build_executor(
            tmp_path, monkeypatch, effects, arm="start_failed")

        import topic_ledger

        async def _append(*, engagement_id, chat_id, topic_id, outcome,
                          closed_at=None, path=None):
            effects.ledger.append((engagement_id, topic_id, outcome))
            reached_ledger.set()

        monkeypatch.setattr(topic_ledger, "append", _append)

        async def _slow_paint(*, engagement_id, new_state):
            await release_paint.wait()
            effects.painted.append((engagement_id, new_state))

        channel.update_topic_state = AsyncMock(side_effect=_slow_paint)

        import agent as agent_mod
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            caller = asyncio.ensure_future(handler.handler({
                "executor_type": "configurator", "task": "do it",
                "context": "",
            }))
            await asyncio.wait_for(reached_ledger.wait(), 10)

            # Past the durable terminal commit and the ledger append, blocked
            # in the paint. The topic is NOT yet closed.
            assert effects.closed == []
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller

            release_paint.set()
            await asyncio.wait_for(
                tools_mod.drain_launch_death_reports(), 30)
        finally:
            release_paint.set()
            agent_mod.origin_var.reset(token)

        # The abort outlived its launcher: the topic is painted and CLOSED.
        assert len(effects.painted) == 1, effects.painted
        assert len(effects.closed) == 1, effects.closed
        assert effects.notices == 0
        assert [r["status"] for r in _disk_rows(tombstone)] == ["error"]
