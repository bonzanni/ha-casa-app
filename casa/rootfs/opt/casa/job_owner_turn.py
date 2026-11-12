"""The turn a specialist gets when a job it started for someone else ends.

A specialist working for a requester (the resident, or a specialist that
handed work on) can start a job hosted on itself. When that job completes,
the specialist hears the result first, in a delegated turn of its own with its
normal tools, and what that turn ends with decides what the requester is told:

* an answer replaces the job's result in the notice to the requester;
* a closing message that is exactly ``<silent/>`` declares progress only:
  the specialist has started more work whose end will come back to it, so
  nobody is told and no notice stays owed for this job;
* a turn that could not run, did not complete or was cancelled leaves the
  job's own result to be sent, with :data:`FOLLOW_UP_FAILED_LINE` after it.

The turn counts as part of the operator's task when the operator started the
job, and carries how many such turns came before it along the same task;
after :data:`MAX_OWNER_PASSES` a job's end goes straight to the requester.

A job started by a resident on a specialist, from the specialist's own chat,
or one that ended any other way than completed, gets no such turn.
"""
from __future__ import annotations

import dataclasses
import logging
import time
import uuid
from collections.abc import Mapping

from handoff import handoff_chain
from output_boundary import SILENCE_SENTINEL
from plugin_continuations import (
    CONTINUATION_MARKER, GIVE_UP_AFTER_S, ContinuationDropped, _lane_turn,
    _run_turn_cancellable)
from provenance import (
    OPERATOR_TASK_KEY, OWNER_PASS_KEY, carry_route, carry_task_delivery)

logger = logging.getLogger(__name__)

# How many times in a row, along one task, a job's end is first handed to the
# specialist that started it. A bound against a loop, set well above what real
# work needs.
MAX_OWNER_PASSES = 5

FOLLOW_UP_FAILED_LINE = "(The follow-up on this result did not run.)"

OWNER_TASK = (
    'Your job "{title}" has finished. Its result:\n'
    "\n"
    "{result}\n"
    "\n"
    "You started this job for someone else, and they have not been told yet: your answer to\n"
    "this message is what they hear. Before you answer, check with your own tools what is\n"
    "still open. If a colleague could fill a gap, hand the whole batch to them in one request\n"
    "with the facts they need, then use what comes back. Say plainly in your answer what\n"
    "could not be done.\n"
    "\n"
    "If you have just started more work whose end will come back to you (another job, a\n"
    "colleague still working), close with exactly <silent/> and nothing else: nobody is told\n"
    "until that work ends. Otherwise answer now.")


@dataclasses.dataclass(frozen=True)
class OwnerResult:
    kind: str          # "answer" | "silent" | "failed"
    text: str = ""     # the owner's answer when kind == "answer"


def _job(origin: Mapping) -> Mapping:
    job = origin.get("job")
    return job if isinstance(job, Mapping) else {}


def owner_role(frozen: Mapping, outcome: str) -> str | None:
    """The specialist that hears this job's end before its requester, or
    ``None``: the job completed, its host started it itself, for someone other
    than itself, and fewer than ``MAX_OWNER_PASSES`` such turns came before
    along this task. *frozen* is the finalize funnel's snapshot of the ended
    engagement."""
    origin = frozen["origin"]
    host = frozen["role_or_type"]
    if outcome != "completed" or _job(origin).get("self_host") is not True:
        return None
    if (origin.get("role") or "assistant") == host:
        return None
    if origin.get(OWNER_PASS_KEY, 0) >= MAX_OWNER_PASSES:
        return None
    return host


def owner_origin(frozen: Mapping) -> dict:
    """The origin the owner's delegated turn runs under: rebuilt from the
    job's stored origin as the turn of whoever asked the owner, in the same
    chat and through the same route. Nothing of the message that started the
    job is carried; the turn belongs to the operator's task only when the
    operator started the job."""
    o = frozen["origin"]
    host = frozen["role_or_type"]
    root = o.get("role") or "assistant"
    chain = handoff_chain(o)
    upto = chain[:chain.index(host)] if host in chain else chain[:1]
    return {
        "role": root,
        "execution_role": upto[-1] if upto else root,
        "channel": "telegram", "source": "telegram", "message_type": "channel_in",
        "chat_id": o.get("chat_id"), "user_id": o.get("user_id"),
        "user_name": o.get("user_name"),
        "cid": "own-" + uuid.uuid4().hex[:12],
        "user_text": "",
        "synthetic": CONTINUATION_MARKER,
        "_origin_route": o.get("_origin_route", "telegram"),
        "_origin_clearance": o.get("_origin_clearance", "private"),
        "delegation_depth": max(len(upto) - 1, 0),
        "_handoff_chain": tuple(upto) or (root,),
        OWNER_PASS_KEY: o.get(OWNER_PASS_KEY, 0) + 1,
        **({OPERATOR_TASK_KEY: True} if _job(o).get("started_by") == "operator" else {}),
        **carry_route(o),
        **carry_task_delivery(o),
    }


async def run_owner_turn(frozen: Mapping, result_text: str) -> OwnerResult:
    """Run the owner's turn on *result_text*, the ended job's result, and say
    how it ended. Raises nothing but ``asyncio.CancelledError``.

    The caller is the ended job's finalizer, running in that job's own
    context, where the engagement is still bound. The turn is an ordinary
    delegated turn, not a turn of the ended job, so it runs with no engagement
    bound: the binding is cleared around the lane wait and the turn (whose
    task is created inside that scope) and restored after."""
    import tools as tools_mod
    host = frozen["role_or_type"]
    cfg = tools_mod._agent_role_map.get(host)
    if cfg is None:
        logger.warning("job %s: %s is not loaded; its end goes to the requester as it is",
                       str(frozen.get("id", ""))[:8], host)
        return OwnerResult("failed")
    origin = frozen["origin"]
    task = OWNER_TASK.format(title=_job(origin).get("title") or _job(origin).get("name") or "",
                             result=result_text)
    unbound = tools_mod.engagement_var.set(None)
    try:
        async with _lane_turn(origin.get("chat_id"), host,
                              deadline=time.monotonic() + GIVE_UP_AFTER_S):
            output = await _run_turn_cancellable(
                cfg, task, owner_origin(frozen), quota_key="owner-" + uuid.uuid4().hex)
    except ContinuationDropped as exc:
        logger.warning("job %s: %s got no turn at its end (%s)",
                       str(frozen.get("id", ""))[:8], host, exc)
        return OwnerResult("failed")
    except Exception:  # noqa: BLE001 — a failed follow-up never loses the job's result
        logger.warning("job %s: %s's turn at its end failed",
                       str(frozen.get("id", ""))[:8], host, exc_info=True)
        return OwnerResult("failed")
    finally:
        tools_mod.engagement_var.reset(unbound)
    if output.run_aborted:
        return OwnerResult("failed")
    if tools_mod._closing_text(output).strip() == SILENCE_SENTINEL:
        return OwnerResult("silent")
    return OwnerResult("answer", output.text)
