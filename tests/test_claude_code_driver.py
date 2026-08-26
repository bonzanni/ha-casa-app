"""Tests for ClaudeCodeDriver — s6-rc orchestration + workspace + FIFO."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _make_defn(tmp_path, plugins=None):
    from config import ExecutorDefinition

    exec_dir = tmp_path / "defaults-executors" / "hello-driver"
    exec_dir.mkdir(parents=True)
    (exec_dir / "prompt.md").write_text("You are hello-driver. Task: {task}.")
    plugins_dir = ""
    if plugins is not None:
        pdir = exec_dir / "plugins"
        pdir.mkdir()
        for p in plugins:
            (pdir / p).mkdir()
        plugins_dir = str(pdir)
    return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
        type="hello-driver",
        description="Test harness executor type for claude_code driver.",
        model="sonnet",
        driver="claude_code",
        enabled=False,
        tools_allowed=["mcp__casa-framework__emit_completion"],
        permission_mode="dontAsk",
        mcp_server_names=["casa-framework"],
        prompt_template_path=str(exec_dir / "prompt.md"),
        plugins_dir=plugins_dir,
    )


def _make_record(allocated_uid=None):
    from engagement_registry import EngagementRecord
    from engagement_uids import UNALLOCATED_UID
    return EngagementRecord(
        id="abc12345def67890", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=999,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None,
        origin={"channel": "telegram", "chat_id": "42"}, task="say hello",
        # Task 6 (containment stage 2): start() feeds allocated_uid into
        # render_run_script's setpriv wrapper (raises on the sentinel).
        # Default stays UNALLOCATED_UID so callers that don't care about
        # start()'s uid wiring (e.g. session-id capture's owner_uid_or_none
        # check, which treats the sentinel as "no ownership check") are
        # unaffected — tests that exercise start() pass a real uid.
        allocated_uid=(
            UNALLOCATED_UID if allocated_uid is None else allocated_uid
        ),
    )


def _patch_uid_drop_ok(monkeypatch):
    """Task 7 (containment stage 2): ``start()`` now runs
    ``_preflight_uid_drop`` before planting the service. Its real
    preconditions (workspace chown, passwd entry) are made true by
    Task 8 — so orchestration tests that exercise ``start()`` end-to-end
    (not testing the preflight itself, which has its own dedicated test
    class below) stub it to a no-op rather than fake an entire uid/NSS
    environment.

    Task 8: also stubs ``chown_workspace``/``ensure_identity`` themselves —
    ``provision_workspace`` now calls them for real when given a real
    ``allocated_uid`` (every fixture here uses 200005), and a real
    ``os.chown`` to an arbitrary uid requires root, which the unit runner
    is not. Ordering/call-sequence of these two is covered by
    ``tests/test_workspace.py`` directly; this helper only needs them to be
    no-ops so end-to-end orchestration tests don't hit ``PermissionError``.
    """
    from drivers import claude_code_driver as ccd
    from drivers import workspace as ws_mod
    monkeypatch.setattr(ccd, "_preflight_uid_drop", lambda rec, ws: None)
    monkeypatch.setattr(ws_mod, "chown_workspace", lambda ws, uid, gid: None)
    monkeypatch.setattr(ws_mod, "ensure_identity", lambda uid, home: None)


class TestArchiveFetchedByTheDriver:
    """#583: the launch's ONE archive recall belongs to the driver.

    The engager-side half is pinned in
    ``tests/test_engage_executor_tool.py::TestExecutorArchiveFetchedOncePerLaunch``,
    which mocks the driver and so counts zero. On its own that cannot tell
    "the engager stopped fetching" from "nobody fetches at all" — delete the
    driver's fetch and it stays green (Terra, diff round). This is the other
    half: the real ``start``, counting the fetch it actually performs.
    """

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_a_memory_enabled_launch_fetches_the_archive_exactly_once(
        self, monkeypatch, tmp_path,
    ):
        from config import ExecutorMemoryConfig
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import claude_code_driver as ccd
        import tools as tools_mod

        calls: list = []

        async def _counting_fetch(**kwargs):
            calls.append(kwargs)
            return "## Prior engagements\n- a lesson"

        monkeypatch.setattr(tools_mod, "_fetch_executor_archive", _counting_fetch)

        # Stop the launch at provisioning: everything under test has already
        # happened by then, and this keeps the test off s6 and the FIFO.
        class _Sentinel(RuntimeError):
            pass

        async def _stop(**kwargs):
            raise _Sentinel("stop after the fetch")

        monkeypatch.setattr(ccd, "provision_workspace", _stop)

        defn = _make_defn(tmp_path)
        defn.memory = ExecutorMemoryConfig(enabled=True, token_budget=500)
        rec = _make_record(allocated_uid=200005)
        rec.procedural_epoch = "epoch-1"

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()

        with pytest.raises(_Sentinel):
            await drv.start(rec, prompt="first turn", options=defn)

        assert len(calls) == 1, calls
        # Filtered at the RECORD's own markers and epoch — that is why this is
        # the copy kept, rather than the engager's.
        assert calls[0]["task"] == rec.task
        assert "epoch-1" in str(calls[0]["epoch_tag"])

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_a_memory_disabled_launch_fetches_nothing(
        self, monkeypatch, tmp_path,
    ):
        """No shipped claude_code executor opts in, so this is the path every
        launch takes today — it must stay at zero recalls, not one."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import claude_code_driver as ccd
        import tools as tools_mod

        calls: list = []

        async def _counting_fetch(**kwargs):
            calls.append(kwargs)
            return ""

        monkeypatch.setattr(tools_mod, "_fetch_executor_archive", _counting_fetch)

        class _Sentinel(RuntimeError):
            pass

        async def _stop(**kwargs):
            raise _Sentinel("stop after the fetch")

        monkeypatch.setattr(ccd, "provision_workspace", _stop)

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()

        with pytest.raises(_Sentinel):
            await drv.start(
                _make_record(allocated_uid=200005),
                prompt="first turn", options=_make_defn(tmp_path),
            )

        assert calls == []


class TestStart:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_start_provisions_writes_service_compiles_starts(self, monkeypatch, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        _patch_uid_drop_ok(monkeypatch)

        # Mock every s6_rc subprocess call to avoid actually running s6-rc-compile.
        calls: list[tuple[str, dict]] = []

        async def fake_cau():
            calls.append(("compile_and_update_locked", {}))
        async def fake_start(engagement_id):
            calls.append(("start_service", {"engagement_id": engagement_id}))

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        # start_service in impl uses kw-only "engagement_id"; wrap accordingly.
        async def fake_start_kw(*, engagement_id):
            await fake_start(engagement_id)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_kw)

        # Redirect s6_rc.ENGAGEMENT_SOURCES_ROOT to a tmp dir.
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT",
                            str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()

        # Don't actually spawn the background tasks (log relay, respawn
        # poller, session-id capture) in the unit test — they're covered by
        # their dedicated test classes. Without this patch they poll
        # non-existent paths forever and hang CI.
        monkeypatch.setattr(
            ClaudeCodeDriver, "_spawn_background_tasks",
            lambda self, engagement: None,
        )

        # Don't block on FIFO open — the real FIFO has no reader in this test
        # because the s6 service is mocked away. Bypassing is safe: this test
        # only verifies start() provisioning + dispatch, not FIFO I/O.
        async def _noop_write(self, engagement, text):
            return None
        monkeypatch.setattr(
            ClaudeCodeDriver, "_write_to_fifo", _noop_write,
        )

        defn = _make_defn(tmp_path)
        rec = _make_record(allocated_uid=200005)

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()
        (tmp_path / "base-plugins").mkdir()

        await drv.start(rec, prompt="system prompt body", options=defn)

        # Compile must run BEFORE start_service
        names = [c[0] for c in calls]
        assert names == ["compile_and_update_locked", "start_service"]
        # Service dir written with the correct engagement id
        assert (tmp_path / "svc-root" / f"engagement-{rec.id}").is_dir()
        assert (tmp_path / "svc-root" / f"engagement-{rec.id}" / "run").is_file()
        # Workspace provisioned
        assert (tmp_path / "engagements" / rec.id / "CLAUDE.md").exists()
        # Task 4 (containment stage 2): the FIFO lives in the control dir.
        from drivers.workspace import fifo_path
        assert os.path.exists(fifo_path(rec.id))
        assert not (tmp_path / "engagements" / rec.id / "stdin.fifo").exists()


    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_start_carries_brief_envelope_into_claude_md(
        self, monkeypatch, tmp_path,
    ):
        """W3 (Task 8): a brief-bearing record → CLAUDE.md carries the actual
        acceptance criteria + VERBATIM process requirements + completion
        accounting (start passes task=brief_task_for(engagement, defn))."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc
        from drivers.brief import COMPLETION_ACCOUNTING_LINE

        _patch_uid_drop_ok(monkeypatch)

        async def fake_cau():
            return None
        async def fake_start_kw(*, engagement_id):
            return None
        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_kw)
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT",
                            str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()
        monkeypatch.setattr(
            ClaudeCodeDriver, "_spawn_background_tasks",
            lambda self, engagement: None,
        )
        async def _noop_write(self, engagement, text):
            return None
        monkeypatch.setattr(ClaudeCodeDriver, "_write_to_fifo", _noop_write)

        defn = _make_defn(tmp_path)
        rec = _make_record(allocated_uid=200005)
        rec.origin["brief"] = {
            "objective": "Rotate the API keys",
            "acceptance_criteria": ["old keys revoked"],
            "process_requirements": ["Announce the rotation window first"],
        }

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()

        await drv.start(rec, prompt="ignored fifo prompt", options=defn)

        claude_md = (tmp_path / "engagements" / rec.id / "CLAUDE.md").read_text()
        assert "Rotate the API keys" in claude_md
        assert "old keys revoked" in claude_md
        assert "Announce the rotation window first" in claude_md
        assert COMPLETION_ACCOUNTING_LINE in claude_md


class TestStartRollback:
    """Bug 13 (v0.14.6): if any step in start() fails, the partial
    workspace + service-dir + s6-rc compile must be rolled back.

    Pre-fix the workspace was left UNDERGOING and the sweeper skipped it
    forever, leaking disk and producing ghost engagements that boot
    replay would attempt to resurrect.
    """

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_start_service_failure_cleans_up(self, monkeypatch, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        _patch_uid_drop_ok(monkeypatch)

        compile_calls: list[str] = []

        async def fake_cau():
            compile_calls.append("compile")

        async def fake_start_fail(*, engagement_id):
            raise RuntimeError("simulated s6-rc start failure")

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_fail)
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT",
                            str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()

        defn = _make_defn(tmp_path)
        rec = _make_record(allocated_uid=200005)

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()
        (tmp_path / "base-plugins").mkdir()

        with pytest.raises(RuntimeError, match="simulated s6-rc start failure"):
            await drv.start(rec, prompt="hi", options=defn)

        # The original failure is re-raised.
        # Rollback removed the workspace.
        assert not (tmp_path / "engagements" / rec.id).exists(), (
            "Bug 13: workspace not cleaned up on start_service failure — "
            "leaves an orphan UNDERGOING that the sweeper will skip forever"
        )
        # And the s6 service dir.
        assert not (tmp_path / "svc-root" / f"engagement-{rec.id}").exists(), (
            "Bug 13: s6 service dir not cleaned up on start_service failure"
        )
        # _compile_and_update_locked should be called twice: once forward,
        # once on rollback.
        assert compile_calls.count("compile") == 2

    async def test_cancellation_mid_launch_rolls_back_like_a_failure(
            self, monkeypatch, tmp_path):
        """Sol r2 (#363 family): a task CANCELLATION delivered at a launch
        await (compile, summary post, start_service) must run the same
        rollback as an exception — pre-fix the ``except Exception`` handler
        skipped it, abandoning half-written service dirs and the workspace."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        _patch_uid_drop_ok(monkeypatch)

        async def fake_cau():
            return None

        async def fake_start_cancelled(*, engagement_id):
            raise asyncio.CancelledError()

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_cancelled)
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT",
                            str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()

        defn = _make_defn(tmp_path)
        rec = _make_record(allocated_uid=200005)
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()
        (tmp_path / "base-plugins").mkdir()

        with pytest.raises(asyncio.CancelledError):
            await drv.start(rec, prompt="hi", options=defn)
        assert not (tmp_path / "engagements" / rec.id).exists()
        assert not (tmp_path / "svc-root" / f"engagement-{rec.id}").exists()

    async def test_start_service_failure_removes_engagement_outbox(
            self, monkeypatch, tmp_path):
        """M1 (containment stage 2 fix-wave, final review): the rollback path
        must also remove the uid's private outbox dir that
        ``provision_workspace`` created (Task 11,
        ``plugin_outbox.provision_engagement_outbox``) — pre-fix the rollback
        removed the workspace/control dir and pruned the passwd identity but
        left this dir behind, and uids are never reused so a repeatedly-
        failing launch leaked a permanent empty dir per attempt. Asymmetric
        with the sweeper/``delete_engagement_workspace`` teardown paths, which
        already remove it (``tools.py`` Task 11 call site)."""
        import plugin_outbox
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        _patch_uid_drop_ok(monkeypatch)

        async def fake_cau():
            pass

        async def fake_start_fail(*, engagement_id):
            raise RuntimeError("simulated s6-rc start failure")

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_fail)
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT",
                            str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()

        defn = _make_defn(tmp_path)
        rec = _make_record(allocated_uid=200005)

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()
        (tmp_path / "base-plugins").mkdir()

        # Sanity: provision_workspace (real, not faked here) really creates
        # the private outbox dir for this uid before start_service fails —
        # otherwise this test would trivially pass with a no-op fix.
        outbox_dir = plugin_outbox.engagement_outbox_dir(200005)

        with pytest.raises(RuntimeError, match="simulated s6-rc start failure"):
            await drv.start(rec, prompt="hi", options=defn)

        assert not os.path.isdir(outbox_dir), (
            "M1: rollback must remove the uid's private outbox dir "
            "provisioned during start() — a failed launch otherwise leaks a "
            "permanent empty dir (uids are never reused)"
        )

    async def test_provision_failure_cleans_up(self, monkeypatch, tmp_path):
        """Failure during provisioning (before service-dir write) — only
        the workspace tree needs cleanup, not the (never-written) service dir."""
        from drivers import workspace as ws_mod
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        async def fail_provision(**kw):
            # Simulate partial workspace creation, then explode.
            from pathlib import Path
            ws = Path(kw["engagements_root"]) / kw["engagement_id"]
            ws.mkdir(parents=True, exist_ok=False)
            (ws / "CLAUDE.md").write_text("partial", encoding="utf-8")
            raise OSError("disk full")

        monkeypatch.setattr(ws_mod, "provision_workspace", fail_provision)

        # Also patch the imported reference inside the driver module.
        from drivers import claude_code_driver as ccd
        monkeypatch.setattr(ccd, "provision_workspace", fail_provision)

        async def fake_cau():
            pass

        async def fake_start(*, engagement_id):
            pass

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "start_service", fake_start)
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT",
                            str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()

        defn = _make_defn(tmp_path)
        rec = _make_record()

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()
        (tmp_path / "base-plugins").mkdir()

        with pytest.raises(OSError, match="disk full"):
            await drv.start(rec, prompt="hi", options=defn)

        # Provisioning aborted before write_casa_meta — but the partial
        # workspace tree should still be removed (rmtree(ignore_errors=True)
        # is best-effort).
        assert not (tmp_path / "engagements" / rec.id).exists()


class TestPreflightUidDrop:
    """Task 7 (containment stage 2): `_preflight_uid_drop` is the
    fail-closed gate run before a service is planted — it must refuse
    (never plant) whenever the setpriv uid drop `render_run_script` bakes
    into the run script cannot actually succeed."""

    def test_refuses_when_setpriv_missing(self, monkeypatch, tmp_path):
        from drivers import claude_code_driver as ccd

        monkeypatch.setattr(ccd.shutil, "which", lambda name: None)
        rec = _make_record(allocated_uid=200005)

        with pytest.raises(ccd.UidDropRefused, match="setpriv"):
            ccd._preflight_uid_drop(rec, str(tmp_path))

    def test_refuses_uid_sentinel(self, monkeypatch, tmp_path):
        from drivers import claude_code_driver as ccd

        monkeypatch.setattr(ccd.shutil, "which", lambda name: "/usr/bin/setpriv")
        rec = _make_record()  # defaults to UNALLOCATED_UID

        with pytest.raises(ccd.UidDropRefused, match="UID_BASE"):
            ccd._preflight_uid_drop(rec, str(tmp_path))

    def test_refuses_workspace_owner_mismatch(self, monkeypatch, tmp_path):
        from drivers import claude_code_driver as ccd

        monkeypatch.setattr(ccd.shutil, "which", lambda name: "/usr/bin/setpriv")
        rec = _make_record(allocated_uid=200005)

        class FakeStat:
            st_uid = 999999  # not the allocated uid — chown hasn't run

        monkeypatch.setattr(ccd.os, "stat", lambda path, *a, **kw: FakeStat())

        with pytest.raises(ccd.UidDropRefused, match="owned by uid"):
            ccd._preflight_uid_drop(rec, str(tmp_path))

    def test_refuses_missing_passwd_entry(self, monkeypatch, tmp_path):
        from drivers import claude_code_driver as ccd

        monkeypatch.setattr(ccd.shutil, "which", lambda name: "/usr/bin/setpriv")
        rec = _make_record(allocated_uid=200005)

        class FakeStat:
            st_uid = 200005  # ownership check passes

        monkeypatch.setattr(ccd.os, "stat", lambda path, *a, **kw: FakeStat())

        def fake_getpwuid(uid):
            raise KeyError(uid)

        monkeypatch.setattr(ccd.pwd, "getpwuid", fake_getpwuid)

        with pytest.raises(ccd.UidDropRefused, match="passwd entry"):
            ccd._preflight_uid_drop(rec, str(tmp_path))

    def test_refuses_unreadable_plugin_dir(self, monkeypatch, tmp_path):
        from drivers import claude_code_driver as ccd

        monkeypatch.setattr(ccd.shutil, "which", lambda name: "/usr/bin/setpriv")
        rec = _make_record(allocated_uid=200005)
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        os.chmod(plugin_dir, 0o700)  # owner-only — no world r/x
        rec.plugin_artifacts = ({"path": str(plugin_dir)},)

        real_stat = os.stat

        def fake_stat(path, *a, **kw):
            if os.fspath(path) == str(tmp_path):
                class Owned:
                    st_uid = 200005
                return Owned()
            return real_stat(path, *a, **kw)  # real stat for the plugin dir

        monkeypatch.setattr(ccd.os, "stat", fake_stat)
        monkeypatch.setattr(ccd.pwd, "getpwuid", lambda uid: object())

        with pytest.raises(ccd.UidDropRefused, match="not world-readable"):
            ccd._preflight_uid_drop(rec, str(tmp_path))

    def test_happy_path_all_checks_pass(self, monkeypatch, tmp_path):
        from drivers import claude_code_driver as ccd

        monkeypatch.setattr(ccd.shutil, "which", lambda name: "/usr/bin/setpriv")
        rec = _make_record(allocated_uid=200005)
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        rec.plugin_artifacts = ({"path": str(plugin_dir)},)

        class FakeStat:
            st_uid = 200005
            st_mode = 0o40755  # dir, rwxr-xr-x — world r+x too

        monkeypatch.setattr(ccd.os, "stat", lambda path, *a, **kw: FakeStat())
        monkeypatch.setattr(ccd.pwd, "getpwuid", lambda uid: object())

        ccd._preflight_uid_drop(rec, str(tmp_path))  # must not raise


class TestUidDropRefusalWiredIntoStart:
    """Task 7: the caller in `start()` must map `UidDropRefused` to a
    dedicated `mark_error(kind="refuse_uid_drop_failed")` + best-effort
    `ensure_service_down`, and must NEVER plant the service (write_service_dir
    / start_service) on refusal."""

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_refusal_marks_error_and_never_plants_service(
        self, monkeypatch, tmp_path,
    ):
        from drivers.claude_code_driver import ClaudeCodeDriver, UidDropRefused
        from drivers import claude_code_driver as ccd
        from drivers import s6_rc

        def boom(rec, ws):
            raise UidDropRefused("simulated uid-drop refusal")

        monkeypatch.setattr(ccd, "_preflight_uid_drop", boom)
        # Task 8: provision_workspace (step 1) runs BEFORE this preflight
        # (step 1.5) and now really chowns for a record with a real
        # allocated_uid (200005, below) — stub so this test doesn't need
        # root.
        from drivers import workspace as ws_mod
        monkeypatch.setattr(ws_mod, "chown_workspace", lambda ws, uid, gid: None)
        monkeypatch.setattr(ws_mod, "ensure_identity", lambda uid, home: None)

        write_calls: list[str] = []
        start_calls: list[str] = []

        def fake_write_service_dir(**kw):
            write_calls.append(kw["engagement_id"])
        async def fake_start_kw(*, engagement_id):
            start_calls.append(engagement_id)
        monkeypatch.setattr(s6_rc, "write_service_dir", fake_write_service_dir)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_kw)

        ensure_down_calls: list[str] = []
        async def fake_ensure_down(*, engagement_id, **kw):
            ensure_down_calls.append(engagement_id)
            return True
        monkeypatch.setattr(s6_rc, "ensure_service_down", fake_ensure_down)

        monkeypatch.setattr(
            s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()

        mark_error_calls: list[tuple] = []

        class FakeRegistry:
            async def mark_error(self, engagement_id, kind, message):
                mark_error_calls.append((engagement_id, kind, message))
                return True

        defn = _make_defn(tmp_path)
        rec = _make_record(allocated_uid=200005)

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
            registry=FakeRegistry(),
        )
        (tmp_path / "engagements").mkdir()
        (tmp_path / "base-plugins").mkdir()

        with pytest.raises(UidDropRefused, match="simulated uid-drop refusal"):
            await drv.start(rec, prompt="hi", options=defn)

        assert write_calls == [], "write_service_dir must not run on refusal"
        assert start_calls == [], "start_service must not run on refusal"
        assert ensure_down_calls == [rec.id]
        assert len(mark_error_calls) == 1
        eid, kind, message = mark_error_calls[0]
        assert eid == rec.id
        assert kind == "refuse_uid_drop_failed"
        assert "simulated uid-drop refusal" in message


class TestNoRemoteControlNotices:
    """v0.64.0: URL capture removed — headless claude auto-degrades to
    one-shot --print mode on non-TTY stdout and never prints a
    'Remote Control URL:' line (live-verified 2026-07-10), so the driver
    must neither watch for one nor post any remote-control topic notice.
    See docs/superpowers/specs/2026-07-10-v0.64.0-remote-control-honesty-design.md."""

    async def test_background_tasks_never_post_to_topic(self, tmp_path, caplog):
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        sender = AsyncMock()
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
        )
        rec = _make_record()
        # DEBUG-enable subprocess_cli so the full roster (incl. relay) runs.
        with caplog.at_level(logging.DEBUG, logger="subprocess_cli"):
            drv._spawn_background_tasks(rec)
            tasks = drv._tasks[rec.id]
            # respawn poller + sequencer watcher + session-id capture +
            # always-on topic relay + DEBUG log relay + summary-pin; no URL
            # capture. (v0.75.0 added the always-on topic relay; v0.79.0 added
            # the per-engagement output-sequencer watcher AND the summary
            # initial-pin task, so DEBUG-enabled is 6.)
            assert len(tasks) == 6
            await asyncio.sleep(0.3)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        sender.assert_not_awaited()


class TestRelaySpawnGate:
    """v0.64.0 efficiency: the DEBUG raw-log relay tails a now-real file at
    10 Hz only to discard every line unless subprocess_cli is DEBUG-enabled —
    so THAT task is only spawned when it is. (A LOG_LEVEL flip requires an
    add-on restart, which respawns these tasks anyway.) v0.75.0: the SEPARATE
    always-on topic-stream relay is spawned regardless of LOG_LEVEL."""

    async def test_relay_skipped_when_debug_disabled(self, tmp_path):
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        lg = logging.getLogger("subprocess_cli")
        old_level = lg.level
        lg.setLevel(logging.WARNING)
        try:
            drv._spawn_background_tasks(rec)
            tasks = drv._tasks[rec.id]
            # respawn poller + sequencer watcher + session-id capture +
            # always-on topic relay + summary-pin. The DEBUG raw-log relay is
            # skipped; the topic relay + sequencer watcher + summary-pin are NOT
            # (v0.79.0: 5 tasks).
            assert len(tasks) == 5
            names = [t.get_name() for t in tasks]
            assert any(n.startswith("topic_relay:") for n in names), names
            assert any(n.startswith("seq_watcher:") for n in names), names
        finally:
            lg.setLevel(old_level)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class TestTailFileResilience:
    """s6-log rotates `current` by rename; if that lands between the
    tailer's exists() and open(), the open raises FileNotFoundError. The
    relay task is unobserved — the tailer must retry, not die."""

    async def test_survives_transient_open_failure(self, tmp_path, monkeypatch):
        import pathlib
        from drivers.claude_code_driver import _tail_file

        f = tmp_path / "current"
        f.write_text("hello\n", encoding="utf-8")

        real_open = pathlib.Path.open
        state = {"raised": False}

        def flaky(self, *args, **kwargs):
            if self == f and not state["raised"]:
                state["raised"] = True
                raise FileNotFoundError("rotated away")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "open", flaky)

        gen = _tail_file(str(f))
        line = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert line == "hello\n"
        assert state["raised"], "the transient failure was never exercised"
        await gen.aclose()


class TestSessionIdCapture:
    """P31 (v0.37.10): watch claude CLI's own session storage dir for new
    ``<uuid>.jsonl`` files. The filename (minus extension) IS the SDK
    session UUID. Persist to ``<workspace>/.session_id`` atomically so
    boot-replay's ``--resume`` plumbing picks it up after a Casa restart.

    Replaces v0.37.9's s6-log tailing approach which was non-functional
    in production: the s6-rc service dir's log/ subdir lacked the
    producer-for / consumer-for wiring required to compile the
    producer-consumer pair, so ``/var/log/casa-engagement-<id>/current``
    was never created. Live evidence: 2026-05-14 exploration6 — both
    engagements ``28fdeb04`` and ``3e44c2cf`` saw zero session_id writes.

    Claude CLI session storage layout (HOME=<ws>/.home, CWD=<ws>):

        <ws>/.home/.claude/projects/-data-engagements-<id>/<uuid>.jsonl

    The directory-name encoding replaces ``/`` with ``-`` in the
    workspace path (claude CLI native behavior).
    """

    @staticmethod
    def _projects_dir(ws):
        """Mirror the encoding the claude CLI uses for cwd directory names."""
        return ws / ".home" / ".claude" / "projects" / (
            f"-data-engagements-{ws.name}"
        )

    async def test_writes_session_id_on_first_jsonl(self, tmp_path):
        """Happy path: claude CLI creates the projects dir with a single
        ``<uuid>.jsonl`` file. Watcher persists the UUID to
        ``<ws>/.session_id`` and invokes ``persist_session_id`` callback.
        """
        from pathlib import Path
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers.workspace import provision_control_dir, session_id_path

        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        provision_control_dir(rec.id)
        projects = self._projects_dir(ws)
        projects.mkdir(parents=True)
        sid = "8ab67de0-1234-5678-9abc-def012345678"
        (projects / f"{sid}.jsonl").write_text(
            '{"type":"system_init","session_id":"' + sid + '"}\n',
            encoding="utf-8",
        )

        persisted: list[tuple[str, str]] = []

        async def fake_persist(engagement_id: str, session_id: str) -> None:
            persisted.append((engagement_id, session_id))

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="x",
            persist_session_id=fake_persist,
        )

        task = asyncio.create_task(drv._capture_session_id(rec))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Task 4 (containment stage 2): .session_id lives in the control dir.
        session_file = Path(session_id_path(rec.id))
        assert session_file.exists(), (
            f".session_id file must be written to the control dir "
            f"({session_file.parent}); contents: "
            f"{list(session_file.parent.iterdir())}"
        )
        assert session_file.read_text(encoding="utf-8").strip() == sid
        assert persisted == [(rec.id, sid)], (
            f"persist_session_id callback must be invoked exactly once "
            f"with (engagement_id, session_id); got {persisted!r}"
        )

    async def test_waits_for_projects_dir_to_appear(self, tmp_path):
        """Projects dir does not exist at watcher start (claude CLI has
        not spawned yet — there is a small window between s6 starting
        the service and the CLI writing its first jsonl). Watcher polls
        until the directory + file appear.
        """
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers.workspace import provision_control_dir, session_id_path

        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        provision_control_dir(rec.id)
        projects = self._projects_dir(ws)
        # Don't create projects yet — let the watcher poll.

        persisted: list[tuple[str, str]] = []

        async def fake_persist(engagement_id: str, session_id: str) -> None:
            persisted.append((engagement_id, session_id))

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="x",
            persist_session_id=fake_persist,
        )

        task = asyncio.create_task(drv._capture_session_id(rec))

        # Claude CLI starts up after 0.2s and writes its jsonl.
        await asyncio.sleep(0.2)
        projects.mkdir(parents=True)
        sid = "11111111-2222-3333-4444-555555555555"
        (projects / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

        # Give the poller a beat to notice.
        await asyncio.sleep(0.4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        import pathlib as _pathlib
        assert _pathlib.Path(session_id_path(rec.id)).read_text(
            encoding="utf-8").strip() == sid
        assert persisted == [(rec.id, sid)]

    async def test_ignores_non_uuid_filenames(self, tmp_path):
        """Watcher must only accept UUID-shaped filenames (claude CLI's
        session files). Other files in the projects dir (logs, locks,
        partial writes) must NOT be persisted as session_ids."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers.workspace import provision_control_dir, session_id_path

        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        provision_control_dir(rec.id)
        projects = self._projects_dir(ws)
        projects.mkdir(parents=True)
        # Decoy files that look superficially like jsonl but are NOT
        # valid UUIDs. The watcher must skip these.
        (projects / "log.jsonl").write_text("{}\n", encoding="utf-8")
        (projects / "lockfile.jsonl").write_text("{}\n", encoding="utf-8")

        persisted: list[tuple[str, str]] = []

        async def fake_persist(engagement_id: str, session_id: str) -> None:
            persisted.append((engagement_id, session_id))

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="x",
            persist_session_id=fake_persist,
        )

        task = asyncio.create_task(drv._capture_session_id(rec))
        await asyncio.sleep(0.3)

        # Now drop in a real UUID-named file.
        sid = "abcdef00-0000-0000-0000-000000000000"
        (projects / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # The decoy files were ignored; the UUID-named file was captured.
        import pathlib as _pathlib
        assert _pathlib.Path(session_id_path(rec.id)).read_text(
            encoding="utf-8").strip() == sid
        assert persisted == [(rec.id, sid)]

    async def test_atomic_write_via_temp_rename(self, tmp_path):
        """A partial-write crash must NOT leave a half-written
        ``.session_id`` that a subsequent boot-replay would feed to
        ``claude --resume <truncated>``. Verify temp+rename atomicity
        (no leftover ``.session_id.tmp`` in workspace)."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers.workspace import control_dir, provision_control_dir

        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        provision_control_dir(rec.id)
        projects = self._projects_dir(ws)
        projects.mkdir(parents=True)
        sid = "deadbeef-0000-0000-0000-000000000000"
        (projects / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="x",
            persist_session_id=None,  # None tolerated — no registry hook in test
        )

        task = asyncio.create_task(drv._capture_session_id(rec))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Task 4: .session_id (and its .tmp) live in the control dir.
        import pathlib as _pathlib
        ctl = _pathlib.Path(control_dir(rec.id))
        leftovers = [p.name for p in ctl.iterdir() if p.name.startswith(".session_id")]
        assert ".session_id" in leftovers
        tmp_leftovers = [n for n in leftovers if n != ".session_id"]
        assert tmp_leftovers == [], (
            f"atomic-write temp file leaked into the control dir: {tmp_leftovers!r}"
        )

    @pytest.mark.parametrize("has_openat2", [True, False])
    async def test_refuses_symlinked_projects_dir(
        self, tmp_path, monkeypatch, has_openat2,
    ):
        """Containment stage 2, Task 5: engagement A's ``.home/.claude/
        projects/<name>`` is a SYMLINK to sibling engagement B's own
        projects dir. The watcher must refuse to follow it — never adopting
        B's session UUID as A's — and stop watching (no adoption at all),
        rather than silently reading through the symlink. Parametrized over
        both the ``openat2`` fast path and the per-component fallback (the
        two independent enforcement mechanisms in ``safe_fs``)."""
        from pathlib import Path

        import safe_fs
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers.workspace import provision_control_dir, session_id_path

        monkeypatch.setattr(safe_fs, "HAS_OPENAT2", has_openat2)

        rec = _make_record()
        ws_a = tmp_path / rec.id
        ws_a.mkdir()
        provision_control_dir(rec.id)

        # Sibling B's own (legitimate) projects dir, holding B's session.
        ws_b = tmp_path / "sibling-b"
        b_projects = (
            ws_b / ".home" / ".claude" / "projects" / "-data-engagements-sibling-b"
        )
        b_projects.mkdir(parents=True)
        b_sid = "bbbbbbbb-0000-0000-0000-000000000000"
        (b_projects / f"{b_sid}.jsonl").write_text("{}\n", encoding="utf-8")

        # A's own projects dir path is a SYMLINK to B's — the attack this
        # task closes: once workspaces are uid-chowned (Task 8), a
        # compromised A can plant this to read B's session artifacts.
        a_home_claude_projects = ws_a / ".home" / ".claude" / "projects"
        a_home_claude_projects.mkdir(parents=True)
        (a_home_claude_projects / f"-data-engagements-{rec.id}").symlink_to(
            b_projects, target_is_directory=True,
        )

        persisted: list[tuple[str, str]] = []

        async def fake_persist(engagement_id: str, session_id: str) -> None:
            persisted.append((engagement_id, session_id))

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="x",
            persist_session_id=fake_persist,
        )

        # No infinite poll loop: a refusal must make the watcher RETURN
        # promptly rather than spin forever re-hitting the same symlink.
        await asyncio.wait_for(drv._capture_session_id(rec), timeout=5.0)

        # No adoption at all — neither B's uuid nor anything else.
        assert persisted == [], (
            f"session capture must not adopt ANY session id through a "
            f"symlinked projects dir; got {persisted!r}"
        )
        assert not Path(session_id_path(rec.id)).exists(), (
            ".session_id must not be written when the projects dir "
            "resolution was refused as a symlink"
        )


class TestRespawnPoller:
    async def test_emits_bus_event_on_pid_change(self, monkeypatch, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        pids = iter([100, 100, 200, 200, 200])
        async def fake_pid(*, engagement_id):
            try:
                return next(pids)
            except StopIteration:
                return 200
        monkeypatch.setattr(s6_rc, "service_pid", fake_pid)

        bus_events: list[dict] = []
        async def fake_publish(*args, **kwargs):
            bus_events.append({"args": args, "kwargs": kwargs})

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        drv._publish_bus_event = fake_publish     # dependency injection

        rec = _make_record()
        task = asyncio.create_task(
            drv._poll_respawns(rec, interval_s=0.05)
        )
        await asyncio.sleep(0.4)        # enough ticks to see the 100 → 200 change
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

        # At least one subprocess_respawn event with previous=100, new=200
        respawn = [e for e in bus_events if
                   e["args"][0].get("event") == "subprocess_respawn"]
        assert len(respawn) >= 1
        assert respawn[0]["args"][0]["previous_pid"] == 100
        assert respawn[0]["args"][0]["new_pid"] == 200


class TestCancel:
    async def test_cancel_stops_service_and_removes_dir(self, monkeypatch, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        stopped: list[str] = []
        async def fake_stop(*, engagement_id):
            stopped.append(engagement_id)
        async def fake_cau(): pass

        monkeypatch.setattr(s6_rc, "stop_service", fake_stop)
        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc"))
        (tmp_path / "svc").mkdir()
        (tmp_path / "svc" / "engagement-abc12345def67890").mkdir()
        (tmp_path / "svc" / "engagement-abc12345def67890" / "type").write_text("longrun\n")
        (tmp_path / "svc" / "engagement-abc12345def67890-log").mkdir()
        (tmp_path / "svc" / "engagement-abc12345def67890-log" / "type").write_text("longrun\n")

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "eng"),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        await drv.cancel(rec)

        # v0.64.0: the sibling logger service is stopped explicitly too, so
        # the follow-up recompile never has to down a still-live service.
        assert stopped == [rec.id, f"{rec.id}-log"]
        assert not (tmp_path / "svc" / f"engagement-{rec.id}").exists()
        assert not (tmp_path / "svc" / f"engagement-{rec.id}-log").exists()

    async def test_cancel_skips_logger_stop_for_legacy_engagement(
        self, monkeypatch, tmp_path,
    ):
        """Engagements created pre-v0.64.0 have no logger service — cancel
        must not exec a doomed `s6-rc -d change engagement-<id>-log`."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        stopped: list[str] = []
        async def fake_stop(*, engagement_id):
            stopped.append(engagement_id)
        async def fake_cau(): pass

        monkeypatch.setattr(s6_rc, "stop_service", fake_stop)
        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc"))
        (tmp_path / "svc").mkdir()
        (tmp_path / "svc" / "engagement-abc12345def67890").mkdir()
        # No engagement-<id>-log sibling (legacy layout).

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "eng"),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        await drv.cancel(rec)

        assert stopped == [rec.id]


class TestRelayLogLines:
    """G5 — claude_code driver relays its per-engagement s6-log lines
    into Casa's logger at DEBUG, on the same `subprocess_cli` logger
    used by Bug 4's stderr callback."""

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="_tail_file uses path semantics that don't work cleanly on Windows",
    )
    async def test_relay_log_lines_emits_debug_per_line(
        self, tmp_path, caplog,
    ):
        import asyncio
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        rec = _make_record()  # id="abc12345def67890"
        log_file = tmp_path / "log-current"

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://x",
        )

        with caplog.at_level(logging.DEBUG, logger="subprocess_cli"):
            task = asyncio.create_task(
                drv._relay_log_lines(rec, log_path=str(log_file)),
            )
            # Lines are written AFTER the relay starts (the real flow: s6-log
            # creates the file once the fresh engagement's CLI first writes).
            await asyncio.sleep(0.2)
            log_file.write_text(
                "first line\n"
                "second line\n"
                "third line https://example/123\n",
                encoding="utf-8",
            )
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        recs = [r for r in caplog.records if r.name == "subprocess_cli"]
        msgs = [r.getMessage() for r in recs]
        assert any("first line" in m for m in msgs), msgs
        assert any("second line" in m for m in msgs), msgs
        assert any("third line" in m for m in msgs), msgs
        # Every relayed record carries engagement_id (first 8 chars of rec.id)
        for r in recs:
            assert getattr(r, "engagement_id", None) == "abc12345", (
                f"missing engagement_id on relay record: {r.getMessage()}"
            )
        assert all(r.levelno == logging.DEBUG for r in recs), (
            "relay must emit DEBUG, not INFO"
        )

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="_tail_file uses path semantics that don't work cleanly on Windows",
    )
    async def test_relay_starts_at_end_of_preexisting_file(
        self, tmp_path, caplog,
    ):
        """Boot replay re-spawns the relay against a file that may already
        hold up to 1 MB of history — re-relaying it would bury fresh lines
        at DEBUG. A pre-existing file is tailed from its end."""
        import asyncio
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        rec = _make_record()
        log_file = tmp_path / "log-current"
        log_file.write_text("old historical line\n", encoding="utf-8")

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://x",
        )

        with caplog.at_level(logging.DEBUG, logger="subprocess_cli"):
            task = asyncio.create_task(
                drv._relay_log_lines(rec, log_path=str(log_file)),
            )
            await asyncio.sleep(0.3)
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write("fresh line\n")
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        msgs = [
            r.getMessage() for r in caplog.records
            if r.name == "subprocess_cli"
        ]
        assert any("fresh line" in m for m in msgs), msgs
        assert not any("old historical line" in m for m in msgs), msgs


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="mkfifo Linux-only")
class TestWriteToFifoBounded:
    """M13: _write_to_fifo must never park a pooled thread forever when no
    FIFO reader exists — it opens/writes non-blocking with a bounded deadline."""

    def _driver(self, tmp_path, sent):
        from drivers.claude_code_driver import ClaudeCodeDriver

        async def send(topic_id, text):
            sent.append((topic_id, text))
        return ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=send, casa_framework_mcp_url="http://unused",
        )

    async def test_no_reader_returns_within_deadline_and_notifies(self, tmp_path):
        import os
        from types import SimpleNamespace

        ws = tmp_path / "eng-no-reader"
        ws.mkdir()
        from drivers.workspace import fifo_path, provision_control_dir
        provision_control_dir("eng-no-reader")
        os.mkfifo(fifo_path("eng-no-reader"))
        sent = []
        driver = self._driver(tmp_path, sent)
        rec = SimpleNamespace(id="eng-no-reader", topic_id=42)
        # Pre-fix: parks a pool thread forever in open() -> wait_for raises
        # TimeoutError and this FAILS. Fixed: returns within ~1s + notifies.
        await asyncio.wait_for(
            driver._write_to_fifo(rec, "hello", timeout_s=1.0, poll_s=0.05),
            timeout=5.0,
        )
        assert sent and sent[0][0] == 42
        assert rec.id not in driver._last_turn_ts

    async def test_no_reader_retained_notice_promises_auto_retry(self, tmp_path):
        """#322: when the caller RETAINS the envelope for auto-redelivery (the
        spool path), the no-reader notice must not tell the operator to
        resend — the retained envelope redelivers on the next spawn, so a
        resend duplicates the turn. The notice promises the retry instead."""
        import os
        from types import SimpleNamespace

        ws = tmp_path / "eng-retained"
        ws.mkdir()
        from drivers.workspace import fifo_path, provision_control_dir
        provision_control_dir("eng-retained")
        os.mkfifo(fifo_path("eng-retained"))
        sent = []
        driver = self._driver(tmp_path, sent)
        rec = SimpleNamespace(id="eng-retained", topic_id=42)
        ok = await asyncio.wait_for(
            driver._write_to_fifo(
                rec, "hello", timeout_s=1.0, poll_s=0.05, retained=True),
            timeout=5.0,
        )
        assert ok is False
        assert sent and sent[0][0] == 42
        notice = sent[0][1]
        assert "Try again" not in notice
        assert "will be delivered" in notice

    async def test_no_reader_default_notice_still_asks_for_resend(self, tmp_path):
        """The legacy no-spool direct-write path retains nothing — 'Try
        again' stays the honest copy there."""
        import os
        from types import SimpleNamespace

        ws = tmp_path / "eng-legacy"
        ws.mkdir()
        from drivers.workspace import fifo_path, provision_control_dir
        provision_control_dir("eng-legacy")
        os.mkfifo(fifo_path("eng-legacy"))
        sent = []
        driver = self._driver(tmp_path, sent)
        rec = SimpleNamespace(id="eng-legacy", topic_id=42)
        await asyncio.wait_for(
            driver._write_to_fifo(rec, "hello", timeout_s=1.0, poll_s=0.05),
            timeout=5.0,
        )
        assert sent and "Try again" in sent[0][1]

    async def test_spool_pump_writes_fifo_with_retained_notice_wiring(self, tmp_path):
        """#322 wiring: the spool's ``write_fifo`` seam passes
        ``retained=True`` — the spool is exactly the caller that retains."""
        from unittest.mock import AsyncMock
        from drivers.claude_code_driver import ClaudeCodeDriver

        driver = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="http://unused",
        )
        rec = _make_record()
        (tmp_path / rec.id).mkdir()
        seen: list[dict] = []

        async def probe(engagement, text, **kwargs):
            seen.append(kwargs)
            return True

        driver._write_to_fifo = probe  # type: ignore[assignment]
        tasks = []
        try:
            driver._spawn_background_tasks(rec)
            tasks = driver._tasks[rec.id]
            spool = driver._inbound[rec.id]
            await spool._write_fifo("ping")
            assert seen and seen[0].get("retained") is True
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_with_reader_delivers_text(self, tmp_path):
        import os
        import threading
        from types import SimpleNamespace

        from drivers.workspace import fifo_path, provision_control_dir

        ws = tmp_path / "eng-reader"
        ws.mkdir()
        provision_control_dir("eng-reader")
        fifo = fifo_path("eng-reader")
        os.mkfifo(fifo)
        got = []
        t = threading.Thread(
            target=lambda: got.append(
                open(fifo, "r", encoding="utf-8").readline()),
        )
        t.start()
        sent = []
        driver = self._driver(tmp_path, sent)
        rec = SimpleNamespace(id="eng-reader", topic_id=7)
        await asyncio.wait_for(
            driver._write_to_fifo(rec, "hi there", timeout_s=5.0), timeout=5.0,
        )
        t.join(timeout=5.0)
        assert got == ["hi there\n"]
        assert sent == []
        assert rec.id in driver._last_turn_ts


# ---------------------------------------------------------------------------
# W1/Sol B8 — spawn-keyed one-turn inbound queue.
# ---------------------------------------------------------------------------


class _FakeWriter:
    """Injectable ``write_fifo`` for _InboundSpool — records calls, returns a
    mutable ``result`` (True = whole line written)."""

    def __init__(self, result: bool = True):
        self.result = result
        self.calls: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.calls.append(text)
        return self.result


def _async_recorder(store: list):
    async def _fn(text: str) -> None:
        store.append(text)
    return _fn


class _FakeRegistry:
    """Fake engagement registry WITH advance_interaction_state (Task-7 contract
    pinned now)."""

    def __init__(self):
        self.advances: list[tuple[str, str]] = []

    async def advance_interaction_state(self, eng_id: str, kind: str) -> None:
        self.advances.append((eng_id, kind))


class _FakeSequencer:
    """Records set_turn_reply_to targets (§3 reply-threading)."""

    def __init__(self):
        self.reply_targets: list = []

    def set_turn_reply_to(self, message_id):
        self.reply_targets.append(message_id)


def _make_spool(
    tmp_path, *, writer=None, notices=None, registry=None,
    is_turn_running=None, current_epoch=None, sequencer=None,
    spool_path=None,
):
    """Build an _InboundSpool with injectable primitives.

    ``notices`` collects ``(text, reply_to)`` tuples; the send always succeeds.
    Pass an ``notices`` object that is a ``_FlakyNotice`` to model send failure.
    """
    from drivers.claude_code_driver import _InboundSpool

    writer = writer if writer is not None else _FakeWriter(True)
    notices = notices if notices is not None else _RecordNotice()
    return _InboundSpool(
        engagement_id="eng1abc",
        spool_path=spool_path or str(tmp_path / ".inbound_spool.jsonl"),
        write_fifo=writer,
        send_notice=notices,
        is_turn_running=is_turn_running or (lambda: False),
        current_epoch=current_epoch or (lambda: None),
        registry=registry,
        sequencer=sequencer,
    )


class _RecordNotice:
    """A ``send_notice`` that records (text, reply_to) and always delivers."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple[str, "int | None"]] = []

    async def __call__(self, text, reply_to):
        self.calls.append((text, reply_to))
        return self.ok


class TestInboundSpool:
    async def test_message_then_spawn_delivers_round_trip(self, tmp_path):
        writer = _FakeWriter(True)
        s = _make_spool(tmp_path, writer=writer)
        await s.enqueue("hello", tg_message_id=5)   # reader unarmed → queued
        assert writer.calls == []
        # Durable: the envelope is on disk with state=queued.
        assert (tmp_path / ".inbound_spool.jsonl").exists()
        await s.on_spawn()                          # arm → deliver
        assert writer.calls == ["hello"]
        assert s.reader_ready is False              # disarmed after one message

    async def test_spool_recovery_reloads_queued_envelope(self, tmp_path):
        writer = _FakeWriter(True)
        s = _make_spool(tmp_path, writer=writer)
        await s.enqueue("survive me", tg_message_id=9)
        # New spool over the SAME file (Casa restart) reloads the envelope.
        s2 = _make_spool(tmp_path, writer=writer)
        assert len(s2._lane_members()) == 1
        await s2.on_spawn()
        assert writer.calls == ["survive me"]

    async def test_one_message_per_spawn(self, tmp_path):
        writer = _FakeWriter(True)
        s = _make_spool(tmp_path, writer=writer)
        await s.enqueue("a")
        await s.enqueue("b")
        await s.on_spawn()
        assert writer.calls == ["a"]                # exactly one per spawn
        await s.on_turn_start()                     # "a" consumed (turn ran)
        await s.on_spawn()
        assert writer.calls == ["a", "b"]

    async def test_failed_write_retains_and_redelivers(self, tmp_path):
        writer = _FakeWriter(False)                 # no reader — write fails
        s = _make_spool(tmp_path, writer=writer)
        await s.enqueue("a")
        await s.on_spawn()
        assert writer.calls == ["a"]                # attempted
        assert len(s._lane_members()) == 1          # retained, not dropped
        writer.result = True
        await s.on_spawn()
        assert writer.calls == ["a", "a"]

    async def test_consumed_only_on_turn_start_evidence(self, tmp_path):
        # delivered → NOT consumed until turn_start for the SAME epoch.
        epoch = {"v": 7}
        writer = _FakeWriter(True)
        s = _make_spool(
            tmp_path, writer=writer, current_epoch=lambda: epoch["v"])
        await s.enqueue("do it", tg_message_id=3)
        await s.on_spawn()
        env = s._envelopes[0]
        assert env.state == "delivered" and env.delivery_epoch == 7
        await s.on_turn_start()
        # #341: consumed with nothing owed ⇒ pruned (no longer deliverable,
        # never redelivered, spool does not grow without bound).
        assert env.state == "consumed"
        assert s._envelopes == []

    async def test_delivered_but_no_turn_start_redelivers_next_spawn(self, tmp_path):
        # Process died pre-turn_start ⇒ the delivered envelope reverts to
        # queued and redelivers on the next spawn (§3 redelivery-by-construction).
        epoch = {"v": 1}
        writer = _FakeWriter(True)
        s = _make_spool(
            tmp_path, writer=writer, current_epoch=lambda: epoch["v"])
        await s.enqueue("again")
        await s.on_spawn()
        assert writer.calls == ["again"]
        # No turn_start; a NEW spawn (new epoch) reverts + redelivers.
        epoch["v"] = 2
        await s.on_spawn()
        assert writer.calls == ["again", "again"]

    async def test_initial_prompt_no_state_transition(self, tmp_path):
        reg = _FakeRegistry()
        s = _make_spool(tmp_path, registry=reg)
        await s.enqueue("system prompt", is_initial=True)
        await s.on_spawn()
        assert reg.advances == []

    async def test_ordinary_message_advances_interaction_state(self, tmp_path):
        reg = _FakeRegistry()
        s = _make_spool(tmp_path, registry=reg)
        await s.enqueue("operator says hi")
        await s.on_spawn()
        assert reg.advances == [("eng1abc", "operator_turn")]

    async def test_disposition_queued_and_dropped_full(self, tmp_path):
        notices = _RecordNotice()
        s = _make_spool(tmp_path, notices=notices)
        for i in range(10):
            assert await s.enqueue(f"m{i}") == "queued"
        # 11th ordinary at cap → dropped_full with the existing notice.
        assert await s.enqueue("overflow", tg_message_id=99) == "dropped_full"
        assert len(s._lane_members()) == 10
        assert len(notices.calls) == 1
        assert notices.calls[0][1] == 99                # threaded to the message

    async def test_receipt_only_while_turn_running(self, tmp_path):
        notices = _RecordNotice()
        running = {"v": False}
        s = _make_spool(
            tmp_path, notices=notices, is_turn_running=lambda: running["v"])
        # Idle ⇒ not_required, no receipt.
        await s.enqueue("idle msg", tg_message_id=1)
        assert notices.calls == []
        assert s._envelopes[-1].receipt == "not_required"
        # Turn running ⇒ pending receipt, sent after the atomic write.
        running["v"] = True
        await s.enqueue("busy msg", tg_message_id=2)
        assert (RECEIPT, 2) in [(c[0], c[1]) for c in notices.calls]
        assert s._envelopes[-1].receipt == "sent"

    async def test_receipt_pre_send_crash_retries_at_touchpoint(self, tmp_path):
        # Receipt send fails at enqueue (stays pending) then succeeds at the
        # next touchpoint (turn end) — at-least-once with a durable tri-state.
        notices = _RecordNotice(ok=False)
        s = _make_spool(
            tmp_path, notices=notices, is_turn_running=lambda: True)
        await s.enqueue("busy", tg_message_id=4)
        assert s._envelopes[-1].receipt == "pending"    # not sent
        notices.ok = True
        await s.on_turn_end()                            # retry touchpoint
        assert s._envelopes and s._envelopes[-1].receipt == "sent"


class TestRedirectLane:
    async def test_redirect_detection_stop_and_prefix(self):
        from drivers.claude_code_driver import _is_redirect
        assert _is_redirect("STOP")
        assert _is_redirect("stop\ndo the other thing")
        assert _is_redirect("redirect: pivot now")
        assert _is_redirect("REDIRECT: pivot")
        assert not _is_redirect("please stop by later")
        assert not _is_redirect("continue as planned")

    async def test_redirect_delivered_with_prefix_ahead_of_ordinary(self, tmp_path):
        from drivers.claude_code_driver import _REDIRECT_PREFIX
        writer = _FakeWriter(True)
        s = _make_spool(tmp_path, writer=writer)
        await s.enqueue("ordinary work")
        await s.enqueue("STOP\nchange plans")           # redirect → priority lane
        await s.on_spawn()
        # Priority lane drains first, with the redirect prefix.
        assert writer.calls == [f"{_REDIRECT_PREFIX}\nSTOP\nchange plans"]
        await s.on_turn_start()                         # redirect consumed
        await s.on_spawn()
        assert writer.calls[-1] == "ordinary work"

    async def test_redirects_fifo_within_priority(self, tmp_path):
        from drivers.claude_code_driver import _REDIRECT_PREFIX
        writer = _FakeWriter(True)
        s = _make_spool(tmp_path, writer=writer)
        await s.enqueue("redirect: first")
        await s.enqueue("redirect: second")
        await s.on_spawn()
        await s.on_turn_start()
        await s.on_spawn()
        assert writer.calls == [
            f"{_REDIRECT_PREFIX}\nredirect: first",
            f"{_REDIRECT_PREFIX}\nredirect: second",
        ]

    async def test_redirect_evicts_newest_ordinary_with_notice(self, tmp_path):
        from drivers.claude_code_driver import _EVICTION_COPY
        notices = _RecordNotice()
        s = _make_spool(tmp_path, notices=notices)
        for i in range(10):
            await s.enqueue(f"m{i}", tg_message_id=100 + i)   # fill ordinary lane
        disp = await s.enqueue("STOP", tg_message_id=200)
        # Newest ordinary (m9, tg 109) evicted, threaded eviction notice.
        assert disp == "evicted_other(109)"
        assert (_EVICTION_COPY, 109) in notices.calls
        assert s._ordinary_count() == 9
        assert s._priority_count() == 1

    async def test_eviction_rollback_restores_the_actual_victim(self, tmp_path):
        """#324: the spool-write-failure rollback must un-mark the envelope it
        actually evicted — matching by (tg_message_id, notice) restored the
        wrong envelope when the victim's tg_message_id was None and an older
        None-id pending notice existed."""
        notices = _RecordNotice(ok=False)      # eviction notices fail → stay pending
        s = _make_spool(tmp_path, notices=notices)
        # Fill the ordinary lane; the newest (first victim) has no tg id.
        for i in range(9):
            await s.enqueue(f"m{i}", tg_message_id=100 + i)
        await s.enqueue("victim-1", tg_message_id=None)
        # First redirect evicts victim-1; its notice send fails, so it is
        # retained EARLY in the list with notice="pending", tg_message_id=None.
        assert await s.enqueue("STOP\nfirst") == "evicted_other(None)"
        v1 = next(e for e in s._envelopes if e.text == "victim-1")
        assert v1.notice == "pending" and v1.tg_message_id is None

        # Refill the freed ordinary slot with a SECOND None-id message (newer).
        await s.enqueue("victim-2", tg_message_id=None)

        # Second redirect evicts victim-2, but the spool write fails.
        real_persist = s._persist
        def _failing_persist():
            _failing_persist.calls += 1
            if _failing_persist.calls == 1:
                raise OSError("disk full")
            return real_persist()
        _failing_persist.calls = 0
        s._persist = _failing_persist
        assert await s.enqueue("STOP\nsecond") == "error"

        v2 = next(e for e in s._envelopes if e.text == "victim-2")
        assert v2.notice == "none", "the evicted victim was not rolled back"
        assert v1.notice == "pending", (
            "rollback un-marked an earlier envelope still owed its notice")

    async def test_priority_cap_drops_with_notice(self, tmp_path):
        from drivers.claude_code_driver import _PRIORITY_CAP_COPY
        notices = _RecordNotice()
        s = _make_spool(tmp_path, notices=notices)
        for i in range(3):
            await s.enqueue(f"redirect: r{i}")
        disp = await s.enqueue("STOP\none too many", tg_message_id=77)
        assert disp == "dropped_full"
        assert (_PRIORITY_CAP_COPY, 77) in notices.calls
        assert s._priority_count() == 3

    async def test_dropped_full_notice_is_durable_and_retries(self, tmp_path):
        """F8: a capacity-DROP notice (priority-full / ordinary-full) with a
        FAILED send must NOT be fire-and-forget — it survives as a durable
        pending notice so ``has_pending()`` stays True and it retries at the next
        touchpoint (the eviction-notice lane, now extended to drops)."""
        from drivers.claude_code_driver import (
            _ORDINARY_FULL_COPY, _PRIORITY_CAP_COPY,
        )
        notices = _RecordNotice(ok=False)               # every send fails
        s = _make_spool(tmp_path, notices=notices)
        # Ordinary lane full → drop with a failed send.
        for i in range(10):
            await s.enqueue(f"m{i}", tg_message_id=100 + i)
        assert await s.enqueue("overflow", tg_message_id=99) == "dropped_full"
        # The drop notice is durable + pending (send failed) → retries.
        assert s.has_pending() is True
        assert (_ORDINARY_FULL_COPY, 99) in notices.calls
        # Priority lane full → drop with a failed send, also durable.
        for i in range(3):
            await s.enqueue(f"redirect: r{i}")
        assert await s.enqueue("STOP\nextra", tg_message_id=77) == "dropped_full"
        assert s.has_pending() is True
        # Retry at a touchpoint once the transport recovers → both notices send.
        notices.ok = True
        await s.on_turn_end()
        assert (_PRIORITY_CAP_COPY, 77) in notices.calls
        assert s.has_pending() is False

    async def test_notice_first_suppression(self, tmp_path):
        # An evicted envelope holding BOTH pending receipt and pending notice
        # sends ONLY the notice; its receipt flips to not_required.
        from drivers.claude_code_driver import _EVICTION_COPY, _RECEIPT_COPY
        notices = _RecordNotice(ok=False)               # receipts fail at enqueue
        s = _make_spool(
            tmp_path, notices=notices, is_turn_running=lambda: True)
        for i in range(10):
            await s.enqueue(f"m{i}", tg_message_id=100 + i)
        # The newest ordinary now has receipt=pending (send failed).
        victim = max(s._lane_members(), key=lambda e: e.seq)
        victim_tg = victim.tg_message_id
        assert victim.receipt == "pending"
        notices.ok = True
        pre = len(notices.calls)
        await s.enqueue("STOP", tg_message_id=200)       # evicts victim
        after = notices.calls[pre:]
        # After eviction, the victim gets ONLY its eviction notice (no receipt),
        # and its receipt flips to not_required (notice-first suppression).
        victim_sends = [c for c in after if c[1] == victim_tg]
        assert victim_sends == [(_EVICTION_COPY, victim_tg)]
        # No receipt was ever sent to the victim (notice-first suppression), and
        # the fully-settled evicted envelope is pruned from the spool.
        assert all(c[0] != _RECEIPT_COPY for c in after if c[1] == victim_tg)
        assert not any(e.tg_message_id == victim_tg for e in s._envelopes)


# Convenience aliases for the exact copies (asserted above).
from drivers.claude_code_driver import _RECEIPT_COPY as RECEIPT  # noqa: E402

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


class TestSpawnBackgroundTasksInbound:
    async def test_relay_task_always_spawned(self, tmp_path):
        """The always-on TopicStreamRelay task is registered regardless of
        LOG_LEVEL (it is the operator's live window, not a debug aid)."""
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        lg = logging.getLogger("subprocess_cli")
        old = lg.level
        lg.setLevel(logging.WARNING)          # DEBUG raw-log relay OFF
        try:
            drv._spawn_background_tasks(rec)
            tasks = drv._tasks[rec.id]
            names = [t.get_name() for t in tasks]
            assert any(n.startswith("topic_relay:") for n in names), names
            assert rec.id in drv._inbound       # queue wired
        finally:
            lg.setLevel(old)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_spool_recovery_redelivers_on_boot(self, tmp_path):
        """v0.79.0 (§3): boot replay calls _spawn_background_tasks DIRECTLY. A
        surviving spool file with an undelivered (queued) envelope is loaded and
        redelivered on the next spawn — no zero-with-uncertainty 'please resend'
        notice is ever posted."""
        from drivers.claude_code_driver import (
            ClaudeCodeDriver, _RECEIPT_COPY,
        )

        sender = AsyncMock()
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
        )
        from drivers.workspace import inbound_spool_path, provision_control_dir

        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        provision_control_dir(rec.id)
        # A surviving spool with one queued envelope + one consumed envelope
        # still owing a receipt (pending). Task 4: lives in the control dir.
        import json
        lines = [
            json.dumps({
                "text": "not delivered yet", "tg_message_id": 11,
                "priority": False, "receipt": "not_required", "notice": "none",
                "enqueued_at": 1.0, "delivery_epoch": None, "state": "queued",
                "seq": 0, "is_initial": False,
            }),
            json.dumps({
                "text": "consumed but owes a receipt", "tg_message_id": 12,
                "priority": False, "receipt": "pending", "notice": "none",
                "enqueued_at": 2.0, "delivery_epoch": 5, "state": "consumed",
                "seq": 1, "is_initial": False,
            }),
        ]
        import pathlib as _pathlib
        _pathlib.Path(inbound_spool_path(rec.id)).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        try:
            drv._spawn_background_tasks(rec)
            await asyncio.sleep(0.1)            # let recover() run
            spool = drv._inbound[rec.id]
            # The queued envelope survived; the pending receipt was retried.
            assert any(e.text == "not delivered yet" and e.state == "queued"
                       for e in spool._envelopes)
            receipt_posts = [
                c for c in sender.await_args_list
                if _RECEIPT_COPY in c.args
            ]
            assert receipt_posts, "pending receipt should retry on boot recovery"
            # NO 'please resend' zero-uncertainty notice.
            assert not any(
                "please resend" in str(c.args) or "may not have been delivered"
                in str(c.args) for c in sender.await_args_list
            )
        finally:
            for t in drv._tasks.get(rec.id, []):
                t.cancel()
            await asyncio.gather(
                *drv._tasks.get(rec.id, []), return_exceptions=True)


class TestAbnormalExitCorrelation:
    async def test_abnormal_exit_correlates_epoch_stderr(self, tmp_path, caplog):
        """r5-B2: spawn(1) then spawn(2) with no intervening result → the
        driver reads the UNIQUE .stderr.1.log and WARNs its tail."""
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        from drivers.workspace import provision_control_dir, stderr_path

        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        provision_control_dir(rec.id)
        # Task 4: the per-epoch stderr ring lives in the control dir.
        __import__("pathlib").Path(stderr_path(rec.id, 1)).write_text(
            "traceback: boom on epoch 1\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            await drv._on_stream_event(rec, "spawn", {"epoch": 1})
            await drv._on_stream_event(rec, "spawn", {"epoch": 2})

        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert any("boom on epoch 1" in m for m in warnings), warnings
        assert any("epoch 1" in m and "abnormal" in m for m in warnings)

    async def test_abnormal_exit_pruned_epoch_diagnostics_unavailable(
        self, tmp_path, caplog,
    ):
        """r5-B2: an abnormal-exit lookup for an epoch whose .stderr.<e>.log
        was pruned → 'diagnostics unavailable', never misattributed."""
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        # No .stderr.1.log — epoch 1 was pruned after advancing >= 4 epochs.
        with caplog.at_level(logging.WARNING):
            await drv._on_stream_event(rec, "spawn", {"epoch": 1})
            await drv._on_stream_event(rec, "spawn", {"epoch": 5})

        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert any("diagnostics unavailable" in m for m in warnings), warnings

    async def test_result_clears_abnormal_flag(self, tmp_path, caplog):
        """A normal result between spawns clears the pending epoch — the next
        spawn is NOT flagged abnormal."""
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        (tmp_path / rec.id).mkdir()
        with caplog.at_level(logging.WARNING):
            await drv._on_stream_event(rec, "spawn", {"epoch": 1})
            await drv._on_stream_event(rec, "result", {"subtype": "success"})
            await drv._on_stream_event(rec, "spawn", {"epoch": 2})
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert not any("abnormal" in m for m in warnings), warnings


class _FakeInteractionRegistry:
    """Registry stand-in for the mutating_tool seam — carries a single
    record whose ``interaction_state`` the test sets directly, plus a
    tracked ``set_interaction_violated``."""

    def __init__(self, interaction_state: str):
        from types import SimpleNamespace
        self._rec = SimpleNamespace(interaction_state=interaction_state)
        self.violated: list[str] = []

    def get(self, eng_id: str):
        return self._rec

    async def set_interaction_violated(self, eng_id: str) -> None:
        self.violated.append(eng_id)


class TestMutatingToolViolationSeam:
    """W2/Sol B9 (Task 7): ``_on_stream_event``'s ``mutating_tool`` branch —
    activated now that the registry carries ``interaction_state`` +
    ``set_interaction_violated``. The invariant that a ``reply``/``ask``/
    ``set_progress`` control tool-use never REACHES this branch as a
    ``mutating_tool`` event is enforced upstream by
    ``topic_stream.is_mutating_tooluse`` (pinned by
    ``test_topic_stream.py::test_is_mutating_tooluse_allowlist``) — the
    driver trusts the event kind it's handed and does not re-inspect the
    tool name.
    """

    async def test_mutating_tool_while_awaiting_operator_notifies_and_flags(
        self, tmp_path,
    ):
        from drivers.claude_code_driver import ClaudeCodeDriver

        sender = AsyncMock()
        reg = _FakeInteractionRegistry("awaiting_operator")
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
            registry=reg,
        )
        rec = _make_record()

        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Bash"})

        sender.assert_awaited_once()
        assert sender.await_args.args[0] == rec.topic_id
        assert reg.violated == [rec.id]

    async def test_mutating_tool_notice_fires_only_once_per_engagement(
        self, tmp_path,
    ):
        """A second mutating_tool event during the SAME awaiting_operator
        window must not re-post the notice (per-engagement guard)."""
        from drivers.claude_code_driver import ClaudeCodeDriver

        sender = AsyncMock()
        reg = _FakeInteractionRegistry("awaiting_operator")
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
            registry=reg,
        )
        rec = _make_record()

        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Bash"})
        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Write"})

        assert sender.await_count == 1

    async def test_mutating_tool_while_authorized_does_not_flag(self, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver

        sender = AsyncMock()
        reg = _FakeInteractionRegistry("authorized")
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
            registry=reg,
        )
        rec = _make_record()

        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Bash"})

        sender.assert_not_awaited()
        assert reg.violated == []

    async def test_mutating_tool_with_non_interaction_required_state_does_not_flag(
        self, tmp_path,
    ):
        """Default ("") interaction_state — most engagements aren't
        interaction-required at all — never flags."""
        from drivers.claude_code_driver import ClaudeCodeDriver

        sender = AsyncMock()
        reg = _FakeInteractionRegistry("")
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
            registry=reg,
        )
        rec = _make_record()

        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Bash"})

        sender.assert_not_awaited()
        assert reg.violated == []

    async def test_mutating_tool_without_registry_is_noop(self, tmp_path):
        """No registry wired (e.g. a unit test) — the seam must not raise."""
        from drivers.claude_code_driver import ClaudeCodeDriver

        sender = AsyncMock()
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
        )
        rec = _make_record()

        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Bash"})

        sender.assert_not_awaited()

    async def test_cancel_clears_violation_notified_guard(
        self, monkeypatch, tmp_path,
    ):
        """cancel() drops the per-engagement notified flag — bounded growth,
        matches the other per-engagement dict pops."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        async def fake_stop(*, engagement_id):
            pass

        async def fake_cau():
            pass

        monkeypatch.setattr(s6_rc, "stop_service", fake_stop)
        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc"))
        (tmp_path / "svc").mkdir()

        sender = AsyncMock()
        reg = _FakeInteractionRegistry("awaiting_operator")
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "eng"),
            send_to_topic=sender, casa_framework_mcp_url="x",
            registry=reg,
        )
        rec = _make_record()

        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Bash"})
        assert rec.id in drv._violation_notified

        await drv.cancel(rec)
        assert rec.id not in drv._violation_notified

    async def test_mutating_tool_notice_post_failure_retries_next_frame(
        self, tmp_path,
    ):
        """B4 (Sol r1): a transient failure of the notice POST must not
        permanently consume the once-guard — the next mutating_tool frame
        retries and the notice lands exactly once (one SUCCESSFUL notice)."""
        from drivers.claude_code_driver import ClaudeCodeDriver

        sender = AsyncMock(side_effect=[RuntimeError("net down"), 123])
        reg = _FakeInteractionRegistry("awaiting_operator")
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
            registry=reg,
        )
        rec = _make_record()

        # Frame 1: notice post raises (swallowed, guard NOT consumed).
        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Bash"})
        # Frame 2: retried, this time it succeeds.
        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Write"})

        assert sender.await_count == 2  # retried after the transient failure
        assert rec.id in drv._violation_notified  # eventually marked
        # Flag persisted once (independent of the notice-post failure).
        assert reg.violated == [rec.id]

    async def test_mutating_tool_flag_failure_retries_next_frame(
        self, tmp_path,
    ):
        """B4 (Sol r1): a transient failure of set_interaction_violated must
        retry on the next frame while the notice still posts at-most-once."""
        from drivers.claude_code_driver import ClaudeCodeDriver

        class _FlakyViolatedRegistry(_FakeInteractionRegistry):
            def __init__(self, state, fail_times):
                super().__init__(state)
                self._fail_times = fail_times

            async def set_interaction_violated(self, eng_id: str) -> None:
                if self._fail_times > 0:
                    self._fail_times -= 1
                    raise RuntimeError("db down")
                self.violated.append(eng_id)

        sender = AsyncMock()
        reg = _FlakyViolatedRegistry("awaiting_operator", fail_times=1)
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
            registry=reg,
        )
        rec = _make_record()

        # Frame 1: notice ok, flag raises (swallowed, flag-guard NOT consumed).
        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Bash"})
        # Frame 2: notice already posted (not re-sent); flag retries, succeeds.
        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Write"})

        assert sender.await_count == 1  # exactly one visible notice
        assert reg.violated == [rec.id]  # flag eventually persisted once


class TestCancelBypassesQueue:
    async def test_cancel_immediate_while_busy(self, monkeypatch, tmp_path):
        """/cancel drops any messages still waiting for a spawn — it never
        flushes the inbound queue."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        async def fake_stop(*, engagement_id):
            pass
        async def fake_cau():
            pass
        monkeypatch.setattr(s6_rc, "stop_service", fake_stop)
        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(
            s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc"))
        (tmp_path / "svc").mkdir()

        from drivers.workspace import provision_control_dir

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        (tmp_path / rec.id).mkdir()
        provision_control_dir(rec.id)  # Task 4: spool persist needs the control dir

        # Wire the spool; enqueue while unarmed (busy — no spawn yet).
        drv._spawn_background_tasks(rec)
        writer = _FakeWriter(True)
        drv._inbound[rec.id]._write_fifo = writer      # observe FIFO writes
        await drv.send_user_turn(rec, "queued but never delivered")
        assert len(drv._inbound[rec.id]._lane_members()) == 1

        await drv.cancel(rec)

        # Spool state torn down; the message was NEVER written to the FIFO.
        assert rec.id not in drv._inbound
        assert rec.id not in drv._reply_texts
        assert rec.id not in drv._epoch_pending
        assert rec.id not in drv._turn_running
        assert writer.calls == []


class TestSpoolThreadingAndSeam:
    async def test_delivery_sets_sequencer_reply_target(self, tmp_path):
        """§3 reply-threading: delivering an envelope sets the turn's reply
        target to the operator's Telegram message id."""
        seq = _FakeSequencer()
        writer = _FakeWriter(True)
        s = _make_spool(tmp_path, writer=writer, sequencer=seq)
        await s.enqueue("hello", tg_message_id=4242)
        await s.on_spawn()
        assert writer.calls == ["hello"]
        assert seq.reply_targets == [4242]

    async def test_advance_high_water_seals_narration(self, tmp_path):
        """§3 T1 seam: an inbound operator message advances the topic high-water
        and SEALS open narration."""
        from drivers.claude_code_driver import ClaudeCodeDriver

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(return_value=7),
            edit_topic_message=AsyncMock(return_value=True),
            casa_framework_mcp_url="x",
        )
        rec = _make_record()
        seq = drv._ensure_sequencer(rec)
        await seq.open_narration("mid-turn narration")
        assert seq.narration_msg_id is not None
        await drv.advance_topic_high_water_for_inbound(rec.id, 99)
        assert seq.narration_msg_id is None          # sealed
        assert seq.high_water == 99


class TestTerminalSpoolDrainAndReconcile:
    def _write_spool(self, engagement_id, *, receipt="pending", notice="none", tg=12):
        """Task 4: the spool lives in the control dir, keyed by engagement id
        (not the workspace path callers used to pass)."""
        import json
        import pathlib as _pathlib
        from drivers.workspace import inbound_spool_path, provision_control_dir

        provision_control_dir(engagement_id)
        line = json.dumps({
            "text": "owes a receipt", "tg_message_id": tg,
            "priority": False, "receipt": receipt, "notice": notice,
            "enqueued_at": 1.0, "delivery_epoch": 5, "state": "consumed",
            "seq": 0, "is_initial": False,
        })
        _pathlib.Path(inbound_spool_path(engagement_id)).write_text(
            line + "\n", encoding="utf-8")

    async def test_drain_inbound_spool_flushes_pending_receipt(self, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver, _RECEIPT_COPY

        sender = AsyncMock(return_value=1)
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
        )
        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir(parents=True, exist_ok=True)
        self._write_spool(rec.id)
        drv._spawn_background_tasks(rec)
        # Cancel the recover task so it doesn't also drain (isolate the drain).
        for t in drv._tasks.get(rec.id, []):
            t.cancel()
        await asyncio.gather(*drv._tasks.get(rec.id, []), return_exceptions=True)
        # Re-load a fresh spool over the (still pending) file and drain it.
        drv._spawn_background_tasks(rec)
        for t in drv._tasks.get(rec.id, []):
            t.cancel()
        await asyncio.gather(*drv._tasks.get(rec.id, []), return_exceptions=True)
        await drv.drain_inbound_spool(rec)
        assert any(_RECEIPT_COPY in c.args for c in sender.await_args_list)

    async def test_reconcile_terminal_spool_posts_when_topic_exists(self, tmp_path):
        """terminal-commit→kill→boot-drain: a terminal spool with a pending
        receipt is drained to the (existing) topic on boot reconciliation."""
        import pathlib as _pathlib
        from drivers.claude_code_driver import ClaudeCodeDriver, _RECEIPT_COPY
        from drivers.workspace import inbound_spool_path

        sender = AsyncMock(return_value=1)
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
        )
        rec = _make_record()                          # topic_id = 999
        self._write_spool(rec.id)
        await drv.reconcile_terminal_spool(rec)
        posts = [c for c in sender.await_args_list if _RECEIPT_COPY in c.args]
        assert posts and posts[0].args[0] == rec.topic_id
        # Settled + pruned on disk (no pending left).
        remaining = _pathlib.Path(inbound_spool_path(rec.id)).read_text()
        assert '"receipt": "pending"' not in remaining

    async def test_reconcile_terminal_spool_warn_drops_when_topic_gone(
        self, tmp_path,
    ):
        import pathlib as _pathlib
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers.workspace import inbound_spool_path
        from engagement_registry import EngagementRecord

        sender = AsyncMock()
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
        )
        rec = EngagementRecord(
            id="deadbeefdeadbeef", kind="executor", role_or_type="hello-driver",
            driver="claude_code", status="completed", topic_id=None,
            started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
            completed_at=1.0, sdk_session_id=None,
            origin={"channel": "telegram"}, task="t",
        )
        self._write_spool(rec.id)
        await drv.reconcile_terminal_spool(rec)
        # Topic gone → WARN-drop, nothing sent, pending settled so it won't retry.
        assert sender.await_count == 0
        remaining = _pathlib.Path(inbound_spool_path(rec.id)).read_text()
        assert '"receipt": "pending"' not in remaining

    async def test_drain_failure_then_restart_retries(self, tmp_path):
        """drain-failure→restart→retry: a send that fails at drain leaves the
        receipt pending; a later reconcile (restart) retries and succeeds."""
        from drivers.claude_code_driver import ClaudeCodeDriver, _RECEIPT_COPY

        import pathlib as _pathlib
        from drivers.workspace import inbound_spool_path

        # First send raises (drain fails), later sends succeed.
        sender = AsyncMock(side_effect=[RuntimeError("telegram down"), 1, 1])
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
        )
        rec = _make_record()
        self._write_spool(rec.id)
        await drv.reconcile_terminal_spool(rec)       # send fails → still pending
        remaining = _pathlib.Path(inbound_spool_path(rec.id)).read_text()
        assert '"receipt": "pending"' in remaining
        await drv.reconcile_terminal_spool(rec)       # restart → retry, succeeds
        assert any(_RECEIPT_COPY in c.args for c in sender.await_args_list)
        remaining = _pathlib.Path(inbound_spool_path(rec.id)).read_text()
        assert '"receipt": "pending"' not in remaining


# ---------------------------------------------------------------------------
# v0.79.0 (§4) — ask-lifecycle spool + driver seams
# ---------------------------------------------------------------------------


class TestAskLifecycleSeams:
    async def test_generation_and_unread_depth_track_enqueue(self, tmp_path):
        from drivers.claude_code_driver import _InboundSpool

        s = _InboundSpool(
            engagement_id="e", spool_path=str(tmp_path / "s.jsonl"),
            write_fifo=_FakeWriter(True), send_notice=_RecordNotice(),
        )
        assert s.generation() == 0 and s.unread_depth() == 0
        await s.enqueue("hi", tg_message_id=1)
        assert s.generation() == 1
        assert s.unread_depth() == 1  # queued, not yet delivered
        await s.enqueue("again", tg_message_id=2)
        assert s.generation() == 2 and s.unread_depth() == 2

    async def test_supersede_fires_on_operator_message_not_initial(self, tmp_path):
        from drivers.claude_code_driver import _InboundSpool

        fired = {"n": 0}

        async def _supersede():
            fired["n"] += 1

        s = _InboundSpool(
            engagement_id="e", spool_path=str(tmp_path / "s.jsonl"),
            write_fifo=_FakeWriter(False), send_notice=_RecordNotice(),
            supersede_pending_asks=_supersede,
        )
        await s.enqueue("task", tg_message_id=1, is_initial=True)
        assert fired["n"] == 0  # initial task never supersedes an ask
        await s.enqueue("real operator msg", tg_message_id=2)
        assert fired["n"] == 1

    async def test_anchor_settle_threads_delivery_to_anchor(self, tmp_path):
        from drivers.claude_code_driver import _InboundSpool

        seq = _FakeSequencer()

        async def _settle(op_mid):
            return 8001  # the anchor's tg_message_id

        s = _InboundSpool(
            engagement_id="e", spool_path=str(tmp_path / "s.jsonl"),
            write_fifo=_FakeWriter(True), send_notice=_RecordNotice(),
            sequencer=seq, settle_anchor_on_delivery=_settle,
        )
        await s.enqueue("my answer", tg_message_id=42)
        await s.on_spawn()  # arms + pumps → delivers
        # Threaded to the ANCHOR (8001), not the operator's own message (42).
        assert seq.reply_targets[-1] == 8001

    async def test_boot_reconcile_settles_open_questions(self, tmp_path, monkeypatch):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "claude_code", "t", {}, topic_id=999)
        # A button question and a free-text anchor both left open across a restart.
        n1 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n1, 7001, text="Q1: Proceed?",
                                    kind="button")
        n2 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n2, 7002, text="Q2: DB name?",
                                    kind="anchor")

        edits: list = []

        async def _edit(topic_id, message_id, text, *, clear_keyboard=False):
            edits.append((message_id, text, clear_keyboard))
            return True

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://x",
            edit_topic_message=_edit,
            registry=reg,
        )
        await drv.reconcile_open_questions(rec)

        # BOTH settled: expired copy + keyboard cleared; ledger emptied;
        # next_question_number preserved (never rewound).
        assert len(edits) == 2
        assert all(e[2] is True for e in edits)  # clear_keyboard
        assert all(e[1].endswith("⌛ expired — answer by text below") for e in edits)
        assert reg.open_question_numbers(rec.id) == []
        assert rec.next_question_number == 3

    async def test_reconcile_settles_stale_despite_concurrent_live_ask(
        self, tmp_path, monkeypatch,
    ):
        """Review I1: reconcile settles the ATTACH-TIME snapshot (prior-process)
        UNCONDITIONALLY. A fresh live in-scope ask registered concurrently (a
        NEW numbered entry + a live broker record) must NOT suppress settling the
        genuinely-stale prior-process keyboard."""
        import verdict_broker
        from verdict_broker import VerdictBroker
        from drivers.claude_code_driver import ClaudeCodeDriver
        from engagement_registry import EngagementRegistry

        fresh = VerdictBroker()
        monkeypatch.setattr(verdict_broker, "BROKER", fresh)

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "claude_code", "t", {}, topic_id=999)
        # A PRIOR-PROCESS stale question (the attach-time snapshot).
        n1 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n1, 7001, text="Q1: Proceed?",
                                    kind="button")
        snapshot = list(rec.open_questions)

        # A fresh SAME-PROCESS ask registers CONCURRENTLY: a new numbered ledger
        # entry AND a live broker record for the same scope.
        n2 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n2, 8002, text="Q2: Which region?",
                                    kind="button")
        live_req, _created = fresh.register(
            namespace="engagement_ask", scope=rec.id, request_id="live-rid",
            timeout_s=300.0)

        edits: list = []

        async def _edit(topic_id, message_id, text, *, clear_keyboard=False):
            edits.append((message_id, text, clear_keyboard))
            return True

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="http://x",
            edit_topic_message=_edit, registry=reg,
        )
        # Reconcile the attach-time snapshot (the stale entry only).
        await drv.reconcile_open_questions(rec, snapshot)

        # The stale entry SETTLED despite the live ask.
        assert edits == [(7001, "Q1: Proceed?\n⌛ expired — answer by text below",
                          True)]
        # The fresh ask's entry is untouched and its broker request stays live.
        assert reg.open_question_numbers(rec.id) == [n2]
        assert fresh.pending(namespace="engagement_ask", scope=rec.id) == [
            "live-rid"]
        assert not live_req._future.done()

    async def test_set_reply_anchor_sets_sequencer_one_shot(self, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://x",
        )
        seq = _FakeSequencer()
        drv._sequencers["eng"] = seq
        drv.set_engagement_reply_anchor("eng", 5555)
        assert seq.reply_targets == [5555]

    async def test_reconcile_preserves_ledger_on_unconfirmed_edit(
        self, tmp_path,
    ):
        """W-R1 (Sol r2-2): a transiently-failing settle edit (returns False)
        during boot reconciliation must retry EXACTLY 3× (0.5→1→2 backoff via an
        injected clock) and leave the ledger entry INTACT for the next boot."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "claude_code", "t", {}, topic_id=999)
        n1 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n1, 7001, text="Q1: Proceed?",
                                    kind="button")

        attempts = {"n": 0}

        async def _edit(topic_id, message_id, text, *, clear_keyboard=False):
            attempts["n"] += 1
            return False  # transient failure every attempt

        sleeps: list[float] = []

        async def _sleep(d):
            sleeps.append(d)

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="http://x",
            edit_topic_message=_edit, registry=reg,
        )
        drv._sleep = _sleep
        await drv.reconcile_open_questions(rec)

        assert attempts["n"] == 3            # exactly 3 bounded attempts
        assert sleeps == [0.5, 1.0, 2.0]     # 0.5→1→2 backoff
        # Ledger entry PRESERVED (NOT closed on a failed edit).
        assert reg.open_question_numbers(rec.id) == [n1]

    async def test_reconcile_closes_when_edit_confirmed_on_retry_two(
        self, tmp_path,
    ):
        """W-R1: edit confirmed on the SECOND attempt → ledger closed once."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "claude_code", "t", {}, topic_id=999)
        n1 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n1, 7001, text="Q1: Proceed?",
                                    kind="button")

        attempts = {"n": 0}

        async def _edit(topic_id, message_id, text, *, clear_keyboard=False):
            attempts["n"] += 1
            return attempts["n"] >= 2  # fail 1, confirm on 2

        sleeps: list[float] = []

        async def _sleep(d):
            sleeps.append(d)

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="http://x",
            edit_topic_message=_edit, registry=reg,
        )
        drv._sleep = _sleep
        await drv.reconcile_open_questions(rec)

        assert attempts["n"] == 2
        assert sleeps == [0.5]
        assert reg.open_question_numbers(rec.id) == []  # closed once

    async def test_boot_reconcile_refreshes_summary_open_questions(
        self, tmp_path,
    ):
        """F1 (Sol diff gate): boot reconciliation closes open-question ledger
        entries; the pinned summary's open-questions line must be REFRESHED to
        reflect the post-reconcile set (pre-fix the summary went stale — the
        close path never touched it)."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers.summary_controller import (
            STATUS_WAITING_REPLY, SummaryController,
        )
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "claude_code", "t", {}, topic_id=999)
        n1 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n1, 7001, text="Q1: A?",
                                    kind="button")
        n2 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n2, 7002, text="Q2: B?",
                                    kind="button")
        # Reconcile ONLY Q1 (attach-time snapshot); Q2 stays open.
        snapshot = [q for q in rec.open_questions if q.get("n") == n1]

        async def _edit(topic_id, message_id, text, *, clear_keyboard=False):
            return True

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="http://x",
            edit_topic_message=_edit, registry=reg,
        )

        class _SumSeq:
            def __init__(self):
                self.edits: list = []

            @asynccontextmanager
            async def serialized(self):
                yield

            async def edit_summary(self, mid, text):
                self.edits.append((mid, text))
                return "applied"

        sseq = _SumSeq()
        ctrl = SummaryController(
            engagement_id=rec.id, sequencer=sseq, goal_line="do X",
            open_question_numbers=lambda: reg.open_question_numbers(rec.id),
            message_id=8888,
        )
        ctrl._status = STATUS_WAITING_REPLY
        # As if the summary currently shows BOTH questions.
        ctrl._last_rendered = "stale text showing Q1 and Q2"
        drv._summaries[rec.id] = ctrl

        await drv.reconcile_open_questions(rec, snapshot)

        assert reg.open_question_numbers(rec.id) == [n2]
        assert sseq.edits, "summary was not refreshed after reconcile"
        last = sseq.edits[-1][1]
        assert f"Q{n2}" in last and f"Q{n1}" not in last

    async def test_reconcile_preserves_ledger_when_edit_primitive_absent(
        self, tmp_path,
    ):
        """F3/R1 (Sol diff gate): a MESSAGE-BACKED open-question entry must NOT
        be closed when NO edit primitive exists (``edit_topic_message is None``)
        — the settle cannot be confirmed, so the entry is PRESERVED for a later
        reconciliation (fail-CLOSED, not fail-open)."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "claude_code", "t", {}, topic_id=999)
        n1 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n1, 7001, text="Q1: Proceed?",
                                    kind="button")

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="http://x",
            edit_topic_message=None, registry=reg,
        )
        await drv.reconcile_open_questions(rec)

        # Message-backed entry + no edit primitive → UNCONFIRMED → PRESERVED.
        assert reg.open_question_numbers(rec.id) == [n1]

    async def test_anchor_settle_preserves_ledger_when_edit_primitive_absent(
        self, tmp_path,
    ):
        """F3/R1 (Sol diff gate): the anchor settle path likewise must NOT close
        a message-backed anchor when ``edit_topic_message is None`` — preserved
        (fail-closed), while STILL returning the anchor mid so the operator's
        answer threads correctly."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "claude_code", "t", {}, topic_id=999)
        n1 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n1, 8001, text="Q1: DB name?",
                                    kind="anchor")

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="http://x",
            edit_topic_message=None, registry=reg,
        )
        amid = await drv._settle_open_anchor(rec, operator_msg_id=42)

        assert amid == 8001                              # still threads
        assert reg.open_question_numbers(rec.id) == [n1]  # PRESERVED

    async def test_anchor_settle_preserves_ledger_on_unconfirmed_edit(
        self, tmp_path,
    ):
        """W-R1: a transiently-failing anchor settle edit must retry 3× and
        leave the anchor's ledger entry INTACT (still recoverable at next boot),
        while STILL returning the anchor mid so the turn threads correctly."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "claude_code", "t", {}, topic_id=999)
        n1 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n1, 8001, text="Q1: DB name?",
                                    kind="anchor")

        attempts = {"n": 0}

        async def _edit(topic_id, message_id, text, *, clear_keyboard=False):
            attempts["n"] += 1
            return False  # transient failure every attempt

        sleeps: list[float] = []

        async def _sleep(d):
            sleeps.append(d)

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="http://x",
            edit_topic_message=_edit, registry=reg,
        )
        drv._sleep = _sleep
        amid = await drv._settle_open_anchor(rec, operator_msg_id=42)

        assert amid == 8001                  # still threads to the anchor
        assert attempts["n"] == 3            # exactly 3 bounded attempts
        assert sleeps == [0.5, 1.0, 2.0]
        # Ledger entry PRESERVED (NOT closed on a failed edit).
        assert reg.open_question_numbers(rec.id) == [n1]


class TestBootSummary:
    """v0.79.0 (§5): the pinned live summary is posted + persisted BEFORE the
    subprocess starts; a post failure aborts the launch."""

    def _mock_s6(self, monkeypatch, tmp_path, order):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        _patch_uid_drop_ok(monkeypatch)

        async def fake_cau():
            return None

        async def fake_start_kw(*, engagement_id):
            order.append("start_service")

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_kw)
        monkeypatch.setattr(
            s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()
        monkeypatch.setattr(
            ClaudeCodeDriver, "_spawn_background_tasks",
            lambda self, engagement: None)

        async def _noop_write(self, engagement, text):
            return None
        monkeypatch.setattr(ClaudeCodeDriver, "_write_to_fifo", _noop_write)

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo (Linux-only)")
    async def test_summary_posted_and_persisted_before_start_service(
        self, monkeypatch, tmp_path,
    ):
        from drivers.claude_code_driver import ClaudeCodeDriver

        order: list[str] = []
        self._mock_s6(monkeypatch, tmp_path, order)

        sent: list[tuple[int, str]] = []

        async def send(topic_id, text, **kw):
            order.append("summary_post")
            sent.append((topic_id, text))
            return 4242

        class FakeReg:
            def __init__(self):
                self.persisted = None

            async def set_summary_message_id(self, eid, mid):
                self.persisted = (eid, mid)
        reg = FakeReg()

        defn = _make_defn(tmp_path)
        rec = _make_record(allocated_uid=200005)
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=send,
            casa_framework_mcp_url="http://x",
            registry=reg,
        )
        (tmp_path / "engagements").mkdir()

        await drv.start(rec, prompt="p", options=defn)

        assert order.index("summary_post") < order.index("start_service")
        assert rec.summary_message_id == 4242
        assert reg.persisted == (rec.id, 4242)
        assert "⚙️ working" in sent[0][1]

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo (Linux-only)")
    async def test_summary_post_failure_aborts_launch(self, monkeypatch, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver

        order: list[str] = []
        self._mock_s6(monkeypatch, tmp_path, order)

        async def boom(topic_id, text, **kw):
            raise RuntimeError("telegram down")

        defn = _make_defn(tmp_path)
        rec = _make_record(allocated_uid=200005)
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=boom,
            casa_framework_mcp_url="http://x",
        )
        (tmp_path / "engagements").mkdir()

        with pytest.raises(RuntimeError):
            await drv.start(rec, prompt="p", options=defn)
        assert "start_service" not in order  # subprocess never started

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo (Linux-only)")
    async def test_summary_post_none_id_aborts_launch(self, monkeypatch, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver

        order: list[str] = []
        self._mock_s6(monkeypatch, tmp_path, order)

        async def send(topic_id, text, **kw):
            return None

        defn = _make_defn(tmp_path)
        rec = _make_record(allocated_uid=200005)
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=send,
            casa_framework_mcp_url="http://x",
        )
        (tmp_path / "engagements").mkdir()

        with pytest.raises(RuntimeError):
            await drv.start(rec, prompt="p", options=defn)
        assert "start_service" not in order


class TestSummaryStreamWiring:
    """v0.79.0 (§5): _on_stream_event drives the summary controller."""

    async def _driver_with_summary(self, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver

        edits: list[tuple[int, str]] = []

        async def edit(topic_id, mid, text):
            edits.append((mid, text))
            return True

        class FakeReg:
            def __init__(self):
                self.rev = 0
                self.open: list[int] = []

            async def allocate_summary_revision(self, eid):
                r = self.rev
                self.rev += 1
                return r

            def open_question_numbers(self, eid):
                return list(self.open)
        reg = FakeReg()

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(return_value=1),
            casa_framework_mcp_url="x",
            edit_topic_message=edit,
            registry=reg,
        )
        rec = _make_record()
        rec.summary_message_id = 500
        ctrl = drv._ensure_summary(rec)
        ctrl.adopt_message_id(500)
        return drv, rec, ctrl, edits, reg

    async def test_turn_start_working_result_waiting(self, tmp_path):
        from drivers.summary_controller import (
            STATUS_WAITING_REPLY, STATUS_WORKING,
        )
        drv, rec, ctrl, edits, reg = await self._driver_with_summary(tmp_path)
        await drv._on_stream_event(rec, "turn_start", {})
        assert ctrl._status == STATUS_WORKING
        await drv._on_stream_event(rec, "result", {"subtype": "success"})
        assert ctrl._status == STATUS_WAITING_REPLY
        assert any("waiting for your reply" in t for _m, t in edits)
        ctrl.shutdown()

    async def test_status_transition_warns_and_drops_on_alloc_failure(
        self, tmp_path, caplog,
    ):
        """T4 F-1 (review): allocate_summary_revision raising must not be
        silent — it now logs a WARNING — and the status transition itself is
        STILL dropped (submit_status no-ops on revision=None; no fallback
        revision is synthesized)."""
        from drivers.summary_controller import STATUS_WORKING
        drv, rec, ctrl, edits, reg = await self._driver_with_summary(tmp_path)

        async def boom(eid):
            raise RuntimeError("allocator unavailable")
        reg.allocate_summary_revision = boom

        prior_status, prior_rev = ctrl._status, ctrl._status_rev
        with caplog.at_level("WARNING", logger="drivers.claude_code_driver"):
            await drv._summary_status_transition(rec.id, STATUS_WORKING)

        # Drop semantics preserved — no fallback revision, no state change.
        assert ctrl._status == prior_status
        assert ctrl._status_rev == prior_rev
        assert any(
            "allocate_summary_revision failed" in r.message
            for r in caplog.records
        )
        ctrl.shutdown()

    async def test_tool_use_updates_activity_and_plan(self, tmp_path):
        drv, rec, ctrl, edits, reg = await self._driver_with_summary(tmp_path)
        await drv._on_stream_event(rec, "turn_start", {})
        await drv._on_stream_event(
            rec, "tool_use", {"tool": "Bash", "input": {"command": "ls"}})
        assert ctrl._activity == "running commands"
        await drv._on_stream_event(
            rec, "tool_use",
            {"tool": "TodoWrite", "input": {"todos": [
                {"content": "a", "status": "completed"},
                {"content": "b", "status": "in_progress"},
            ]}},
        )
        assert ctrl._plan_total == 2 and ctrl._plan_done == 1
        ctrl.shutdown()

    async def test_control_tool_ignored_for_activity(self, tmp_path):
        drv, rec, ctrl, edits, reg = await self._driver_with_summary(tmp_path)
        await drv._on_stream_event(rec, "turn_start", {})
        await drv._on_stream_event(
            rec, "tool_use",
            {"tool": "mcp__casa-engagement-channel__ask", "input": {}})
        assert ctrl._activity is None
        ctrl.shutdown()

    async def test_finalize_summary_terminal_absolute(self, tmp_path):
        from drivers.summary_controller import STATUS_COMPLETED
        drv, rec, ctrl, edits, reg = await self._driver_with_summary(tmp_path)
        await drv._on_stream_event(rec, "turn_start", {})
        await drv.finalize_summary(rec, "completed")
        assert ctrl._status == STATUS_COMPLETED
        # Terminal absolute: a later turn_start cannot revert it.
        await drv._on_stream_event(rec, "turn_start", {})
        assert ctrl._status == STATUS_COMPLETED
        assert ctrl._tick_task is None  # tick cancelled at finalize


class TestF7AdoptSummaryOnAttach:
    """v0.79.0 (§5, F7): a LEGACY pre-v0.79 ACTIVE engagement replayed at boot
    (summary_message_id is None) gets a summary posted + persisted on attach —
    §5's invariant (no running engagement without a summary) must hold without
    depending on N150 currently having none active."""

    async def test_adopts_summary_for_legacy_record(self, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "claude_code", "t", {}, topic_id=42)
        assert rec.summary_message_id is None            # legacy: no summary yet

        sender = AsyncMock(return_value=555)
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x", registry=reg)

        await drv.adopt_summary_if_missing(rec)

        # Posted + persisted (both in-memory and durably in the registry).
        sender.assert_awaited()
        assert rec.summary_message_id == 555
        assert reg.get(rec.id).summary_message_id == 555

    async def test_noop_when_summary_already_present(self, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "claude_code", "t", {}, topic_id=42)
        await reg.set_summary_message_id(rec.id, 900)
        rec.summary_message_id = 900

        sender = AsyncMock(return_value=555)
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x", registry=reg)
        await drv.adopt_summary_if_missing(rec)
        sender.assert_not_awaited()                      # already has one
        assert rec.summary_message_id == 900


class TestF2ViolationNoticeThroughSequencer:
    """F2 (Sol r2): the interaction-violation notice is a PLATFORM notice — with
    a live sequencer it MUST route through post_platform_notice (seals open
    narration + posts below it under the one lock), never a direct send_to_topic
    around the writer."""

    async def test_notice_routes_through_sequencer_and_seals(self, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from channels.output_sequencer import OutputSequencer, SEALED

        sent_direct = AsyncMock()
        reg = _FakeInteractionRegistry("awaiting_operator")
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path), send_to_topic=sent_direct,
            casa_framework_mcp_url="x", registry=reg)
        rec = _make_record()

        posts: list = []

        async def _send(topic, text, reply_to=None):
            posts.append((topic, text))
            return 500 + len(posts)

        async def _edit(topic, mid, text):
            return True

        seq = OutputSequencer(
            engagement_id=rec.id, topic_id=rec.topic_id,
            send_message=_send, edit_message=_edit)
        drv._sequencers[rec.id] = seq
        nar = await seq.open_narration("live narration")

        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Bash"})

        # Notice posted THROUGH the sequencer, which SEALED open narration.
        assert any("waiting for your reply" in t for _, t in posts)
        assert seq.narration_msg_id is None
        assert await seq.edit_narration_if_latest(nar, "late") == SEALED
        # NOT a direct send_to_topic around the writer.
        sent_direct.assert_not_awaited()
        assert reg.violated == [rec.id]
        assert rec.id in drv._violation_notified

    async def test_notice_falls_back_to_direct_send_without_sequencer(self, tmp_path):
        """No live sequencer ⇒ the pre-v0.79 direct send is still used."""
        from drivers.claude_code_driver import ClaudeCodeDriver

        sent_direct = AsyncMock()
        reg = _FakeInteractionRegistry("awaiting_operator")
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path), send_to_topic=sent_direct,
            casa_framework_mcp_url="x", registry=reg)
        rec = _make_record()
        await drv._on_stream_event(rec, "mutating_tool", {"tool": "Bash"})
        sent_direct.assert_awaited_once()
        assert rec.id in drv._violation_notified


@pytest.mark.skipif(sys.platform == "win32", reason="mkfifo Linux-only")
class TestNoReaderNoticeThroughSequencer:
    """F3 (Sol r3): the FIFO no-reader ('not accepting input') notice is a
    PLATFORM notice — on an abnormal respawn with a live sequencer it MUST route
    through post_platform_notice (seals open narration + posts under the one
    lock), never a direct send around the writer."""

    async def test_no_reader_notice_routes_through_sequencer_and_seals(
        self, tmp_path,
    ):
        import os
        from drivers.claude_code_driver import ClaudeCodeDriver
        from channels.output_sequencer import OutputSequencer, SEALED

        sent_direct = AsyncMock()
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path), send_to_topic=sent_direct,
            casa_framework_mcp_url="x")
        from drivers.workspace import fifo_path, provision_control_dir

        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        provision_control_dir(rec.id)
        os.mkfifo(fifo_path(rec.id))  # exists, but NO reader ⇒ deadline hit

        posts: list = []

        async def _send(topic, text, reply_to=None):
            posts.append((topic, text))
            return 700 + len(posts)

        async def _edit(topic, mid, text):
            return True

        seq = OutputSequencer(
            engagement_id=rec.id, topic_id=rec.topic_id,
            send_message=_send, edit_message=_edit)
        drv._sequencers[rec.id] = seq
        nar = await seq.open_narration("live narration")

        ok = await asyncio.wait_for(
            drv._write_to_fifo(rec, "hello", timeout_s=0.3, poll_s=0.05),
            timeout=5.0,
        )
        assert ok is False  # turn retained for the next spawn

        # Notice posted THROUGH the sequencer, which SEALED open narration.
        assert any("isn't accepting input" in t for _, t in posts)
        assert seq.narration_msg_id is None
        assert await seq.edit_narration_if_latest(nar, "late") == SEALED
        # NOT a direct send around the writer.
        sent_direct.assert_not_awaited()

    async def test_no_reader_notice_falls_back_to_direct_send_without_sequencer(
        self, tmp_path,
    ):
        """No live sequencer ⇒ the pre-v0.79 direct send is still used."""
        import os
        from drivers.claude_code_driver import ClaudeCodeDriver

        sent_direct = AsyncMock()
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path), send_to_topic=sent_direct,
            casa_framework_mcp_url="x")
        from drivers.workspace import fifo_path, provision_control_dir

        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        provision_control_dir(rec.id)
        os.mkfifo(fifo_path(rec.id))

        ok = await asyncio.wait_for(
            drv._write_to_fifo(rec, "hello", timeout_s=0.3, poll_s=0.05),
            timeout=5.0,
        )
        assert ok is False
        sent_direct.assert_awaited_once()


class TestF3FinalizeDrainsToCompletionBlock:
    """F3 (Sol r2): finalize_completion_post must DRAIN the relay to the
    emit_completion block (wait for its consumption debt) BEFORE posting the
    completion text — so lagging prior-frame narration can't post below it."""

    async def test_completion_waits_for_debt_consumption(self, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from channels.output_sequencer import (
            OutputSequencer, EMIT_COMPLETION_TOOL, projection_hash,
        )

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path), send_to_topic=AsyncMock(),
            casa_framework_mcp_url="x")
        rec = _make_record()

        posts: list = []

        async def _send(topic, text, reply_to=None):
            posts.append((topic, text))
            return 700 + len(posts)

        async def _edit(topic, mid, text):
            return True

        seq = OutputSequencer(
            engagement_id=rec.id, topic_id=rec.topic_id,
            send_message=_send, edit_message=_edit, slot_hold_s=5.0)
        drv._sequencers[rec.id] = seq
        # Register the emit_completion consumption debt (as tools.py does before
        # calling _finalize_engagement).
        drv.register_completion_consumption(rec.id, {"summary": "done"})

        fin = asyncio.ensure_future(
            drv.finalize_completion_post(rec, "Engagement completed."))
        await asyncio.sleep(0.05)
        # Blocked draining to the completion block; completion NOT posted yet.
        assert not fin.done()
        assert posts == []

        # Relay reaches the emit_completion block → debt consumed → drain wakes.
        phash = projection_hash(EMIT_COMPLETION_TOOL, {"summary": "done"})
        assert await seq.post_for_block(
            EMIT_COMPLETION_TOOL, phash) == "debt_consumed"
        assert await asyncio.wait_for(fin, timeout=1.0) is True
        assert any("completed" in t for _, t in posts)


class TestF7StrictSetterRollback:
    """F7 (Sol r2): _post_initial_summary must NOT pre-assign the in-memory
    summary_message_id before the strict setter snapshots it — otherwise a
    forced persist failure rolls back to the NEW id and leaves it in memory."""

    async def test_persist_failure_rolls_back_in_memory_to_prior(
        self, tmp_path, monkeypatch,
    ):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "claude_code", "t", {}, topic_id=42)
        assert rec.summary_message_id is None            # true prior value

        async def _boom(*a, **k):
            raise RuntimeError("disk full")
        # Force the strict persist to fail AFTER the setter mutates in memory.
        monkeypatch.setattr(reg, "_write_tombstone_locked", _boom)

        async def _send(topic, text, **kw):
            return 555

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path), send_to_topic=_send,
            casa_framework_mcp_url="x", registry=reg)

        with pytest.raises(RuntimeError):
            await drv._post_initial_summary(rec)

        # The in-memory record (SAME object the registry holds) rolled back to
        # the TRUE prior value (None) — NOT the new 555 the old pre-assign left.
        assert rec.summary_message_id is None
        assert reg.get(rec.id).summary_message_id is None


def test_all_drivers_accept_tg_message_id_kwarg():
    """v0.79.2: telegram passes tg_message_id to send_user_turn
    unconditionally — every driver must accept it (in_casa ignored it and
    TypeErrored on operator topic messages; caught by the e2e E-block)."""
    import inspect
    import drivers.in_casa_driver as icd
    import drivers.claude_code_driver as ccd
    for cls_mod, name in ((icd, "InCasaDriver"), (ccd, "ClaudeCodeDriver")):
        cls = getattr(cls_mod, name, None)
        if cls is None:  # fall back: find the class exposing send_user_turn
            cands = [v for v in vars(cls_mod).values()
                     if inspect.isclass(v) and hasattr(v, "send_user_turn")]
            assert cands, f"no driver class in {cls_mod.__name__}"
            cls = cands[0]
        sig = inspect.signature(cls.send_user_turn)
        assert "tg_message_id" in sig.parameters, (
            f"{cls.__name__}.send_user_turn must accept tg_message_id")


# --- G4 (v0.96.0): completion-gate driver surface ---------------------------


class TestCompletionGateDriverSurface:
    def _driver(self):
        import drivers.claude_code_driver as ccd
        d = ccd.ClaudeCodeDriver.__new__(ccd.ClaudeCodeDriver)
        d._inbound = {}
        d._completion_refusals = {}
        d._inbound_reservations = {}
        d._inbound_command_reservations = {}
        return d

    def test_message_reservation_projection_bookkeeping(self):
        """#664: a command-born reservation counts toward the veto total but
        never toward the disclosure projection; each kind's release
        decrements its own counter, clamped, and a mismatch under-discloses
        rather than fabricating a lost message."""
        d = self._driver()
        d.reserve_inbound("e1")                     # ordinary message
        d.reserve_inbound("e1", command=True)       # /cancel in processing
        assert d.inbound_reservations("e1") == 2    # veto sees both
        assert d.inbound_message_reservations("e1") == 1
        d.release_inbound_reservation("e1", command=True)
        assert d.inbound_reservations("e1") == 1
        assert d.inbound_message_reservations("e1") == 1
        d.release_inbound_reservation("e1")
        assert d.inbound_reservations("e1") == 0
        assert d.inbound_message_reservations("e1") == 0
        assert "e1" not in d._inbound_reservations          # no leak
        assert "e1" not in d._inbound_command_reservations  # no leak
        # over-release of the command kind clamps; the projection stays
        # non-negative even if the command counter outlives the total
        d.reserve_inbound("e2", command=True)
        d.release_inbound_reservation("e2")   # WRONG kind released (bug)
        assert d.inbound_reservations("e2") == 0
        assert d.inbound_message_reservations("e2") == 0  # clamped, not -1

    def test_reservation_counter_lifecycle(self):
        d = self._driver()
        assert d.inbound_reservations("e1") == 0
        d.reserve_inbound("e1"); d.reserve_inbound("e1")
        assert d.inbound_reservations("e1") == 2
        d.release_inbound_reservation("e1")
        assert d.inbound_reservations("e1") == 1
        d.release_inbound_reservation("e1")
        d.release_inbound_reservation("e1")   # floor at 0, no negative
        assert d.inbound_reservations("e1") == 0
        assert "e1" not in d._inbound_reservations   # no leak

    def test_completion_refusal_counter(self):
        d = self._driver()
        assert d.record_completion_refusal("e1") == 1
        assert d.record_completion_refusal("e1") == 2
        assert d.record_completion_refusal("e2") == 1

    def test_unread_texts_empty_without_spool(self):
        d = self._driver()
        assert d.inbound_unread_texts("nope") == []


    def test_spool_unread_texts_population(self, tmp_path):
        """G4 D4: texts mirror unread_depth's population — queued,
        non-initial only (delivered/initial excluded)."""
        import asyncio
        import drivers.claude_code_driver as ccd

        async def run():
            spool = ccd._InboundSpool(
                engagement_id="e" * 32,
                spool_path=str(tmp_path / "spool.jsonl"),
                write_fifo=AsyncMock(return_value=False),
                send_notice=AsyncMock(),
                is_turn_running=lambda: True,
                current_epoch=lambda: 1,
            )
            await spool.enqueue("the initial prompt", is_initial=True)
            await spool.enqueue("first operator message")
            await spool.enqueue("second operator message")
            texts = spool.unread_texts()
            depth = spool.unread_depth()
            assert len(texts) == depth
            assert "the initial prompt" not in texts
            assert any("first operator" in t for t in texts)
        asyncio.run(run())


# ---------------------------------------------------------------------------
# #341 — driver durability: completion-escalation epoch guard + single-flight,
# same-epoch spawn replay, spool crash-durability, notice/receipt fail-safety.
# ---------------------------------------------------------------------------


def _make_driver(tmp_path, *, send_to_topic=None):
    from drivers.claude_code_driver import ClaudeCodeDriver
    return ClaudeCodeDriver(
        engagements_root=str(tmp_path / "engagements"),
        send_to_topic=send_to_topic or AsyncMock(),
        casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
    )


class TestCompletionEscalationEpochGuard:
    """#341 (high): the completion-gate force-end used to run with
    ``expected_epoch=None`` (epoch guard disabled) and no single-flight
    state — a delayed escalation could kill a freshly respawned turn that
    had already consumed a queued operator envelope."""

    async def test_escalation_passes_current_spawn_epoch(self, tmp_path):
        drv = _make_driver(tmp_path)
        rec = _make_record()
        recorded: dict = {}

        async def fake_ftb(**kw):
            recorded.update(kw)
            return True

        drv._force_turn_boundary = fake_ftb
        drv._epoch_pending[rec.id] = 7
        await drv.force_completion_turn_boundary(rec)
        assert recorded.get("expected_epoch") == 7

    async def test_escalation_without_live_epoch_does_not_signal(self, tmp_path):
        """No pending spawn epoch ⇒ no live turn is attributable — the kill
        must not fire blind (it could only hit a NEWER generation)."""
        drv = _make_driver(tmp_path)
        rec = _make_record()
        recorded: dict = {}

        async def fake_ftb(**kw):
            recorded.update(kw)
            return True

        drv._force_turn_boundary = fake_ftb
        await drv.force_completion_turn_boundary(rec)
        assert recorded == {}

    async def test_escalation_single_flight_with_inflight_kill(self, tmp_path):
        """A kill already in flight (completion- or away-triggered) owns the
        boundary; a racing second emit_completion must not signal again."""
        drv = _make_driver(tmp_path)
        rec = _make_record()
        recorded: dict = {}

        async def fake_ftb(**kw):
            recorded.update(kw)
            return True

        drv._force_turn_boundary = fake_ftb
        drv._epoch_pending[rec.id] = 7
        hold = asyncio.create_task(asyncio.sleep(30))
        drv._force_tasks[rec.id] = hold
        try:
            await drv.force_completion_turn_boundary(rec)
            assert recorded == {}
        finally:
            hold.cancel()


class TestSpawnReplaySameEpoch:
    """#341: TopicStream delivers spawn at-least-once. A replayed spawn frame
    carrying the SAME epoch is not a new generation — treating it as one
    logged a phantom abnormal exit and requeued the already-delivered FIFO
    envelope (duplicate operator turn on the next real spawn)."""

    async def test_same_epoch_replay_keeps_delivered_envelope(
        self, tmp_path, caplog,
    ):
        import logging

        drv = _make_driver(tmp_path)
        drv._consume_reanchor = AsyncMock()
        rec = _make_record()
        epoch = {"v": None}
        writer = _FakeWriter(True)
        s = _make_spool(tmp_path, writer=writer,
                        current_epoch=lambda: epoch["v"])
        drv._inbound[rec.id] = s
        await s.enqueue("msg one")

        epoch["v"] = 5
        await drv._on_stream_event(rec, "spawn", {"epoch": 5})
        assert writer.calls == ["msg one"]

        with caplog.at_level(logging.INFO):
            await drv._on_stream_event(rec, "spawn", {"epoch": 5})
        assert writer.calls == ["msg one"], "replay redelivered the envelope"
        assert s._envelopes[0].state == "delivered"
        assert "exited without a result frame" not in caplog.text


class TestSpoolCrashDurability:
    """#341: spool persistence gaps — missing directory fsync after the
    rename, a capacity-drop notice lost when its first write fails, and
    consumed envelopes retained forever (unbounded spool growth)."""

    async def test_persist_fsyncs_spool_directory(self, tmp_path, monkeypatch):
        import stat as stat_mod
        import drivers.claude_code_driver as ccd

        dir_synced: list[int] = []
        real_fsync = os.fsync

        def spy_fsync(fd):
            if stat_mod.S_ISDIR(os.fstat(fd).st_mode):
                dir_synced.append(fd)
            real_fsync(fd)

        monkeypatch.setattr(ccd.os, "fsync", spy_fsync)
        s = _make_spool(tmp_path)
        await s.enqueue("durable?")
        assert dir_synced, "spool directory never fsynced after os.replace"

    async def test_drop_notice_survives_failed_first_persist(self, tmp_path):
        """A capacity-drop notice whose first spool write fails must still be
        retried to disk at the next touchpoint — otherwise a crash before any
        later successful send silently loses the owed notice."""
        notices = _RecordNotice(ok=False)          # send keeps failing
        s = _make_spool(tmp_path, notices=notices)
        for i in range(10):
            assert await s.enqueue(f"m{i}") == "queued"

        orig_persist = s._persist
        state = {"failed_once": False}

        def flaky_persist():
            if not state["failed_once"]:
                state["failed_once"] = True
                raise OSError("disk hiccup")
            orig_persist()

        s._persist = flaky_persist
        assert await s.enqueue("overflow") == "dropped_full"

        s2 = _make_spool(tmp_path)                 # crash + reload
        assert s2.has_pending(), (
            "capacity-drop notice was lost with the failed first persist"
        )

    async def test_consumed_envelope_pruned_from_spool_file(self, tmp_path):
        """A consumed envelope with nothing pending owes nothing — it must be
        pruned at the turn_start persist, not retained forever."""
        epoch = {"v": 1}
        s = _make_spool(tmp_path, current_epoch=lambda: epoch["v"])
        await s.enqueue("job")
        await s.on_spawn()
        await s.on_turn_start()

        s2 = _make_spool(tmp_path)                 # reload from disk
        assert s2._envelopes == []


class TestTerminalSpoolNoticeDeliveryEvidence:
    """#341: the terminal-reconciliation direct-send fallback ignored the
    returned message id — a ``None`` (Telegram unavailable at boot) was
    treated as delivered and the receipt dropped."""

    async def test_direct_send_returning_none_is_not_delivered(self, tmp_path):
        drv = _make_driver(
            tmp_path, send_to_topic=AsyncMock(return_value=None))
        rec = _make_record()
        ok = await drv._spool_send_notice(rec, "receipt text", None)
        assert ok is False

    async def test_direct_send_returning_mid_is_delivered(self, tmp_path):
        drv = _make_driver(
            tmp_path, send_to_topic=AsyncMock(return_value=123))
        rec = _make_record()
        ok = await drv._spool_send_notice(rec, "receipt text", None)
        assert ok is True


class TestCompletionEscalationInnerCancel:
    """Sol r8 (#341): the shared _force_tasks entry can be cancelled by its
    OWNER (operator re-engagement clears the away kill; teardown cancels) —
    shield() then raises CancelledError even though the emit_completion
    caller was never cancelled, and the tool handler (which catches only
    Exception) dies instead of returning its documented refusal. An
    inner-task cancel must return quietly; only genuine outer cancellation
    propagates."""

    async def test_inner_force_task_cancel_returns_quietly(self, tmp_path):
        drv = _make_driver(tmp_path)
        rec = _make_record()
        started = asyncio.Event()

        async def blocking_ftb(**kw):
            started.set()
            await asyncio.sleep(30)
            return True

        drv._force_turn_boundary = blocking_ftb
        drv._epoch_pending[rec.id] = 7

        outer = asyncio.create_task(drv.force_completion_turn_boundary(rec))
        await started.wait()
        drv._force_tasks[rec.id].cancel()      # owner cancels the kill
        await outer                            # must NOT raise CancelledError


class TestProceduralEpochRevalidation:
    """#215 (Sol diff-r1): provisioning re-reads the prompt and copies the
    doctrine AFTER the launch hashed them — an edit landing in that window
    must abort the launch rather than stamp the engagement with an epoch it
    did not run under."""

    def _mk(self, monkeypatch, tmp_path):
        from unittest.mock import AsyncMock
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        _patch_uid_drop_ok(monkeypatch)

        async def _noop():
            return None

        async def _noop_start(*, engagement_id):
            return None

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", _noop)
        monkeypatch.setattr(s6_rc, "start_service", _noop_start)
        monkeypatch.setattr(
            s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()
        monkeypatch.setattr(
            ClaudeCodeDriver, "_spawn_background_tasks",
            lambda self, engagement: None)

        async def _noop_write(self, engagement, text):
            return None
        monkeypatch.setattr(ClaudeCodeDriver, "_write_to_fifo", _noop_write)

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()
        return drv

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_epoch_mismatch_aborts_launch(self, monkeypatch, tmp_path):
        drv = self._mk(monkeypatch, tmp_path)
        defn = _make_defn(tmp_path)
        rec = _make_record(allocated_uid=200005)
        rec.procedural_epoch = "0" * 64   # stale: never matches the real bytes

        with pytest.raises(RuntimeError, match="materials changed"):
            await drv.start(rec, prompt="system prompt body", options=defn)
        # Bug-13 rollback: no half-provisioned workspace left behind.
        assert not (tmp_path / "engagements" / rec.id).exists()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_matching_epoch_launches(self, monkeypatch, tmp_path):
        from executor_epoch import compute_procedural_epoch

        drv = self._mk(monkeypatch, tmp_path)
        defn = _make_defn(tmp_path)
        rec = _make_record(allocated_uid=200005)
        with open(defn.prompt_template_path, encoding="utf-8") as fh:
            rec.procedural_epoch = compute_procedural_epoch(
                defn, prompt_template=fh.read())

        await drv.start(rec, prompt="system prompt body", options=defn)
        assert (tmp_path / "engagements" / rec.id / "CLAUDE.md").exists()


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="mkfifo Linux-only")
class TestTurnDeliveryAdmission:
    """#588 — the record must be `active` before the engagement's CLI can see
    the first byte of a turn.

    A casa-main restart leaves a queued-but-unconsumed operator turn behind.
    Boot reconcile rewrites the record `active→idle`, the respawned CLI blocks
    on its stdin FIFO, and the inbound spool pumps the turn straight in — with
    nothing on that path calling `update_user_turn`, which is what the ordinary
    Telegram arrival path uses. The turn then ran against an `idle` record, and
    the bridge grant-gate (active-only binding) refused every non-terminal casa
    tool it called as `tool_not_granted`, including `query_engager`, which the
    engagement holds. `emit_completion` is gate-exempt, so the turn still ended
    and nothing looked stuck.

    The admission is deliberately placed between the FIFO open (which succeeds
    only once the CLI is reading) and the first write (the first thing the CLI
    can observe), with NO suspension point in between. These tests pin that
    placement from both sides.
    """

    class _SpyRegistry:
        """`begin_turn_delivery` is a PLAIN FUNCTION returning a bool, not a
        coroutine — so if production code ever awaits the seam (which would
        reopen the very window it closes), every test here fails with
        `TypeError: object bool can't be used in an 'await' expression`."""

        def __init__(self, *, admit=True, on_call=None):
            self._admit = admit
            self._on_call = on_call
            self.calls: list[str] = []

        def begin_turn_delivery(self, engagement_id):
            self.calls.append(engagement_id)
            if self._on_call is not None:
                self._on_call()
            return self._admit

    def _driver(self, tmp_path, sent, registry=None):
        from drivers.claude_code_driver import ClaudeCodeDriver

        async def send(topic_id, text):
            sent.append((topic_id, text))
        return ClaudeCodeDriver(
            engagements_root=str(tmp_path), send_to_topic=send,
            casa_framework_mcp_url="http://unused", registry=registry,
        )

    @staticmethod
    def _fifo_with_reader(eng_id):
        """Return (fifo_path, reader_fd). A NON-BLOCKING reader fd, not a
        reader thread: the test can then ask 'has anything been written yet?'
        with no scheduler race at all."""
        import os

        from drivers.workspace import fifo_path, provision_control_dir
        provision_control_dir(eng_id)
        fifo = fifo_path(eng_id)
        os.mkfifo(fifo)
        return fifo, os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)

    @staticmethod
    def _drain(rfd):
        import os
        try:
            return os.read(rfd, 65536)
        except BlockingIOError:
            return b""

    async def test_admission_precedes_the_first_byte(self, tmp_path):
        """Red case A. The seam reads the FIFO from the other end while it
        runs: nothing may have been written yet. Moving the admission after
        the write makes this read return the turn, deterministically."""
        from types import SimpleNamespace

        rec = SimpleNamespace(id="eng-admit-order", topic_id=7)
        _fifo, rfd = self._fifo_with_reader(rec.id)
        seen_at_admission: list[bytes] = []
        reg = self._SpyRegistry(
            on_call=lambda: seen_at_admission.append(self._drain(rfd)))
        sent: list = []
        driver = self._driver(tmp_path, sent, registry=reg)

        ok = await asyncio.wait_for(
            driver._write_to_fifo(rec, "hi there", timeout_s=5.0), timeout=5.0)

        assert ok is True
        assert reg.calls == [rec.id]
        assert seen_at_admission == [b""], (
            "the CLI had already been handed bytes when the record was "
            f"admitted: {seen_at_admission!r}")
        assert self._drain(rfd) == b"hi there\n"

    async def test_no_reader_never_admits(self, tmp_path):
        """Red case B. The mirror of A: no reader means no delivery, so the
        record must NOT be made `active`. This is what kills the naive
        placement (admit at pump time, before the open): the 20 s ENXIO retry
        can expire with the turn never delivered, and a claude_code record is
        never idled again by the daily sweep."""
        import os
        from types import SimpleNamespace

        from drivers.workspace import fifo_path, provision_control_dir

        rec = SimpleNamespace(id="eng-admit-noreader", topic_id=7)
        provision_control_dir(rec.id)
        os.mkfifo(fifo_path(rec.id))
        reg = self._SpyRegistry()
        sent: list = []
        driver = self._driver(tmp_path, sent, registry=reg)

        ok = await asyncio.wait_for(
            driver._write_to_fifo(
                rec, "hi", timeout_s=1.0, poll_s=0.05, retained=True),
            timeout=5.0)

        assert ok is False
        assert reg.calls == []

    async def test_terminal_record_is_never_handed_a_turn(self, tmp_path):
        """A terminal record refuses delivery outright: `_finalize_engagement`
        commits the terminal status well before it tears the driver down, so a
        queued turn could otherwise be pumped into a still-live CLI and run
        against a cancelled engagement."""
        from types import SimpleNamespace

        rec = SimpleNamespace(id="eng-admit-terminal", topic_id=7)
        _fifo, rfd = self._fifo_with_reader(rec.id)
        reg = self._SpyRegistry(admit=False)
        sent: list = []
        driver = self._driver(tmp_path, sent, registry=reg)

        ok = await asyncio.wait_for(
            driver._write_to_fifo(rec, "run this", timeout_s=5.0), timeout=5.0)

        assert ok is False
        assert reg.calls == [rec.id]
        assert self._drain(rfd) == b"", "a terminal engagement was handed a turn"
        # Not the no-reader path: that notice promises an automatic retry, and
        # there is nothing to retry here.
        assert sent == []
        assert rec.id not in driver._last_turn_ts

    async def test_redelivery_after_restart_reactivates_the_record(
            self, tmp_path):
        """Red case D — the composition, with the real registry and the real
        spool. An engagement whose record boot reconcile left `idle`, with a
        turn still queued: arming the spool (what a respawned CLI's `spawn`
        control frame does) must deliver the turn AND leave the record
        `active`, so the grant-gate binds it."""
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "eng.json"), bus=None)
        rec = await reg.create(
            "executor", "plugin-developer", "claude_code", "t",
            {"channel": "telegram"}, None)
        _fifo, rfd = self._fifo_with_reader(rec.id)
        sent: list = []
        driver = self._driver(tmp_path, sent, registry=reg)
        (tmp_path / rec.id).mkdir(exist_ok=True)

        tasks = []
        try:
            driver._spawn_background_tasks(rec)
            tasks = list(driver._tasks[rec.id])
            spool = driver._inbound[rec.id]
            # A turn arrives and is spooled while no CLI is reading.
            await spool.enqueue("what is the status?")
            assert self._drain(rfd) == b""
            # casa-main restarts: boot reconcile idles the record.
            await reg.mark_idle(rec.id)
            assert reg.get(rec.id).status == "idle"
            # The respawned CLI's spawn frame arms the spool, which redelivers.
            await asyncio.wait_for(spool.on_spawn(), timeout=5.0)

            assert self._drain(rfd) == b"what is the status?\n"
            assert reg.get(rec.id).status == "active", (
                "the redelivered turn ran against an idle record — every "
                "non-terminal casa tool it calls is refused")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# #591 / #592 — the terminal question is not the ask question
#
# `unread_*` answers "is the operator still waiting to be answered?", for which
# excluding a `delivered` envelope is right (it may already have been read —
# Sol g4-r1-5). The TERMINAL question is "is there operator input in flight
# that no turn has taken up?", and for that the same exclusion is wrong: the
# completion gate could commit while a turn sat in the engagement's stdin FIFO,
# and the no-silent-loss annotation never mentioned it.
# ---------------------------------------------------------------------------


class TestInFlightIsADifferentQuestion:

    async def _delivered(self, tmp_path, text="operator message"):
        """A spool with one envelope in the `delivered` state (written into the
        FIFO, no turn_start evidence yet)."""
        writer = _FakeWriter(True)
        s = _make_spool(tmp_path, writer=writer, current_epoch=lambda: 7)
        await s.enqueue(text, tg_message_id=11)
        await s.on_spawn()                      # arms the reader → pumps
        assert writer.calls == [text], "envelope was not delivered"
        return s

    async def test_delivered_is_in_flight_and_not_unread(self, tmp_path):
        """The split itself. Red case: make `_in_flight` return [] and the
        completion gate is blind again exactly as #591 reported."""
        s = await self._delivered(tmp_path)

        assert s.unread_texts() == [], "the ask gate's answer must not change"
        assert s.unread_depth() == 0
        assert s.in_flight_texts() == ["operator message"]
        assert s.in_flight_blocking_depth(60.0) == 1

    async def test_turn_start_ends_the_in_flight_window(self, tmp_path):
        """`consumed` requires positive turn_start evidence — that, and only
        that, is what stops an envelope counting."""
        s = await self._delivered(tmp_path)
        await s.on_turn_start()

        assert s.in_flight_texts() == []
        assert s.in_flight_blocking_depth(60.0) == 0
        assert s.unread_texts() == []

    async def test_envelope_being_written_is_already_unread(self, tmp_path):
        """Why there is no third carrier, and why the first design's `_writing`
        marker was cut: throughout the awaited write the envelope is still
        `queued`, so `unread_*` already sees it. Counting it as in-flight too
        would report one operator message TWICE in the terminal annotation.

        This is also the whole of #592 for a COMPLETION: a payload larger than
        the pipe buffer suspends mid-write, and the gate is already closed over
        that suspension.
        """
        release = asyncio.Event()
        observed: dict = {}
        s = None

        async def _blocking_writer(text):
            observed["unread"] = s.unread_texts()
            observed["in_flight"] = s.in_flight_texts()
            await release.wait()
            return True

        s = _make_spool(tmp_path, writer=_blocking_writer)
        await s.enqueue("mid-write message")
        pump = asyncio.create_task(s.on_spawn())
        await asyncio.sleep(0)                  # let the writer start
        while "unread" not in observed:
            await asyncio.sleep(0)

        assert observed["unread"] == ["mid-write message"], (
            "a mid-write envelope must still be visible to the existing gate")
        assert observed["in_flight"] == [], (
            "a mid-write envelope counted twice — annotation would duplicate it")

        release.set()
        await asyncio.wait_for(pump, timeout=5)
        assert s.in_flight_texts() == ["mid-write message"]
        assert s.unread_texts() == []

    async def test_initial_prompt_is_never_in_flight(self, tmp_path):
        """The launch prompt is the task, not an unanswered operator message —
        excluded from both populations, as it already is from `unread_*`."""
        writer = _FakeWriter(True)
        s = _make_spool(tmp_path, writer=writer)
        await s.enqueue("the initial task", is_initial=True)
        await s.on_spawn()

        assert writer.calls == ["the initial task"]
        assert s.in_flight_texts() == []
        assert s.in_flight_blocking_depth(60.0) == 0

    async def test_a_respawn_never_drops_the_envelope_from_both_populations(
            self, tmp_path):
        """A respawn reverts `delivered → queued` and immediately re-pumps, so
        the envelope crosses between the two populations. What must hold is
        that it is always in exactly ONE of them — a gap would be the same
        silence #591 is about, one state later.

        Both outcomes of the redelivery are covered: the write succeeds (back
        to in-flight for the new epoch) and the write fails (retained as
        unread, which is what re-arms the D3 escalation).
        """
        s = await self._delivered(tmp_path)
        await s.on_spawn()                      # reverts, then redelivers
        assert s.in_flight_texts() == ["operator message"]
        assert s.unread_texts() == []

        failing = _FakeWriter(False)
        s2 = _make_spool(tmp_path, writer=failing,
                         spool_path=str(tmp_path / "s2.jsonl"))
        await s2.enqueue("operator message")
        await s2.on_spawn()                     # write refused → stays queued
        assert failing.calls == ["operator message"]
        assert s2.unread_texts() == ["operator message"]
        assert s2.in_flight_texts() == []

    async def test_stale_delivery_stops_vetoing_but_is_still_disclosed(
            self, tmp_path):
        """The fail-open bound. A turn that never starts must not make a
        successful completion impossible for the life of the engagement — but
        the operator is still told about the message.

        Red case: drop the age comparison and this fails, because the veto
        would then hold forever on a delivery nothing will ever consume.
        """
        s = await self._delivered(tmp_path)
        env = [e for e in s._envelopes if e.state == "delivered"][0]
        env.delivered_monotonic -= 3600.0        # an hour with no turn_start

        assert s.in_flight_blocking_depth(60.0) == 0, "veto must not be eternal"
        assert s.in_flight_texts() == ["operator message"], (
            "an expired veto must not become a silent loss")

    async def test_delivered_without_a_stamp_never_vetoes(self, tmp_path):
        """A `delivered` row read back from the spool file at boot carries no
        stamp (the field is deliberately not persisted). Unknown age reads as
        stale — annotate, never veto — because vetoing on an unknowable age is
        the wedge this bound exists to prevent."""
        s = await self._delivered(tmp_path)
        env = [e for e in s._envelopes if e.state == "delivered"][0]
        env.delivered_monotonic = None           # as _load() reconstructs it

        assert s.in_flight_blocking_depth(60.0) == 0
        assert s.in_flight_texts() == ["operator message"]

    async def test_stamp_is_not_persisted(self, tmp_path):
        """Pins the reason above, at the serialisation boundary."""
        import json
        s = await self._delivered(tmp_path)
        rows = [json.loads(line) for line in
                (tmp_path / ".inbound_spool.jsonl").read_text().splitlines()
                if line.strip()]

        assert rows, "spool file was never written"
        assert all("delivered_monotonic" not in r for r in rows)


class TestInFlightDriverSurface:
    def _driver(self):
        import drivers.claude_code_driver as ccd
        d = ccd.ClaudeCodeDriver.__new__(ccd.ClaudeCodeDriver)
        d._inbound = {}
        return d

    def test_no_spool_reads_as_empty(self):
        """Same fail-open shape as every other inbound accessor: an engagement
        with no spool has no opinion, it does not have a fabricated depth."""
        d = self._driver()
        assert d.inbound_in_flight_texts("nope") == []
        assert d.inbound_in_flight_blocking("nope") == 0


class TestLargeTurnIsWrittenWhole:
    """#592 — a turn larger than the pipe buffer used to be written in pieces,
    suspending between them. The suspension is a real scheduling point at which
    an ungated terminal transition can commit, and the remainder is then
    written to a record that is already terminal; nothing can revoke it,
    because closing the writer is itself an EOF the CLI acts on.

    So the fix removes the suspension instead of guarding it: grow the pipe so
    the payload fits and the first `os.write` completes it.
    """

    @staticmethod
    def _fifo_with_reader(eng_id):
        import os

        from drivers.workspace import fifo_path, provision_control_dir
        provision_control_dir(eng_id)
        fifo = fifo_path(eng_id)
        os.mkfifo(fifo)
        # A reader that never reads: the payload has to fit in the pipe itself.
        return fifo, os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)

    async def test_payload_larger_than_the_default_buffer_writes_in_full(
            self, tmp_path):
        """Red case: delete the `_grow_pipe_to_fit` call and this returns False
        — the write stalls at the 64 KiB default capacity and gives up at the
        deadline, having delivered a truncated prefix.
        """
        import fcntl
        import os
        from types import SimpleNamespace

        from drivers.claude_code_driver import ClaudeCodeDriver

        rec = SimpleNamespace(id="eng-big-turn", topic_id=7)
        _fifo, rfd = self._fifo_with_reader(rec.id)
        assert fcntl.fcntl(rfd, fcntl.F_GETPIPE_SZ) < 200 * 1024, (
            "test is void — this pipe already fits the payload by default")

        driver = ClaudeCodeDriver(
            engagements_root=str(tmp_path), send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://unused")
        payload = "x" * (200 * 1024)

        ok = await asyncio.wait_for(
            driver._write_to_fifo(rec, payload, timeout_s=2.0), timeout=10.0)

        assert ok is True
        assert os.read(rfd, 1024 * 1024) == (payload + "\n").encode()

    async def test_ordinary_turn_is_unaffected(self, tmp_path):
        """Mutation guard: the grow must be a no-op for a normal turn, not a
        capacity change on every write."""
        import fcntl
        from types import SimpleNamespace

        from drivers.claude_code_driver import ClaudeCodeDriver

        rec = SimpleNamespace(id="eng-small-turn", topic_id=7)
        _fifo, rfd = self._fifo_with_reader(rec.id)
        before = fcntl.fcntl(rfd, fcntl.F_GETPIPE_SZ)

        driver = ClaudeCodeDriver(
            engagements_root=str(tmp_path), send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://unused")
        assert await driver._write_to_fifo(rec, "hello", timeout_s=2.0) is True
        assert fcntl.fcntl(rfd, fcntl.F_GETPIPE_SZ) == before

    async def test_a_kernel_that_refuses_the_grow_costs_nothing(
            self, tmp_path, monkeypatch):
        """The grow is strictly best-effort: EPERM above pipe-max-size, EBUSY,
        a kernel without F_SETPIPE_SZ. A refusal must cost the delivery
        nothing.

        The REAL helper runs here — an earlier version of this test stubbed it
        out with a no-op, which proved only that a no-op is harmless and would
        have passed even if the helper's own exception handling regressed
        (Terra, diff review). `fcntl.fcntl` is made to raise instead, so the
        failure happens where a kernel refusal would.
        """
        import fcntl
        from types import SimpleNamespace

        import drivers.claude_code_driver as ccd

        def _refuse(*a, **k):
            raise OSError(1, "Operation not permitted")

        rec = SimpleNamespace(id="eng-grow-fails", topic_id=7)
        _fifo, rfd = self._fifo_with_reader(rec.id)
        monkeypatch.setattr(fcntl, "fcntl", _refuse)

        driver = ccd.ClaudeCodeDriver(
            engagements_root=str(tmp_path), send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://unused")
        assert await driver._write_to_fifo(rec, "small", timeout_s=2.0) is True
        monkeypatch.undo()
        assert self._drain_all(rfd) == b"small\n"

    @staticmethod
    def _drain_all(rfd):
        import os
        try:
            return os.read(rfd, 1024 * 1024)
        except BlockingIOError:
            return b""

    def test_grow_helper_never_raises(self):
        """Every failure path of the helper itself, on a fd that is not a pipe
        at all (EINVAL/ENOTTY territory) and on a closed one (EBADF)."""
        import os

        from drivers.claude_code_driver import _grow_pipe_to_fit

        r, w = os.open(os.devnull, os.O_RDONLY), os.open(os.devnull, os.O_WRONLY)
        try:
            _grow_pipe_to_fit(w, 1024 * 1024, "eng-not-a-pipe")
        finally:
            os.close(r); os.close(w)
        _grow_pipe_to_fit(w, 1024 * 1024, "eng-closed-fd")   # EBADF

    async def test_a_denied_grow_on_an_oversized_payload_degrades_not_breaks(
            self, tmp_path, monkeypatch):
        """Terra, diff review r2: the refusal test above uses a small payload,
        so it proves the exception handling and not the BEHAVIOUR when a
        kernel denies the grow for a payload that needs it.

        The contract for that case is explicit — fall back to exactly what the
        code did before #592: write what fits, keep trying to the deadline,
        report failure so the spool retains the envelope and redelivers it. The
        one thing it must not do is raise, hang past the deadline, or claim
        success over a truncated delivery.
        """
        import fcntl
        import time
        from types import SimpleNamespace

        import drivers.claude_code_driver as ccd

        real_fcntl = fcntl.fcntl

        def _deny_growth_only(fd, op, *a):
            if op == fcntl.F_SETPIPE_SZ:
                raise OSError(1, "Operation not permitted")
            return real_fcntl(fd, op, *a)

        rec = SimpleNamespace(id="eng-denied-grow", topic_id=7)
        _fifo, rfd = self._fifo_with_reader(rec.id)
        monkeypatch.setattr(fcntl, "fcntl", _deny_growth_only)

        driver = ccd.ClaudeCodeDriver(
            engagements_root=str(tmp_path), send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://unused")

        started = time.monotonic()
        ok = await asyncio.wait_for(
            driver._write_to_fifo(rec, "x" * (200 * 1024), timeout_s=1.0,
                                  poll_s=0.05),
            timeout=10.0)
        elapsed = time.monotonic() - started
        monkeypatch.undo()

        assert ok is False, (
            "a partially written turn must be reported as not delivered")
        assert elapsed < 5.0, "the deadline must still bound the write"


# ---------------------------------------------------------------------------
# #755 — the launch rollback is not cancellation-complete
# ---------------------------------------------------------------------------


class _RollbackProbe:
    """Records every synchronous rollback effect, by exact target, in order.

    ``TestStartRollback`` asserts paths are absent afterwards. Absence cannot
    tell "the removal ran" from "the removal ran twice", and it cannot see
    ORDER at all — Sol reproduced a mutant that recompiles BEFORE
    ``remove_service_dir`` and leaves the failed service in the live db while
    every path/count assertion still passes. So this probe keeps one ordered
    trace and the tests assert on indices in it.
    """

    def __init__(self, monkeypatch, *, svc_root, ws_path, ctl_path, uid,
                 outbox_path):
        import shutil as _real_shutil

        from drivers import claude_code_driver as ccd
        from drivers import s6_rc
        import plugin_outbox

        self.trace: list[tuple] = []
        self.svc_root = svc_root
        self.ws_path = ws_path
        self.ctl_path = ctl_path
        self.uid = uid
        self.outbox_path = outbox_path

        real_remove_dir = s6_rc.remove_service_dir

        def _remove_service_dir(*, svc_root, engagement_id):
            self.trace.append(("remove_service_dir", engagement_id))
            return real_remove_dir(svc_root=svc_root,
                                   engagement_id=engagement_id)

        monkeypatch.setattr(s6_rc, "remove_service_dir", _remove_service_dir)

        class _ShutilShim:
            """Only ``rmtree`` is observed; everything else delegates.

            Patched onto the DRIVER module's ``shutil`` name, never onto the
            shared ``shutil`` module object — a global patch would follow every
            other importer of it into the same test.
            """

            def __getattr__(_self, name):
                return getattr(_real_shutil, name)

            @staticmethod
            def rmtree(path, *a, **kw):
                self.trace.append(("rmtree", str(path)))
                return _real_shutil.rmtree(path, *a, **kw)

        monkeypatch.setattr(ccd, "shutil", _ShutilShim())

        def _prune_identity(u):
            self.trace.append(("prune_identity", u))

        monkeypatch.setattr(ccd, "prune_identity", _prune_identity)

        real_teardown = plugin_outbox.teardown_engagement_outbox

        def _teardown(u, *, root=None):
            self.trace.append(("teardown_outbox", u))
            return real_teardown(u, root=root)

        monkeypatch.setattr(plugin_outbox, "teardown_engagement_outbox",
                            _teardown)

    # -- the assertion vocabulary -------------------------------------------

    def counts(self) -> dict:
        return {
            "remove_service_dir": sum(
                1 for e in self.trace if e[0] == "remove_service_dir"),
            "rmtree_workspace": sum(
                1 for e in self.trace
                if e == ("rmtree", str(self.ws_path))),
            "rmtree_control": sum(
                1 for e in self.trace if e == ("rmtree", str(self.ctl_path))),
            "prune_identity": sum(
                1 for e in self.trace
                if e == ("prune_identity", self.uid)),
            "teardown_outbox": sum(
                1 for e in self.trace if e == ("teardown_outbox", self.uid)),
        }

    @staticmethod
    def each_once() -> dict:
        return {
            "remove_service_dir": 1,
            "rmtree_workspace": 1,
            "rmtree_control": 1,
            "prune_identity": 1,
            "teardown_outbox": 1,
        }

    def paths_absent(self) -> dict:
        return {
            "workspace": os.path.exists(self.ws_path),
            "control": os.path.exists(self.ctl_path),
            "outbox": os.path.isdir(self.outbox_path),
        }


async def _spin_until(predicate, *, what: str, limit: int = 2000):
    """Yield to the loop until ``predicate()`` — no sleeps, no wall clock.

    A bound on ITERATIONS is not an elapsed allowance: it fires when the loop
    has run ``limit`` times without the awaited state appearing, which is a
    no-output condition, and it fails the test loudly instead of hanging the
    suite forever.
    """
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"never observed: {what}")


class _RollbackHarness:
    """One shared fixture for every #755 case.

    Provisioning is REAL — the workspace, its control directory and the uid's
    private outbox are actually created — so their survival after a truncated
    rollback is the defect itself and not a fixture artefact.
    """

    def __init__(self, monkeypatch, tmp_path):
        from drivers import s6_rc
        from drivers import workspace as ws_mod
        import plugin_outbox

        _patch_uid_drop_ok(monkeypatch)

        self.uid = 200005
        self.rec = _make_record(allocated_uid=self.uid)
        self.defn = _make_defn(tmp_path)

        (tmp_path / "engagements").mkdir()
        (tmp_path / "svc-root").mkdir()
        (tmp_path / "base-plugins").mkdir()
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT",
                            str(tmp_path / "svc-root"))

        # A FRESH compile lock per test. The module-level one is created at
        # import and caches its loop the first time it is CONTENDED, so the
        # competing-acquirer case would otherwise bind it to one test's loop
        # for the rest of the worker process.
        monkeypatch.setattr(s6_rc, "_compile_lock", asyncio.Lock())

        self.ws_path = tmp_path / "engagements" / self.rec.id
        self.ctl_path = ws_mod.control_dir(self.rec.id)
        self.outbox_path = plugin_outbox.engagement_outbox_dir(self.uid)

        self.compile_entries: list[int] = []
        self.ensure_down_entries: list[int] = []
        self.mark_error_entries: list[int] = []
        self.rollback_gate = asyncio.Event()

        self.probe = _RollbackProbe(
            monkeypatch, svc_root=str(tmp_path / "svc-root"),
            ws_path=self.ws_path, ctl_path=self.ctl_path, uid=self.uid,
            outbox_path=self.outbox_path)

        from drivers.claude_code_driver import ClaudeCodeDriver
        self.drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )

    # -- fakes ---------------------------------------------------------------

    def compile_fake(self, *, suspend_on_call: int | None):
        """``_compile_and_update_locked`` that can genuinely SUSPEND.

        ``tests/test_claude_code_driver.py``'s existing cancellation test uses
        a fake that raises SYNCHRONOUSLY, so the rollback's own awaits never
        suspend and no cancellation can be delivered inside the rollback —
        which is exactly why it stays green with #755 live.
        """
        async def _fake():
            self.compile_entries.append(len(self.compile_entries) + 1)
            if suspend_on_call is not None \
                    and len(self.compile_entries) == suspend_on_call:
                await self.rollback_gate.wait()
        return _fake

    async def launch(self):
        return await self.drv.start(self.rec, prompt="hi", options=self.defn)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
)
class TestRollbackCancellationCompleteness:
    """#755: a cancellation delivered at one of the rollback's own awaits
    must not skip the removals below it.

    No corpus invariant id is cited here on purpose. The completeness property
    this pins is described as prose under "Failure behavior" in
    ``docs/architecture/engagements.md``; DECLARING it as an ``INV-ENG-*`` is a
    separate, returned decision (see the cluster's handback) because no bytes
    predating this change establish the requirement, only the defect.

    ``ClaudeCodeDriver.start``'s rollback guards each of its three awaits with
    ``except Exception``. ``asyncio.CancelledError`` is a ``BaseException``, so
    a cancellation delivered at one of them escapes the whole
    ``except BaseException`` handler and abandons the workspace ``rmtree``, the
    control-directory ``rmtree`` (which is also ``.casa-meta.json``'s removal),
    ``prune_identity`` and the outbox teardown. The meta is already
    ``UNDERGOING`` by then and ``workspace._sweep_one_workspace`` returns on
    ``UNDERGOING`` before it reads ``retention_until``, so nothing ever reaps
    the residue — and uids are never reused (INV-CONT-001), so it is one
    permanent leak per failed attempt.

    Every case here delivers a REAL ``task.cancel()`` against a genuinely
    suspended await. The pre-existing
    ``test_cancellation_mid_launch_rolls_back_like_a_failure`` raises
    ``CancelledError`` synchronously, so ``Task.cancelling()`` stays 0 under it
    and nothing lands inside the rollback: it pins "a cancellation ENTERS the
    rollback", not "a cancellation lands INSIDE it".
    """

    async def test_runtime_failure_cancelled_inside_rollback_compile_runs_sync_tail(
            self, monkeypatch, tmp_path):
        """Case 1 — the SINGLE-cancellation shape #755's own text misses.

        An ordinary provisioning failure enters the rollback, and then ONE
        cancellation lands in the rollback's compile. Kills: ``except
        Exception`` on the rollback compile; re-raising the interrupted
        ``RuntimeError`` instead of the delivered cancellation; losing the
        interrupted failure from the cancellation's ``__context__``; skipping
        any synchronous removal.
        """
        from drivers import s6_rc

        h = _RollbackHarness(monkeypatch, tmp_path)
        start_failure = RuntimeError("simulated s6-rc start failure")

        async def fake_start_fail(*, engagement_id):
            raise start_failure

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked",
                            h.compile_fake(suspend_on_call=2))
        monkeypatch.setattr(s6_rc, "start_service", fake_start_fail)

        task = asyncio.ensure_future(h.launch())
        await _spin_until(lambda: len(h.compile_entries) == 2,
                          what="the rollback's compile to be entered")
        task.cancel()
        h.rollback_gate.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task

        assert h.compile_entries == [1, 2]
        assert task.cancelling() == 1
        assert raised.value.__context__ is start_failure, (
            "the cancellation must carry the launch failure it interrupted — "
            "both signals leave start() on one exception object")
        assert h.probe.counts() == _RollbackProbe.each_once(), (
            "#755: a cancellation at the rollback's compile await skipped the "
            "durable removals below it")
        assert h.probe.paths_absent() == {
            "workspace": False, "control": False, "outbox": False}

    async def test_second_cancel_inside_rollback_compile_runs_sync_tail(
            self, monkeypatch, tmp_path):
        """Case 2 — the shape #755's text names: cancel the launch, then cancel
        again inside the rollback it entered.

        Kills: handling only a single cancellation; using ``Task.cancelling()``
        as a boolean that conflates the first and second deliveries; swallowing
        the second cancellation; re-raising the FIRST cancellation instead of
        the one delivered at the rollback.
        """
        from drivers import s6_rc
        from drivers.claude_code_driver import ClaudeCodeDriver

        h = _RollbackHarness(monkeypatch, tmp_path)
        launch_gate = asyncio.Event()
        launch_cancel_seen: list[BaseException] = []

        async def suspending_summary(self, engagement):
            try:
                await launch_gate.wait()
            except asyncio.CancelledError as exc:
                launch_cancel_seen.append(exc)
                raise

        monkeypatch.setattr(ClaudeCodeDriver, "_post_initial_summary",
                            suspending_summary)
        monkeypatch.setattr(s6_rc, "_compile_and_update_locked",
                            h.compile_fake(suspend_on_call=2))

        task = asyncio.ensure_future(h.launch())
        await _spin_until(lambda: len(h.compile_entries) == 1,
                          what="the forward compile")
        task.cancel()
        await _spin_until(lambda: len(h.compile_entries) == 2,
                          what="the rollback's compile to be entered")
        task.cancel()
        h.rollback_gate.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task

        assert h.compile_entries == [1, 2]
        assert task.cancelling() == 2
        assert len(launch_cancel_seen) == 1
        assert raised.value is not launch_cancel_seen[0], (
            "the cancellation delivered INSIDE the rollback is the one that "
            "propagates, not the one that entered it")
        assert raised.value.__context__ is launch_cancel_seen[0]
        assert h.probe.counts() == _RollbackProbe.each_once()
        assert h.probe.paths_absent() == {
            "workspace": False, "control": False, "outbox": False}

    async def test_rollback_compile_without_cancellation_preserves_the_failure(
            self, monkeypatch, tmp_path):
        """Case 3 — the mutation CONTROL. Same fakes, same suspension, no
        cancellation: all five effects run and the original failure is the one
        re-raised. Without it a green result could come from a broken fixture.

        Also pins the ORDER Sol reproduced a mutant against: the service pair
        must already be gone when the rollback's compile is entered, or the
        compile publishes a live db that still contains the failed service
        while every count and path assertion passes.
        """
        from drivers import s6_rc

        h = _RollbackHarness(monkeypatch, tmp_path)
        start_failure = RuntimeError("simulated s6-rc start failure")
        svc_gone_at_compile: list[bool] = []
        svc_pair = tmp_path / "svc-root" / f"engagement-{h.rec.id}"

        async def fake_start_fail(*, engagement_id):
            raise start_failure

        base_compile = h.compile_fake(suspend_on_call=2)

        async def compile_watching_order():
            svc_gone_at_compile.append(not svc_pair.exists())
            await base_compile()

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked",
                            compile_watching_order)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_fail)

        task = asyncio.ensure_future(h.launch())
        await _spin_until(lambda: len(h.compile_entries) == 2,
                          what="the rollback's compile to be entered")
        h.rollback_gate.set()
        with pytest.raises(RuntimeError) as raised:
            await task

        assert raised.value is start_failure
        assert task.cancelling() == 0
        assert h.compile_entries == [1, 2]
        assert svc_gone_at_compile == [False, True], (
            "remove_service_dir must precede the rollback's recompile — "
            "otherwise the recompile republishes the failed service")
        assert h.probe.counts() == _RollbackProbe.each_once()
        assert h.probe.paths_absent() == {
            "workspace": False, "control": False, "outbox": False}

    async def test_uid_drop_refusal_cancelled_inside_ensure_down_runs_sync_tail(
            self, monkeypatch, tmp_path):
        """Case 4 — the ``UidDropRefused`` pre-branch's FIRST await.

        ``ensure_service_down`` (`:1489`) carries the identical
        ``except Exception`` mismatch. Kills: ``except Exception`` there;
        attempting ``mark_error`` after a cancellation was recorded; replacing
        the delivered cancellation with the ``UidDropRefused``; skipping any
        synchronous removal.
        """
        from drivers import s6_rc
        from drivers import claude_code_driver as ccd

        h = _RollbackHarness(monkeypatch, tmp_path)
        uid_failure = ccd.UidDropRefused("simulated uid-drop refusal")
        ensure_gate = asyncio.Event()

        def refuse(rec, ws):
            raise uid_failure

        async def suspending_ensure_down(*, engagement_id, attempts=3):
            h.ensure_down_entries.append(1)
            await ensure_gate.wait()
            return True

        async def fake_mark_error(*a, **kw):
            h.mark_error_entries.append(1)

        monkeypatch.setattr(ccd, "_preflight_uid_drop", refuse)
        monkeypatch.setattr(s6_rc, "ensure_service_down",
                            suspending_ensure_down)
        monkeypatch.setattr(s6_rc, "_compile_and_update_locked",
                            h.compile_fake(suspend_on_call=None))
        h.drv._registry = MagicMock(mark_error=fake_mark_error)

        task = asyncio.ensure_future(h.launch())
        await _spin_until(lambda: len(h.ensure_down_entries) == 1,
                          what="ensure_service_down to be entered")
        task.cancel()
        ensure_gate.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task

        assert task.cancelling() == 1
        assert h.ensure_down_entries == [1]
        assert h.mark_error_entries == [], (
            "no rollback await may be attempted after a cancellation was "
            "recorded")
        assert h.compile_entries == [], (
            "UidDropRefused is raised before write_service_dir, so no compile "
            "runs on this path")
        assert raised.value.__context__ is uid_failure
        assert h.probe.counts() == _RollbackProbe.each_once()
        assert h.probe.paths_absent() == {
            "workspace": False, "control": False, "outbox": False}

    async def test_uid_drop_refusal_cancelled_inside_mark_error_runs_sync_tail(
            self, monkeypatch, tmp_path):
        """Case 6 — the ``UidDropRefused`` pre-branch's SECOND await.

        Sol's seam finding: with no case cancelling here, a mutant that
        narrows only ``mark_error``'s catch back to ``Exception`` ships green
        while skipping the entire synchronous tail (measured 0/500 removals
        against 500/500 for the correct catch).
        """
        from drivers import s6_rc
        from drivers import claude_code_driver as ccd

        h = _RollbackHarness(monkeypatch, tmp_path)
        uid_failure = ccd.UidDropRefused("simulated uid-drop refusal")
        mark_gate = asyncio.Event()

        def refuse(rec, ws):
            raise uid_failure

        async def ok_ensure_down(*, engagement_id, attempts=3):
            h.ensure_down_entries.append(1)
            return True

        async def suspending_mark_error(*a, **kw):
            h.mark_error_entries.append(1)
            await mark_gate.wait()

        monkeypatch.setattr(ccd, "_preflight_uid_drop", refuse)
        monkeypatch.setattr(s6_rc, "ensure_service_down", ok_ensure_down)
        monkeypatch.setattr(s6_rc, "_compile_and_update_locked",
                            h.compile_fake(suspend_on_call=None))
        h.drv._registry = MagicMock(mark_error=suspending_mark_error)

        task = asyncio.ensure_future(h.launch())
        await _spin_until(lambda: len(h.mark_error_entries) == 1,
                          what="mark_error to be entered")
        task.cancel()
        mark_gate.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task

        assert task.cancelling() == 1
        assert h.ensure_down_entries == [1]
        assert h.mark_error_entries == [1]
        assert h.compile_entries == []
        assert raised.value.__context__ is uid_failure
        assert h.probe.counts() == _RollbackProbe.each_once()
        assert h.probe.paths_absent() == {
            "workspace": False, "control": False, "outbox": False}

    async def test_compile_lock_is_not_released_before_the_sync_tail_finishes(
            self, monkeypatch, tmp_path):
        """Case 5 — the rollback keeps its launcher's ``_compile_lock`` across
        the synchronous tail, so a queued competitor cannot compile while the
        workspace/identity/outbox removals are still running.

        Sol's seam finding: a naive version of this test cannot tell held from
        released. The tail contains no await, so a competitor's coroutine body
        cannot run before the tail finishes EITHER WAY — measured identical
        across 1,000 trials of both shapes. What discriminates is the release
        itself, so the lock is instrumented and its release is recorded into
        the same ordered trace as the removals.

        Kills: moving the synchronous tail outside ``async with
        s6_rc._compile_lock``; releasing the lock right after the cancelled
        rollback compile; handing the lock to a queued compiler before the
        identity/outbox cleanup finishes.
        """
        from drivers import s6_rc

        h = _RollbackHarness(monkeypatch, tmp_path)
        trace = h.probe.trace

        class _ProbeLock(asyncio.Lock):
            def release(self):
                super().release()
                trace.append(("lock_released", None))

        monkeypatch.setattr(s6_rc, "_compile_lock", _ProbeLock())

        start_failure = RuntimeError("simulated s6-rc start failure")
        competitor_entries: list[int] = []

        async def fake_start_fail(*, engagement_id):
            raise start_failure

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked",
                            h.compile_fake(suspend_on_call=2))
        monkeypatch.setattr(s6_rc, "start_service", fake_start_fail)

        async def competitor():
            async with s6_rc._compile_lock:
                competitor_entries.append(1)
                trace.append(("competitor_entered", None))

        task = asyncio.ensure_future(h.launch())
        await _spin_until(lambda: len(h.compile_entries) == 2,
                          what="the rollback's compile to be entered")
        rival = asyncio.ensure_future(competitor())
        await _spin_until(lambda: s6_rc._compile_lock._waiters,
                          what="the competitor to queue on the compile lock")
        task.cancel()
        h.rollback_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await rival

        assert competitor_entries == [1]
        assert h.probe.counts() == _RollbackProbe.each_once()
        # TWO releases: the launcher's ``async with`` and then the
        # competitor's own. Only the FIRST is the launcher's, and it is the one
        # the ordering is asserted against.
        released_at = [i for i, e in enumerate(trace)
                       if e[0] == "lock_released"]
        assert len(released_at) == 2, trace
        for effect in (("rmtree", str(h.ws_path)),
                       ("rmtree", str(h.ctl_path)),
                       ("prune_identity", h.uid),
                       ("teardown_outbox", h.uid)):
            assert trace.index(effect) < released_at[0], (
                f"{effect} ran after the compile lock was released: a queued "
                f"compiler could have taken it mid-teardown")
            assert trace.index(effect) < trace.index(
                ("competitor_entered", None)), (
                f"a queued compiler held the lock while {effect} was still "
                f"running")
