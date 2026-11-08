"""Latch an engagement's service down, then kill every process under its uid.

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

**How the uid set is killed** — and why the ladder kills rather than freezes,
why zombies count as extinct, and how a signal is pinned against pid reuse —
is in ``uid_kill``, where the ladder lives.

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
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from uid_kill import (
    UID_BASE, QuiesceResult, _KILL_DEADLINE_S, _POLL_INTERVAL_S, _UNSET,
    kill_uid_until_empty,
)

logger = logging.getLogger(__name__)

# Latch-probe attempts, spaced by the ladder's poll interval. ``_UNSET`` is
# imported, never redefined: ``quiesce_engagement`` passes its defaults straight
# into ``kill_uid_until_empty``, which tests them against ``uid_kill._UNSET`` by
# identity — a second sentinel would be called as a pidfd primitive.
_LATCH_ATTEMPTS = 10


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
