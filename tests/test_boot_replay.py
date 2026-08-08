"""Tests for replay_undergoing_engagements — s6 boot-replay + orphan sweep."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest.fixture(autouse=True)
def _confirmed_service_stop(monkeypatch):
    """#335: these workspaces carry no ``.mcp.json``, so every record looks
    like a credential migration and replay cycles its service — which shells
    out to ``s6-rc``, absent in the test image. Default the stop to CONFIRMED
    so the cycle behaves as it does in production; the tests that exercise a
    teardown failure re-patch this themselves (a later monkeypatch wins)."""
    from drivers import s6_rc
    monkeypatch.setattr(
        s6_rc, "ensure_service_down", AsyncMock(return_value=True))


def _boot_driver():
    """A driver fake for boot-replay tests.

    F6 (whole-branch gate): ``casa_core.replay_undergoing_engagements`` calls
    ``schedule_boot_reconcile`` SYNCHRONOUSLY (it is sync by design — it schedules
    a task and returns it, or None). An unconstrained ``AsyncMock`` fabricates it
    as async, returning a coroutine casa_core never awaits → an
    unawaited-coroutine RuntimeWarning per call. Pin it to a plain sync no-op so
    the gate stays warning-clean. (``_spawn_background_tasks`` is likewise
    overridden by each caller.)"""
    driver = AsyncMock()
    driver.schedule_boot_reconcile = lambda *a, **k: None
    # #335: the replay loop reads this URL for the .mcp.json refresh; an
    # AsyncMock auto-attribute is not JSON-serializable and would make every
    # heal refuse resume with ".mcp.json refresh failed".
    driver._casa_framework_mcp_url = "http://127.0.0.1:8100/mcp/casa-framework"
    return driver


async def _make_registry(records):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path="/tmp/x-nope.json", bus=None)
    for r in records:
        reg._records[r.id] = r
    return reg


def _rec(eid, driver="claude_code", status="active"):
    from engagement_registry import EngagementRecord
    return EngagementRecord(
        id=eid, kind="executor", role_or_type="hello-driver",
        driver=driver, status=status, topic_id=1,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )


def _seed_current_settings(ws_dir, defn):
    """Task 6 (#360): seed .claude/settings.json exactly as boot replay would
    regenerate it from ``defn``, so a test asserting 'ordinary restart,
    nothing changes' isn't tripped by the settings-floor regeneration cycle
    (comparison is by parsed JSON content, so exact formatting doesn't
    matter — only that the two computations agree)."""
    from casa_core import _regenerate_cc_settings

    claude_dir = Path(ws_dir) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps(_regenerate_cc_settings(defn), indent=2, sort_keys=True)
        + "\n", encoding="utf-8",
    )


async def test_replay_sweeps_orphans_and_replants_missing(monkeypatch, tmp_path):
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    # Pre-existing: one legitimate UNDERGOING engagement dir, one orphan.
    (svc_root / "engagement-keep1").mkdir()
    (svc_root / "engagement-keep1" / "type").write_text("longrun\n")
    (svc_root / "engagement-orphan1").mkdir()
    (svc_root / "engagement-orphan1" / "type").write_text("longrun\n")

    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    cau_calls = []
    start_calls = []
    async def fake_cau(): cau_calls.append(1)
    async def fake_start(*, engagement_id): start_calls.append(engagement_id)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    reg = await _make_registry([_rec("keep1"), _rec("done1", status="completed")])

    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    # #314: a resumable record needs its workspace — a missing one is now
    # refused (this test's subject is the orphan sweep, not the M7 guard).
    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root),
    )

    # Orphan gone, keep1 still there
    assert not (svc_root / "engagement-orphan1").exists()
    assert (svc_root / "engagement-keep1").exists()
    # Compile ran exactly once
    assert len(cau_calls) == 1
    # start_service called for keep1 only
    assert start_calls == ["keep1"]


async def test_replay_heals_missing_service_dir_with_known_executor(
    monkeypatch, tmp_path,
):
    """UNDERGOING engagement + missing service dir + known executor → re-plant."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from config import ExecutorDefinition

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    # NO service dir for keep1 — simulates manual rm-rf.
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    # Capture the planted service dir.
    write_calls: list[dict] = []
    def fake_write(**kw):
        write_calls.append(kw)
        (svc_root / f"engagement-{kw['engagement_id']}").mkdir()
        return str(svc_root / f"engagement-{kw['engagement_id']}")
    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)

    async def fake_cau():
        return None
    async def fake_start(*, engagement_id):
        return None
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    class FakeExecReg:
        def __init__(self, defs):
            self._defs = defs
        def get(self, t):
            return self._defs.get(t)
        def definition_any(self, t):
            return self._defs.get(t)

    exec_reg = FakeExecReg({
        "hello-driver": ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
            type="hello-driver", description="test", model="haiku",
            driver="claude_code", enabled=True,
            tools_allowed=[], tools_disallowed=[], permission_mode="bypassPermissions",
            mcp_server_names=[], idle_reminder_days=1,
            prompt_template_path="/nope/prompt.md", hooks_path=None,
            observer_policy_path=None, doctrine_dir="/nope/doctrine",
            extra_dirs=[], mirror_chat_to_topic=False,
            plugins_dir="",
        ),
    })

    reg = await _make_registry([_rec("keep1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    # M7: heal only happens when the workspace dir still exists.
    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=exec_reg,
        engagements_root=str(ws_root),
    )

    # Service dir re-planted via write_service_dir.
    assert len(write_calls) == 1
    assert write_calls[0]["engagement_id"] == "keep1"
    # FIFO created in the workspace alongside the planted service.
    from drivers.workspace import fifo_path
    assert os.path.exists(fifo_path("keep1"))


async def test_replay_rerenders_stale_prev75_run_script(monkeypatch, tmp_path):
    """B1 (Sol r1): a COMPLETE pre-v0.75 service pair (run script emits
    neither ``--output-format stream-json`` nor the ``casa_control`` spawn
    NDJSON frame) must be re-rendered on boot replay — otherwise the new
    _InboundSpool never arms and operator turns queue forever. The stale pair
    is dropped so the heal path re-plants it from the current template."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from config import ExecutorDefinition

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    # Seed a COMPLETE v0.74-style pair (real write_service_dir → main +
    # producer-for + -log sibling), but with a run script that predates the
    # v0.75 streaming frame markers.
    s6_rc.write_service_dir(
        svc_root=str(svc_root), engagement_id="keep1",
        run_script=(
            "#!/command/with-contenv bash\nset -e\n"
            "exec claude --print --permission-mode acceptEdits\n"
        ),
        depends_on=["init-setup-configs"],
        log_run_script="#!/command/with-contenv sh\nexec s6-log n20 s1000000 /x\n",
    )
    assert s6_rc.service_pair_complete(
        svc_root=str(svc_root), engagement_id="keep1",
    )

    start_calls: list[str] = []
    async def fake_cau(): return None
    async def fake_start(*, engagement_id): start_calls.append(engagement_id)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    class FakeExecReg:
        def __init__(self, defs): self._defs = defs
        def get(self, t): return self._defs.get(t)
        def definition_any(self, t): return self._defs.get(t)

    exec_reg = FakeExecReg({
        "hello-driver": ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
            type="hello-driver", description="test", model="haiku",
            driver="claude_code", enabled=True, tools_allowed=[],
            tools_disallowed=[], permission_mode="acceptEdits",
            mcp_server_names=[], idle_reminder_days=1,
            prompt_template_path="/nope/prompt.md", hooks_path=None,
            observer_policy_path=None, doctrine_dir="/nope/doctrine",
            extra_dirs=[], mirror_chat_to_topic=False, plugins_dir="",
        ),
    })

    reg = await _make_registry([_rec("keep1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=exec_reg,
        engagements_root=str(ws_root),
    )

    # Pair re-rendered from the current template → both streaming markers now
    # present in the persisted run script.
    run_text = (svc_root / "engagement-keep1" / "run").read_text()
    assert "--output-format stream-json" in run_text
    assert "casa_control" in run_text
    assert start_calls == ["keep1"]


async def test_replay_leaves_alone_unknown_executor(monkeypatch, tmp_path):
    """Missing service dir + executor DISABLED (get() → None, definition_any()
    still resolves it — Task 6/#360 requires settings regeneration to see
    every claude_code record via definition_any, so a fully-unresolvable type
    now refuses resume; this test isolates the narrower heal-path gate: known
    to definition_any, not registered under get()) → log + move on, no
    re-plant."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    write_calls: list[dict] = []
    def fake_write(**kw):
        write_calls.append(kw)
    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)

    async def noop(**_kw): return None
    async def noop2(): return None
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", noop2)
    monkeypatch.setattr(s6_rc, "start_service", noop)

    class EmptyReg:
        def get(self, _t): return None
        def definition_any(self, _t): return _brief_defn(tmp_path)

    reg = await _make_registry([_rec("keep1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    # Workspace present so we exercise the unknown-executor branch (not the
    # M7 missing-workspace warn-and-skip that precedes it).
    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=EmptyReg(),
        engagements_root=str(ws_root),
    )

    # No re-plant.
    assert write_calls == []


async def test_replay_replants_incomplete_pair(monkeypatch, tmp_path):
    """v0.64.0: 'service dir present' no longer means 'unit present' — the
    heal predicate is pair-completeness (main + producer-for + -log sibling).
    A legacy v0.63.x dir (no producer-for, nested log/) or a torn pair is
    re-planted, which also migrates engagements surviving the upgrade."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from config import ExecutorDefinition

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    # Legacy layout: main dir exists, no producer-for, no -log sibling.
    legacy = svc_root / "engagement-keep1"
    legacy.mkdir()
    (legacy / "type").write_text("longrun\n")
    (legacy / "log").mkdir()  # ignored-by-compile nested dir, pre-v0.64.0
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    write_calls: list[dict] = []

    def fake_write(**kw):
        write_calls.append(kw)
        # Mirrors the real exist_ok=False semantics: collides if the old
        # dir was not removed first.
        (svc_root / f"engagement-{kw['engagement_id']}").mkdir()
        return str(svc_root / f"engagement-{kw['engagement_id']}")

    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)

    async def fake_cau(): return None
    async def fake_start(*, engagement_id): return None
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    class FakeExecReg:
        def get(self, t):
            return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
                type="hello-driver", description="test", model="haiku",
                driver="claude_code", enabled=True,
                tools_allowed=[], tools_disallowed=[],
                permission_mode="bypassPermissions",
                mcp_server_names=[], idle_reminder_days=1,
                prompt_template_path="/nope/prompt.md", hooks_path=None,
                observer_policy_path=None, doctrine_dir="/nope/doctrine",
                extra_dirs=[], mirror_chat_to_topic=False, plugins_dir="",
            )
        def definition_any(self, t):
            return self.get(t)

    reg = await _make_registry([_rec("keep1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=FakeExecReg(),
        engagements_root=str(ws_root),
    )

    assert len(write_calls) == 1, "incomplete pair must be re-planted"
    assert write_calls[0]["engagement_id"] == "keep1"


async def test_replay_heals_when_only_log_sibling_survives(
    monkeypatch, tmp_path,
):
    """Torn state: -log sibling present, main dir gone (crash between the
    two rmtrees). The sweep keeps the sibling (its engagement is live);
    heal must re-plant without tripping over it — pre-fix the REAL
    write_service_dir raised FileExistsError on the sibling mkdir."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from config import ExecutorDefinition

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    (svc_root / "engagement-keep1-log").mkdir()
    (svc_root / "engagement-keep1-log" / "type").write_text("longrun\n")
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    async def fake_cau(): return None
    async def fake_start(*, engagement_id): return None
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    # NOTE: write_service_dir is REAL here — the collision is the point.

    class FakeExecReg:
        def get(self, t):
            return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
                type="hello-driver", description="test", model="haiku",
                driver="claude_code", enabled=True,
                tools_allowed=[], tools_disallowed=[],
                permission_mode="bypassPermissions",
                mcp_server_names=[], idle_reminder_days=1,
                prompt_template_path="/nope/prompt.md", hooks_path=None,
                observer_policy_path=None, doctrine_dir="/nope/doctrine",
                extra_dirs=[], mirror_chat_to_topic=False, plugins_dir="",
            )
        def definition_any(self, t):
            return self.get(t)

    reg = await _make_registry([_rec("keep1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=FakeExecReg(),
        engagements_root=str(ws_root),
    )

    # Healed to a complete pair; no FileExistsError escaped.
    assert (svc_root / "engagement-keep1").is_dir()
    assert (svc_root / "engagement-keep1" / "producer-for").exists()
    assert (svc_root / "engagement-keep1-log" / "consumer-for").exists()


async def test_replay_one_bad_heal_does_not_abort_others(
    monkeypatch, tmp_path,
):
    """A single record's heal failure must not skip the compile/start of
    every other undergoing engagement."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from config import ExecutorDefinition

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    def fake_write(**kw):
        if kw["engagement_id"] == "bad1":
            raise OSError("disk full")
        (svc_root / f"engagement-{kw['engagement_id']}").mkdir()
        return str(svc_root / f"engagement-{kw['engagement_id']}")

    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)

    cau_calls: list[int] = []
    start_calls: list[str] = []
    async def fake_cau(): cau_calls.append(1)
    async def fake_start(*, engagement_id): start_calls.append(engagement_id)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    class FakeExecReg:
        def get(self, t):
            return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
                type="hello-driver", description="test", model="haiku",
                driver="claude_code", enabled=True,
                tools_allowed=[], tools_disallowed=[],
                permission_mode="bypassPermissions",
                mcp_server_names=[], idle_reminder_days=1,
                prompt_template_path="/nope/prompt.md", hooks_path=None,
                observer_policy_path=None, doctrine_dir="/nope/doctrine",
                extra_dirs=[], mirror_chat_to_topic=False, plugins_dir="",
            )
        def definition_any(self, t):
            return self.get(t)

    reg = await _make_registry([_rec("bad1"), _rec("good1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    ws_root = tmp_path / "eng"
    (ws_root / "bad1").mkdir(parents=True)
    (ws_root / "good1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=FakeExecReg(),
        engagements_root=str(ws_root),
    )

    assert len(cau_calls) == 1, "compile must still run"
    assert "good1" in start_calls, "healthy engagement must still start"


async def test_replay_warn_and_skips_heal_when_workspace_missing(
    monkeypatch, tmp_path,
):
    """UNDERGOING + missing svc dir + known executor BUT missing workspace →
    warn-and-skip (M7, 4a.1 §7.3). Planting a service whose run script does
    `cd <workspace>` under set -e would crash-loop s6."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from config import ExecutorDefinition

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    write_calls: list[dict] = []
    monkeypatch.setattr(
        s6_rc, "write_service_dir", lambda **kw: write_calls.append(kw),
    )
    async def fake_cau(): return None
    async def fake_start(*, engagement_id): return None
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    class FakeExecReg:
        def get(self, t):
            return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
                type="hello-driver", description="test", model="haiku",
                driver="claude_code", enabled=True,
                tools_allowed=[], tools_disallowed=[],
                permission_mode="bypassPermissions",
                mcp_server_names=[], idle_reminder_days=1,
                prompt_template_path="/nope/prompt.md", hooks_path=None,
                observer_policy_path=None, doctrine_dir="/nope/doctrine",
                extra_dirs=[], mirror_chat_to_topic=False, plugins_dir="",
            )
        def definition_any(self, t):
            return self.get(t)

    reg = await _make_registry([_rec("keep1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    ws_root = tmp_path / "eng"   # exists, but keep1/ subdir deliberately absent
    ws_root.mkdir()

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=FakeExecReg(),
        engagements_root=str(ws_root),
    )

    # Missing workspace → NO service planted.
    assert write_calls == []


def _rec_pa(eid, artifacts, topic_id=1):
    from engagement_registry import EngagementRecord
    return EngagementRecord(
        id=eid, kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=topic_id,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
        plugin_artifacts=tuple(artifacts),
    )


def _exec_reg():
    from config import ExecutorDefinition
    class FakeExecReg:
        def get(self, t):
            return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
                type="hello-driver", description="test", model="haiku",
                driver="claude_code", enabled=True, tools_allowed=[],
                tools_disallowed=[], permission_mode="bypassPermissions",
                mcp_server_names=[], idle_reminder_days=1,
                prompt_template_path="/nope/prompt.md", hooks_path=None,
                observer_policy_path=None, doctrine_dir="/nope/doctrine",
                extra_dirs=[], mirror_chat_to_topic=False, plugins_dir="",
            )
        def definition_any(self, t):
            return self.get(t)
    return FakeExecReg()


async def test_replay_renders_plugin_dir_flags_from_record(monkeypatch, tmp_path):
    """§3.8: the run script renders --plugin-dir flags from the RECORDED
    artifacts; replay never re-resolves current assignments."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    import plugin_registry

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
    write_calls: list[dict] = []
    def fake_write(**kw):
        write_calls.append(kw)
        (svc_root / f"engagement-{kw['engagement_id']}").mkdir()
    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    monkeypatch.setattr(s6_rc, "start_service", AsyncMock())
    # Replay must NEVER call resolve_for.
    monkeypatch.setattr(plugin_registry, "resolve_for",
                        lambda t: (_ for _ in ()).throw(
                            AssertionError("replay re-resolved!")))

    art_dir = tmp_path / "store" / "sp" / ("a" * 64)
    art_dir.mkdir(parents=True)
    rec = _rec_pa("keep1", [{"name": "superpowers", "artifact_id": "a" * 64,
                             "path": str(art_dir)}])
    reg = await _make_registry([rec])
    driver = _boot_driver(); driver._spawn_background_tasks = lambda r: None
    ws_root = tmp_path / "eng"; (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    assert len(write_calls) == 1
    assert f"--plugin-dir {art_dir}" in write_calls[0]["run_script"]


async def test_replay_refuses_when_recorded_artifact_missing(monkeypatch, tmp_path):
    """Sol F5: a missing recorded artifact refuses resume — no service written,
    no start_service, no background tasks; a topic notice fires; others heal."""
    from unittest.mock import MagicMock
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
    write_ids: list[str] = []
    def fake_write(**kw):
        write_ids.append(kw["engagement_id"])
        (svc_root / f"engagement-{kw['engagement_id']}").mkdir()
    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    start_ids: list[str] = []
    async def fake_start(*, engagement_id): start_ids.append(engagement_id)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    good_dir = tmp_path / "store" / "ok" / ("b" * 64)
    good_dir.mkdir(parents=True)
    refused = _rec_pa("refused1",
                      [{"name": "gone", "artifact_id": "a" * 64,
                        "path": str(tmp_path / "does-not-exist")}], topic_id=5)
    healthy = _rec_pa("healthy1",
                      [{"name": "ok", "artifact_id": "b" * 64,
                        "path": str(good_dir)}], topic_id=6)
    reg = await _make_registry([refused, healthy])

    driver = _boot_driver()
    bg = MagicMock()
    driver._spawn_background_tasks = bg
    ws_root = tmp_path / "eng"
    (ws_root / "refused1").mkdir(parents=True)
    (ws_root / "healthy1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    # Refused: no service, no start, no background tasks; topic notice fired.
    assert "refused1" not in write_ids and "refused1" not in start_ids
    driver._send_to_topic.assert_awaited_once()
    assert driver._send_to_topic.await_args.args[0] == 5    # topic_id
    bg_ids = {c.args[0].id for c in bg.call_args_list}
    assert "refused1" not in bg_ids
    # Healthy engagement still healed + started + background-tasked.
    assert "healthy1" in write_ids and "healthy1" in start_ids
    assert "healthy1" in bg_ids


# ---------------------------------------------------------------------------
# W3 (Task 8): brief boot re-render + fail-closed checked-teardown refusal.
# ---------------------------------------------------------------------------


def _brief_defn(tmp_path, *, type_="hello-driver", enabled=True):
    from config import ExecutorDefinition
    exec_dir = tmp_path / "defs" / type_
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / "prompt.md").write_text(
        "You are {executor_type}.\nTASK:\n{task}\nMEM:{executor_memory}\n"
    )
    return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
        type=type_, description="brief executor twenty chars ok!", model="haiku",
        driver="claude_code", enabled=enabled, tools_allowed=[],
        tools_disallowed=[], permission_mode="bypassPermissions",
        mcp_server_names=[], idle_reminder_days=1,
        prompt_template_path=str(exec_dir / "prompt.md"), hooks_path=None,
        observer_policy_path=None, doctrine_dir="", extra_dirs=[],
        mirror_chat_to_topic=False, plugins_dir="",
    )


def _exec_reg_any(defn, *, enabled=True):
    """Fake registry: get() returns the defn only when enabled (mirrors the
    real registry stripping disabled from _defs); definition_any always does."""
    class Reg:
        def get(self, t):
            return defn if enabled else None
        def definition_any(self, t):
            return defn
    return Reg()


def _seed_current_credential(ws_dir, rec):
    """#335: make a test workspace look like an ORDINARY restart — its
    ``.mcp.json`` already carries the record's credential — so replay's
    credential-migration cycle does not fire."""
    from drivers.workspace import write_workspace_mcp_json
    rec.auth_token = rec.auth_token or "tok-seeded"
    write_workspace_mcp_json(
        str(ws_dir), engagement_id=rec.id,
        engagement_auth_token=rec.auth_token,
        casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework")


def _brief_rec(eid, brief, *, role="hello-driver", topic_id=1):
    from engagement_registry import EngagementRecord
    return EngagementRecord(
        id=eid, kind="executor", role_or_type=role, driver="claude_code",
        status="active", topic_id=topic_id, started_at=0.0,
        last_user_turn_ts=0.0, last_idle_reminder_ts=0.0, completed_at=None,
        sdk_session_id=None, origin={"brief": brief}, task=brief["objective"],
    )


_BRIEF = {
    "objective": "Reconcile the ledger",
    "acceptance_criteria": ["balances match"],
    "process_requirements": ["Freeze writes during reconciliation"],
}


async def test_replay_a_re_renders_claude_md_on_complete_pair(monkeypatch, tmp_path):
    """(a) COMPLETE pair + blanked CLAUDE.md → re-rendered from origin["brief"]
    (exact criteria + verbatim process strings) DESPITE the fast-path continue."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from drivers.brief import COMPLETION_ACCOUNTING_LINE

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    main = svc_root / "engagement-keep1"; main.mkdir()
    (main / "type").write_text("longrun\n")
    (main / "producer-for").write_text("engagement-keep1-log\n")
    (svc_root / "engagement-keep1-log").mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    async def fake_cau(): return None
    start_ids: list[str] = []
    async def fake_start(*, engagement_id): start_ids.append(engagement_id)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    defn = _brief_defn(tmp_path)
    ws_root = tmp_path / "eng"; (ws_root / "keep1").mkdir(parents=True)
    (ws_root / "keep1" / "CLAUDE.md").write_text("")   # blanked

    reg = await _make_registry([_brief_rec("keep1", _BRIEF)])
    driver = _boot_driver(); driver._spawn_background_tasks = lambda r: None

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg_any(defn),
        engagements_root=str(ws_root))

    claude_md = (ws_root / "keep1" / "CLAUDE.md").read_text()
    assert "Reconcile the ledger" in claude_md
    assert "balances match" in claude_md
    assert "Freeze writes during reconciliation" in claude_md
    assert COMPLETION_ACCOUNTING_LINE in claude_md
    assert start_ids == ["keep1"]   # still resumed


async def test_replay_b_refresh_failure_refuses_with_checked_teardown(
    monkeypatch, tmp_path,
):
    """(b) refresh raising → refused_ids has the id, ensure_service_down
    CONFIRMED down (True), remove_service_dir called, start_service +
    _spawn_background_tasks BOTH uncalled."""
    from unittest.mock import MagicMock
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc, workspace as ws_mod

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    # Seed an already-up complete pair (would resume without the refusal).
    main = svc_root / "engagement-keep1"; main.mkdir()
    (main / "type").write_text("longrun\n")
    (main / "producer-for").write_text("engagement-keep1-log\n")
    (svc_root / "engagement-keep1-log").mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    async def fake_cau(): return None
    start_ids: list[str] = []
    async def fake_start(*, engagement_id): start_ids.append(engagement_id)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    ensure_down = AsyncMock(return_value=True)
    monkeypatch.setattr(s6_rc, "ensure_service_down", ensure_down)
    removed: list[str] = []
    monkeypatch.setattr(
        s6_rc, "remove_service_dir",
        lambda *, svc_root, engagement_id: removed.append(engagement_id))

    def boom(*a, **k): raise RuntimeError("refresh exploded")
    monkeypatch.setattr(ws_mod, "refresh_claude_md", boom)

    defn = _brief_defn(tmp_path)
    ws_root = tmp_path / "eng"; (ws_root / "keep1").mkdir(parents=True)
    reg = await _make_registry([_brief_rec("keep1", _BRIEF)])
    driver = _boot_driver(); bg = MagicMock(); driver._spawn_background_tasks = bg

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg_any(defn),
        engagements_root=str(ws_root))

    ensure_down.assert_awaited_once()
    assert ensure_down.await_args.kwargs["engagement_id"] == "keep1"
    assert removed == ["keep1"]
    assert start_ids == []
    assert bg.call_count == 0


async def test_replay_b2_removal_and_compile_failures_after_confirmed_stop(
    monkeypatch, tmp_path,
):
    """(b2) remove_service_dir swallowing OSError AND _compile_and_update
    raising — ensure_service_down had CONFIRMED the stop BEFORE either failure."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc, workspace as ws_mod

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    order: list[str] = []
    async def _ensure(**k):
        order.append("ensure_down"); return True
    monkeypatch.setattr(s6_rc, "ensure_service_down", _ensure)

    # remove_service_dir swallows OSError itself in prod; here assert the CALL
    # happened after the confirmed stop.
    def _remove_safe(*, svc_root, engagement_id):
        order.append("remove")
    monkeypatch.setattr(s6_rc, "remove_service_dir", _remove_safe)

    async def fake_cau_raise():
        order.append("compile"); raise RuntimeError("compile failed")
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau_raise)
    monkeypatch.setattr(s6_rc, "start_service", AsyncMock())

    def boom(*a, **k): raise RuntimeError("refresh exploded")
    monkeypatch.setattr(ws_mod, "refresh_claude_md", boom)

    defn = _brief_defn(tmp_path)
    ws_root = tmp_path / "eng"; (ws_root / "keep1").mkdir(parents=True)
    reg = await _make_registry([_brief_rec("keep1", _BRIEF)])
    driver = _boot_driver(); driver._spawn_background_tasks = lambda r: None

    with pytest.raises(RuntimeError, match="compile failed"):
        await replay_undergoing_engagements(
            registry=reg, driver=driver, executor_registry=_exec_reg_any(defn),
            engagements_root=str(ws_root))

    # ensure_down (confirmed stop) ran BEFORE the removal AND before compile.
    assert order.index("ensure_down") < order.index("remove")
    assert order.index("ensure_down") < order.index("compile")


async def test_replay_c_missing_registry_refuses_brief(monkeypatch, tmp_path):
    """(c) brief-bearing record + no executor_registry → refused + removed +
    neither started."""
    from unittest.mock import MagicMock
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    start_ids: list[str] = []
    async def fake_start(*, engagement_id): start_ids.append(engagement_id)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    ensure_down = AsyncMock(return_value=True)
    monkeypatch.setattr(s6_rc, "ensure_service_down", ensure_down)
    removed: list[str] = []
    monkeypatch.setattr(
        s6_rc, "remove_service_dir",
        lambda *, svc_root, engagement_id: removed.append(engagement_id))

    ws_root = tmp_path / "eng"; (ws_root / "keep1").mkdir(parents=True)
    reg = await _make_registry([_brief_rec("keep1", _BRIEF)])
    driver = _boot_driver(); bg = MagicMock(); driver._spawn_background_tasks = bg

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=None,
        engagements_root=str(ws_root))

    ensure_down.assert_awaited_once()
    assert removed == ["keep1"]
    assert start_ids == [] and bg.call_count == 0


async def test_replay_d_definition_any_none_refuses_brief(monkeypatch, tmp_path):
    """(d) definition_any → None (brief record) → refused + removed + neither
    started."""
    from unittest.mock import MagicMock
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    start_ids: list[str] = []
    async def fake_start(*, engagement_id): start_ids.append(engagement_id)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    ensure_down = AsyncMock(return_value=True)
    monkeypatch.setattr(s6_rc, "ensure_service_down", ensure_down)
    removed: list[str] = []
    monkeypatch.setattr(
        s6_rc, "remove_service_dir",
        lambda *, svc_root, engagement_id: removed.append(engagement_id))

    class NoneReg:
        def get(self, t): return None
        def definition_any(self, t): return None

    ws_root = tmp_path / "eng"; (ws_root / "keep1").mkdir(parents=True)
    reg = await _make_registry([_brief_rec("keep1", _BRIEF)])
    driver = _boot_driver(); bg = MagicMock(); driver._spawn_background_tasks = bg

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=NoneReg(),
        engagements_root=str(ws_root))

    ensure_down.assert_awaited_once()
    assert removed == ["keep1"]
    assert start_ids == [] and bg.call_count == 0


async def test_replay_e_disabled_defn_incomplete_pair_heals(monkeypatch, tmp_path):
    """(e) DISABLED definition + INCOMPLETE pair + brief → pair reconstructed
    from the definition_any result, CLAUDE.md refreshed, service started,
    background tasks restored (the false-green get()-re-resolution would hide)."""
    from unittest.mock import MagicMock
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"; svc_root.mkdir()   # NO pair — incomplete
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    write_ids: list[str] = []
    def fake_write(**kw):
        write_ids.append(kw["engagement_id"])
        (svc_root / f"engagement-{kw['engagement_id']}").mkdir()
    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    start_ids: list[str] = []
    async def fake_start(*, engagement_id): start_ids.append(engagement_id)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    defn = _brief_defn(tmp_path, enabled=False)      # DISABLED
    ws_root = tmp_path / "eng"; (ws_root / "keep1").mkdir(parents=True)
    (ws_root / "keep1" / "CLAUDE.md").write_text("")

    reg = await _make_registry([_brief_rec("keep1", _BRIEF)])
    driver = _boot_driver(); bg = MagicMock(); driver._spawn_background_tasks = bg

    # get() returns None (disabled), definition_any returns the defn.
    await replay_undergoing_engagements(
        registry=reg, driver=driver,
        executor_registry=_exec_reg_any(defn, enabled=False),
        engagements_root=str(ws_root))

    assert write_ids == ["keep1"], "disabled defn must still heal via definition_any"
    assert start_ids == ["keep1"]
    bg_ids = {c.args[0].id for c in bg.call_args_list}
    assert "keep1" in bg_ids
    claude_md = (ws_root / "keep1" / "CLAUDE.md").read_text()
    assert "Freeze writes during reconciliation" in claude_md


async def test_replay_true_exhaustion_marks_error_via_real_registry(
    monkeypatch, tmp_path,
):
    """(r14-B1) teardown unconfirmable (ensure_service_down → False) → the REAL
    replay path lands registry.mark_error(kind="refuse_teardown_failed"). Guards
    against the _engagement_registry NameError the per-record catch would hide."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc, workspace as ws_mod
    from engagement_registry import EngagementRegistry

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    monkeypatch.setattr(s6_rc, "start_service", AsyncMock())
    monkeypatch.setattr(s6_rc, "ensure_service_down", AsyncMock(return_value=False))
    monkeypatch.setattr(
        s6_rc, "remove_service_dir",
        lambda *, svc_root, engagement_id: None)

    def boom(*a, **k): raise RuntimeError("refresh exploded")
    monkeypatch.setattr(ws_mod, "refresh_claude_md", boom)

    defn = _brief_defn(tmp_path)
    ws_root = tmp_path / "eng"; (ws_root / "keep1").mkdir(parents=True)

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "tomb.json"), bus=None)
    reg._records["keep1"] = _brief_rec("keep1", _BRIEF)
    driver = _boot_driver(); driver._spawn_background_tasks = lambda r: None

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg_any(defn),
        engagements_root=str(ws_root))

    rec = reg._records["keep1"]
    assert rec.status == "error"
    assert rec.origin["error_kind"] == "refuse_teardown_failed"


# ---------------------------------------------------------------------------
# B2 (Sol diff r2): the stale-run-script migration path must VERIFY the
# remove_service_dir actually removed the pair before re-planting — the helper
# swallows rmtree failures, so a survivor (full or partial) fails CLOSED like
# the brief-refusal path (no compile / start / background spawn for the stale
# engagement).
# ---------------------------------------------------------------------------


def _seed_stale_pair(svc_root, eid):
    """Plant a COMPLETE pair whose run script predates the v0.75 markers."""
    from drivers import s6_rc
    s6_rc.write_service_dir(
        svc_root=str(svc_root), engagement_id=eid,
        run_script=(
            "#!/command/with-contenv bash\nset -e\n"
            "exec claude --print --permission-mode acceptEdits\n"
        ),
        depends_on=["init-setup-configs"],
        log_run_script="#!/command/with-contenv sh\nexec s6-log n20 s1000000 /x\n",
    )


async def test_replay_b2_stale_migration_removal_fails_refuses_closed(
    monkeypatch, tmp_path,
):
    """remove_service_dir fully no-ops (rmtree swallowed) → the surviving old
    pair is detected via service_dirs_absent → refuse CLOSED: ensure_service_down
    ran, mark_error(refuse_migration_failed) landed (down unconfirmable), and
    start_service AND _spawn_background_tasks are BOTH uncalled."""
    from unittest.mock import MagicMock
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from engagement_registry import EngagementRegistry

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
    _seed_stale_pair(svc_root, "keep1")
    assert s6_rc.service_pair_complete(svc_root=str(svc_root), engagement_id="keep1")

    # remove_service_dir SWALLOWS the failure (survivor stays) — simulate by
    # making it a no-op, so the pair remains present after the "removal".
    monkeypatch.setattr(
        s6_rc, "remove_service_dir",
        lambda *, svc_root, engagement_id: None)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    start_ids: list[str] = []
    async def fake_start(*, engagement_id): start_ids.append(engagement_id)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    # Down unconfirmable → mark_error path exercised on the real registry.
    monkeypatch.setattr(s6_rc, "ensure_service_down", AsyncMock(return_value=False))
    write_ids: list[str] = []
    monkeypatch.setattr(
        s6_rc, "write_service_dir",
        lambda **kw: write_ids.append(kw["engagement_id"]))

    defn = _brief_defn(tmp_path)
    exec_reg = _exec_reg_any(defn)
    ws_root = tmp_path / "eng"; (ws_root / "keep1").mkdir(parents=True)

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "tomb.json"), bus=None)
    reg._records["keep1"] = _brief_rec("keep1", _BRIEF)
    # #335 + Task 6: this test is about the STALE-PAIR migration refusal, so
    # give the workspace an already-current credential AND settings.json —
    # otherwise the (also unconfirmable) merged workspace-state cycle
    # refuses first and the assertion below would be testing that refusal
    # instead of this one.
    _seed_current_credential(ws_root / "keep1", reg._records["keep1"])
    _seed_current_settings(ws_root / "keep1", defn)
    driver = _boot_driver(); bg = MagicMock(); driver._spawn_background_tasks = bg

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=exec_reg,
        engagements_root=str(ws_root))

    rec = reg._records["keep1"]
    assert rec.status == "error"
    assert rec.origin["error_kind"] == "refuse_migration_failed"
    assert start_ids == []
    assert bg.call_count == 0
    assert write_ids == []  # never re-planted a stale pair


async def test_replay_b2_partial_removal_main_survives_refuses_closed(
    monkeypatch, tmp_path,
):
    """PARTIAL removal — the -log half is removed but the main dir survives —
    also refuses CLOSED (service_dirs_absent is False), never re-planting."""
    from unittest.mock import MagicMock
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
    _seed_stale_pair(svc_root, "keep1")

    # Partial removal: drop only the -log sibling, leave the main behind.
    def _partial_remove(*, svc_root, engagement_id):
        import shutil
        shutil.rmtree(
            Path(svc_root) / s6_rc._log_service_name(engagement_id),
            ignore_errors=True)
    monkeypatch.setattr(s6_rc, "remove_service_dir", _partial_remove)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    start_ids: list[str] = []
    async def fake_start(*, engagement_id): start_ids.append(engagement_id)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    ensure_down = AsyncMock(return_value=True)
    monkeypatch.setattr(s6_rc, "ensure_service_down", ensure_down)
    write_ids: list[str] = []
    monkeypatch.setattr(
        s6_rc, "write_service_dir",
        lambda **kw: write_ids.append(kw["engagement_id"]))

    exec_reg = _exec_reg_any(_brief_defn(tmp_path))
    ws_root = tmp_path / "eng"; (ws_root / "keep1").mkdir(parents=True)

    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "tomb.json"), bus=None)
    reg._records["keep1"] = _brief_rec("keep1", _BRIEF)
    driver = _boot_driver(); bg = MagicMock(); driver._spawn_background_tasks = bg

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=exec_reg,
        engagements_root=str(ws_root))

    ensure_down.assert_awaited()
    assert start_ids == []
    assert bg.call_count == 0
    assert write_ids == []


async def test_replay_b2_migration_succeeds_when_removal_confirmed(
    monkeypatch, tmp_path,
):
    """Control: when remove_service_dir genuinely removes the pair, migration
    proceeds — the pair is re-planted from the current template and started."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"; svc_root.mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
    _seed_stale_pair(svc_root, "keep1")

    async def fake_cau(): return None
    start_ids: list[str] = []
    async def fake_start(*, engagement_id): start_ids.append(engagement_id)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    exec_reg = _exec_reg_any(_brief_defn(tmp_path))
    ws_root = tmp_path / "eng"; (ws_root / "keep1").mkdir(parents=True)

    reg = await _make_registry([_brief_rec("keep1", _BRIEF)])
    driver = _boot_driver(); driver._spawn_background_tasks = lambda r: None

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=exec_reg,
        engagements_root=str(ws_root))

    run_text = (svc_root / "engagement-keep1" / "run").read_text()
    assert "--output-format stream-json" in run_text
    assert "casa_control" in run_text
    assert start_ids == ["keep1"]
    assert reg._records["keep1"].status in ("active", "idle")


# ---------------------------------------------------------------------------
# Task 6 (#360) — boot replay regenerates settings.json for EVERY resumed
# claude_code record from the load-time snapshot, and cycles the service
# when the regenerated document differs from what's on disk.
# ---------------------------------------------------------------------------


async def test_task_only_record_settings_floor_regenerated_on_heal(
    monkeypatch, tmp_path,
):
    """RED CASE 1: a TASK-ONLY (no brief) record whose on-disk settings.json
    was hollowed (``{}``, no PreToolUse block at all) carries the canonical
    containment floor after boot replay — proving the old ``has_brief`` gate
    no longer excludes task-only records from settings regeneration. Exercises
    the HEAL path (incomplete pair) so the write is directly observable."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"; svc_root.mkdir()   # no pair — heal path
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
    monkeypatch.setattr(s6_rc, "write_service_dir", lambda **kw: None)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    start_ids: list[str] = []
    async def fake_start(*, engagement_id): start_ids.append(engagement_id)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    rec = _rec("keep1")               # origin={} — task-only, no brief
    reg = await _make_registry([rec])
    driver = _boot_driver(); driver._spawn_background_tasks = lambda r: None

    ws_root = tmp_path / "eng"; ws_dir = ws_root / "keep1"
    ws_dir.mkdir(parents=True)
    _seed_current_credential(ws_dir, rec)   # isolate the settings trigger
    claude_dir = ws_dir / ".claude"; claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{}")   # hollowed floor

    exec_reg = _exec_reg()
    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=exec_reg,
        engagements_root=str(ws_root))

    settings = json.loads((claude_dir / "settings.json").read_text())
    pre_commands = {
        h["command"].rsplit(" ", 1)[-1]
        for e in settings["hooks"]["PreToolUse"]
        for h in e["hooks"]
    }
    assert {"block_dangerous_bash", "path_scope"} <= pre_commands
    assert start_ids == ["keep1"]


async def test_fast_path_settings_diff_drives_confirmed_cycle(
    monkeypatch, tmp_path,
):
    """RED CASE 2: a COMPLETE, non-stale, current-credential service pair
    (the ordinary-restart fast path, ~casa_core.py:649) whose settings.json
    differs from the regenerated snapshot must NOT just get a silent file
    rewrite — boot replay drives a CONFIRMED service down/up cycle before
    ever reaching the fast-path continue. Asserts the checked-teardown
    ladder is entered (ensure_service_down awaited) when that cycle can't be
    confirmed — proving this is a real cycle, not an inert rewrite."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    start_calls = _fast_path_env(monkeypatch, tmp_path)
    down_calls: list[str] = []
    async def fake_down(*, engagement_id):
        down_calls.append(engagement_id)
        return False   # unconfirmable — must refuse, not silently rewrite
    monkeypatch.setattr(s6_rc, "ensure_service_down", fake_down)
    monkeypatch.setattr(
        s6_rc, "remove_service_dir",
        lambda *, svc_root, engagement_id: None)

    rec = _rec("keep1")
    reg = await _make_registry([rec])
    driver = _boot_driver(); driver._spawn_background_tasks = lambda r: None

    ws_root = tmp_path / "eng"; ws_dir = ws_root / "keep1"
    ws_dir.mkdir(parents=True)
    _seed_current_credential(ws_dir, rec)   # isolate the settings trigger
    # NO settings.json seeded at all — a fresh/never-provisioned .claude/.

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    # The cycle was actually attempted (not an inert rewrite-only path)...
    assert "keep1" in down_calls
    # ...and since it couldn't be confirmed, the resume was refused CLOSED —
    # never started into a CLI still running with the stale cached hooks.
    assert start_calls == []
    assert reg.get("keep1").status == "error"
    assert reg.get("keep1").origin["error_kind"] == (
        "refuse_workspace_cycle_failed")


async def test_settings_snapshot_missing_floor_refuses_resume(
    monkeypatch, tmp_path,
):
    """RED CASE 3: a regenerated settings snapshot that does NOT carry the
    containment floor (defense-in-depth belt-and-suspenders — Task 4's
    ``translate_hooks_to_settings`` always appends it, so this simulates a
    bypass of that emitter) refuses the resume fail-closed rather than
    shipping a hollow floor."""
    import casa_core
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    start_calls = _fast_path_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        casa_core, "_regenerate_cc_settings",
        lambda defn: {"hooks": {}, "permissions": {}})
    ensure_down = AsyncMock(return_value=True)
    monkeypatch.setattr(s6_rc, "ensure_service_down", ensure_down)
    monkeypatch.setattr(
        s6_rc, "remove_service_dir",
        lambda *, svc_root, engagement_id: None)

    rec = _rec("keep1")
    reg = await _make_registry([rec])
    driver = _boot_driver(); driver._spawn_background_tasks = lambda r: None

    ws_root = tmp_path / "eng"; ws_dir = ws_root / "keep1"
    ws_dir.mkdir(parents=True)
    _seed_current_credential(ws_dir, rec)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    assert start_calls == []
    assert not (ws_dir / ".claude" / "settings.json").exists(), (
        "a floor-less snapshot must never reach disk")


class TestReconcileTerminalSpools:
    async def test_drains_only_terminal_claude_code_records(self):
        from casa_core import reconcile_terminal_spools

        live = _rec("a" * 16, driver="claude_code", status="active")
        gone = _rec("b" * 16, driver="claude_code", status="completed")
        in_casa_gone = _rec("c" * 16, driver="in_casa", status="cancelled")
        reg = await _make_registry([live, gone, in_casa_gone])

        drained: list[str] = []

        class _Driver:
            async def reconcile_terminal_spool(self, rec):
                drained.append(rec.id)

        await reconcile_terminal_spools(registry=reg, driver=_Driver())
        # Only the TERMINAL claude_code record is drained.
        assert drained == [gone.id]

    async def test_one_failure_does_not_abort_others(self):
        from casa_core import reconcile_terminal_spools

        r1 = _rec("d" * 16, driver="claude_code", status="completed")
        r2 = _rec("e" * 16, driver="claude_code", status="error")
        reg = await _make_registry([r1, r2])

        drained: list[str] = []

        class _Driver:
            async def reconcile_terminal_spool(self, rec):
                if rec.id == r1.id:
                    raise RuntimeError("boom")
                drained.append(rec.id)

        await reconcile_terminal_spools(registry=reg, driver=_Driver())
        assert drained == [r2.id]        # r2 still drained despite r1 failing


async def test_replay_aborts_resume_when_summary_adopt_fails(monkeypatch, tmp_path):
    """F7 (Sol r2): pinned-summary adoption failure ABORTS the resume — the
    service is NOT started and the record is marked error (§5: never run a
    summary-less engagement). Adoption runs BEFORE start."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    (svc_root / "engagement-keep1").mkdir()
    (svc_root / "engagement-keep1" / "type").write_text("longrun\n")
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    start_calls: list = []
    down_calls: list = []

    async def fake_cau():
        pass

    async def fake_start(*, engagement_id):
        start_calls.append(engagement_id)

    async def fake_down(*, engagement_id):
        down_calls.append(engagement_id)
        return True

    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    monkeypatch.setattr(s6_rc, "ensure_service_down", fake_down)

    reg = await _make_registry([_rec("keep1")])

    adopt_calls: list = []

    async def _adopt_boom(rec):
        adopt_calls.append(rec.id)
        raise RuntimeError("telegram down")

    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None
    driver.adopt_summary_if_missing = _adopt_boom

    # #314: provision the workspace — a missing one is now refused BEFORE
    # adoption, and this test's subject is the adoption-failure abort.
    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    # Adoption was ATTEMPTED, and its failure aborted the resume:
    assert adopt_calls == ["keep1"]
    assert start_calls == []                       # NOT started summary-less
    # Two downs are expected: the #335 credential-migration cycle (the fresh
    # workspace has no .mcp.json) plus the adoption-failure abort itself.
    assert down_calls and set(down_calls) == {"keep1"}   # confirmed down
    assert reg.get("keep1").status == "error"      # marked error


async def test_replay_start_failure_refuses_background_attach(
    monkeypatch, tmp_path,
):
    """#342: a start_service failure must refuse the record (mark error,
    no background spool/relay machinery) — not attach operator-facing
    state to an engagement whose service never started."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    (svc_root / "engagement-keep1").mkdir()
    (svc_root / "engagement-keep1" / "type").write_text("longrun\n")
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    async def fake_cau():
        pass

    async def fake_start(*, engagement_id):
        raise RuntimeError("s6-rc change failed")

    async def fake_down(*, engagement_id):
        return True

    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    monkeypatch.setattr(s6_rc, "ensure_service_down", fake_down)

    reg = await _make_registry([_rec("keep1")])

    attached: list = []
    driver = _boot_driver()
    driver._spawn_background_tasks = (
        lambda rec, **kw: attached.append(rec.id))

    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    assert attached == []                          # no background machinery
    assert reg.get("keep1").status == "error"      # marked error


async def test_replay_fifo_recreate_failure_refuses_resume(
    monkeypatch, tmp_path,
):
    """#342: a failed stdin.fifo recreation in the heal path must refuse
    the resume — the run template (`set -e`, stdin from the FIFO) makes
    every spawn exit immediately, so starting it crash-loops forever.
    Sol r2-1/r3-2: the refusal runs the checked-teardown ladder — downs
    any live remnant and REMOVES the just-replanted service pair — and
    with containment confirmed the record stays non-terminal so the next
    boot retries the heal (and its mkfifo)."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from config import ExecutorDefinition

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    # NO service dir for keep1 — the heal path re-plants it.
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    def fake_write(**kw):
        (svc_root / f"engagement-{kw['engagement_id']}").mkdir()
        return str(svc_root / f"engagement-{kw['engagement_id']}")
    monkeypatch.setattr(s6_rc, "write_service_dir", fake_write)

    start_calls: list = []
    down_calls: list = []

    async def fake_cau():
        pass

    async def fake_start(*, engagement_id):
        start_calls.append(engagement_id)

    async def fake_down(*, engagement_id):
        down_calls.append(engagement_id)
        return True

    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    monkeypatch.setattr(s6_rc, "ensure_service_down", fake_down)

    def _mkfifo_boom(path, mode=0o600):
        raise OSError("no space")
    monkeypatch.setattr(os, "mkfifo", _mkfifo_boom)

    class FakeExecReg:
        def __init__(self, defs):
            self._defs = defs
        def get(self, t):
            return self._defs.get(t)
        def definition_any(self, t):
            return self._defs.get(t)

    exec_reg = FakeExecReg({
        "hello-driver": ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
            type="hello-driver", description="test", model="haiku",
            driver="claude_code", enabled=True,
            tools_allowed=[], tools_disallowed=[], permission_mode="bypassPermissions",
            mcp_server_names=[], idle_reminder_days=1,
            prompt_template_path="/nope/prompt.md", hooks_path=None,
            observer_policy_path=None, doctrine_dir="/nope/doctrine",
            extra_dirs=[], mirror_chat_to_topic=False,
            plugins_dir="",
        ),
    })

    reg = await _make_registry([_rec("keep1")])

    attached: list = []
    driver = _boot_driver()
    driver._spawn_background_tasks = (
        lambda rec, **kw: attached.append(rec.id))

    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=exec_reg,
        engagements_root=str(ws_root),
    )

    assert start_calls == []                       # never started
    assert attached == []                          # no background machinery
    # Sol r2-1: remnant downed + the replanted pair removed (not compiled).
    assert "keep1" in down_calls
    assert not (svc_root / "engagement-keep1").exists()
    # Containment confirmed → no terminal mark; the next boot retries.
    assert reg.get("keep1").status != "error"


async def test_fast_path_recreates_missing_fifo(monkeypatch, tmp_path):
    """Sol r3-2: an intact COMPLETE pair does not imply an intact FIFO —
    the fast path must recreate a missing stdin.fifo before the start
    loop runs the record (a `set -e` read of a missing FIFO crash-loops
    under s6 forever)."""
    from casa_core import replay_undergoing_engagements
    from drivers.workspace import fifo_path

    start_calls = _fast_path_env(monkeypatch, tmp_path)
    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    reg = await _make_registry([_rec("keep1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec, **kw: None

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    # Task 4 (containment stage 2): the FIFO lives in the control dir.
    assert os.path.exists(fifo_path("keep1"))
    assert start_calls == ["keep1"]


async def test_fast_path_replaces_non_fifo_stdin_path(monkeypatch, tmp_path):
    """Terra r4-1: a REGULAR FILE (or dir) at the stdin.fifo path reads as
    instant EOF / fails outright — the same crash-loop as a missing FIFO.
    The guard must verify the type and repair, not just existence."""
    import stat as stat_mod

    from casa_core import replay_undergoing_engagements
    from drivers.workspace import fifo_path, provision_control_dir

    start_calls = _fast_path_env(monkeypatch, tmp_path)
    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)
    # Task 4: the (corrupted) non-fifo entry lives in the control dir now.
    provision_control_dir("keep1")
    fifo = fifo_path("keep1")
    with open(fifo, "w", encoding="utf-8") as f:
        f.write("not a fifo")

    reg = await _make_registry([_rec("keep1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec, **kw: None

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    st = os.stat(fifo)
    assert stat_mod.S_ISFIFO(st.st_mode), "regular file must be replaced"
    assert start_calls == ["keep1"]

    # Directory variant: also repaired.
    import os as os_mod
    os_mod.remove(fifo)
    os_mod.mkdir(fifo)
    reg2 = await _make_registry([_rec("keep1")])
    await replay_undergoing_engagements(
        registry=reg2, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))
    st = os.stat(fifo)
    assert stat_mod.S_ISFIFO(st.st_mode), "directory must be replaced"


async def test_fast_path_symlinked_workspace_refuses_without_repair(
    monkeypatch, tmp_path,
):
    """Sol r5-1: the FIFO verify/repair must never operate THROUGH a
    symlinked workspace dir — a corrupted symlink targeting a foreign
    tree would let the repair rmtree entries outside the workspace.
    Refused instead; the foreign target is untouched."""
    import os as os_mod

    from casa_core import replay_undergoing_engagements

    start_calls = _fast_path_env(monkeypatch, tmp_path)
    ws_root = tmp_path / "eng"
    ws_root.mkdir()
    outside = tmp_path / "outside"
    (outside / "stdin.fifo").mkdir(parents=True)  # a DIRECTORY at the name
    (outside / "stdin.fifo" / "precious.txt").write_text("keep me")
    os_mod.symlink(outside, ws_root / "keep1")    # workspace IS a symlink

    reg = await _make_registry([_rec("keep1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec, **kw: None

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    assert start_calls == []
    assert (outside / "stdin.fifo" / "precious.txt").exists(), (
        "repair must not reach through the symlink")


async def test_fast_path_fifo_failure_refuses_start(monkeypatch, tmp_path):
    """Sol r3-2: when the fast-path FIFO recreation fails, the record is
    refused (no start, pair removed) instead of started into a
    crash-loop — closing the intact-pair-after-failed-refusal residue."""
    import os as os_mod

    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    start_calls = _fast_path_env(monkeypatch, tmp_path)
    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    def _mkfifo_boom(path, mode=0o600):
        raise OSError("no space")
    monkeypatch.setattr(os_mod, "mkfifo", _mkfifo_boom)

    reg = await _make_registry([_rec("keep1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec, **kw: None

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    assert start_calls == []
    assert not (
        Path(s6_rc.ENGAGEMENT_SOURCES_ROOT) / "engagement-keep1").exists()


async def test_replay_adopts_summary_before_start(monkeypatch, tmp_path):
    """F7: on the happy path, adoption runs BEFORE start_service (ordering)."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    (svc_root / "engagement-keep1").mkdir()
    (svc_root / "engagement-keep1" / "type").write_text("longrun\n")
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

    order: list = []

    async def fake_cau():
        pass

    async def fake_start(*, engagement_id):
        order.append(("start", engagement_id))

    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)

    reg = await _make_registry([_rec("keep1")])

    async def _adopt(rec):
        order.append(("adopt", rec.id))

    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None
    driver.adopt_summary_if_missing = _adopt

    # #314: provision the workspace — a missing one is now refused.
    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    assert order == [("adopt", "keep1"), ("start", "keep1")]


# ---------------------------------------------------------------------------
# #335 — credential migration cycle (Sol, review r2)
# ---------------------------------------------------------------------------


async def test_credential_migration_rewrites_and_cycles_the_service(
    monkeypatch, tmp_path,
):
    """A pre-#335 workspace (no token in .mcp.json) is refreshed from the
    record AND its service cycled, because a running CLI caches .mcp.json at
    spawn and would otherwise authenticate with a credential it does not
    hold."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from drivers.workspace import workspace_mcp_token
    from engagement_registry import EngagementRegistry

    monkeypatch.setattr(s6_rc, "sweep_orphan_service_dirs", lambda **kw: [])
    monkeypatch.setattr(s6_rc, "sweep_orphan_compiled_dbs", lambda: None)
    monkeypatch.setattr(s6_rc, "service_pair_complete", lambda **kw: True)
    monkeypatch.setattr(s6_rc, "run_script_is_stale", lambda **kw: False)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    started: list[str] = []
    async def fake_start(*, engagement_id): started.append(engagement_id)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    down = AsyncMock(return_value=True)
    monkeypatch.setattr(s6_rc, "ensure_service_down", down)

    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "tomb.json"), bus=None)
    rec = _brief_rec("keep1", _BRIEF)
    rec.origin = {}                      # brief-less: pure credential path
    rec.auth_token = "tok-fresh"
    reg._records["keep1"] = rec
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    # Task 6: seed a MATCHING settings.json so this test's single ``down``
    # call is attributable to the credential migration alone, not a second
    # concurrent settings-regeneration cycle (they're merged into one).
    exec_reg = _exec_reg()
    _seed_current_settings(ws_root / "keep1", exec_reg.get("hello-driver"))

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=exec_reg,
        engagements_root=str(ws_root))

    assert workspace_mcp_token(str(ws_root / "keep1")) == "tok-fresh"
    down.assert_awaited_once()           # cycled so the CLI reloads it
    assert started == ["keep1"]          # and brought back up
    assert reg._records["keep1"].status != "error"


async def test_stale_url_with_matching_token_rewrites_and_cycles(
    monkeypatch, tmp_path,
):
    """v0.164.0: a workspace whose .mcp.json carries the record's CURRENT
    token but a stale server URL (e.g. the retired public-8099 endpoint) is
    rewritten to the served URL and its service cycled — the token check
    alone would skip it, leaving an engagement whose every tool call hits a
    route that no longer exists. Red case demonstrated: dropping the
    workspace_mcp_url comparison from replay's rewrite predicate fails this
    test."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from drivers.workspace import (
        workspace_mcp_token, workspace_mcp_url, write_workspace_mcp_json,
    )
    from engagement_registry import EngagementRegistry

    monkeypatch.setattr(s6_rc, "sweep_orphan_service_dirs", lambda **kw: [])
    monkeypatch.setattr(s6_rc, "sweep_orphan_compiled_dbs", lambda: None)
    monkeypatch.setattr(s6_rc, "service_pair_complete", lambda **kw: True)
    monkeypatch.setattr(s6_rc, "run_script_is_stale", lambda **kw: False)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    started: list[str] = []
    async def fake_start(*, engagement_id): started.append(engagement_id)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    down = AsyncMock(return_value=True)
    monkeypatch.setattr(s6_rc, "ensure_service_down", down)

    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "tomb.json"), bus=None)
    rec = _brief_rec("keep1", _BRIEF)
    rec.origin = {}
    rec.auth_token = "tok-same"
    reg._records["keep1"] = rec
    # The token already matches the record; ONLY the URL is stale.
    write_workspace_mcp_json(
        str(ws_root / "keep1"), engagement_id="keep1",
        engagement_auth_token="tok-same",
        casa_framework_mcp_url="http://127.0.0.1:8099/mcp/casa-framework")
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    # Task 6: seed a MATCHING settings.json so the single ``down`` call
    # below is attributable to the URL-staleness cycle alone.
    exec_reg = _exec_reg()
    _seed_current_settings(ws_root / "keep1", exec_reg.get("hello-driver"))

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=exec_reg,
        engagements_root=str(ws_root))

    assert (workspace_mcp_url(str(ws_root / "keep1"))
            == "http://127.0.0.1:8100/mcp/casa-framework")
    assert workspace_mcp_token(str(ws_root / "keep1")) == "tok-same"
    down.assert_awaited_once()           # cycled so the CLI reloads it
    assert started == ["keep1"]
    assert reg._records["keep1"].status != "error"


async def test_unchanged_credential_neither_rewrites_nor_cycles(
    monkeypatch, tmp_path,
):
    """The ordinary restart: the on-disk credential already matches, so replay
    touches nothing and no engagement is disturbed."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from engagement_registry import EngagementRegistry

    monkeypatch.setattr(s6_rc, "sweep_orphan_service_dirs", lambda **kw: [])
    monkeypatch.setattr(s6_rc, "sweep_orphan_compiled_dbs", lambda: None)
    monkeypatch.setattr(s6_rc, "service_pair_complete", lambda **kw: True)
    monkeypatch.setattr(s6_rc, "run_script_is_stale", lambda **kw: False)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    monkeypatch.setattr(s6_rc, "start_service", AsyncMock())
    down = AsyncMock(return_value=True)
    monkeypatch.setattr(s6_rc, "ensure_service_down", down)

    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "tomb.json"), bus=None)
    rec = _brief_rec("keep1", _BRIEF)
    rec.origin = {}
    rec.auth_token = "tok-same"
    reg._records["keep1"] = rec
    _seed_current_credential(ws_root / "keep1", rec)
    mtime_before = (ws_root / "keep1" / ".mcp.json").stat().st_mtime_ns
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    # Task 6: an ordinary restart must also carry a MATCHING settings.json —
    # otherwise the (correct) settings-floor regeneration would itself be a
    # trigger, and this test's "nothing changes" claim would be false.
    exec_reg = _exec_reg()
    _seed_current_settings(ws_root / "keep1", exec_reg.get("hello-driver"))

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=exec_reg,
        engagements_root=str(ws_root))

    down.assert_not_awaited()
    assert (ws_root / "keep1" / ".mcp.json").stat().st_mtime_ns == mtime_before


@pytest.mark.parametrize(
    "stop_outcome", ["returns_false", "raises", "raises_persistently"])
async def test_unconfirmed_stop_after_credential_refresh_refuses_resume(
    monkeypatch, tmp_path, stop_outcome,
):
    """Sol r2: if the stop cannot be CONFIRMED after the credential is already
    on disk, a surviving CLI holds one that authenticates nowhere — and the
    next boot would see the on-disk token matching the record and never retry
    the cycle. So the resume is refused (terminal mark + excluded from the
    start and background loops) rather than left as a mute zombie."""
    from unittest.mock import MagicMock
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc
    from engagement_registry import EngagementRegistry

    monkeypatch.setattr(s6_rc, "sweep_orphan_service_dirs", lambda **kw: [])
    monkeypatch.setattr(s6_rc, "sweep_orphan_compiled_dbs", lambda: None)
    monkeypatch.setattr(s6_rc, "service_pair_complete", lambda **kw: True)
    monkeypatch.setattr(s6_rc, "run_script_is_stale", lambda **kw: False)
    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())
    monkeypatch.setattr(
        s6_rc, "remove_service_dir",
        lambda *, svc_root, engagement_id: None)
    started: list[str] = []
    async def fake_start(*, engagement_id): started.append(engagement_id)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    if stop_outcome == "returns_false":
        monkeypatch.setattr(
            s6_rc, "ensure_service_down", AsyncMock(return_value=False))
    elif stop_outcome == "raises":
        # The cycle attempt itself raises; the refusal helper's own checked
        # teardown then runs and cannot confirm the stop either.
        monkeypatch.setattr(
            s6_rc, "ensure_service_down",
            AsyncMock(side_effect=[OSError("s6-rc missing"), False]))
    else:
        # Sol r3: EVERY stop raises, including the refusal helper's own — the
        # helper then returns before marking anything, so the terminal mark
        # has to be landed by the caller that chose to refuse.
        monkeypatch.setattr(
            s6_rc, "ensure_service_down",
            AsyncMock(side_effect=OSError("s6-rc missing")))

    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "tomb.json"), bus=None)
    rec = _brief_rec("keep1", _BRIEF)
    rec.origin = {}
    rec.auth_token = "tok-fresh"
    reg._records["keep1"] = rec
    driver = _boot_driver()
    bg = MagicMock()
    driver._spawn_background_tasks = bg

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=_exec_reg(),
        engagements_root=str(ws_root))

    assert reg._records["keep1"].status == "error"
    # Task 6: the credential and settings cycles are merged into ONE
    # decision (a workspace with no .mcp.json AND no settings.json — this
    # test's fresh workspace — has both triggers live), so the kind is the
    # shared "workspace cycle" one, not the credential-only legacy name.
    assert reg._records["keep1"].origin["error_kind"] == (
        "refuse_workspace_cycle_failed")
    assert started == []        # never resumed
    assert bg.call_count == 0   # and no background tasks attached


# ---------------------------------------------------------------------------
# #314 — the complete-pair fast path must not skip the missing-workspace /
# missing-artifact guards, and a missing workspace refuses the start.
# ---------------------------------------------------------------------------


def _fast_path_env(monkeypatch, tmp_path, *, pair_complete=True):
    """Wire s6_rc so `keep1` has a COMPLETE, CURRENT service pair (the
    ordinary-restart fast path) without fabricating real s6 sources."""
    from drivers import s6_rc

    svc_root = tmp_path / "svc"
    svc_root.mkdir()
    (svc_root / "engagement-keep1").mkdir()
    (svc_root / "engagement-keep1" / "type").write_text("longrun\n")
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
    monkeypatch.setattr(
        s6_rc, "service_pair_complete", lambda **kw: pair_complete)
    monkeypatch.setattr(s6_rc, "run_script_is_stale", lambda **kw: False)

    start_calls: list[str] = []

    async def fake_cau():
        return None

    async def fake_start(*, engagement_id):
        start_calls.append(engagement_id)

    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
    monkeypatch.setattr(s6_rc, "start_service", fake_start)
    return start_calls


async def test_complete_pair_missing_workspace_refuses_start(
    monkeypatch, tmp_path,
):
    """#314 (1+3): an intact pair with a DELETED workspace used to take the
    early continue and start anyway — the run script's `set -e; cd` then
    exits and s6 respawns it forever (exactly the loop M7 exists to
    prevent). The record must be refused, not started."""
    from casa_core import replay_undergoing_engagements

    start_calls = _fast_path_env(monkeypatch, tmp_path)
    reg = await _make_registry([_rec("keep1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    await replay_undergoing_engagements(
        registry=reg, driver=driver,
        engagements_root=str(tmp_path / "eng"),   # keep1 workspace ABSENT
    )
    assert start_calls == []


async def test_complete_pair_missing_artifact_refuses_start(
    monkeypatch, tmp_path,
):
    """#314 (2): the §3.8 recorded-artifact check only ran on the heal path —
    an intact pair resumed the CLI with a missing --plugin-dir target instead
    of the documented fail-closed refusal."""
    from casa_core import replay_undergoing_engagements

    start_calls = _fast_path_env(monkeypatch, tmp_path)
    ws_root = tmp_path / "eng"
    (ws_root / "keep1").mkdir(parents=True)

    rec = _rec("keep1")
    rec.plugin_artifacts = (
        {"name": "ghost", "path": str(tmp_path / "gone-artifact")},
    )
    reg = await _make_registry([rec])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    await replay_undergoing_engagements(
        registry=reg, driver=driver, engagements_root=str(ws_root),
    )
    assert start_calls == []
    # The operator notice about the unresumable engagement is still owed.
    driver._send_to_topic.assert_awaited()


async def test_heal_path_missing_workspace_refuses_start(
    monkeypatch, tmp_path,
):
    """#314 (3): even when the workspace guard WAS reached (incomplete pair),
    it only warned — the start loop filtered on refused_ids alone, so the
    service was started regardless. Missing workspace ⇒ refused."""
    from casa_core import replay_undergoing_engagements

    start_calls = _fast_path_env(monkeypatch, tmp_path, pair_complete=False)
    reg = await _make_registry([_rec("keep1")])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    await replay_undergoing_engagements(
        registry=reg, driver=driver,
        engagements_root=str(tmp_path / "eng"),   # workspace ABSENT
    )
    assert start_calls == []


async def test_brief_record_missing_workspace_takes_m7_refusal(
    monkeypatch, tmp_path,
):
    """Terra r1 (#314): a brief-bearing record with a deleted workspace must
    take the M7 refusal (retained UNDERGOING, no teardown) — not the brief
    CLAUDE.md-refresh checked teardown, which removes the service pair and
    can terminal-mark the record."""
    from casa_core import replay_undergoing_engagements
    from drivers import s6_rc

    start_calls = _fast_path_env(monkeypatch, tmp_path)
    down_calls: list[str] = []

    async def fake_down(*, engagement_id):
        down_calls.append(engagement_id)
        return True

    monkeypatch.setattr(s6_rc, "ensure_service_down", fake_down)

    rec = _rec("keep1")
    rec.origin["brief"] = {"objective": "x"}
    reg = await _make_registry([rec])
    driver = _boot_driver()
    driver._spawn_background_tasks = lambda rec: None

    await replay_undergoing_engagements(
        registry=reg, driver=driver, executor_registry=None,
        engagements_root=str(tmp_path / "eng"),   # workspace ABSENT
    )
    assert start_calls == []
    assert down_calls == []                       # no checked teardown ran
    # The M7 refusal retains the record and its service sources for diagnosis.
    assert reg.get("keep1").status == "active"
    svc_root = Path(s6_rc.ENGAGEMENT_SOURCES_ROOT)
    assert (svc_root / "engagement-keep1").exists()
