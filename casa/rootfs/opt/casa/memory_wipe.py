# casa/rootfs/opt/casa/memory_wipe.py
"""#411: the supported long-term-memory wipe.

Two pieces live here:

- :class:`RetainFence` — a process-wide readers/writer gate with a monotonic
  *wipe generation*. Every long-term-bank writer wraps its retain-and-spool
  critical section in the SHARED side, passing the generation it captured
  synchronously at its decision point; a generation that moved (a wipe
  completed in between) makes the writer DISCARD its work — no retain and no
  spool write, because the operator consented to deleting exactly that
  content. The wipe takes the EXCLUSIVE side, which drains in-flight writers
  (bounded) and bumps the generation before anything is deleted.

- :func:`wipe_long_term_memory` — the single orchestrator both doors (the
  root-gated ``/admin/memory/wipe`` route and the operator-consented
  ``wipe_memory`` tool) call, plus the single-flight/lifecycle plumbing
  (:func:`start_wipe_task`, :func:`freeze_wipes`, :func:`drain_wipe_task`)
  that keeps at most one wipe alive and lets shutdown finish a running wipe
  before the channels and the memory backend go away.

The registry claims (``begin_retirement``) that steer racing turns away from
dying sessions live in :mod:`session_registry`; this module only consumes
them.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from rw_barrier import RwBarrier

logger = logging.getLogger(__name__)

# How long the wipe waits for in-flight retains to drain before aborting.
# Generous: the slowest legitimate shared section is a full transcript
# retain (bounded by the memory backend's HTTP timeouts).
_EXCLUSIVE_DRAIN_TIMEOUT_S = 60.0

# #345's durable retry spool — the wipe drops it so no pre-wipe record can
# replay into the emptied bank. Kept in sync with session_saver's constant
# (imported there from here would cycle; a test pins the equality).
_COLD_RETAIN_RETRY_DIR = "/data/cold-retain-retry"


class WipeAborted(Exception):
    """The wipe could not proceed; NOTHING was deleted."""


class RetainFence:
    """Readers/writer gate + wipe generation for the long-term bank.

    Writers (shared side) may overlap freely; the wipe (exclusive side) waits
    until all in-flight shared sections finish, blocks new ones for its
    duration, and bumps the generation. There is exactly one instance per
    process, wired at casa_core construction and published module-globally
    for the writers (mirroring ``agent.active_semantic_memory``).
    """

    def __init__(self) -> None:
        self._generation = 0
        # #578: the readers/writer core is shared with session_gate's
        # TurnAdmission. It carries the cancellation repair below, which a
        # second hand-written copy could silently omit.
        self._barrier = RwBarrier()

    def generation(self) -> int:
        """Capture point contract (#411 design r3/r4): call this in the SAME
        no-await block as the decision/snapshot that commits the writer to
        its pre-wipe source data — beside the pool's resume decision, at the
        delegated-summary assembly, at an inline writer's entry — never
        inside a detached coroutine body (it may not run until after a wipe)
        and never after an await that follows the decision."""
        return self._generation

    def retaining(self, entered_generation: int) -> "_SharedSection":
        """``async with fence.retaining(gen):`` — the shared section.

        Raises :class:`StaleGeneration` on entry when ``entered_generation``
        no longer matches (a wipe completed since the caller captured it):
        the caller's source data is pre-wipe and must be discarded — no
        retain, no spool write."""
        return _SharedSection(self, entered_generation)

    async def _enter_shared(self, entered_generation: int) -> None:
        # Block while a wipe holds/awaits exclusivity, then re-check the
        # generation: a writer that slept through a wipe discards. The check
        # and the increment share a no-await block, so a wipe cannot land
        # between them.
        await self._barrier.wait_for_exclusive_clear()
        if entered_generation != self._generation:
            raise StaleGeneration(
                f"retain fence generation moved "
                f"({entered_generation} -> {self._generation}); pre-wipe "
                f"work discarded"
            )
        self._barrier.enter_shared()

    def _exit_shared(self) -> None:
        self._barrier.exit_shared()

    def exclusive_wipe(self) -> "_ExclusiveSection":
        """``async with fence.exclusive_wipe():`` — drain writers (bounded),
        bump the generation, hold new writers out until exit. Raises
        :class:`WipeAborted` on drain timeout, with nothing changed."""
        return _ExclusiveSection(self)


class StaleGeneration(Exception):
    """The fence generation moved since capture — discard, don't write."""


class _SharedSection:
    def __init__(self, fence: RetainFence, entered_generation: int) -> None:
        self._fence = fence
        self._generation = entered_generation

    async def __aenter__(self) -> None:
        await self._fence._enter_shared(self._generation)

    async def __aexit__(self, *exc) -> bool:
        self._fence._exit_shared()
        return False


class _ExclusiveSection:
    def __init__(self, fence: RetainFence) -> None:
        self._fence = fence

    async def __aenter__(self) -> None:
        # Sol diff-r1: a CANCELLATION during the drain (shutdown killing the
        # wipe task, an admin client dropping) left the exclusive lock held
        # forever — every later retain and wipe then deadlocked. Any
        # exceptional exit must release the lock. That repair now lives in
        # RwBarrier.acquire_exclusive, shared with TurnAdmission (#578), so
        # neither barrier can drift away from it.
        try:
            await self._fence._barrier.acquire_exclusive(
                drain_timeout_s=_EXCLUSIVE_DRAIN_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, TimeoutError):
            raise WipeAborted(
                "in-flight memory writers did not drain within "
                f"{_EXCLUSIVE_DRAIN_TIMEOUT_S:.0f}s; nothing was deleted"
            ) from None
        self._fence._generation += 1

    async def __aexit__(self, *exc) -> bool:
        self._fence._barrier.release_exclusive()
        return False


@dataclass
class WipeReport:
    """What the wipe removed — returned to both doors verbatim."""
    spool_records_dropped: int = 0
    session_entries_dropped: int = 0
    bank_deleted: bool = False
    residual_note: str = (
        "A conversation or engagement already in flight when the wipe ran "
        "may still contribute one post-wipe item; everything durable "
        "(the bank, the retry spool, the session pointers) was removed."
    )

    def summary(self) -> str:
        return (
            f"Long-term memory wiped: bank deleted={self.bank_deleted}, "
            f"{self.spool_records_dropped} spooled retry record(s) dropped, "
            f"{self.session_entries_dropped} session pointer(s) dropped "
            f"without retention. {self.residual_note}"
        )


async def wipe_long_term_memory(
    *, registry, semantic_memory, fence: RetainFence, bank: str,
    retry_dir: str | Path = _COLD_RETAIN_RETRY_DIR,
    admission=None,
) -> WipeReport:
    """The one wipe orchestrator (#411, design v3 Part C).

    Order matters and is pinned by tests: TURN ADMISSION first (#578 — see
    below), then claims (racing turns steer fresh and dying sids cannot
    re-register), then the exclusive fence (in-flight writers drained,
    generation bumped — pre-wipe writers that resume later DISCARD), then the
    durable spool, then the session pointers (sid-guarded: a steered-fresh
    session registered mid-wipe SURVIVES), then the bank itself. Claims
    release in ``finally`` so no exit path can leave a key steering fresh
    forever.

    #578: the whole body runs with turn admission held EXCLUSIVELY, so no turn
    is running and none can start. Before this, joining an in-flight turn was
    ``notify_reset``'s job, and its only listener anywhere is the client
    pool's ``close_key`` — so a turn the pool never owned (a SCHEDULED turn, a
    webhook one-shot, a ``PoolUnavailable`` fallback) was not joined, and
    re-armed the session pointer once the claims released, after this function
    had reported completion. Closing admission BEFORE the keys are enumerated
    also covers a FIRST-EVER turn on a key, which is running with no registry
    entry for ``all_entries`` to find.

    The drain is bounded and fails CLOSED: turns that do not finish in time
    raise :class:`WipeAborted` with nothing deleted, matching the spool
    enumeration below. A wipe that cannot prove it drained everything must not
    claim it deleted everything.

    ``admission`` is injectable for tests; production passes nothing and gets
    the process-wide singleton.
    """
    from session_gate import TURN_ADMISSION, AdmissionTimeout

    if admission is None:
        admission = TURN_ADMISSION
    try:
        async with admission.exclusive():
            return await _wipe_locked(
                registry=registry, semantic_memory=semantic_memory,
                fence=fence, bank=bank, retry_dir=retry_dir,
            )
    except AdmissionTimeout as exc:
        raise WipeAborted(f"{exc}") from None


async def _wipe_locked(
    *, registry, semantic_memory, fence: RetainFence, bank: str,
    retry_dir: str | Path,
) -> WipeReport:
    """The wipe body, run with turn admission already held exclusively."""
    report = WipeReport()
    claims: dict[str, tuple[object, str | None]] = {}
    # 1. Claim every key in one no-await sweep.
    for key, entry in registry.all_entries().items():
        sid = entry.get("sdk_session_id")
        claims[key] = (registry.begin_retirement(key, sid), sid)
    try:
        async with fence.exclusive_wipe():  # 2. drain + bump gen
            # 3. Drop the durable retry spool — FAIL CLOSED (Sol diff-r1): a
            # record that cannot be enumerated or removed is a durable
            # pre-wipe writer that would replay into the emptied bank, so the
            # wipe aborts BEFORE any pointer or the bank is touched rather
            # than reporting a success it did not deliver. Records already
            # unlinked stay unlinked — removing a retry record can only lose
            # a retry, never add content — so a retried wipe is safe.
            # Terra diff-r2: NOT Path.glob() — pathlib's globbing swallows
            # the scandir OSError internally (an unreadable spool dir yields
            # [] with no exception, verified empirically on 3.12), which
            # silently re-opened the fail-open this block exists to close.
            # os.scandir raises, and a MISSING dir is the one legitimate
            # empty (fresh install, nothing ever spooled).
            root = Path(retry_dir)
            try:
                with os.scandir(root) as it:
                    records = sorted(
                        Path(entry.path) for entry in it
                        if entry.name.endswith(".json")
                    )
            except FileNotFoundError:
                records = []
            except OSError as exc:
                raise WipeAborted(
                    f"could not enumerate the retry spool ({exc}); nothing "
                    "was deleted"
                ) from exc
            failed_unlinks = 0
            for path in records:
                try:
                    path.unlink()
                    report.spool_records_dropped += 1
                except OSError:
                    failed_unlinks += 1
                    logger.warning("wipe: could not unlink %s", path)
            if failed_unlinks:
                raise WipeAborted(
                    f"{failed_unlinks} spool record(s) could not be removed; "
                    "session pointers and the bank were left untouched"
                )
            # 4. Drop session pointers WITHOUT retention (retiring saves
            # first — exactly the residue #411 names). notify_reset joins
            # any in-flight pool turn and flushes/closes the warm client.
            for key, (_token, sid) in claims.items():
                await registry.notify_reset(key)
                before = registry.get(key) is not None
                if sid is not None:
                    await registry.remove(key, expected_sid=sid)
                else:
                    # The entry had no sid at claim time; remove only while
                    # that is still true (an explicit-None expected_sid).
                    await registry.remove(key, expected_sid=None)
                if before and registry.get(key) is None:
                    report.session_entries_dropped += 1
            # 5. The bank.
            report.bank_deleted = await semantic_memory.delete_bank(bank)
    finally:
        for key, (token, _sid) in claims.items():
            registry.end_retirement(key, token)
    logger.warning("memory wipe completed: %s", report.summary())
    return report


# The process-wide fence instance (module singleton, like verdict_broker's
# BROKER): every bank writer and the wipe share exactly this one.
FENCE = RetainFence()


# ---------------------------------------------------------------------------
# Single-flight + shutdown lifecycle (#411 design r2 Sol S-C3 / r3 Terra)
# ---------------------------------------------------------------------------

_wipe_task: asyncio.Task | None = None
_wipes_frozen = False


def start_wipe_task(coro) -> asyncio.Task | None:
    """Admit ``coro`` (a wipe_long_term_memory call) as THE running wipe.

    Returns the task, or ``None`` when a wipe is already running or wipes
    are frozen for shutdown — the caller must not fall back to running the
    coroutine itself (that would defeat both single-flight and the shutdown
    drain). The caller owns user-facing reporting; this owns the slot."""
    global _wipe_task
    if _wipes_frozen or (_wipe_task is not None and not _wipe_task.done()):
        coro.close()
        return None
    _wipe_task = asyncio.get_running_loop().create_task(
        coro, name="memory-wipe",
    )
    return _wipe_task


def freeze_wipes() -> None:
    """Shutdown step 1 (r3 Terra TOCTOU): refuse NEW wipe admissions before
    the broker finish hooks are drained, so a hook that fires during
    shutdown cannot spawn a wipe after the drain already looked."""
    global _wipes_frozen
    _wipes_frozen = True


async def drain_wipe_task() -> None:
    """Shutdown step 3: wait out a running wipe (after freeze_wipes and the
    broker-hook drain, before channels/semantic memory close)."""
    task = _wipe_task
    if task is not None and not task.done():
        try:
            await task
        except Exception:  # noqa: BLE001 — report/log is the task's job
            logger.exception("memory wipe failed during shutdown drain")
