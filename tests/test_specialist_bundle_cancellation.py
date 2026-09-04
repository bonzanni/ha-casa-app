"""#828 (INV-SPEC-013) — a cancelled specialist bundle handler still finishes
its transaction.

The four bundle tool handlers commit their generation in a worker thread, run
an async post-commit sequencer, and only then complete the journal that says
"undo me at boot". ``CancelledError`` is a ``BaseException``, so their
``except Exception`` arm neither compensates nor completes: a cancelled handler
leaves a committed generation beside a live undo record, and ``reconcile_boot``
replays that record blind over whatever generation was committed afterwards.

Every case here drives the REAL tool handlers and the REAL lifecycle library
over the shipped bundle fixtures on ``tmp_path``; only the post-commit sequencer
is replaced, and only by a controllable gate around the real operation boundary.
Assertions are COUNTS and on-disk generations — never statuses, never
``thread.is_alive()``.

Specified externally (sol, red-case round, MODE: SPECIFY); acceptance runs
against the tests-only commit that carries this file.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import plugin_registry
import specialist_bundle_journal
import specialist_install
import specialist_receipt
import tools as tools_mod
from broker_helpers import wait_until
from test_specialist_lifecycle_lock import _Handshake, _fresh_plugin_tools_lock
from test_specialist_rollback_owned_generation import (
    _SLUG, _install, _prep_bundle, _read_doc, _sidecar_paths, _slug_dir,
    _upgrade, generation_rows, registry_rows,
)

_TIMEOUT = 10.0
# How long a would-be surviving child is given to expose a library dispatch in
# the arms that require ZERO. A positive window, not a fixed sleep: the wait
# ends the moment the forbidden call happens, and its absence is the assertion.
_NEGATIVE_WINDOW = 0.75


@pytest.fixture(autouse=True)
def _fresh_registry_snapshot(tmp_path):
    plugin_registry.reload_snapshot(registry_path=tmp_path / "snap-registry.json",
                                    store_root=tmp_path / "snap-store")
    yield


async def _drain_loop(turns: int = 12) -> None:
    """Give the loop enough turns to DELIVER a requested cancellation before the
    test releases whatever the handler is parked on. `Task.cancel()` only
    schedules; a task suspended on a future is woken by one `call_soon` hop, so
    a handful of turns is deterministic. Every arm that uses this also asserts
    the cancellation COUNT, so a delivery that somehow arrived late fails the
    test rather than passing it."""
    for _ in range(turns):
        await asyncio.sleep(0)


def _generation(ctx) -> tuple:
    """The release triple: active tuple root (None when absent), active sidecar
    rows, registry owned rows."""
    from personality_binding import InstanceDir
    slug_dir = _slug_dir(ctx)
    root = None
    if (slug_dir / "active.yaml").is_file():
        root = InstanceDir(slug_dir).active().root
    active_sidecar, _ = _sidecar_paths(slug_dir)
    return (root, generation_rows(_read_doc(active_sidecar)),
            registry_rows(ctx.kw["registry_path"]))


def _in_progress(ops_dir: Path) -> int:
    if not ops_dir.is_dir():
        return 0
    return sum(1 for p in ops_dir.glob("*.json")
               if json.loads(p.read_text()).get("state") == "in-progress")


def _replayable(ops_dir: Path) -> int:
    if not ops_dir.is_dir():
        return 0
    return sum(1 for p in ops_dir.glob("*.json")
               if specialist_bundle_journal.classify_journal(p)[0]
               == specialist_bundle_journal.JOURNAL_REPLAY)


def _boot(ctx) -> list:
    return specialist_bundle_journal.reconcile_boot(
        ops_dir=ctx.kw["ops_dir"], registry_path=ctx.kw["registry_path"],
        specialists_dir=ctx.kw["specialists_dir"], acks_path=ctx.acks.path,
        receipts_dir=ctx.kw["specialists_dir"] / ".receipts",
        agents_specialists_dir=ctx.kw["agents_specialists_dir"])


def _rolled_back(actions: list) -> int:
    return sum(1 for a in actions if a.get("action") == "rolled_back")


# ---------------------------------------------------------------------------
# The binding harness: real handlers, real library, this tmp tree
# ---------------------------------------------------------------------------

_LIB_EXTRA = {
    "commit_specialist_install": ("specialists_dir", "agents_specialists_dir",
                                  "registry_path", "plugin_store_root", "ops_dir"),
    "upgrade_specialist": ("specialists_dir", "agents_specialists_dir",
                           "registry_path", "plugin_store_root", "ops_dir"),
    "rollback_specialist": ("specialists_dir", "agents_specialists_dir",
                            "registry_path", "plugin_store_root", "ops_dir"),
    "uninstall_specialist": ("specialists_dir", "agents_specialists_dir",
                             "registry_path", "ops_dir"),
}


class _Sequencer:
    """The post-commit sequencer, gated. Call N returns ``results[N-1]`` (the
    last entry repeats); the FIRST call optionally parks on an asyncio Event so
    a cancellation can be delivered while the handler is inside it."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.gate: "asyncio.Event | None" = None
        self.results: list[dict] = []

    def _result(self, removed) -> dict:
        base = {"ok": True, "kind": None, "reloaded": [], "reload_errors": [],
                "not_ready": [], "absent_violations": [], "verify": {},
                "removed_artifact_ids": list(removed)}
        if not self.results:
            return base
        override = self.results[min(len(self.calls), len(self.results)) - 1]
        base.update(override)
        return base

    async def __call__(self, slug, *, removed_artifact_ids, targets_removed):
        self.calls.append({"slug": slug, "removed": list(removed_artifact_ids),
                           "targets_removed": list(targets_removed)})
        if self.gate is not None and len(self.calls) == 1:
            await self.gate.wait()
        return self._result(removed_artifact_ids)


class _Bound:
    """Counters + the monkeypatches that point every production default at this
    tmp tree. Installed AFTER the fixture's own direct library calls, so the
    counts describe the handler under test and nothing else."""

    def __init__(self, monkeypatch, ctx, receipts_dir: Path, *,
                 worker_gate=None) -> None:
        self.ctx = ctx
        self.library = 0
        self.begins = 0
        self.completes = 0
        self.compensations = 0
        self.rollback_disks = 0
        self.receipt_prunes = 0
        self.staging_reclaims = 0
        self.txns: list = []
        self.seq = _Sequencer()
        self._install(monkeypatch, ctx, receipts_dir, worker_gate)

    def _install(self, monkeypatch, ctx, receipts_dir, worker_gate) -> None:
        me = self

        real_load = specialist_receipt.load

        def _load(receipt_id, **k):
            k.setdefault("receipts_dir", receipts_dir)
            return real_load(receipt_id, **k)

        monkeypatch.setattr(specialist_receipt, "load", _load)

        real_delete = specialist_receipt.delete

        def _delete(receipt_id, **k):
            me.receipt_prunes += 1
            k.setdefault("receipts_dir", receipts_dir)
            return real_delete(receipt_id, **k)

        monkeypatch.setattr(specialist_receipt, "delete", _delete)

        for name, keys in _LIB_EXTRA.items():
            real = getattr(specialist_install, name)

            def _wrapped(_real=real, _keys=keys, **kw):
                me.library += 1
                kw.update({k: ctx.kw[k] for k in _keys})
                kw["acks"] = ctx.acks
                out = _real(**kw)
                txn = out[1] if isinstance(out, tuple) else out
                me.txns.append(txn)
                if worker_gate is not None:
                    worker_gate()       # parked INSIDE the worker thread
                return out

            monkeypatch.setattr(specialist_install, name, _wrapped)

        real_begin = specialist_bundle_journal.begin

        def _begin(op, slug, **k):
            me.begins += 1
            return real_begin(op, slug, **k)

        monkeypatch.setattr(specialist_bundle_journal, "begin", _begin)

        real_complete = specialist_bundle_journal.complete

        def _complete(path):
            me.completes += 1
            return real_complete(path)

        monkeypatch.setattr(specialist_bundle_journal, "complete", _complete)

        real_compensate = tools_mod._bundle_compensate

        async def _compensate(txn):
            me.compensations += 1
            return await real_compensate(txn)

        monkeypatch.setattr(tools_mod, "_bundle_compensate", _compensate)

        real_rollback_disk = specialist_bundle_journal.BundleTxn.rollback_disk

        def _rollback_disk(self_txn):
            me.rollback_disks += 1
            return real_rollback_disk(self_txn)

        monkeypatch.setattr(specialist_bundle_journal.BundleTxn,
                            "rollback_disk", _rollback_disk)

        monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", self.seq)

        real_reclaim = specialist_install.reclaim_staging_tree

        def _reclaim(path):
            me.staging_reclaims += 1
            return real_reclaim(path)

        monkeypatch.setattr(specialist_install, "reclaim_staging_tree", _reclaim)


async def _settle(predicate, timeout: float = _NEGATIVE_WINDOW) -> None:
    """A bounded, POSITIVE opportunity for `predicate` to become true. Used only
    where the arm asserts the condition must NOT hold: the wait ends the instant
    the forbidden thing happens, and its absence is what the vector asserts."""
    try:
        await wait_until(predicate, timeout=timeout)
    except TimeoutError:
        pass


class _Case:
    def __init__(self, ctx, tool, args, target, prunes: int, reclaims: int) -> None:
        self.ctx = ctx
        self.tool = tool
        self.args = args
        self.target = target
        self.prunes = prunes
        self.reclaims = reclaims


def _bundle_args(ctx, *, slug: bool = False) -> dict:
    args = {"component_id": ctx.inspection.component_id,
            "version": ctx.inspection.version,
            "root_digest": ctx.inspection.root_digest,
            "slug": ctx.inspection.slug,
            "staged_dir": str(ctx.inspection.staged_dir),
            "receipt_id": ctx.inspection.receipt_id}
    if slug:
        args["slug"] = _SLUG
    return args


def _setup(op: str, tmp_path: Path, monkeypatch) -> _Case:
    """Build this row's REAL initial state with direct library calls, then hand
    back the handler that will be cancelled. The binding harness is installed by
    the test AFTERWARDS, so no setup call is counted."""
    if op == "install":
        ctx = _prep_bundle(tmp_path, monkeypatch, ["mtg"])
        return _Case(ctx, tools_mod.specialist_install_commit, _bundle_args(ctx),
                     lambda g: g[0] is not None and len(g[2]) == 1, 1, 1)
    if op == "upgrade":
        base = _install(tmp_path, monkeypatch, ["mtg"])
        up = _prep_bundle(tmp_path, monkeypatch, ["mtg", "extra"], root="v2",
                          ref="v2", sha="b" * 40, base_ctx=base, version="0.2.0")
        return _Case(up, tools_mod.specialist_upgrade, _bundle_args(up, slug=True),
                     lambda g: len(g[2]) == 2, 1, 1)
    if op == "rollback":
        base = _install(tmp_path, monkeypatch, ["mtg"])
        g1 = _generation(base)
        _upgrade(tmp_path, monkeypatch, base, ["mtg", "extra"], root="v2",
                 ref="v2", sha="b" * 40, version="0.2.0")
        return _Case(base, tools_mod.specialist_rollback, {"slug": _SLUG},
                     lambda g: g == g1, 0, 0)
    if op == "uninstall":
        base = _install(tmp_path, monkeypatch, ["mtg"])
        return _Case(base, tools_mod.specialist_uninstall, {"slug": _SLUG},
                     lambda g: g == (None, (), ()), 0, 0)
    raise AssertionError(op)


_OPS = ["install", "upgrade", "rollback", "uninstall"]


# ---------------------------------------------------------------------------
# 1 — cancellation while the library call is still in its worker thread
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", _OPS)
@pytest.mark.asyncio
async def test_cancel_after_commit_finishes_each_handler_transaction(
        op: str, tmp_path: Path, monkeypatch) -> None:
    """The generation is committed inside the worker thread and the handler is
    cancelled while `asyncio.to_thread` is still pending. The transaction must
    still run its sequencer and complete its journal, the handler must still
    finish cancelled exactly once, and boot must roll nothing back.

    RED at base: the cancellation interrupts the `await asyncio.to_thread(...)`,
    the worker's returned transaction is discarded, and the vector is
    `sequencer 0, complete 0, in-progress 1, boot rollbacks 1` with the
    committed generation replaced by the captured earlier one.

    Parameterised over all four handlers because a single-handler probe leaves
    "omit the helper from one of the other three" alive (both reviewers, seam
    round)."""
    case = _setup(op, tmp_path, monkeypatch)
    lock = _fresh_plugin_tools_lock(monkeypatch)
    hs = _Handshake()
    bound = _Bound(monkeypatch, case.ctx, tmp_path / "receipts",
                   worker_gate=hs.pause)

    task = asyncio.create_task(case.tool.handler(case.args))
    await wait_until(lambda: hs.entered.is_set() or task.done(), timeout=_TIMEOUT)
    assert hs.entered_count == 1, "the library worker never reached the gate"
    committed = 1 if case.target(_generation(case.ctx)) else 0

    task.cancel()
    await _drain_loop()
    hs.release.set()

    done, pending = await asyncio.wait({task}, timeout=_TIMEOUT)
    assert not pending, "the handler never finished"
    cancellations = 1 if task.cancelled() else 0
    if not task.cancelled() and task.exception() is not None:
        raise task.exception()
    await _settle(lambda: bound.completes >= 1)

    in_progress = _in_progress(case.ctx.kw["ops_dir"])
    rollback_disks = bound.rollback_disks
    actions = _boot(case.ctx)
    final = 1 if case.target(_generation(case.ctx)) else 0

    assert (bound.library, committed, len(bound.seq.calls), bound.compensations,
            rollback_disks, bound.completes, cancellations, in_progress,
            _rolled_back(actions), final, bound.receipt_prunes,
            bound.staging_reclaims) == (
        1, 1, 1, 0, 0, 1, 1, 0, 0, 1, case.prunes, case.reclaims)
    assert not lock.locked()


# ---------------------------------------------------------------------------
# 2 — cancellation while the failing sequencer is parked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", _OPS)
@pytest.mark.asyncio
async def test_cancel_during_failed_sequence_compensates_each_handler(
        op: str, tmp_path: Path, monkeypatch) -> None:
    """The library committed, the post-commit sequencer is parked, and the
    handler is cancelled inside it. Released with `ok: false`, the transaction
    must still compensate — disk rollback, compensating sequencer, journal
    completion — before the handler reports its single cancellation, and the
    pre-operation generation must be back.

    RED at base: `CancelledError` escapes the parked sequencer through the
    handlers' `except Exception` arm, so
    `(sequencer, compensations, rollback_disk, complete) == (1, 0, 0, 0)` and one
    replayable journal is left behind."""
    case = _setup(op, tmp_path, monkeypatch)
    before = _generation(case.ctx)
    _fresh_plugin_tools_lock(monkeypatch)
    bound = _Bound(monkeypatch, case.ctx, tmp_path / "receipts")
    gate = asyncio.Event()
    bound.seq.gate = gate
    bound.seq.results = [{"ok": False, "kind": "postcondition_failed",
                          "not_ready": ["mtg.mtg"]}, {"ok": True}]

    task = asyncio.create_task(case.tool.handler(case.args))
    await wait_until(lambda: len(bound.seq.calls) == 1 or task.done(),
                     timeout=_TIMEOUT)
    assert len(bound.seq.calls) == 1, "the forward sequencer was never entered"
    forward = 1 if case.target(_generation(case.ctx)) else 0

    task.cancel()
    await _drain_loop()
    gate.set()

    done, pending = await asyncio.wait({task}, timeout=_TIMEOUT)
    assert not pending, "the handler never finished"
    cancellations = 1 if task.cancelled() else 0
    if not task.cancelled() and task.exception() is not None:
        raise task.exception()
    await _settle(lambda: bound.completes >= 1)

    restored = 1 if _generation(case.ctx) == before else 0
    in_progress = _in_progress(case.ctx.kw["ops_dir"])
    rollback_disks = bound.rollback_disks
    actions = _boot(case.ctx)

    assert (bound.library, forward, len(bound.seq.calls), bound.compensations,
            rollback_disks, bound.completes, cancellations, restored,
            in_progress, _rolled_back(actions)) == (1, 1, 2, 1, 1, 1, 1, 1, 0, 0)


# ---------------------------------------------------------------------------
# 3 — a disk rollback that itself fails leaves the journal DELIBERATELY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_disk_compensation_leaves_exactly_one_replayable_journal(
        tmp_path: Path, monkeypatch) -> None:
    """The one shape in which an in-progress journal survives a cancelled
    handler on purpose: compensation was attempted and its disk rollback raised.
    The journal must stay replayable and must NOT be completed — that is the
    recovery contract, not residue.

    RED at base: cancellation prevents compensation from starting at all, so
    `compensations == 0` and `rollback_disk == 0`; the journal that survives is
    the untouched forward one, which is exactly the state this arm must be able
    to tell apart from a deliberate one."""
    case = _setup("upgrade", tmp_path, monkeypatch)
    _fresh_plugin_tools_lock(monkeypatch)
    bound = _Bound(monkeypatch, case.ctx, tmp_path / "receipts")
    gate = asyncio.Event()
    bound.seq.gate = gate
    bound.seq.results = [{"ok": False, "kind": "postcondition_failed",
                          "not_ready": ["mtg.mtg"]}, {"ok": True}]

    def _raising_rollback(_self_txn):
        bound.rollback_disks += 1
        raise OSError("registry unwritable")

    monkeypatch.setattr(specialist_bundle_journal.BundleTxn, "rollback_disk",
                        _raising_rollback)

    task = asyncio.create_task(case.tool.handler(case.args))
    await wait_until(lambda: len(bound.seq.calls) == 1 or task.done(),
                     timeout=_TIMEOUT)
    assert len(bound.seq.calls) == 1
    committed = 1 if case.target(_generation(case.ctx)) else 0

    task.cancel()
    await _drain_loop()
    gate.set()

    done, pending = await asyncio.wait({task}, timeout=_TIMEOUT)
    assert not pending, "the handler never finished"
    cancellations = 1 if task.cancelled() else 0
    if not task.cancelled() and task.exception() is not None:
        raise task.exception()
    await _settle(lambda: bound.compensations >= 1)

    ops_dir = case.ctx.kw["ops_dir"]
    assert (1, len(bound.seq.calls), bound.compensations, bound.rollback_disks,
            bound.completes, cancellations, committed,
            _replayable(ops_dir)) == (1, 1, 1, 1, 0, 1, 1, 1)
    assert sum(
        specialist_bundle_journal.classify_journal(p)[0]
        == specialist_bundle_journal.JOURNAL_REPLAY
        for p in ops_dir.glob("*.json")) == 1


# ---------------------------------------------------------------------------
# 4 — a cancel that lands before the mutation lock starts nothing at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", _OPS)
@pytest.mark.asyncio
async def test_cancel_before_lock_starts_nothing_for_each_handler(
        op: str, tmp_path: Path, monkeypatch) -> None:
    """A preservation arm, and the one that keeps the fix honest: cancellation
    that arrives while the handler is still queueing for the mutation lock must
    abort it outright. It kills a fix that signals "entered" before acquiring
    the lock, absorbs this cancellation, and then performs the mutation the
    operator aborted."""
    case = _setup(op, tmp_path, monkeypatch)
    before = _generation(case.ctx)
    lock = _fresh_plugin_tools_lock(monkeypatch)
    bound = _Bound(monkeypatch, case.ctx, tmp_path / "receipts")

    await lock.acquire()
    task = asyncio.create_task(case.tool.handler(case.args))
    await wait_until(lambda: bool(getattr(lock, "_waiters", None)) or task.done(),
                     timeout=_TIMEOUT)
    assert not task.done(), "the handler finished before reaching the lock"

    task.cancel()
    done, pending = await asyncio.wait({task}, timeout=_TIMEOUT)
    assert not pending, "the handler never finished"
    cancellations = 1 if task.cancelled() else 0
    lock.release()
    await _settle(lambda: bound.library > 0)

    assert (cancellations, bound.library, bound.begins, len(bound.seq.calls),
            bound.completes,
            0 if _generation(case.ctx) == before else 1) == (1, 0, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# 5 — bounded abandonment keeps the child AND the mutation lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bounded_abandonment_keeps_child_and_lock_until_terminal(
        tmp_path: Path, monkeypatch) -> None:
    """The absorption is BOUNDED, and what the bound gives up is only the WAIT.
    Past it the caller finishes cancelled while its transaction keeps running
    and keeps the mutation lock, so a second real rollback cannot dispatch its
    library call until the first reaches its terminal journal state.

    A second cancellation issued late in the shortened grace must not re-anchor
    the deadline and must not produce a second observed cancellation.

    RED at base: A reports cancellation immediately (elapsed ~0, not past the
    grace), releases `_PLUGIN_TOOLS_LOCK`, and B dispatches and commits while
    A's journal is still in progress. The production constant does not exist."""
    case = _setup("rollback", tmp_path, monkeypatch)
    lock = _fresh_plugin_tools_lock(monkeypatch)
    bound = _Bound(monkeypatch, case.ctx, tmp_path / "receipts")
    grace_is_60 = 1 if getattr(
        tools_mod, "_SPECIALIST_BUNDLE_CANCEL_GRACE_S", None) == 60.0 else 0
    grace = 0.4
    monkeypatch.setattr(tools_mod, "_SPECIALIST_BUNDLE_CANCEL_GRACE_S", grace,
                        raising=False)
    gate = asyncio.Event()
    bound.seq.gate = gate

    a = asyncio.create_task(tools_mod.specialist_rollback.handler({"slug": _SLUG}))
    await wait_until(lambda: len(bound.seq.calls) == 1 or a.done(),
                     timeout=_TIMEOUT)
    assert len(bound.seq.calls) == 1, "A's sequencer was never entered"

    loop = asyncio.get_running_loop()
    a.cancel()
    t0 = loop.time()

    async def _late_second_cancel() -> None:
        await wait_until(lambda: loop.time() - t0 >= grace * 0.4,
                         timeout=_TIMEOUT)
        a.cancel()

    late = asyncio.create_task(_late_second_cancel())
    done, pending = await asyncio.wait({a}, timeout=_TIMEOUT)
    elapsed = loop.time() - t0
    await late
    assert not pending, "A never finished — the absorption was not bounded"
    a_cancellations = 1 if a.cancelled() else 0
    if not a.cancelled() and a.exception() is not None:
        raise a.exception()

    completes_before = bound.completes
    b = asyncio.create_task(tools_mod.specialist_rollback.handler({"slug": _SLUG}))
    await _settle(lambda: bound.library >= 2)
    b_dispatched_early = 1 if bound.library >= 2 else 0
    in_progress_before = _in_progress(case.ctx.kw["ops_dir"])

    assert (grace_is_60, a_cancellations, completes_before, b_dispatched_early,
            in_progress_before) == (1, 1, 0, 0, 1)
    assert elapsed >= grace, (
        f"A finished {elapsed:.3f}s after cancellation, inside the {grace}s grace")
    assert elapsed <= grace * 2 + 3.0, (
        f"A finished {elapsed:.3f}s after cancellation — the deadline was "
        f"re-anchored by the second cancel")

    gate.set()
    done_b, pending_b = await asyncio.wait({b}, timeout=_TIMEOUT)
    assert not pending_b, "B never finished after A's transaction landed"
    if b.exception() is not None:
        raise b.exception()

    assert (len(bound.seq.calls), bound.completes, bound.library,
            _in_progress(case.ctx.kw["ops_dir"])) == (2, 2, 2, 0)
    assert not lock.locked()


# ---------------------------------------------------------------------------
# 6 — a genuinely crash-orphaned journal is STILL replayed at boot
# ---------------------------------------------------------------------------


def test_boot_replays_one_real_process_crash_journal(
        tmp_path: Path, monkeypatch) -> None:
    """The arm that catches a "fix" which closed #828 by making boot stop
    replaying. No handler, no cancellation helper: the real library commits a
    second generation and the process is simply gone before anything completes
    the journal. Boot must still roll that generation back — exactly once — and
    leave no journal behind.

    Green at base and required to stay green: this is the recovery the journal
    exists for."""
    base = _install(tmp_path, monkeypatch, ["mtg"])
    g1 = _generation(base)
    up = _prep_bundle(tmp_path, monkeypatch, ["mtg", "extra"], root="v2",
                      ref="v2", sha="b" * 40, base_ctx=base, version="0.2.0")
    instance, _txn = specialist_install.upgrade_specialist(**up.kw)
    commits = 1 if instance.state == "active" else 0
    g2 = _generation(base)
    committed_v2 = 1 if (len(g2[2]) == 2 and g2 != g1) else 0
    ops_dir = base.kw["ops_dir"]
    replayable_before = _replayable(ops_dir)

    actions = _boot(base)

    assert (commits, committed_v2, replayable_before, _rolled_back(actions),
            1 if _generation(base) == g1 else 0,
            len(list(ops_dir.glob("*.json")))) == (1, 1, 1, 1, 1, 0)
