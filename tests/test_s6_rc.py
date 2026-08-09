"""Unit tests for drivers.s6_rc — pure s6-rc orchestration."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _pair(svc_root: Path, eid: str) -> None:
    """Write a healthy producer/consumer engagement pair."""
    from drivers.s6_rc import write_service_dir

    write_service_dir(
        svc_root=str(svc_root), engagement_id=eid,
        run_script="#!/bin/sh\nexec true\n", depends_on=[],
        log_run_script="#!/bin/sh\nexec s6-log /tmp/x\n",
    )


class TestWriteServiceDir:
    @pytest.mark.skipif(sys.platform == "win32", reason="chmod exec-bits not meaningful on Windows")
    async def test_writes_type_run_and_dependencies(self, tmp_path):
        from drivers.s6_rc import write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        run_contents = "#!/command/with-contenv sh\nexec true\n"

        write_service_dir(
            svc_root=str(svc_root),
            engagement_id="abc12345",
            run_script=run_contents,
            depends_on=["init-setup-configs"],
        )

        svc_dir = svc_root / "engagement-abc12345"
        assert (svc_dir / "type").read_text() == "longrun\n"
        assert (svc_dir / "run").read_text() == run_contents
        mode = os.stat(svc_dir / "run").st_mode
        assert mode & stat.S_IXUSR, "run script must be executable"
        assert (svc_dir / "dependencies.d" / "init-setup-configs").exists()


    @pytest.mark.skipif(sys.platform == "win32", reason="chmod exec-bits not meaningful on Windows")
    async def test_writes_sibling_logger_service_when_log_script_provided(self, tmp_path):
        """v0.64.0: s6-rc-compile ignores nested log/ subdirs (skarnet docs) —
        a logged service must be TWO sibling top-level services wired
        producer-for/consumer-for. s6-rc auto-adds the producer→consumer
        dependency (verified empirically on s6-rc 0.6.0.0)."""
        from drivers.s6_rc import write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        log_script = (
            "#!/command/with-contenv sh\n"
            "exec s6-log n20 s1000000 /var/log/casa-engagement-abc12345\n"
        )

        write_service_dir(
            svc_root=str(svc_root),
            engagement_id="abc12345",
            run_script="#!/command/with-contenv sh\nexec true\n",
            depends_on=["init-setup-configs"],
            log_run_script=log_script,
        )

        main_dir = svc_root / "engagement-abc12345"
        log_dir = svc_root / "engagement-abc12345-log"
        assert not (main_dir / "log").exists(), \
            "nested log/ is ignored by s6-rc-compile — must not be written"
        assert (main_dir / "producer-for").read_text() == "engagement-abc12345-log\n"
        assert (log_dir / "type").read_text() == "longrun\n"
        assert (log_dir / "run").read_text() == log_script
        mode = os.stat(log_dir / "run").st_mode
        assert mode & stat.S_IXUSR
        assert (log_dir / "consumer-for").read_text() == "engagement-abc12345\n"
        assert (log_dir / "dependencies.d" / "init-setup-configs").exists()

    async def test_no_logger_artifacts_without_log_script(self, tmp_path):
        from drivers.s6_rc import write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        write_service_dir(
            svc_root=str(svc_root), engagement_id="abc12345",
            run_script="#!/bin/sh\nexec true\n", depends_on=[],
        )
        assert not (svc_root / "engagement-abc12345" / "producer-for").exists()
        assert not (svc_root / "engagement-abc12345-log").exists()


class TestRemoveServiceDir:
    async def test_removes_existing_dir(self, tmp_path):
        from drivers.s6_rc import remove_service_dir, write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        write_service_dir(
            svc_root=str(svc_root), engagement_id="x1",
            run_script="#!/bin/sh\nexec true\n", depends_on=[],
        )
        assert (svc_root / "engagement-x1").exists()

        remove_service_dir(svc_root=str(svc_root), engagement_id="x1")
        assert not (svc_root / "engagement-x1").exists()

    async def test_remove_missing_is_noop(self, tmp_path):
        from drivers.s6_rc import remove_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        # Does not raise.
        remove_service_dir(svc_root=str(svc_root), engagement_id="nosuch")

    async def test_removes_sibling_logger_dir(self, tmp_path):
        from drivers.s6_rc import remove_service_dir, write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        write_service_dir(
            svc_root=str(svc_root), engagement_id="x1",
            run_script="#!/bin/sh\nexec true\n", depends_on=[],
            log_run_script="#!/bin/sh\nexec s6-log /var/log/casa-engagement-x1\n",
        )
        assert (svc_root / "engagement-x1-log").exists()

        remove_service_dir(svc_root=str(svc_root), engagement_id="x1")
        assert not (svc_root / "engagement-x1").exists()
        assert not (svc_root / "engagement-x1-log").exists()

    async def test_remove_continues_past_rmtree_failure(
        self, tmp_path, monkeypatch,
    ):
        """One rmtree failing must not abort the other half's removal —
        a torn pair would otherwise persist (the compile-path prune is the
        backstop, but removal should make progress on its own)."""
        from drivers import s6_rc

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        _pair(svc_root, "x1")

        real_rmtree = s6_rc.shutil.rmtree

        def flaky(path, *args, **kwargs):
            if str(path).endswith("engagement-x1"):
                raise OSError("EBUSY")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(s6_rc.shutil, "rmtree", flaky)
        # Does not raise; the -log half is removed despite the main failing.
        s6_rc.remove_service_dir(svc_root=str(svc_root), engagement_id="x1")
        assert not (svc_root / "engagement-x1-log").exists()
        assert (svc_root / "engagement-x1").exists()


class TestPruneBrokenPairs:
    """v0.64.0: the pair dirs cross-reference each other, so NO write/remove
    ordering is crash-atomic — a torn half (producer-for naming a missing
    service, or a consumer-for whose producer is gone) fails EVERY
    s6-rc-compile, bricking all engagement orchestration. The compile path
    therefore prunes broken halves into a compilable state first."""

    async def test_dangling_producer_for_is_unlinked(self, tmp_path):
        from drivers.s6_rc import _prune_broken_pairs

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        _pair(svc_root, "torn1")
        shutil.rmtree(svc_root / "engagement-torn1-log")

        _prune_broken_pairs(svc_root=str(svc_root))

        # The engagement service survives (unlogged); the dangling
        # cross-reference is gone so the sources compile again.
        assert (svc_root / "engagement-torn1").is_dir()
        assert not (svc_root / "engagement-torn1" / "producer-for").exists()

    async def test_orphan_log_sibling_is_removed(self, tmp_path):
        from drivers.s6_rc import _prune_broken_pairs

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        _pair(svc_root, "torn2")
        shutil.rmtree(svc_root / "engagement-torn2")

        _prune_broken_pairs(svc_root=str(svc_root))

        assert not (svc_root / "engagement-torn2-log").exists()

    async def test_log_sibling_without_producer_for_is_removed(self, tmp_path):
        from drivers.s6_rc import _prune_broken_pairs

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        _pair(svc_root, "torn3")
        (svc_root / "engagement-torn3" / "producer-for").unlink()

        _prune_broken_pairs(svc_root=str(svc_root))

        assert (svc_root / "engagement-torn3").is_dir()
        assert not (svc_root / "engagement-torn3-log").exists()

    async def test_healthy_pair_untouched(self, tmp_path):
        from drivers.s6_rc import _prune_broken_pairs

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        _pair(svc_root, "ok1")

        _prune_broken_pairs(svc_root=str(svc_root))

        assert (svc_root / "engagement-ok1" / "producer-for").exists()
        assert (svc_root / "engagement-ok1-log" / "consumer-for").exists()

    async def test_compile_prunes_before_compiling(self, tmp_path, monkeypatch):
        from drivers import s6_rc

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        _pair(svc_root, "torn4")
        shutil.rmtree(svc_root / "engagement-torn4-log")
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

        calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            calls.append(list(argv))
            class _R: returncode = 0
            return _R()

        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        await s6_rc._compile_and_update_locked()

        assert not (svc_root / "engagement-torn4" / "producer-for").exists()
        assert calls and calls[0][0] == "s6-rc-compile"


class TestStopLogService:
    async def test_stops_when_log_source_dir_exists(self, tmp_path, monkeypatch):
        from drivers import s6_rc

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        (svc_root / "engagement-abc-log").mkdir()
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

        calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            calls.append(list(argv))
            class _R: returncode = 0
            return _R()

        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        await s6_rc.stop_log_service(engagement_id="abc")

        assert calls == [["s6-rc", "-d", "change", "engagement-abc-log"]]

    async def test_noop_when_log_source_dir_absent(self, tmp_path, monkeypatch):
        """Legacy engagements (pre-v0.64.0 layout) have no logger service —
        stopping one would exec a doomed s6-rc and log a spurious warning."""
        from drivers import s6_rc

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

        calls: list[list[str]] = []
        monkeypatch.setattr(
            s6_rc.subprocess, "run",
            lambda argv, check=True, **kw: calls.append(list(argv)),
        )

        await s6_rc.stop_log_service(engagement_id="abc")

        assert calls == []


class TestCompileAndUpdateLocked:
    async def test_invokes_compile_then_update_with_three_sources(self, monkeypatch):
        """The canonical call: compile with overlay + casa + engagement sources, update."""
        from drivers import s6_rc

        calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            calls.append(list(argv))
            class _R: returncode = 0
            return _R()

        # Route both asyncio.to_thread(subprocess.run, ...) and direct subprocess.run
        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        await s6_rc._compile_and_update_locked()

        # Two calls: compile, then update
        assert len(calls) == 2
        compile_cmd = calls[0]
        assert compile_cmd[0] == "s6-rc-compile"
        # Compile receives: new_db, overlay_src, casa_src, engagement_src
        assert compile_cmd[2] == s6_rc.S6_OVERLAY_SOURCES
        assert compile_cmd[3] == s6_rc.CASA_SOURCES
        assert compile_cmd[4] == s6_rc.ENGAGEMENT_SOURCES_ROOT
        new_db = compile_cmd[1]
        assert new_db.startswith("/tmp/s6-casa-db-")

        update_cmd = calls[1]
        assert update_cmd == ["s6-rc-update", new_db]

    async def test_reaps_previous_db_after_successful_swap(self, tmp_path, monkeypatch):
        """L12 leak guard: the previously-live compiled db must be removed
        after a successful swap, so /tmp doesn't accumulate one orphan per
        compile."""
        import subprocess

        from drivers import s6_rc

        old_db = tmp_path / "s6-casa-db-old"
        old_db.mkdir()
        live = tmp_path / "compiled"
        live.symlink_to(old_db)
        monkeypatch.setattr(s6_rc, "LIVE_DB_SYMLINK", str(live))

        calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        await s6_rc._compile_and_update_locked()

        assert calls[0][0] == "s6-rc-compile"
        assert calls[1][0] == "s6-rc-update"
        assert not old_db.exists(), "previous live db must be reaped after a successful swap"

    async def test_keeps_non_casa_previous_db(self, tmp_path, monkeypatch):
        """A foreign/boot db (no s6-casa-db- prefix) must never be reaped."""
        import subprocess

        from drivers import s6_rc

        boot_db = tmp_path / "db"  # simulates the s6-overlay boot db
        boot_db.mkdir()
        live = tmp_path / "compiled"
        live.symlink_to(boot_db)
        monkeypatch.setattr(s6_rc, "LIVE_DB_SYMLINK", str(live))
        monkeypatch.setattr(
            s6_rc.subprocess, "run",
            lambda argv, check=True, **kwargs: subprocess.CompletedProcess(argv, 0),
        )

        await s6_rc._compile_and_update_locked()

        assert boot_db.exists(), "foreign db must never be touched"

    async def test_reaps_new_db_on_failed_swap(self, tmp_path, monkeypatch):
        """A failed compile/update must reap the just-created db (the orphan
        in that scenario), not the previous live one."""
        import subprocess

        from drivers import s6_rc

        old_db = tmp_path / "s6-casa-db-old"
        old_db.mkdir()
        live = tmp_path / "compiled"
        live.symlink_to(old_db)
        monkeypatch.setattr(s6_rc, "LIVE_DB_SYMLINK", str(live))

        captured_new_db: list[str] = []

        def fake_run(argv, check=True, **kwargs):
            if argv[0] == "s6-rc-compile":
                captured_new_db.append(argv[1])
                Path(argv[1]).mkdir(parents=True, exist_ok=True)
            raise subprocess.CalledProcessError(1, argv)

        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        with pytest.raises(subprocess.CalledProcessError):
            await s6_rc._compile_and_update_locked()

        assert old_db.exists(), "previous live db must survive a failed swap"
        assert not Path(captured_new_db[0]).exists(), "failed new db must be reaped"


class TestCompileCancellation:
    async def test_cancelled_update_does_not_delete_swapped_db(
        self, tmp_path, monkeypatch,
    ):
        """#344: cancelling while ``s6-rc-update`` runs in its worker
        thread must NOT delete new_db — the worker may still complete the
        swap, making new_db the LIVE compiled database. Cleanup decisions
        belong to the worker (which sees the true outcome), not to the
        cancelled awaiter."""
        import asyncio
        import subprocess
        import threading
        import time

        from drivers import s6_rc

        old_db = tmp_path / "s6-casa-db-old"
        old_db.mkdir()
        live = tmp_path / "compiled"
        live.symlink_to(old_db)
        monkeypatch.setattr(s6_rc, "LIVE_DB_SYMLINK", str(live))

        entered = threading.Event()
        release = threading.Event()
        captured_new_db: list[str] = []

        def fake_run(argv, check=True, **kwargs):
            if argv[0] == "s6-rc-compile":
                captured_new_db.append(argv[1])
                Path(argv[1]).mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(argv, 0)
            entered.set()                      # s6-rc-update in flight
            assert release.wait(timeout=5)
            return subprocess.CompletedProcess(argv, 0)   # update SUCCEEDS

        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        task = asyncio.create_task(s6_rc._compile_and_update_locked())
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        release.set()
        # Wall-clock-bounded wait for the abandoned worker to finish:
        # its last act on this path is reaping the old db.
        deadline = time.monotonic() + 5
        while old_db.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.001)

        assert Path(captured_new_db[0]).exists(), (
            "the swapped (now live) db must survive the cancellation")
        assert not old_db.exists(), (
            "the worker completes the old-db reap even when abandoned")


    async def test_cancelled_compile_discards_instead_of_swapping(
        self, tmp_path, monkeypatch,
    ):
        """Terra r6-1: a worker whose awaiter was cancelled DURING the
        compile must discard its output instead of swapping — a successor
        holding _compile_lock may have mutated the source tree it read,
        and will produce the authoritative db itself."""
        import asyncio
        import subprocess
        import threading
        import time

        from drivers import s6_rc

        old_db = tmp_path / "s6-casa-db-old"
        old_db.mkdir()
        live = tmp_path / "compiled"
        live.symlink_to(old_db)
        monkeypatch.setattr(s6_rc, "LIVE_DB_SYMLINK", str(live))

        entered = threading.Event()
        release = threading.Event()
        captured_new_db: list[str] = []
        update_calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            if argv[0] == "s6-rc-compile":
                captured_new_db.append(argv[1])
                Path(argv[1]).mkdir(parents=True, exist_ok=True)
                entered.set()                  # compile in flight
                assert release.wait(timeout=5)
                return subprocess.CompletedProcess(argv, 0)
            update_calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        task = asyncio.create_task(s6_rc._compile_and_update_locked())
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        release.set()
        deadline = time.monotonic() + 5
        while Path(captured_new_db[0]).exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.001)

        assert update_calls == [], "abandoned compile must not swap"
        assert not Path(captured_new_db[0]).exists(), "output discarded"
        assert old_db.exists(), "the live db stays untouched"


class TestServiceStatus:
    async def test_service_is_up_parses_svstat(self, monkeypatch):
        from drivers import s6_rc

        def fake_run(argv, **kwargs):
            class _R:
                returncode = 0
                stdout = "12345\n"        # s6-svstat -p prints pid (0 if down)
            assert argv == ["s6-svstat", "-p", "/run/service/engagement-abc"]
            return _R()
        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        pid = await s6_rc.service_pid(engagement_id="abc")
        assert pid == 12345

    async def test_service_is_up_returns_none_when_down(self, monkeypatch):
        from drivers import s6_rc

        def fake_run(argv, **kwargs):
            class _R:
                returncode = 0
                stdout = "0\n"
            return _R()
        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        pid = await s6_rc.service_pid(engagement_id="down")
        assert pid is None


class TestStartStopService:
    async def test_start_service_invokes_rc_change(self, monkeypatch):
        from drivers import s6_rc
        calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            calls.append(list(argv))
            class _R: returncode = 0
            return _R()
        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        await s6_rc.start_service(engagement_id="abc")

        assert calls == [["s6-rc", "-u", "change", "engagement-abc"]]

    async def test_stop_service_invokes_rc_change_down(self, monkeypatch):
        from drivers import s6_rc
        calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            calls.append(list(argv))
            class _R: returncode = 0
            return _R()
        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        await s6_rc.stop_service(engagement_id="abc")

        assert calls == [["s6-rc", "-d", "change", "engagement-abc"]]


class TestSweepOrphans:
    async def test_removes_dirs_not_in_keep_set(self, tmp_path):
        from drivers.s6_rc import sweep_orphan_service_dirs, write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()

        for eid in ("keep1", "keep2", "orphan1", "orphan2"):
            write_service_dir(
                svc_root=str(svc_root), engagement_id=eid,
                run_script="#!/bin/sh\nexec true\n", depends_on=[],
            )

        removed = sweep_orphan_service_dirs(
            svc_root=str(svc_root), keep_engagement_ids={"keep1", "keep2"},
        )

        assert set(removed) == {"orphan1", "orphan2"}
        assert (svc_root / "engagement-keep1").exists()
        assert (svc_root / "engagement-keep2").exists()
        assert not (svc_root / "engagement-orphan1").exists()
        assert not (svc_root / "engagement-orphan2").exists()

    async def test_ignores_non_engagement_dirs(self, tmp_path):
        """A foreign dir under svc_root is left alone (defensive)."""
        from drivers.s6_rc import sweep_orphan_service_dirs

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        (svc_root / "random-other-thing").mkdir()

        removed = sweep_orphan_service_dirs(
            svc_root=str(svc_root), keep_engagement_ids=set(),
        )
        assert removed == []
        assert (svc_root / "random-other-thing").exists()

    async def test_logger_dirs_follow_their_engagement(self, tmp_path):
        """v0.64.0: engagement-<id>-log is kept iff <id> is kept. The
        pre-fix parser read the whole suffix as engagement id '<id>-log'
        (never in the keep set) and swept live loggers at boot."""
        from drivers.s6_rc import sweep_orphan_service_dirs, write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        for eid in ("keep1", "orphan1"):
            write_service_dir(
                svc_root=str(svc_root), engagement_id=eid,
                run_script="#!/bin/sh\nexec true\n", depends_on=[],
                log_run_script="#!/bin/sh\nexec s6-log /tmp/x\n",
            )

        removed = sweep_orphan_service_dirs(
            svc_root=str(svc_root), keep_engagement_ids={"keep1"},
        )

        assert set(removed) == {"orphan1"}, "orphan pair counts once"
        assert (svc_root / "engagement-keep1").exists()
        assert (svc_root / "engagement-keep1-log").exists()
        assert not (svc_root / "engagement-orphan1").exists()
        assert not (svc_root / "engagement-orphan1-log").exists()


class TestSweepOrphanCompiledDbs:
    async def test_removes_stale_dbs_keeps_live_and_foreign(self, tmp_path, monkeypatch):
        from drivers import s6_rc

        keep = tmp_path / "s6-casa-db-live"
        keep.mkdir()
        stale = tmp_path / "s6-casa-db-stale"
        stale.mkdir()
        foreign = tmp_path / "not-ours"
        foreign.mkdir()
        live = tmp_path / "compiled"
        live.symlink_to(keep)
        monkeypatch.setattr(s6_rc, "LIVE_DB_SYMLINK", str(live))

        removed = s6_rc.sweep_orphan_compiled_dbs(tmp_root=str(tmp_path))

        assert removed == [str(stale)]
        assert keep.exists() and foreign.exists() and not stale.exists()

    async def test_missing_tmp_root_is_noop(self, tmp_path):
        from drivers import s6_rc

        missing = tmp_path / "does-not-exist"
        assert s6_rc.sweep_orphan_compiled_dbs(tmp_root=str(missing)) == []


# ---------------------------------------------------------------------------
# ensure_service_down — checked-teardown ladder (W3/Task 8, r13-B1/r14-B1..3)
# ---------------------------------------------------------------------------

import subprocess as _subprocess  # noqa: E402
from types import SimpleNamespace  # noqa: E402


class _FakeSup:
    """A scriptable fake s6 supervisor for the ensure_service_down ladder.

    Commands drive/observe ``self.up_out`` (the ``up,wantedup`` string) and
    ``self.up_rc`` (the s6-svstat -o returncode). ``up_responses`` (if set) is
    a queue of ``(rc, stdout)`` for successive -o probes (last repeats).
    """

    def __init__(self):
        self.up_out = "true true"
        self.up_rc = 0
        self.up_responses = None
        self.rc_d_raises = False
        self.on_svc_D = None
        self.on_combined = None
        self.pid_rc = 0
        self.pid_out = "0"
        self.calls = []

    def run(self, cmd, **kw):
        self.calls.append(list(cmd))
        prog = cmd[0]
        if cmd[:3] == ["s6-rc", "-d", "change"]:
            if self.rc_d_raises:
                raise _subprocess.CalledProcessError(1, cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if prog == "s6-svstat" and "-o" in cmd:
            if self.up_responses:
                if len(self.up_responses) > 1:
                    rc, out = self.up_responses.pop(0)
                else:
                    rc, out = self.up_responses[0]
            else:
                rc, out = self.up_rc, self.up_out
            return SimpleNamespace(returncode=rc, stdout=out, stderr="")
        if prog == "s6-svstat" and "-p" in cmd:
            return SimpleNamespace(returncode=self.pid_rc, stdout=self.pid_out,
                                   stderr="")
        if cmd[:2] == ["s6-svc", "-D"]:
            if self.on_svc_D:
                self.on_svc_D()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if prog == "s6-svc" and "-KD" in cmd:      # combined containment rung
            if self.on_combined:
                self.on_combined()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _wire_fake(monkeypatch, tmp_path, fake, *, make_scandir=True, eid="e1"):
    from drivers import s6_rc
    root = tmp_path / "run-service"
    root.mkdir()
    if make_scandir:
        (root / f"engagement-{eid}").mkdir()
    monkeypatch.setattr(s6_rc, "SERVICE_SCANDIR_ROOT", str(root))
    monkeypatch.setattr(s6_rc, "_ENSURE_DOWN_WAIT_S", 0)
    monkeypatch.setattr(s6_rc.subprocess, "run", fake.run)
    return root


def _has_combined(fake):
    return any("-KD" in c for c in fake.calls)


class TestEnsureServiceDown:
    async def test_scandir_absent_is_down(self, monkeypatch, tmp_path):
        from drivers import s6_rc
        fake = _FakeSup()
        _wire_fake(monkeypatch, tmp_path, fake, make_scandir=False)
        assert await s6_rc.ensure_service_down(engagement_id="e1") is True
        # No svstat -o probe was needed (absent scandir short-circuits to down).
        assert not any(c[:2] == ["s6-svstat", "-o"] for c in fake.calls)

    async def test_stop_failure_svc_fallback_confirms(self, monkeypatch, tmp_path):
        from drivers import s6_rc
        fake = _FakeSup()
        fake.rc_d_raises = True                    # s6-rc -d raises
        fake.up_out = "true true"
        fake.on_svc_D = lambda: setattr(fake, "up_out", "false false")
        _wire_fake(monkeypatch, tmp_path, fake)
        assert await s6_rc.ensure_service_down(engagement_id="e1") is True
        assert any(c[:3] == ["s6-rc", "-d", "change"] for c in fake.calls)
        assert any(c[:2] == ["s6-svc", "-D"] for c in fake.calls)

    async def test_persistent_pid_fallback_downs(self, monkeypatch, tmp_path):
        from drivers import s6_rc
        fake = _FakeSup()
        fake.rc_d_raises = False                   # s6-rc -d succeeds...
        fake.up_out = "true true"                  # ...but probe still up
        fake.on_svc_D = lambda: setattr(fake, "up_out", "false false")
        _wire_fake(monkeypatch, tmp_path, fake)
        assert await s6_rc.ensure_service_down(engagement_id="e1") is True

    async def test_status_query_failure_retries_then_confirms(
        self, monkeypatch, tmp_path,
    ):
        from drivers import s6_rc
        fake = _FakeSup()
        # Unknown (rc=1) on the first probes, resolves to down later. Unknown
        # must NOT be treated as down.
        fake.up_responses = [(1, ""), (1, ""), (0, "false false")]
        _wire_fake(monkeypatch, tmp_path, fake)
        assert await s6_rc.ensure_service_down(engagement_id="e1") is True
        probes = [c for c in fake.calls if c[:2] == ["s6-svstat", "-o"]]
        assert len(probes) >= 3

    async def test_respawn_race_false_true_not_down(self, monkeypatch, tmp_path):
        from drivers import s6_rc
        fake = _FakeSup()
        fake.up_out = "false true"                  # dead but wanted-up → NOT down
        fake.on_svc_D = lambda: setattr(fake, "up_out", "false false")
        _wire_fake(monkeypatch, tmp_path, fake)
        assert await s6_rc.ensure_service_down(engagement_id="e1") is True
        # It only confirmed AFTER the -D latch flipped wantedup to false.
        assert any(c[:2] == ["s6-svc", "-D"] for c in fake.calls)

    async def test_probe_classifies_false_true_as_up(self, monkeypatch, tmp_path):
        from drivers import s6_rc
        fake = _FakeSup()
        fake.up_out = "false true"
        root = _wire_fake(monkeypatch, tmp_path, fake)
        scandir = str(root / "engagement-e1")
        assert await s6_rc._probe_service_down(scandir) == "up"

    async def test_exhaustion_combined_rung_confirms(self, monkeypatch, tmp_path):
        from drivers import s6_rc
        fake = _FakeSup()
        fake.up_out = "true true"                   # -rc/-D both ineffective
        fake.on_combined = lambda: setattr(fake, "up_out", "false false")
        _wire_fake(monkeypatch, tmp_path, fake)
        assert await s6_rc.ensure_service_down(engagement_id="e1") is True
        assert _has_combined(fake), "final rung must run the combined -wD -KD -T"

    async def test_true_exhaustion_returns_false(self, monkeypatch, tmp_path):
        from drivers import s6_rc
        fake = _FakeSup()
        fake.up_rc = 1                              # persistent query failure
        fake.pid_rc = 1                             # -p query also fails → no kill
        _wire_fake(monkeypatch, tmp_path, fake)
        assert await s6_rc.ensure_service_down(engagement_id="e1") is False


class TestDirectKillpg:
    @pytest.mark.skipif(sys.platform == "win32", reason="posix process groups")
    async def test_group_kill_takes_leader_and_child(self, monkeypatch, tmp_path):
        import time
        from drivers import s6_rc

        code = (
            "import os,sys,time\n"
            "pid=os.fork()\n"
            "if pid==0:\n"
            "    time.sleep(60)\n"
            "else:\n"
            "    sys.stdout.write(str(pid)+chr(10)); sys.stdout.flush()\n"
            "    time.sleep(60)\n"
        )
        proc = _subprocess.Popen(
            [sys.executable, "-c", code], start_new_session=True,
            stdout=_subprocess.PIPE, text=True,
        )
        child_pid = int(proc.stdout.readline())
        leader_pid = proc.pid

        root = tmp_path / "run-service"
        root.mkdir()
        (root / "engagement-e1").mkdir()
        monkeypatch.setattr(s6_rc, "SERVICE_SCANDIR_ROOT", str(root))

        real_run = _subprocess.run

        def fake_run(cmd, **kw):
            if cmd[0] == "s6-svstat" and "-p" in cmd:
                return SimpleNamespace(returncode=0, stdout=str(leader_pid),
                                       stderr="")
            return real_run(cmd, **kw)

        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        scandir = str(root / "engagement-e1")
        killed = await s6_rc._direct_killpg(scandir)
        assert killed is True

        def _dead(pid):
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                time.sleep(0.1)
            return False

        try:
            # Child is an orphan (reaped by init) — os.kill eventually raises.
            assert _dead(child_pid), "child must be group-killed too"
            # Leader is our Popen child — SIGKILL leaves a zombie until wait().
            assert proc.wait(timeout=5) == -9, "leader must be SIGKILLed"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


class TestRunScriptIsStale:
    """B1 (Sol diff r2): v0.75.0 streaming requires the ``casa_control`` AND
    ``--output-format stream-json`` markers; containment Stage 2 (v0.170.0)
    adds a third — ``setpriv`` — so every PRE-Stage-2 run script (which exec'd
    ``claude`` directly as root) reads STALE and boot replay re-renders it into
    the uid-dropped form. A script missing ANY marker is stale, and a
    missing/unreadable run file fails CLOSED (stale=True) so boot replay
    re-plants rather than resuming an unarmed / un-contained pair.
    """

    # A run script carrying all three current markers (streaming pair + setpriv
    # uid drop). Used by the "fresh" and "unreadable-but-fresh" cases.
    _CURRENT = (
        "#!/bin/sh\ncasa_control spawn\n"
        "exec setpriv --reuid 200001 --regid 200001 --clear-groups "
        "-- claude --print --output-format stream-json\n"
    )

    def _write_run(self, svc_root: Path, eid: str, run_text: str) -> None:
        from drivers.s6_rc import _main_service_name
        main = svc_root / _main_service_name(eid)
        main.mkdir(parents=True)
        (main / "run").write_text(run_text)

    async def test_only_stream_json_is_stale(self, tmp_path):
        from drivers.s6_rc import run_script_is_stale
        self._write_run(
            tmp_path, "e1",
            "#!/bin/sh\nexec claude --print --output-format stream-json\n")
        assert run_script_is_stale(svc_root=str(tmp_path), engagement_id="e1")

    async def test_only_casa_control_is_stale(self, tmp_path):
        from drivers.s6_rc import run_script_is_stale
        self._write_run(
            tmp_path, "e2",
            "#!/bin/sh\ncasa_control spawn\nexec claude --print\n")
        assert run_script_is_stale(svc_root=str(tmp_path), engagement_id="e2")

    async def test_legacy_no_setpriv_script_is_stale(self, tmp_path):
        # Containment Stage 2: a PRE-Stage-2 script has both streaming markers
        # but NO setpriv wrapper (it ran claude as root) → stale, so replay
        # migrates it to the uid-dropped form.
        from drivers.s6_rc import run_script_is_stale
        self._write_run(
            tmp_path, "e2b",
            "#!/bin/sh\ncasa_control spawn\n"
            "exec claude --print --output-format stream-json\n")
        assert run_script_is_stale(svc_root=str(tmp_path), engagement_id="e2b")

    async def test_all_three_markers_is_fresh(self, tmp_path):
        from drivers.s6_rc import run_script_is_stale
        self._write_run(tmp_path, "e3", self._CURRENT)
        assert not run_script_is_stale(svc_root=str(tmp_path), engagement_id="e3")

    async def test_missing_run_file_is_stale(self, tmp_path):
        from drivers.s6_rc import _main_service_name, run_script_is_stale
        # main dir exists but no run file (torn) — fail closed.
        (tmp_path / _main_service_name("e4")).mkdir()
        assert run_script_is_stale(svc_root=str(tmp_path), engagement_id="e4")

    async def test_absent_dir_is_stale(self, tmp_path):
        from drivers.s6_rc import run_script_is_stale
        # nothing planted at all — fail closed.
        assert run_script_is_stale(svc_root=str(tmp_path), engagement_id="nope")

    async def test_unreadable_run_file_is_stale(self, tmp_path, monkeypatch):
        from drivers import s6_rc
        self._write_run(tmp_path, "e5", self._CURRENT)

        # Even a current (all-marker) script must classify stale if the file
        # cannot be read (permission/OSError) — patch Path.read_text to raise.
        real_read_text = Path.read_text

        def _boom(self, *a, **k):
            if self.name == "run":
                raise PermissionError("no access")
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _boom)
        assert s6_rc.run_script_is_stale(svc_root=str(tmp_path), engagement_id="e5")


class TestIterEngagementServiceIds:
    """Containment Stage 2 (Task 10): the scandir-first down migration
    enumerates every engagement id with an s6 presence — a source-definition
    dir OR a live scandir entry — as the UNION of both roots, so a
    crash-before-cancel orphan (source dir gone but scandir entry lingering, or
    vice versa) is still caught and driven down before migration."""

    async def test_unions_source_and_scandir(self, tmp_path):
        from drivers.s6_rc import (
            _log_service_name, _main_service_name, iter_engagement_service_ids,
        )
        svc = tmp_path / "svc"
        scan = tmp_path / "scan"
        svc.mkdir()
        scan.mkdir()
        # In sources only.
        (svc / _main_service_name("src-only")).mkdir()
        (svc / _log_service_name("src-only")).mkdir()   # -log parses to same id
        # In both.
        (svc / _main_service_name("both")).mkdir()
        (scan / _main_service_name("both")).mkdir()
        # In scandir only (crash-before-cancel orphan whose source was swept).
        (scan / _main_service_name("scan-only")).mkdir()
        # A foreign dir under each root must be ignored.
        (svc / "s6rc-oneshot-runner").mkdir()
        (scan / "casa-mcp").mkdir()

        ids = iter_engagement_service_ids(
            svc_root=str(svc), scandir_root=str(scan))
        assert ids == {"src-only", "both", "scan-only"}

    async def test_missing_roots_yield_empty(self, tmp_path):
        from drivers.s6_rc import iter_engagement_service_ids
        ids = iter_engagement_service_ids(
            svc_root=str(tmp_path / "nope-svc"),
            scandir_root=str(tmp_path / "nope-scan"))
        assert ids == set()


class TestServiceDirsAbsent:
    """B2 (Sol diff r2): verify BOTH the main and -log service source dirs are
    gone after a remove_service_dir in the migration path (remove swallows
    failures, so a survivor must read as NOT absent → fail closed)."""

    async def test_both_gone_is_absent(self, tmp_path):
        from drivers.s6_rc import service_dirs_absent
        assert service_dirs_absent(svc_root=str(tmp_path), engagement_id="e1")

    async def test_surviving_main_is_not_absent(self, tmp_path):
        from drivers.s6_rc import _main_service_name, service_dirs_absent
        (tmp_path / _main_service_name("e2")).mkdir()
        assert not service_dirs_absent(svc_root=str(tmp_path), engagement_id="e2")

    async def test_surviving_log_is_not_absent(self, tmp_path):
        from drivers.s6_rc import _log_service_name, service_dirs_absent
        (tmp_path / _log_service_name("e3")).mkdir()
        assert not service_dirs_absent(svc_root=str(tmp_path), engagement_id="e3")
