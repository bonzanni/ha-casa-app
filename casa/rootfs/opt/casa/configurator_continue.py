"""Casa continues the configurator: one delegation on the resident's behalf,
on the configurator's lane — after an approval, or for an erase episode.

:func:`start` never awaits. It takes the key's launch slot, reads the approval
(when there is one) and spawns one task, which does everything else: the
``<operator>:configurator`` permit (waited for, since the run that asked may
still hold it), a place on the configurator's lane, the durable delegation row
with its completion callback attached at once, then the run, whose bounded
runner takes the configurator lease. Every configurator run takes those in the
same order — permit, lane, lease, then the plugin-tools lock — so no two runs
wait on each other. The task's own ``finally`` gives back the permit, the lane
place and the slot, so a caller's cancellation can leave nothing behind (no
caller awaits the task), and only Casa's shutdown, which cancels every task,
can cut it off.

The task runs in an empty context: nothing of the caller's run (its wait
deadline above all) leaks in. Its own deadline, ``LEASE_WAIT_S`` from when it
starts, bounds the permit, the lane and the lease waits together.

A caller that wants to know how the launch went passes *on_outcome*, an async
callback the task spawns once: ``"started"`` when the run is registered,
``"busy"`` or ``"failed"`` when it was not. The run's own result reaches the
resident as any delegation's does, as a completion on Casa's bot.
"""
from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Literal

import agent_lane
import approval_records
import configurator_run
from provenance import APPROVAL_KEY, task_delivery_marker

logger = logging.getLogger(__name__)

Outcome = Literal["started", "queued", "busy", "failed", "superseded"]
OnOutcome = "Callable[[str], Awaitable[Any]] | None"
#: how often a continuation tries the permit while the run that asked still holds it
RETRY_EVERY_S = 0.5


@dataclasses.dataclass
class _Slot:
    task: "asyncio.Task | None" = None
    follower: "tuple | None" = None             # (continuation, asked, on_outcome) for an erase key


_SLOTS: "dict[str, _Slot]" = {}
_SPAWNED: "set[asyncio.Task]" = set()


def _spawn(coro) -> None:
    t = asyncio.ensure_future(coro)
    _SPAWNED.add(t)
    t.add_done_callback(_SPAWNED.discard)


def _tell(on_outcome: OnOutcome, outcome: str) -> None:
    if on_outcome is not None:
        async def _call() -> None:
            try:
                await on_outcome(outcome)
            except Exception:  # noqa: BLE001 — a notice never fails the work
                logger.warning("continuation outcome not told", exc_info=True)
        _spawn(_call())


APPROVED_VALUES_LINE = "The approved values (commit exactly these): "


def values_line(values: dict) -> str:
    """The line that carries an approval's frozen values into its run."""
    return "\n\n" + APPROVED_VALUES_LINE + json.dumps(values, sort_keys=True)


def continuation_text(kind: str, *, slug: str = "", component_id: str = "",
                      version: str = "", root_digest: str = "", staged_dir: str = "",
                      receipt_id: str = "", persona_id: str = "", checksum: str = "",
                      values: "dict | None" = None) -> str:
    """What Casa tells the configurator when it carries it on after an approval
    of *kind*: the call to make with the staged facts, and what to finish.
    With *values* (the approval's frozen values) the text ends with the line
    :func:`start` adds to an approval's run, so this is the run's whole task."""
    if kind == "persona_install":
        text = (f"The operator approved installing persona {persona_id}. Call "
                f"persona_install_commit with persona_id {persona_id!r}, version "
                f"{version!r}, checksum {checksum!r} and staged_dir {staged_dir!r}, then "
                "finish the recipe (config_git_commit) and end with your answer.")
    else:
        staged = (f"component_id {component_id!r}, version {version!r}, root_digest "
                  f"{root_digest!r}, slug {slug!r}, staged_dir {staged_dir!r}, "
                  f"receipt_id {receipt_id!r}")
        if kind == "specialist_upgrade":
            text = (f"The operator approved upgrading specialist:{slug}. Call "
                    f"specialist_upgrade with {staged}, and exactly the approved values "
                    "below (config and secret_names_provided), then finish the upgrade "
                    "recipe from its step 3 and end with your answer.")
        else:
            text = (f"The operator approved installing specialist:{slug}. Call "
                    f"specialist_install_commit with {staged}, and exactly the approved "
                    "values below (config and secret_names_provided), then finish the "
                    "recipe (wire delegation, config_git_commit) and end with your "
                    "answer.")
    return text + values_line(values) if values is not None else text


def _launch_origin(transport, resident: str, operator: int, asked: str) -> dict:
    """The origin of the resident's DM turn as the DM ingress builds it for the
    operator; ``user_text`` is the operator's own words that led to the work."""
    return {"role": resident, "channel": "telegram", "source": "telegram",
            "message_type": "channel_in", "chat_id": operator, "user_id": operator,
            "user_name": "operator", "cid": "appr-" + uuid.uuid4().hex[:12],
            "user_text": asked, "_origin_route": "telegram",
            "_origin_clearance": "private", "_operator_turn": True,
            "_tg_bot": transport.casa_route()}


def start(continuation: str, *, key: str,
          approval: "approval_records.ApprovalRecord | None" = None,
          asked: str = "", on_outcome: OnOutcome = None) -> Outcome:
    """Take the key's launch slot and spawn the one task that does the rest.
    Never awaits: a caller's cancellation cannot interrupt the launch.

    *key* is the approval's ``rid``, or ``erase:<question id>`` for an erase
    episode. Returns ``"started"`` (also for a duplicate start of an approval
    already launching), ``"queued"`` (an erase episode's follower, started when
    the live run ends) or ``"superseded"`` (the approval is no longer approved
    and current; nothing launched). *on_outcome*, when given, is spawned once
    with ``"started"`` when the run is registered, or ``"busy"``/``"failed"``
    when it was not."""
    slot = _SLOTS.get(key)
    if slot is not None:
        if approval is not None:
            return "started"                                     # a duplicate: the one run uses it
        slot.follower = (continuation, asked, on_outcome)
        return "queued"
    if approval is not None:                                     # still startable (a read)
        r = approval_records.get(approval.rid)
        cur = approval_records.current(approval.component_key)
        if r is None or cur is None or cur.rid != r.rid or r.state != "approved":
            return "superseded"
        approval = r
    slot = _SLOTS[key] = _Slot()
    slot.task = asyncio.get_running_loop().create_task(          # a clean context: nothing of
        _continuation(key, slot, continuation, approval, asked, on_outcome),   # the caller's
        context=contextvars.Context())                           # run leaks in
    return "started"


async def _continuation(key: str, slot: _Slot, continuation: str, approval, asked: str,
                        on_outcome: OnOutcome):
    import agent as agent_mod
    import log_cid
    import tools as tools_mod
    from channels.telegram_transport import telegram_transport
    from specialist_registry import DelegationRecord

    outcome, permit, reservation = "failed", None, None
    try:
        cfg = tools_mod._agent_role_map.get("configurator")      # the configurator, the live ACL
        transport = telegram_transport()
        if cfg is None or not configurator_run.is_configurator(cfg) or transport is None:
            return
        try:
            resident = transport.casa_endpoint().default_agent
        except RuntimeError:
            return
        operator = transport.operator_user_id()
        if operator is None or not tools_mod.declares_delegate(resident, "configurator"):
            return
        # fresh: one bound for the permit, the lane and the lease
        deadline = time.monotonic() + configurator_run.LEASE_WAIT_S
        limiter = tools_mod._specialist_limiter                  # the permit first
        while limiter is not None and permit is None:
            permit = limiter.try_acquire(f"{operator}:configurator")
            if permit is None:
                if time.monotonic() > deadline:
                    outcome = "busy"
                    return
                await asyncio.sleep(RETRY_EVERY_S)
        lane = agent_lane.LANES.get("configurator")              # then a place on the lane
        if lane.faulted:
            return
        reservation = lane.reserve()
        if reservation is None:
            outcome = "busy"
            return
        if approval is not None:                                 # the frozen task text
            asked = approval.asked
            task_text = continuation + values_line(approval.values)
        else:
            task_text = continuation
        delegation_id = str(uuid.uuid4())                        # the durable row
        origin = _launch_origin(transport, resident, operator, asked)
        # the empty context dropped the caller's id: this run's logs carry its own
        log_cid.cid_var.set(origin["cid"])
        birth = tools_mod._task_birth(delegation_id, task_text, "configurator", origin)
        rec = DelegationRecord(id=delegation_id, agent="configurator", started_at=time.time(),
                               origin=dict(origin), **tools_mod._task_record_fields(birth))
        rec.permit = permit
        await tools_mod._specialist_registry.register_delegation(rec)
        tools_mod._attach_completion_callback(asyncio.current_task(), rec, birth=birth)
        tools_mod._record_launch_safe("configurator")
        outcome = "started"
        _tell(on_outcome, "started")
        child = {**origin, **task_delivery_marker(birth.delivery)}   # this task's own context
        if approval is not None:
            child[APPROVAL_KEY] = {"rid": approval.rid, "generation": approval.generation}
        agent_mod.origin_var.set(child)
        tools_mod._delegation_quota_key.set(delegation_id)
        return await _run(lane, reservation, cfg, task_text, deadline)   # lane, lease, the run
    except Exception:  # noqa: BLE001 — told once below; after registration the callback reports it
        if outcome != "started":
            logger.warning("configurator continuation not started", exc_info=True)
            return
        raise
    finally:
        if reservation is not None:
            reservation.release()
        if permit is not None:
            permit.release()
        if _SLOTS.get(key) is slot:
            del _SLOTS[key]
        if outcome != "started":
            _tell(on_outcome, outcome)
        task = asyncio.current_task()
        if slot.follower is not None and not (task is not None and task.cancelling()):
            # not at shutdown: the queued follower takes the slot now
            continuation, asked, follower_outcome = slot.follower
            start(continuation, key=key, asked=asked, on_outcome=follower_outcome)


async def _run(lane, reservation, cfg, task_text: str, deadline: float):
    """The lane, then the bounded runner, which takes the lease."""
    import tools as tools_mod
    async with lane.use(reservation, deadline) as admitted:
        if not admitted:
            raise configurator_run.ConfiguratorBusy(
                f"{configurator_run.busy_error()} — asking again carries it on")
        if lane.faulted:
            raise agent_lane.LaneFaulted(agent_lane.faulted_line(
                tools_mod._display_name_for_role(cfg.role)))
        runs: list = []
        tok = tools_mod._lane_run_sink.set(runs)
        try:
            with configurator_run.waiting_until(deadline):
                return await tools_mod._run_delegated_agent_bounded(cfg, task_text, "")
        finally:
            tools_mod._lane_run_sink.reset(tok)
            lane.hold_unwinding(runs)
