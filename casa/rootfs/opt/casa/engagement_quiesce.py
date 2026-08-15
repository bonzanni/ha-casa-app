"""Bounded, observed kill of every process running under an engagement's uid.

#599: a terminal transition used to leave the engagement's CLI alive for the
whole finalize funnel — the broker drain, the summary finalize, four Telegram
round-trips — and only then reach ``driver.cancel``'s unbounded
``s6-rc -d change``. The record said ``cancelled``; its casa tools were refused
(INV-MCP-001 binds authority to ``active``); nothing at all stopped its native
``Bash``/``Write``/``Edit``.

**The uid is the containment key, not the process group.** The supervised leader
is not guaranteed to be its own group leader (``s6_rc._getpgid``), so a group has
to be read rather than known, and a ``setsid`` child leaves it entirely. The
engagement's uid has neither problem: it is allocated per engagement and NEVER
reused (INV-CONT-001), so "every process with ruid == N" is exactly this
engagement's processes, now and forever, including the ones that ran away from
the group.

**Why the ladder kills rather than freezes.** Two review rounds killed a
freeze-then-verify design: signal *delivery* is not *stopped* (a target was still
``R`` immediately after ``SIGSTOP`` in 5 of 500 trials), so a quiet enumeration
pass certifies stability while a member still executes, and repairing that needs
per-task state observation across ``vfork``, ``CLONE_THREAD``, ``D``-state and
ptrace-stops. Killing needs none of it, because the two loops establish different
things: a freeze must certify *stability* — a claim about the future — while a
kill certifies *emptiness*, which is directly observable from ``/proc`` at an
instant. ``SIGKILL`` delivery is asynchronous exactly as ``SIGSTOP`` is, and a
member may fork after a snapshot and before the signal lands; that child is
enumerated on the next pass and killed. The loop converges because a killed
process cannot be resumed and cannot fork again.

An empty enumeration means "and it stays empty" only because of two
preconditions, and the ladder establishes or reports BOTH rather than assuming
either:

* **s6 cannot respawn it** — the durable down latch, verified by reading
  ``wantedup`` alone. The existing tri-state probe cannot answer this while a
  process is still alive (``_classify_updown`` maps every ``up == "true"`` to
  ``"up"``), which is why this module asks a narrower question.
* **casa cannot start one** — the caller holds the per-engagement lifecycle
  fence for the whole ladder, and every live start path re-checks terminal
  status under that same fence immediately before starting.

Without a verified latch the outcome is NOT_VERIFIED even when the set empties,
because s6 may put it back.

Zombies count as extinct: a ``Z`` member cannot execute, fork or write, and it
lingers only until it is reaped, so treating it as live would turn a harmless
transient into a false NOT_VERIFIED.

Signalling goes through the same race-free primitive
``casa_core._best_effort_kill_uid`` uses: ``pidfd_open`` pins the exact process
instance, the uid is re-verified from the now-current ``/proc/<pid>/status``, and
``pidfd_send_signal`` reaches only the pinned process — never a pid reuser. With
no pidfd primitives the ladder signals NOTHING and reports NOT_VERIFIED; a
never-reused uid does not stop a numeric pid from being recycled for an unrelated
process between the read and the signal.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field

from engagement_uids import UID_BASE

logger = logging.getLogger(__name__)

# Poll cadence and budget. Module-level so tests drive the ladder with an
# injected sleep — NEVER a patch of the shared ``<module>.asyncio.sleep``
# (the memory-cage rule: that attribute is global, and an AsyncMock on it spins
# the SDK pool sweeper at CPU speed).
_POLL_INTERVAL_S = 0.05
_KILL_DEADLINE_S = 2.0
_LATCH_ATTEMPTS = 10
# Two consecutive clear scans are required before extinction is claimed: the
# first exposes a child forked between a snapshot and its reads, the second
# confirms the set stayed empty.
_CLEAR_SCANS_REQUIRED = 2

_UNSET = object()

# ``Z`` is the one /proc state that counts as extinct: a zombie cannot execute,
# fork or write. Every other state (R, S, D, T, t, I) is a process that may yet
# run, and must not qualify.
_INERT_STATES = frozenset("Z")


@dataclass(frozen=True)
class QuiesceResult:
    """Outcome of one ladder run. ``extinct`` is the only success."""

    extinct: bool
    reason: str = ""
    survivors: tuple[int, ...] = ()
    latched: bool = False
    passes: int = 0

    def __bool__(self) -> bool:      # truthy ONLY on a verified extinction
        return self.extinct


@dataclass
class _Signaller:
    """The race-free signal primitive, resolved once per ladder run.

    Resolved via a sentinel so an injected ``None`` EXPLICITLY means "primitive
    unavailable" and reaches the skip path, while the production default uses the
    real primitives (the same sentinel discipline ``_best_effort_kill_uid``
    learned the expensive way — an injected ``None`` that fell back to the real
    ``os.pidfd_open`` made the unavailable-path test signal for real).
    """

    pidfd_open: object = None
    pidfd_send: object = None
    close: object = field(default=os.close)

    @property
    def available(self) -> bool:
        return self.pidfd_open is not None and self.pidfd_send is not None


def _resolve_signaller(pidfd_open, pidfd_send, close) -> _Signaller:
    return _Signaller(
        pidfd_open=(getattr(os, "pidfd_open", None)
                    if pidfd_open is _UNSET else pidfd_open),
        pidfd_send=(getattr(signal, "pidfd_send_signal", None)
                    if pidfd_send is _UNSET else pidfd_send),
        close=close,
    )


def _read_status(proc_root: str, pid: str) -> tuple[int | None, str]:
    """``(ruid, state)`` from ``/proc/<pid>/status``; ``(None, "")`` if unreadable.

    ``Uid:`` is ``real effective saved fs`` — the REAL uid is field 0. A process
    that dies mid-read simply reads as absent, which is the correct answer.
    """
    try:
        with open(os.path.join(proc_root, pid, "status"), "r",
                  encoding="utf-8", errors="replace") as fh:
            ruid: int | None = None
            state = ""
            for line in fh:
                if line.startswith("Uid:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            ruid = int(parts[1])
                        except ValueError:
                            return None, ""
                elif line.startswith("State:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        state = parts[1]
                if ruid is not None and state:
                    break
            return ruid, state
    except (OSError, ValueError):
        return None, ""


@dataclass(frozen=True)
class _Scan:
    """One enumeration of a uid's processes.

    ``ok`` is the load-bearing field: a scan that could not be performed is NOT
    an empty uid set. Representing an observation failure as proof of emptiness
    was the weakest line in the first cut of this module (Sol, diff review) —
    ``os.listdir`` failing would have read as a verified extinction while a
    process kept writing.
    """

    live: tuple[int, ...] = ()
    zombies: tuple[int, ...] = ()
    ok: bool = True

    @property
    def no_live(self) -> bool:
        """A SUCCESSFUL scan that found nothing signalable.

        Not sufficient on its own: a parent can fork a child and become ``Z``
        between the directory snapshot and the reads, so the child is absent
        from that snapshot and the parent is inert — a single such scan would
        certify extinction in the very pass whose successor would have found the
        child (Sol, diff review — reproduced). The loop therefore requires TWO
        consecutive clear scans, the first of which exposes any such child.

        Zombies do not block it: a ``Z`` member cannot execute, fork or write,
        and requiring its absence would hold a real extinction hostage to
        whenever init gets round to reaping it (Sol, re-review — that would also
        contradict INV-CONT-006's published "zombies count as extinct").
        """
        return self.ok and not self.live


def scan_uid(uid: int, *, proc_root: str = "/proc") -> _Scan:
    """Enumerate every process whose REAL uid is *uid*.

    Only real allocated uids (``>= UID_BASE``) are ever enumerated, so root and
    every container service are structurally out of range.
    """
    if uid < UID_BASE:
        return _Scan(ok=True)
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return _Scan(ok=False)          # could not look — NOT "nothing there"
    live: list[int] = []
    zombies: list[int] = []
    ok = True
    for entry in entries:
        if not entry.isdigit():
            continue
        ruid, state = _read_status(proc_root, entry)
        if ruid is None:
            # Unreadable. If the pid is gone it simply exited between the
            # snapshot and the read — the common, benign case. If it is STILL
            # there, we failed to observe a process that exists, and this scan
            # cannot support an extinction claim.
            if os.path.exists(os.path.join(proc_root, entry)):
                ok = False
            continue
        if ruid != uid:
            continue
        if state in _INERT_STATES:
            zombies.append(int(entry))
            continue
        live.append(int(entry))
    return _Scan(live=tuple(live), zombies=tuple(zombies), ok=ok)


def live_pids_for_uid(uid: int, *, proc_root: str = "/proc") -> list[int]:
    """The signalable processes under *uid* — zombies excluded (they cannot
    execute, fork or write)."""
    return list(scan_uid(uid, proc_root=proc_root).live)


def _signal_pid(pid: int, sig: int, uid: int, sigr: _Signaller,
                proc_root: str) -> bool:
    """Signal *pid* iff it is STILL owned by *uid*, pinned against pid reuse.

    Order matters and is the whole point: open the pidfd FIRST (pinning that
    exact process instance), re-verify the uid from the now-current status, then
    signal through the pidfd — which can only ever reach the pinned process, or
    raise ``ESRCH`` if it already exited. A numeric ``os.kill`` after a status
    read can hit a REUSED pid.
    """
    fd = None
    try:
        fd = sigr.pidfd_open(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    try:
        ruid, _state = _read_status(proc_root, str(pid))
        if ruid != uid:
            return False          # exited and the pid was recycled — never signal
        sigr.pidfd_send(fd, sig, None)
        return True
    except (OSError, ProcessLookupError):
        return False
    finally:
        if fd is not None:
            try:
                sigr.close(fd)
            except OSError:
                pass


async def kill_uid_until_empty(
    uid: int,
    *,
    proc_root: str = "/proc",
    deadline_s: float = _KILL_DEADLINE_S,
    poll_interval_s: float = _POLL_INTERVAL_S,
    sleep=asyncio.sleep,
    monotonic=time.monotonic,
    pidfd_open=_UNSET,
    pidfd_send=_UNSET,
    close=os.close,
) -> QuiesceResult:
    """SIGKILL every process under *uid*, re-enumerating until the set is empty.

    Returns a result whose ``extinct`` is True ONLY when an enumeration actually
    observed the set empty — never inferred from "we signalled everything we saw".
    """
    if uid < UID_BASE:
        return QuiesceResult(
            extinct=False, reason="uid is not an allocated engagement uid")
    sigr = _resolve_signaller(pidfd_open, pidfd_send, close)
    if not sigr.available:
        # No race-free primitive → signal NOTHING rather than a bare numeric pid.
        return QuiesceResult(extinct=False, reason="pidfd unavailable")

    end = monotonic() + deadline_s
    passes = 0
    clear_streak = 0
    scan = _Scan()
    while True:
        passes += 1
        scan = scan_uid(uid, proc_root=proc_root)
        if scan.no_live:
            clear_streak += 1
            if clear_streak >= _CLEAR_SCANS_REQUIRED:
                return QuiesceResult(extinct=True, passes=passes)
            if monotonic() >= end:
                # Out of budget with only one clear scan: a fork-race child
                # would still be invisible, so this is not an extinction.
                return QuiesceResult(
                    extinct=False,
                    reason="only one clear scan before the deadline",
                    passes=passes)
            await sleep(poll_interval_s)
            continue
        clear_streak = 0
        if not scan.ok:
            # An observation failure is not emptiness. Keep trying while there
            # is budget, but never convert this into an extinction.
            if monotonic() >= end:
                return QuiesceResult(
                    extinct=False, reason="could not enumerate /proc for the uid",
                    passes=passes)
            await sleep(poll_interval_s)
            continue
        for pid in scan.live:
            _signal_pid(pid, signal.SIGKILL, uid, sigr, proc_root)
        if monotonic() >= end:
            # One last look: the kills above may have landed during this pass,
            # and a lingering zombie may have been reaped.
            scan = scan_uid(uid, proc_root=proc_root)
            if scan.no_live and clear_streak >= _CLEAR_SCANS_REQUIRED - 1:
                return QuiesceResult(extinct=True, passes=passes)
            return QuiesceResult(
                extinct=False,
                reason=("uid set still populated at the deadline"
                        if scan.ok else
                        "could not enumerate /proc for the uid"),
                survivors=tuple(scan.live) + tuple(scan.zombies), passes=passes)
        await sleep(poll_interval_s)


async def quiesce_engagement(
    *,
    engagement_id: str,
    uid: int,
    latch_down,
    wanted_down,
    proc_root: str = "/proc",
    deadline_s: float = _KILL_DEADLINE_S,
    poll_interval_s: float = _POLL_INTERVAL_S,
    latch_attempts: int = _LATCH_ATTEMPTS,
    sleep=asyncio.sleep,
    monotonic=time.monotonic,
    pidfd_open=_UNSET,
    pidfd_send=_UNSET,
    close=os.close,
) -> QuiesceResult:
    """Latch the service down, verify it, then kill the uid set to empty.

    *latch_down* and *wanted_down* are injected supervisor seams (``s6-svc -D``
    and a ``wantedup``-only probe) so this module owns the policy and s6_rc owns
    the commands. Never raises: every failure is a reported outcome, because a
    ladder that raises inside a terminal transition would wedge the funnel it is
    supposed to protect.
    """
    if uid < UID_BASE:
        # Specialist / legacy / in-casa records never had an OS identity.
        return QuiesceResult(
            extinct=False, reason="uid is not an allocated engagement uid")

    latched = False
    try:
        await latch_down(engagement_id=engagement_id)
    except Exception as exc:  # noqa: BLE001 — a failed command is an input
        logger.warning("quiesce %s: down-latch command failed: %s",
                       engagement_id[:8], exc)
    for attempt in range(latch_attempts):
        try:
            latched = bool(await wanted_down(engagement_id=engagement_id))
        except Exception as exc:  # noqa: BLE001 — a failed probe is not proof
            logger.warning("quiesce %s: wantedup probe failed: %s",
                           engagement_id[:8], exc)
            latched = False
        if latched:
            break
        if attempt < latch_attempts - 1:
            await sleep(poll_interval_s)

    killed = await kill_uid_until_empty(
        uid, proc_root=proc_root, deadline_s=deadline_s,
        poll_interval_s=poll_interval_s, sleep=sleep, monotonic=monotonic,
        pidfd_open=pidfd_open, pidfd_send=pidfd_send, close=close,
    )

    if not latched:
        # The set may be empty right now and s6 may put it back — so this is
        # NOT an extinction, whatever the kill loop achieved.
        result = QuiesceResult(
            extinct=False,
            reason=("service not confirmed wanted-down; "
                    + (killed.reason or "uid set emptied")),
            survivors=killed.survivors, latched=False, passes=killed.passes)
    else:
        result = QuiesceResult(
            extinct=killed.extinct, reason=killed.reason,
            survivors=killed.survivors, latched=True, passes=killed.passes)

    if result.extinct:
        logger.info("quiesce %s: uid %s observed extinct after %d pass(es)",
                    engagement_id[:8], uid, result.passes)
    else:
        logger.error(
            "quiesce %s: uid %s NOT verified extinct (%s)%s — the engagement is "
            "terminal but processes under its uid may still be running",
            engagement_id[:8], uid, result.reason,
            f"; survivors={list(result.survivors)}" if result.survivors else "",
        )
    return result
