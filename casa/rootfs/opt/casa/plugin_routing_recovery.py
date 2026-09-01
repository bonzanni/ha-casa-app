"""Plugin routing recovery — the fail-closed PRODUCER behind the trigger and
callback routing sentinel (#746, INV-TRIG-015).

The trigger and callback overlays fail closed to ``ROUTING_UNAVAILABLE`` when a
reconcile's compute raises, runs against a registry it cannot read, or has not
run yet (INV-TRIG-006). Every consumer honours that marker; nothing used to
re-drive the compute. The twenty-one paths that reconcile — boot, the plugin
mutation tools, the reload entry points, the specialist-bundle sequencer, the
consent taps — are all human- or agent-initiated, so a purely transient
failure left plugin webhook and callback ingress shut, and any released setup
episode waiting on its route, until someone happened to touch the system. The
event sibling already had the cure: ``event_spool_recovery`` fires
``event_reconcile.kick()`` every five minutes while its map is the sentinel.
This module is that cure for the other two halves, kept as ONE pair.

Shape, and why each part is what it is:

* :func:`recovery_job` is the scheduler callable. It is ``async def`` and
  awaits nothing: ``AsyncIOScheduler`` runs a coroutine function on the loop,
  whereas a sync callable is dispatched to a worker thread where
  :func:`kick` would find no running loop and silently no-op forever (the
  same seam ``casa_core`` step 14 documents). It probes and kicks, so the
  executor slot is held for microseconds and no lock is ever taken by the job.
* :func:`kick` is fire-and-forget with ONE task slot, so kicks while a pass is
  live coalesce, and a no-op without a running loop.
* :func:`_recover` heals BOTH halves in ONE task, triggers then callbacks —
  the order every existing paired producer uses. Two independent tasks would
  run the two reconciles concurrently under their separate reconcile locks,
  and both reconciles seal setup rounds: an interleaving no producer creates
  today. Each half is isolated, so a trigger failure never prevents the
  callback attempt; the predicate is "either half unavailable", so an
  already-healed half is simply recomputed authoritatively and the pair
  converges. ``prompt=False`` always — a timer must never post a keyboard.
* Lock order: the task takes ``tools._plugin_tools_guard()`` FIRST and holds
  it across both reconciles and the regeneration. That is the documented
  direction (``_PLUGIN_TOOLS_LOCK`` → reload lock → ``_RECONCILE_LOCK``), the
  same one every mutation tool uses, so the heal can never interleave between
  a mutation's registry commit and that mutation's own reconcile, and no path
  anywhere waits for the guard while holding a reconcile lock. The predicates
  are re-read under the guard, so a mutation that healed meanwhile makes the
  task a no-op.
* Health is regenerated once after EVERY attempt, under the guard, after both
  reconcile locks are released. Not on a predicate: every "which transitions
  regenerate" rule proposed missed one (a half that newly fails; the kicked
  setup worker persisting a store reset), and the rule that cannot miss one is
  no rule. A persisting failure therefore costs one report write per tick with
  identical rows and fingerprints — ``_regenerate_plugin_health`` never
  notifies (the boot DM and the mutation sequencer are the only notification
  paths, and both dedup on fingerprints), so nothing is announced twice.

The stranded setup episode is unblocked as a CONSEQUENCE: a successful
reconcile already calls ``plugin_setup_episodes.kick()``. The setup route gate
itself is untouched here.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

#: The event sibling's cadence, deliberately equal.
INTERVAL_MINUTES = 5

_task: "asyncio.Task | None" = None


def _live_runtime():
    import agent as agent_mod

    return getattr(agent_mod, "active_runtime", None)


def _live_registry(runtime=None):
    runtime = _live_runtime() if runtime is None else runtime
    return getattr(runtime, "trigger_registry", None) if runtime else None


def needs_recovery(registry) -> bool:
    """True while EITHER overlay carries the sentinel. A probe that raises
    reads True: recomputing an overlay authoritatively is always safe, so
    "cannot tell" is the direction that recovers."""
    if registry is None:
        return False
    try:
        return bool(registry.plugin_overlay_unavailable()
                    or registry.callback_overlay_unavailable())
    except Exception:  # noqa: BLE001
        logger.exception("plugin routing recovery: availability probe failed")
        return True


async def recovery_job() -> None:
    """The scheduled callable: probe, kick, return. Awaits nothing."""
    if needs_recovery(_live_registry()):
        kick()


def kick() -> None:
    """Fire-and-forget: schedule one paired recovery pass off the live runtime.
    Coalesces onto a live pass; a silent no-op with no running loop."""
    global _task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _task is not None and not _task.done():
        return
    _task = loop.create_task(_recover(), name="plugin-routing-recovery")


async def _recover() -> None:
    import callback_reconcile
    import tools
    import trigger_reconcile

    try:
        async with tools._plugin_tools_guard():
            runtime = _live_runtime()
            registry = _live_registry(runtime)
            if registry is None or not needs_recovery(registry):
                return
            try:
                await trigger_reconcile.reconcile_from_runtime(
                    runtime, prompt=False)
            except Exception:  # noqa: BLE001 — isolated; the callback half
                logger.warning("plugin routing recovery: trigger reconcile "
                               "failed", exc_info=True)          # still runs
            try:
                await callback_reconcile.reconcile_from_runtime(
                    runtime, prompt=False)
            except Exception:  # noqa: BLE001
                logger.warning("plugin routing recovery: callback reconcile "
                               "failed", exc_info=True)
            # Both reconcile locks are released here; the guard is still held,
            # so this regeneration cannot race a mutation's own write.
            await asyncio.to_thread(tools._regenerate_plugin_health, [])
    except Exception:  # noqa: BLE001 — a background pass must never raise
        logger.exception("plugin routing recovery pass failed")


def register(scheduler) -> None:
    """Register the five-minute recovery job. Extracted (like
    ``plugin_outbox.register_sweep``) so casa_core's wiring is testable with a
    fake scheduler; the call site's position before ``scheduler.start()`` is
    pinned structurally."""
    scheduler.add_job(
        recovery_job, trigger="interval", id="plugin_routing_recovery",
        minutes=INTERVAL_MINUTES, replace_existing=True, coalesce=True,
        max_instances=1, misfire_grace_time=600,
    )
