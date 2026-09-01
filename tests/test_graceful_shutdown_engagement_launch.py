"""#698 — a graceful stop's cancellation of a `claude_code` launch.

Red case for **INV-ENG-015**, declared by this change under D34: the corpus
has never stated which removals a launch rollback is entered to run for which
cause — ``docs/architecture/engagements.md`` reserves that question to #698 by
name — so this pins the behaviour the change establishes rather than a
pre-existing statement.

**Every behavioural case runs at every suspension point and both latencies.**
That is a deliberate cross-product rather than a sample: an acceptance round
found a mutant that behaved differently depending on WHICH await the stop landed
on, and a clause asserted at one point is not a clause asserted at all of them.

**Scope, cut twice at acceptance.** The invariant's subject is Casa's
graceful-stop CLEANUP step — the production coroutine these cases execute — and
its object is a launch that step finds REGISTERED. A launch that becomes durable and is not yet
enrolled when the stop latches carries no cause and gets a best-effort reporter
— which is every launch's behaviour today, so it is a case this change does not
improve rather than one it makes worse. Three acceptance rounds each found a
universal clause the red case could not witness, and the third recurrence of
that shape was answered by narrowing the claim to what is witnessed rather than
by widening the harness a third time.

**The launcher is production code, not a harness.** The launch is started by
``tools.engage_executor``'s own handler, driven into the real
``ClaudeCodeDriver``, and cancelled by the stop sequence that
``casa_core.main`` actually runs. Nothing here re-implements the cancellation
arm, the stop block, or the launch-death reporter. That is the whole point of
this file's shape: an earlier draft supplied its own launcher, and every
mutant that deleted a production call — the enrolment, the cancellation arm's
report, the stop's own step — stayed green, because the replacement launcher
kept doing the deleted work. A test that substitutes the launcher cannot
witness whether production still calls anything.

The two probes that are installed are PASS-THROUGH spies: they record that a
production function ran and then run it. Deleting the production call site
makes the spy silent, which is what turns the mutant red.

The final cancellation is delivered by the edge that actually fires at a
graceful stop and by no other: ``asyncio.run``'s ``_cancel_all_tasks``, which
runs AFTER the main coroutine has returned — ``job_registry.begin_shutdown``'s
own docstring names it as such. That is why these tests are SYNCHRONOUS and
call ``asyncio.run`` themselves: an ``asyncio``-marked test runs inside a loop
somebody else shuts down, and the final cancellation sweep is exactly the
thing under test.

Measured at ``f85f37c0`` before this change, with these same production
modules: one real ``task.cancel()`` at a real await inside the guarded region
ran all five removals once each and deleted a ``REPORT.md`` standing in for
the executor's only copy of its work; and the stop edge wrote
``error``/``launch_cancelled``/"the tool call was cancelled during launch"
with no shutdown vocabulary anywhere in the record and **zero**
operator-visible effects at 50 ms and at 0 ms latency alike.

Production names this change introduces are reached through ``getattr`` rather
than imported. That is deliberate and it makes the red case stronger, not
weaker: on a tree without the fix the test then fails on the *outcomes* — the
removals that ran, the record that carries no cause, the notice that never
landed — instead of on an ``AttributeError`` that would say only that a name
is missing.
"""

from __future__ import annotations

import asyncio
import json
import os
import calendar
import shutil as _real_shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


ISO = "%Y-%m-%dT%H:%M:%SZ"
UID = 200011

# The operator-visible strings, EXACTLY. Asserted whole rather than by
# substring: an acceptance round produced a mutant that answered a failed
# metadata write with "Casa will preserve this workspace for one week." —
# neither "retained" nor "7 days" appears in it, so a substring test saw
# nothing while the operator was told a false thing about state they would go
# looking for. Only an exact notice can pin "claims retention only where
# retention was recorded", because the claim is about MEANING, and any wording
# a mutant invents is admitted by any test that looks for two words.
STOP_NOTICE = (
    "\u26a0\ufe0f This engagement stopped before reporting a result (Casa was "
    "stopping when this launch was cancelled). Its task may be incomplete "
    "\u2014 inspect any partial changes before retrying."
)
RETENTION_SENTENCE = (
    "\n\nIts workspace is retained for 7 days so its work can be recovered; "
    "after that it is deleted."
)


class _FixedUidAllocator:
    """The uid source ``EngagementRegistry.create`` calls for a `claude_code`
    record. The production allocator reads ``/etc/passwd``, ``/proc`` and a
    durable high-water file; none of that is under test here, and the record
    needs a REAL uid (``>= UID_BASE``) or the rollback's identity and outbox
    removals are unreachable and their counts would prove nothing."""

    def __init__(self) -> None:
        self._next = UID

    def allocate(self) -> int:
        uid, self._next = self._next, self._next + 1
        return uid


def _defn(tmp_path):
    from config import ExecutorDefinition
    exec_dir = tmp_path / "defaults-executors" / "hello-driver"
    exec_dir.mkdir(parents=True)
    (exec_dir / "prompt.md").write_text(
        "You are hello-driver. Task: {task}. {context} {world_state_summary}")
    return ExecutorDefinition(
        role_artifact=STUB_ROLE_ARTIFACT, type="hello-driver",
        description="Test harness executor type for the #698 red case.",
        model="sonnet", driver="claude_code", enabled=True,
        tools_allowed=["mcp__casa-framework__emit_completion"],
        tools_disallowed=[], permission_mode="dontAsk",
        mcp_server_names=["casa-framework"], idle_reminder_days=7,
        prompt_template_path=str(exec_dir / "prompt.md"),
        hooks_path=None, observer_policy_path=None, doctrine_dir="",
        plugins_dir="",
    )


async def _spin_until(predicate, *, what: str, limit: int = 4000):
    """Yield until ``predicate()`` — no sleeps, no wall clock.

    A bound on ITERATIONS is not an elapsed allowance (D21): it fires when the
    loop has run ``limit`` times without the awaited state appearing, which is
    a no-output condition, and it fails loudly instead of hanging the suite.
    """
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"never observed: {what}")


class _Channel:
    """A transport with a real round trip. ``latency`` is the modelled
    Telegram cost of one operator-visible operation — the reason the defect is
    latency-dependent and the reason this case is parameterized on it."""

    def __init__(self, latency: float, effects: list):
        self._lat, self.effects = latency, effects
        self.stopped = False

    async def stop(self):
        """What ``channel_manager.stop_all()`` does to a real transport.

        Modelled rather than mocked away, because "the stop's engagement step
        runs while the transport is still up" is a real ordering claim: with a
        no-op ``stop_all`` a mutant that moves the engagement step below the
        channel teardown keeps posting notices and stays green.
        """
        self.stopped = True

    def _check(self):
        if self.stopped:
            raise RuntimeError("channel stopped: nothing can be posted now")

    async def _post_engagement_notice(self, rec, text):
        self._check()
        await asyncio.sleep(self._lat)
        self._check()
        self.effects.append(("notice", text))

    async def abort_engagement_topic(self, engagement_id, topic_id):
        self._check()
        await asyncio.sleep(self._lat)
        self.effects.append(("topic_close", engagement_id))


class _Removals:
    """Records every rollback removal by exact target, in order.

    Absence cannot tell "the removal ran" from "it ran twice" and cannot see
    order at all, which is why this records a trace and the assertions read
    counts off it (the shape ``_RollbackProbe`` already uses for #755). Every
    shim delegates to the real function: this observes production, it does not
    replace it.
    """

    def __init__(self, monkeypatch):
        from drivers import claude_code_driver as ccd
        from drivers import s6_rc
        import plugin_outbox

        self.trace: list[tuple] = []
        real_remove_dir = s6_rc.remove_service_dir

        def _remove_service_dir(*, svc_root, engagement_id):
            self.trace.append(("remove_service_dir", engagement_id))
            return real_remove_dir(svc_root=svc_root,
                                   engagement_id=engagement_id)

        monkeypatch.setattr(s6_rc, "remove_service_dir", _remove_service_dir)

        class _ShutilShim:
            """Patched onto the DRIVER module's name only — a global patch
            would follow every other importer into the same test."""

            def __getattr__(_s, name):
                return getattr(_real_shutil, name)

            @staticmethod
            def rmtree(path, *a, **kw):
                self.trace.append(("rmtree", str(path)))
                return _real_shutil.rmtree(path, *a, **kw)

        monkeypatch.setattr(ccd, "shutil", _ShutilShim())
        monkeypatch.setattr(ccd, "prune_identity",
                            lambda u: self.trace.append(("prune_identity", u)))
        real_teardown = plugin_outbox.teardown_engagement_outbox

        def _teardown(u, *, root=None):
            self.trace.append(("teardown_outbox", u))
            return real_teardown(u, root=root)

        monkeypatch.setattr(plugin_outbox, "teardown_engagement_outbox",
                            _teardown)

    def counts(self, *, ws_path, ctl_path, uid) -> dict:
        t = self.trace
        return {
            "remove_service_dir": sum(1 for e in t
                                      if e[0] == "remove_service_dir"),
            "rmtree_workspace": sum(1 for e in t
                                    if e == ("rmtree", str(ws_path))),
            "rmtree_control": sum(1 for e in t
                                  if e == ("rmtree", str(ctl_path))),
            "prune_identity": sum(1 for e in t
                                  if e == ("prune_identity", uid)),
            "teardown_outbox": sum(1 for e in t
                                   if e == ("teardown_outbox", uid)),
        }


def _run_stop(monkeypatch, tmp_path, latency, *, meta_write_fails=False,
              extra_launches=0, suspend_at="compile"):
    """One whole graceful stop, end to end, and the facts it leaves behind.

    The launch is started by the production ``engage_executor`` handler, runs
    into the real ``ClaudeCodeDriver``, and is stopped by whatever
    ``casa_core``'s own cleanup does — then by ``asyncio.run``'s final sweep,
    which is the only canceller on a tree without this change.
    """
    import agent as agent_mod
    import casa_core
    import plugin_outbox
    import tools
    from drivers import claude_code_driver as ccd
    from drivers import s6_rc
    from drivers import workspace as ws_mod
    from drivers.claude_code_driver import ClaudeCodeDriver
    from engagement_registry import EngagementRegistry

    # Real provisioning; only the root-requiring steps are stubbed, exactly as
    # every other start()-level test in this tree stubs them.
    monkeypatch.setattr(ccd, "_preflight_uid_drop", lambda rec, ws: None)
    monkeypatch.setattr(ws_mod, "chown_workspace", lambda ws, uid, gid: None)
    monkeypatch.setattr(ws_mod, "ensure_identity", lambda uid, home: None)

    engagements_root = tmp_path / "engagements"
    engagements_root.mkdir()
    (tmp_path / "svc-root").mkdir()
    monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT",
                        str(tmp_path / "svc-root"))
    # A FRESH compile lock: the module-level one caches its loop the first time
    # it is contended, which would bind it to one test's loop for the worker.
    monkeypatch.setattr(s6_rc, "_compile_lock", asyncio.Lock())

    removals = _Removals(monkeypatch)
    compile_calls: list[int] = []
    # ``suspend_at`` chooses WHICH of the guarded region's awaits the launch is
    # sitting on when the stop cancels it. The rollback is entered from all
    # three, and a retention branch attached to one await rather than to the
    # handler would still satisfy a case that only ever suspends there — which
    # is a mutant the acceptor named. The gate is never set: only a cancel ends
    # the wait.
    assert suspend_at in ("compile", "summary", "service"), suspend_at
    reached = asyncio.Event()
    gate = asyncio.Event()

    async def _compile_fake():
        compile_calls.append(len(compile_calls) + 1)
        if len(compile_calls) == 1 and suspend_at == "compile":
            reached.set()
            await gate.wait()

    monkeypatch.setattr(s6_rc, "_compile_and_update_locked", _compile_fake)

    registry = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None,
        uid_allocator=_FixedUidAllocator())
    driver = ClaudeCodeDriver(
        engagements_root=str(engagements_root),
        send_to_topic=AsyncMock(),
        casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        registry=registry,
    )
    async def _summary(*_a, **_kw):
        if suspend_at == "summary":
            reached.set()
            await gate.wait()

    async def _service_fenced(*_a, **_kw):
        if suspend_at == "service":
            reached.set()
            await gate.wait()
        return True

    monkeypatch.setattr(driver, "_post_initial_summary", _summary)
    monkeypatch.setattr(driver, "_start_service_fenced", _service_fenced)
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver,
                        raising=False)

    defn = _defn(tmp_path)
    exec_reg = MagicMock()
    exec_reg.get = MagicMock(return_value=defn)
    exec_reg.list_types = MagicMock(return_value=["hello-driver"])

    effects: list = []
    live = _Channel(latency, effects)
    channel = MagicMock()
    channel.engagement_supergroup_id = -100123
    channel.engagement_permission_ok = True
    channel.open_engagement_topic = AsyncMock(return_value=999)
    channel.bot = MagicMock()
    channel.bot.edit_forum_topic = AsyncMock()
    channel._post_engagement_notice = live._post_engagement_notice
    channel.abort_engagement_topic = live.abort_engagement_topic
    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)
    cm.stop_all = live.stop
    tools.init_tools(
        channel_manager=cm, bus=MagicMock(), specialist_registry=MagicMock(),
        mcp_registry=MagicMock(), trigger_registry=MagicMock(),
        engagement_registry=registry, executor_registry=exec_reg)

    trace: list[str] = []
    cause_at_cancel: list[str] = []
    rollback_counts: dict[str, int] = {}
    state: dict = {}

    # --- pass-through spies. Each records that a PRODUCTION function ran and
    # then runs it, so deleting the production call site makes the spy silent
    # and the assertions below fail. None of them substitutes behaviour.
    real_abort = tools._abort_launch_on_cancel

    def _spy_abort(channel_, rec_, topic_id_):
        # The rollback has finished by the time the cancellation arm runs, and
        # the reporter's bounded ``driver.cancel`` has not started: this is the
        # only instant at which the ROLLBACK's own removals can be told apart
        # from the teardown's, which legitimately removes the service source
        # and recompiles a moment later. Counting at the end would attribute
        # two owners' work to one.
        trace.append("launch_cancel")
        _lc = getattr(registry, "launch_cause", None)
        cause_at_cancel.append(_lc(rec_.id) if callable(_lc) else "")
        if rec_.id == state["eng_id"]:
            # Only the launch whose rollback these counts describe. A second
            # in-flight launch unwinds from the compile lock without entering
            # the rollback at all, and folding its arm's snapshot in here would
            # re-count the first launch's removals under the second's name.
            rollback_counts.update(removals.counts(
                ws_path=state["ws_path"], ctl_path=state["ctl_path"],
                uid=state["uid"]))
            # The FORWARD compile is call 1 whether it completed or was
            # cancelled; anything beyond it is the rollback's own recompile.
            rollback_counts["rollback_recompile"] = max(
                0, len(compile_calls) - 1)
        return real_abort(channel_, rec_, topic_id_)

    monkeypatch.setattr(tools, "_abort_launch_on_cancel", _spy_abort)

    real_ttt = registry.try_transition_terminal

    async def _traced_ttt(*a, **kw):
        won = await real_ttt(*a, **kw)
        if won and kw.get("strict") is True:
            # STRICT, and the flag is READ rather than assumed. ``strict=True``
            # is persist-or-rollback, so a True return means the row is on
            # disk; the non-strict path swallows a tombstone failure and leaves
            # the record terminal in memory and LIVE on disk, which is exactly
            # the witness that must not authorize terminal workspace metadata.
            # Labelling every truthy return would let a ``strict=False`` mutant
            # keep the ordering assertion green.
            trace.append("strict_terminal_commit")
        return won

    monkeypatch.setattr(registry, "try_transition_terminal", _traced_ttt)

    _real_begin = getattr(registry, "begin_launch_shutdown", None)
    if _real_begin is not None:
        def _traced_begin(*a, **kw):
            trace.append("record_shutdown_cause")
            return _real_begin(*a, **kw)
        monkeypatch.setattr(registry, "begin_launch_shutdown", _traced_begin)

    _real_meta = getattr(ws_mod, "write_terminal_casa_meta", None)
    if _real_meta is not None:
        def _traced_meta(*a, **kw):
            trace.append("terminal_meta_write")
            if meta_write_fails:
                # The writer's own failure arm: it reports False and writes
                # nothing. Everything downstream must believe it.
                return False
            return _real_meta(*a, **kw)
        monkeypatch.setattr(ws_mod, "write_terminal_casa_meta", _traced_meta)

    async def _main():
        """The production launch, then the production stop."""
        # Set INSIDE the main coroutine: a task copies the context at
        # creation, so the launch inherits this, and nothing leaks back into
        # the test's own context when asyncio.run returns.
        agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "42", "cid": "x", "user_text": "hi",
            "_operator_turn": True,
        })
        call = asyncio.ensure_future(tools.engage_executor.handler(
            {"executor_type": "hello-driver", "task": "say hello",
             "context": ""}))
        # Surface an early RETURN (a gate refused the launch) as itself rather
        # than as "never observed", which would send the next reader looking in
        # the wrong place. A real (tiny) yield, not ``sleep(0)``:
        # ``EngagementRegistry.create`` persists its tombstone through
        # ``asyncio.to_thread``, and a loop that only re-enters itself never
        # gives that thread's completion a chance to land. The bound is on
        # POLLS WITH NO OUTPUT, never on elapsed work (D21).
        for _ in range(4000):
            if reached.is_set() or call.done():
                break
            await asyncio.sleep(0.001)
        assert reached.is_set(), (
            "the production engage_executor arm never reached the driver's "
            f"{suspend_at} await"
            + (f"; it returned {call.result()!r}" if call.done() else ""))

        rec = next(iter(registry._records.values()))
        state["eng_id"] = rec.id

        # Further in-flight launches, if the case asks for them. Each is
        # another production handler call; it enrols its own record and then
        # blocks on the driver's compile lock, which the first launch holds —
        # so it is a launch the stop knows about that has not yet reached the
        # rollback. "Every launch the stop found" is a plural claim, and one
        # launch cannot witness it.
        _extra = []
        # A DISTINCT task text per launch: the handler's duplicate-task gate
        # refuses a second engagement whose task overlaps the first at Jaccard
        # >= 0.5, and a refused launch is not an in-flight one.
        for _n in range(extra_launches):
            _extra.append(asyncio.ensure_future(tools.engage_executor.handler(
                {"executor_type": "hello-driver",
                 "task": f"reconcile ledger {_n} for the quarterly audit",
                 "context": ""})))
        for _ in range(4000):
            if len(registry._records) >= 1 + extra_launches:
                break
            await asyncio.sleep(0.001)
        assert len(registry._records) == 1 + extra_launches, (
            f"expected {1 + extra_launches} live records, got "
            f"{len(registry._records)}; extra returned "
            f"{[t.result() if t.done() else 'pending' for t in _extra]}")
        state["uid"] = rec.allocated_uid
        state["ws_path"] = engagements_root / rec.id
        state["ctl_path"] = Path(ws_mod.control_dir(rec.id))
        state["outbox_path"] = Path(plugin_outbox.engagement_outbox_dir(
            rec.allocated_uid))
        (state["ws_path"] / "REPORT.md").write_text(
            "the executor's only copy of its work")

        # The PRODUCTION cleanup, EXECUTED — not re-implemented, and not merely
        # asserted to contain a call. Inlining the stop's statements here would
        # leave a mutant that deleted them from the real block green; driving a
        # helper directly would leave one that deleted the CALL green; and
        # asserting the call in ``main``'s AST would leave ``if False:`` green.
        # The only thing that closes all three is running the block that holds
        # the call.
        cleanup = getattr(casa_core, "_shutdown_cleanup", None)
        if callable(cleanup):
            await cleanup(
                job_registry=MagicMock(begin_shutdown=MagicMock(),
                                       close=AsyncMock()),
                engagement_registry=registry,
                scheduler=MagicMock(shutdown=MagicMock()),
                session_sweeper=MagicMock(stop=AsyncMock()),
                freshness_reaper=MagicMock(stop=AsyncMock()),
                runtime=MagicMock(agents={}, claude_code_driver=None),
                ha_facade=None,
                bus=MagicMock(begin_shutdown=MagicMock(),
                              agent_loop_tasks=MagicMock(return_value=[]),
                              fail_pending=MagicMock()),
                loop_tasks=[],
                channel_manager=cm,
                runners=[],
                semantic_memory=MagicMock(close=AsyncMock()),
            )
        # main() returns here. Everything after this point is asyncio.run's own
        # shutdown, whose _cancel_all_tasks is the edge a stop really delivers —
        # and pre-fix, with no cleanup helper to call, it is the ONLY canceller.

    written_after = time.time()
    asyncio.run(_main())
    written_before = time.time()

    eng_id = state.get("eng_id")
    tomb = json.loads((tmp_path / "engagements.json").read_text())
    rows = tomb if isinstance(tomb, list) else tomb.get("records", [])
    row = next((r for r in rows if r.get("id") == eng_id), {})
    origin = row.get("origin", {})
    meta_path = state["ctl_path"] / ".casa-meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    retention = meta.get("retention_until")
    notices = [t for k, t in effects if k == "notice"]

    def _seven_days_out(value) -> int:
        """Is ``retention_until`` an ISO instant SEVEN DAYS after it was
        written — not merely a parseable one?

        A deadline of "now" parses exactly as well as a real one and lets the
        very next sweep reap a workspace the operator was just told would be
        kept for a week, which is the same false promise as the notice
        claiming a window that was never recorded. Bounded by the two wall
        clocks that bracket the whole run, so the window is the run's own
        duration and nothing else.
        """
        if not isinstance(value, str):
            return 0
        try:
            when = calendar.timegm(time.strptime(value, ISO))
        except ValueError:
            return 0
        return int(written_after + 7 * 86400 - 2
                   <= when <= written_before + 7 * 86400 + 2)

    rows = [
        {"status": r.get("status"),
         "error_kind": (r.get("origin") or {}).get("error_kind"),
         "error_message": (r.get("origin") or {}).get("error_message"),
         "shutdown_reason": (r.get("origin") or {}).get("shutdown_reason")}
        for r in rows
    ]

    return {
        "rows": rows,
        "status": row.get("status"),
        "error_kind": origin.get("error_kind"),
        "error_message": origin.get("error_message"),
        "shutdown_reason": origin.get("shutdown_reason"),
        "remove_service_dir": rollback_counts.get("remove_service_dir"),
        "rollback_recompile": rollback_counts.get("rollback_recompile"),
        "rmtree_workspace": rollback_counts.get("rmtree_workspace"),
        "rmtree_control": rollback_counts.get("rmtree_control"),
        "prune_identity": rollback_counts.get("prune_identity"),
        "teardown_outbox": rollback_counts.get("teardown_outbox"),
        "report_survives": int((state["ws_path"] / "REPORT.md").exists()),
        "control_survives": int(state["ctl_path"].is_dir()),
        "outbox_survives": int(state["outbox_path"].is_dir()),
        "meta_terminal": int(meta.get("status") == "ERROR"),
        "meta_has_iso_deadline": _seven_days_out(retention),
        "total_notices": len(notices),
        "notice_texts": notices,
        "retention_notices": sum(
            1 for t in notices if t == STOP_NOTICE + RETENTION_SENTENCE),
        "trace": trace,
        "cause_at_cancel": cause_at_cancel,
    }


@pytest.mark.skipif(os.name != "posix",
                    reason="workspace provisioning uses mkfifo/symlink")
@pytest.mark.parametrize("latency", [0.05, 0.0])
@pytest.mark.parametrize("suspend_at", ["compile", "summary", "service"])
def test_a_graceful_stop_retains_a_claude_code_launch_and_says_why(
    monkeypatch, tmp_path, latency, suspend_at,
):
    """A graceful stop cancels a launch it has already told the truth about.

    One tuple, so a partial fix cannot pass: the durable record, every removal
    the rollback did and did not run, what survives on disk, and whether the
    operator was told something true.

    The recorded ``error_message`` deviates from the pre-fix sentence
    deliberately. "the tool call was cancelled during launch" is #698's own
    defect — nothing cancelled the tool call; the process is stopping — and
    pinning it here would freeze the false record this change exists to
    correct. The KIND is unchanged: ``launch_cancelled`` remains the outcome
    and ``shutdown_reason`` is the separate signal beside it, which is D32's
    "both facts travel … neither may overwrite the other".

    ``suspend_at`` is the await the launch is sitting on when the stop cancels
    it. All three enter the same rollback handler, and the retention decision
    must belong to the handler rather than to one await: a branch attached only
    to the forward compile would leave a stop that lands during the initial
    summary post or the fenced service start destroying the workspace anyway.
    """
    facts = _run_stop(monkeypatch, tmp_path, latency, suspend_at=suspend_at)

    # The operator-visible text, WHOLE. A substring pair ("retained", "7 days")
    # admits any wording a mutant invents, including one that promises a
    # retention window the metadata never recorded.
    assert facts["notice_texts"] == [STOP_NOTICE + RETENTION_SENTENCE]

    assert (
        facts["status"], facts["error_kind"], facts["error_message"],
        facts["shutdown_reason"],
        facts["remove_service_dir"], facts["rollback_recompile"],
        facts["rmtree_workspace"], facts["rmtree_control"],
        facts["prune_identity"], facts["teardown_outbox"],
        facts["report_survives"], facts["control_survives"],
        facts["outbox_survives"],
        facts["meta_terminal"], facts["meta_has_iso_deadline"],
        facts["retention_notices"],
    ) == (
        # the durable engagement record: the outcome, and the stop beside it
        "error",
        "launch_cancelled",
        "Casa was stopping when this launch was cancelled",
        "casa_shutdown",

        # the rollback's per-cause removal set
        1,   # remove_service_dir — a service source is not the executor's work
        1,   # the rollback recompile still runs
        0,   # workspace rmtree
        0,   # control-dir rmtree
        0,   # prune_identity — uids are never reused (INV-CONT-001)
        0,   # teardown_engagement_outbox

        # what is still on disk afterwards
        1,   # REPORT.md — the executor's only copy of its work
        1,   # the control directory that holds .casa-meta.json
        1,   # the uid's private outbox

        # and retention that ENDS: a terminal meta with a real deadline
        1,   # .casa-meta.json status == "ERROR"
        1,   # retention_until is an ISO instant SEVEN DAYS after the write

        # one notice that actually landed, claiming only what is true
        1,
    )


@pytest.mark.skipif(os.name != "posix",
                    reason="workspace provisioning uses mkfifo/symlink")
@pytest.mark.parametrize("latency", [0.05, 0.0])
@pytest.mark.parametrize("suspend_at", ["compile", "summary", "service"])
def test_the_stop_records_its_cause_before_it_cancels_and_metas_after_it_commits(
    monkeypatch, tmp_path, latency, suspend_at,
):
    """Two orderings, and both are the difference between a fact and a guess.

    The first is INV-JOB-009's doctrine applied to this ledger — *the
    discriminator is the cause of the settling, and cause is carried, not
    inferred*: at the instant the production cancellation arm runs, the cause
    must ALREADY be recorded against this launch, because the site that
    cancelled it wrote it there first. Reversing the two lines leaves the arm
    reading ``""`` and reporting a generic cancellation, with nothing in the
    tree to say so.

    The second is why terminal workspace metadata cannot be written before the
    durable terminal record: metadata that says "terminal, reap after seven
    days" while the record is still live has boot replay resume the engagement
    into that workspace and the sweep delete it underneath the resumed CLI.
    """
    facts = _run_stop(monkeypatch, tmp_path, latency, suspend_at=suspend_at)

    assert facts["cause_at_cancel"] == ["casa_shutdown"]
    trace = facts["trace"]
    assert "strict_terminal_commit" in trace, (
        "the terminal transition that authorizes retention metadata must be "
        "strict=True — a non-strict flip can leave the record live on disk")
    assert trace.index("record_shutdown_cause") < trace.index("launch_cancel")
    assert (trace.index("strict_terminal_commit")
            < trace.index("terminal_meta_write"))
    # EXACTLY ONE write, not merely a first one in the right order. ``index``
    # alone cannot see a second write: a writer called twice with the same
    # terminal status and deadline leaves the record, the survivors, the notice
    # and the final metadata byte-identical, so every other assertion in this
    # file stays green while the metadata is written more than once for one
    # retained workspace.
    assert trace.count("terminal_meta_write") == 1


@pytest.mark.skipif(os.name != "posix",
                    reason="workspace provisioning uses mkfifo/symlink")
@pytest.mark.parametrize("latency", [0.05, 0.0])
@pytest.mark.parametrize("suspend_at", ["compile", "summary", "service"])
def test_a_failed_retention_write_is_not_announced_as_retention(
    monkeypatch, tmp_path, latency, suspend_at,
):
    """The notice may claim only what the metadata write actually recorded.

    The workspace is still retained here — the rollback kept it, and keeping it
    is right — but its ``.casa-meta.json`` is still ``UNDERGOING``, and the
    sweep returns on ``UNDERGOING`` before it reads any deadline. So there is
    no seven-day window, and telling the operator there is one is a durable
    false promise about state they will go looking for.

    A reporter that posts the retention sentence without consulting the
    writer's boolean stays green on every happy-path case, because the happy
    path always returns True. The operator is still told — exactly one notice —
    and the retained workspace is still there. Only the claim about expiry is
    withheld.
    """
    facts = _run_stop(monkeypatch, tmp_path, latency, meta_write_fails=True,
                      suspend_at=suspend_at)

    # One ATTEMPT, and it reported False. A path that retries the write after a
    # refusal would leave the count above one while every outcome below is
    # unchanged.
    assert facts["trace"].count("terminal_meta_write") == 1
    # EXACTLY the stopped-launch notice and nothing after it: no retention
    # sentence, and no differently-worded promise of one either.
    assert facts["notice_texts"] == [STOP_NOTICE]

    assert (
        facts["total_notices"], facts["retention_notices"],
        facts["meta_terminal"], facts["report_survives"],
        facts["shutdown_reason"],
    ) == (
        1,    # the operator is still told the launch was stopped
        0,    # but nothing is promised about a retention window
        0,    # because the metadata really is still UNDERGOING
        1,    # and the executor's only copy of its work is still there
        "casa_shutdown",
    )


@pytest.mark.skipif(os.name != "posix",
                    reason="workspace provisioning uses mkfifo/symlink")
@pytest.mark.parametrize("latency", [0.05, 0.0])
@pytest.mark.parametrize("suspend_at", ["compile", "summary", "service"])
def test_the_stop_covers_every_in_flight_launch_not_only_the_first(
    monkeypatch, tmp_path, latency, suspend_at,
):
    """INV-ENG-015 says *each* such launch — so one launch cannot witness it.

    The acceptor refused a single-launch case with a concrete mutant: a stop
    that returns after the first launch's reporter completes satisfies every
    other assertion in this file, while a second in-flight launch is neither
    cause-recorded nor awaited. Two launches are what falsifies it.

    The second launch is a real production handler call that enrols its record
    and then blocks on the compile lock the first launch holds. It therefore
    never enters the rollback — its unwind is the cancellation arm alone — which
    is why this case asserts the DURABLE RECORD and the NOTICE for both, and
    leaves the removal set to the cases above.
    """
    facts = _run_stop(monkeypatch, tmp_path, latency, extra_launches=1,
                      suspend_at=suspend_at)

    assert facts["cause_at_cancel"] == ["casa_shutdown", "casa_shutdown"]
    assert len(facts["rows"]) == 2
    assert [
        (r["status"], r["error_kind"], r["error_message"],
         r["shutdown_reason"])
        for r in facts["rows"]
    ] == [
        ("error", "launch_cancelled",
         "Casa was stopping when this launch was cancelled", "casa_shutdown"),
    ] * 2
    # One completed death report per launch, both landed before the stop
    # returned — the clause a single-launch case cannot see. The second launch
    # never provisioned a workspace (it unwinds from the compile lock), so its
    # notice must claim no retention, whole-text.
    assert sorted(facts["notice_texts"]) == sorted(
        [STOP_NOTICE + RETENTION_SENTENCE, STOP_NOTICE])


def test_main_awaits_the_shutdown_cleanup_unconditionally_after_the_stop_wait():
    """The one edge the behavioural cases cannot witness: `main` calling it.

    The cases above EXECUTE `casa_core._shutdown_cleanup` — they do not
    re-implement it, and every mutant inside it turns them red. What they
    cannot see is whether `main` still calls it: they obtain the coroutine and
    await it themselves, so deleting the call from `main`'s stop block leaves
    them green while a real SIGTERM falls through to bare task cancellation.
    An acceptance round named exactly that mutant.

    This is a STRUCTURAL pin and it is weaker than the cases above, which is
    said here rather than left to be discovered: it reads `main`'s AST. It is
    written to kill the three mutants that shape admits — the call DELETED, the
    call made unreachable (`if False:`, or any other conditional nesting), and
    the call moved ahead of the stop-event wait — by requiring the await to sit
    as a DIRECT, unconditional statement of `main`'s own body, after the
    statement that waits for the stop signal. It cannot see a reordering WITHIN
    the stop block, and INV-ENG-015 accordingly claims nothing about `main`'s
    internal ordering: its subject is the cleanup step itself.
    """
    import ast
    import inspect

    import casa_core

    tree = ast.parse(inspect.getsource(casa_core))
    main = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                 and n.name == "main"), None)
    assert main is not None, "casa_core.main is gone"

    def _mentions(node, needle: str) -> bool:
        return any(isinstance(n, ast.Name) and n.id == needle
                   or isinstance(n, ast.Attribute) and n.attr == needle
                   for n in ast.walk(node))

    stop_wait_at = [i for i, st in enumerate(main.body)
                    if _mentions(st, "stop_event") and _mentions(st, "wait")]
    cleanup_at = [i for i, st in enumerate(main.body)
                  if isinstance(st, ast.Expr)
                  and isinstance(st.value, ast.Await)
                  and _mentions(st.value, "_shutdown_cleanup")]

    assert stop_wait_at, "main no longer waits on the stop event"
    assert cleanup_at, (
        "main does not await _shutdown_cleanup as a direct, unconditional "
        "statement of its own body — a call nested under any conditional, or "
        "deleted outright, leaves a real graceful stop falling through to bare "
        "task cancellation while every behavioural case above stays green")
    assert min(cleanup_at) > max(stop_wait_at), (
        "main awaits _shutdown_cleanup before it waits for the stop signal")


# ---------------------------------------------------------------------------
# #767 — the delegation SETTLE tail and the graceful stop's ledger-close boundary
# ---------------------------------------------------------------------------
#
# Red case for **INV-JOB-011**, declared under D34: at the graceful stop's
# job-ledger close boundary, every delegation that has already produced a
# success or non-cancellation verdict has had its completion callback run, its
# terminal-write attempt made, and every resulting settle tail awaited — so a
# delegation that finished during the stop reaches the ledger as its real
# outcome, never as a settle tail killed before its first step.
#
# Specified externally (terra, MODE: SPECIFY) against
# 560cf22af557df7eb15fead24691f7a718daa40d; accepted by sol. Written before any
# production change.
#
# What is REAL here: the ``JobRegistry`` on disk, the ``SpecialistRegistry``
# over it, the production ``tools._attach_completion_callback`` (the ONLY
# producer of a settle tail) over real ``asyncio.Task``s, and the production
# ``casa_core._shutdown_cleanup`` executed inside its own ``asyncio.run`` so
# that ``_cancel_all_tasks`` is the post-cleanup canceller, exactly as in
# ``main``. What is faked completes WITHOUT yielding unless the arm needs a
# window, so a missed tail is never handed an incidental extra turn.
#
# Every assertion is on durable facts — the rows at the ledger-close boundary
# and the rows a fresh registry recovers — and on notice counts. Nothing here
# reads a task set, calls a drain, or asserts a trace of the chosen
# implementation: an arrangement assertion would pass a fix that arranged
# without delivering.
#
# The three interleavings, each a falsifier the design named:
#
# * EARLY — its verdict landed BEFORE the stop; its tail is pending when the
#   cleanup begins. Its notice must be enqueued before any resident's
#   ``aclose()`` is entered (a live pool is what lets the notice be told now).
# * LATE-A / LATE-B — A finishes inside a resident's ``aclose()`` window; A's
#   notice is held until the statement immediately before the ledger-close
#   boundary, then releases B's RUN and awaits it. B's tail is therefore born
#   strictly after any snapshot that saw A, and its write has not published
#   when A's tail completes: a drain that gathers ONE snapshot closes the
#   ledger with B still live.
# * LATE-C — released by the statement immediately before the boundary, on a
#   loop turn that lets C's run complete but leaves C's production done
#   callback QUEUED: at the boundary C is done and has no tail yet. A drain
#   that looks only at the tail set closes the ledger with C still live. Run
#   with a successful verdict and with a raised ``RuntimeError``, whose typed
#   kind must survive as itself.


class _StopOutput:
    """The shape the production completion callback reads off a finished
    delegated run (``run_aborted``, ``text``)."""

    def __init__(self, text: str):
        self.text = text
        self.run_aborted = False
        self.run_subtype = "success"
        self.answer_incomplete = False


def _run_delegation_stop(monkeypatch, tmp_path, *, late_pair: bool,
                         queued: bool, queued_raises: bool = False) -> dict:
    """One graceful stop over real delegation tails, and the facts it leaves."""
    import casa_core
    import tools
    from job_registry import ExecutionState, JobRegistry
    from specialist_registry import DelegationRecord, SpecialistRegistry

    jobs_path = tmp_path / "jobs.json"
    ids = ["early"] + (["late-a", "late-b"] if late_pair else []) \
        + (["late-c"] if queued else [])
    origin = {"role": "assistant", "channel": "telegram", "chat_id": "42",
              "cid": "x"}

    flags = {"aclose_entered": False, "begin_shutdown": False}
    gates: dict[str, asyncio.Event] = {}
    tasks: dict[str, asyncio.Task] = {}
    notices: list[tuple] = []          # (delegation_id, aclose_entered)
    at_close: dict = {}

    class _Bus:
        def begin_shutdown(self):
            flags["begin_shutdown"] = True

        def agent_loop_tasks(self):
            return []

        def fail_pending(self):
            pass

        async def notify(self, msg):
            did = msg.context["delegation_id"]
            if did == "late-a":
                # Held until the statement before the boundary; then A's
                # notice releases B's RUN and waits for the run alone — never
                # for B's tail, which does not exist yet.
                await gates["boundary"].wait()
                gates["late-b"].set()
                await tasks["late-b"]
            notices.append((did, flags["aclose_entered"]))

    class _Agent:
        async def aclose(self):
            flags["aclose_entered"] = True
            if late_pair:
                gates["late-a"].set()
                await _spin_until(lambda: tasks["late-a"].done(),
                                  what="LATE-A's run finished in the "
                                       "aclose window")
            else:
                # The window a real pool drain is: one turn, nothing more.
                await asyncio.sleep(0)

    class _Driver:
        async def drain_force_cleanups(self):
            # The statement immediately before the ledger-close boundary.
            gates["boundary"].set()
            if queued:
                gates["late-c"].set()
                # C's run is queued AHEAD of this continuation: it completes
                # on this turn, and its production done callback lands
                # BEHIND the continuation — so the boundary is reached with
                # C done and no tail yet.
                await asyncio.sleep(0)

    async def _run(did: str):
        await gates[did].wait()
        if did == "late-c" and queued_raises:
            raise RuntimeError("boom")
        return _StopOutput(f"answer from {did}")

    async def _main():
        registry = JobRegistry(jobs_path)
        await registry.load()
        spec = SpecialistRegistry(str(tmp_path / "specialists"),
                                  job_registry=registry)
        bus = _Bus()
        cm = MagicMock()
        cm.stop_all = AsyncMock()
        tools.init_tools(
            channel_manager=cm, bus=bus, specialist_registry=spec,
            mcp_registry=MagicMock(), trigger_registry=MagicMock(),
            engagement_registry=MagicMock(), executor_registry=MagicMock())

        for did in ids:
            gates[did] = asyncio.Event()
        gates["boundary"] = asyncio.Event()
        for did in ids:
            rec = DelegationRecord(id=did, agent="researcher",
                                   started_at=time.time(), origin=origin)
            await spec.register_delegation(rec)
            tasks[did] = asyncio.create_task(_run(did))
            # The PRODUCTION callback — the only thing that mints a tail.
            tools._attach_completion_callback(tasks[did], rec)

        # EARLY's verdict lands before the stop. Its run is awaited to
        # completion; its done callback has run; its tail is pending.
        gates["early"].set()
        await tasks["early"]
        assert tasks["early"].done()
        assert registry.get("early").execution_state is ExecutionState.RUNNING

        real_close = registry.close

        async def _close():
            # The ledger-close boundary: durable facts only.
            at_close.update({
                did: registry.get(did).execution_state for did in ids})
            return await real_close()

        monkeypatch.setattr(registry, "close", _close)

        engagement_registry = MagicMock()
        engagement_registry.begin_launch_shutdown = MagicMock(return_value=0)
        engagement_registry.drain_launches = AsyncMock()

        # main's own two statements after the stop wait, in order.
        registry.begin_shutdown()
        await casa_core._shutdown_cleanup(
            job_registry=registry,
            engagement_registry=engagement_registry,
            scheduler=MagicMock(shutdown=MagicMock()),
            session_sweeper=MagicMock(stop=AsyncMock()),
            freshness_reaper=MagicMock(stop=AsyncMock()),
            runtime=MagicMock(agents={"assistant": _Agent()},
                              claude_code_driver=_Driver()),
            ha_facade=None,
            bus=bus,
            loop_tasks=[],
            channel_manager=cm,
            runners=[],
            semantic_memory=MagicMock(close=AsyncMock()),
        )
        # main() returns here; asyncio.run's _cancel_all_tasks is next.

    asyncio.run(_main())

    async def _boot():
        restarted = JobRegistry(jobs_path)
        await restarted.load()
        await restarted.recover_after_restart()
        return {did: restarted.get(did) for did in ids}

    rows = asyncio.run(_boot())
    return {
        "ids": ids,
        "rows": rows,
        "converted": sum(1 for r in rows.values()
                         if r.execution_state is ExecutionState.ORPHANED),
        "restart_orphans": sum(
            1 for r in rows.values()
            if r.failure is not None and r.failure.kind == "restart_orphan"),
        "at_close": at_close,
        "notices": notices,
    }


def _assert_settled(facts: dict, *, failed: dict | None = None) -> None:
    """The durable promise, in counts first and states second."""
    from job_registry import ExecutionState

    failed = failed or {}
    ids = facts["ids"]
    rows = facts["rows"]
    # 1. Recovery converted NOTHING — no row was live at boot.
    assert facts["converted"] == 0, facts
    assert facts["restart_orphans"] == 0, facts
    # 2. Every row is its own verdict, and still owes its announcement.
    for did in ids:
        row = rows[did]
        if did in failed:
            assert row.execution_state is ExecutionState.FAILED, (did, row)
            assert row.failure is not None and row.failure.kind == failed[did], (
                did, row.failure)
        else:
            assert row.execution_state is ExecutionState.SUCCEEDED, (did, row)
            assert row.failure is None, (did, row)
        assert row.terminal_notification_pending is True, (did, row)
        assert row.orphan_notification_pending is False, (did, row)
    # 3. At the ledger-close boundary every row was already terminal.
    live = {ExecutionState.RUNNING, ExecutionState.ACCEPTED}
    assert set(facts["at_close"]) == set(ids), facts["at_close"]
    assert sum(1 for s in facts["at_close"].values() if s in live) == 0, (
        facts["at_close"])
    assert sum(1 for s in facts["at_close"].values()
               if s is ExecutionState.SUCCEEDED) == len(ids) - len(failed)
    assert sum(1 for s in facts["at_close"].values()
               if s is ExecutionState.FAILED) == len(failed)
    # 4. Exactly one notice per delegation.
    assert sorted(d for d, _ in facts["notices"]) == sorted(ids), facts["notices"]
    # 5. A verdict that landed BEFORE the stop is told while the resident
    #    pools are still up — before any aclose() was entered.
    early = [entered for d, entered in facts["notices"] if d == "early"]
    assert early == [False], facts["notices"]


def test_a_graceful_stop_settles_a_verdict_that_landed_before_it(
    monkeypatch, tmp_path,
):
    """EARLY alone: the tail that was pending when the stop began."""
    facts = _run_delegation_stop(monkeypatch, tmp_path, late_pair=False,
                                 queued=False)
    _assert_settled(facts)


def test_a_graceful_stop_settles_a_verdict_born_while_it_was_already_draining(
    monkeypatch, tmp_path,
):
    """LATE-A / LATE-B: B's tail is born after any snapshot that saw A."""
    facts = _run_delegation_stop(monkeypatch, tmp_path, late_pair=True,
                                 queued=False)
    _assert_settled(facts)


@pytest.mark.parametrize("raises", [False, True])
def test_a_graceful_stop_settles_a_run_whose_callback_is_still_queued(
    monkeypatch, tmp_path, raises,
):
    """LATE-C: done at the boundary, callback queued, no tail yet — as a
    success and as a typed failure whose kind must survive as itself."""
    import tools

    facts = _run_delegation_stop(monkeypatch, tmp_path, late_pair=False,
                                 queued=True, queued_raises=raises)
    failed = ({"late-c": tools._classify_error(RuntimeError("boom")).value}
              if raises else {})
    assert "restart_orphan" not in failed.values()
    _assert_settled(facts, failed=failed)
