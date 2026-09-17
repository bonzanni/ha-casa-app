"""Background jobs: a plugin-declared skill run as a specialist engagement in batches.

A plugin declares jobs in its manifest's ``casa.jobs`` block
(``plugin_store.manifest_jobs`` validates them). The assistant starts one with
the ``start_job`` tool; it runs as an interactive engagement of the specialist
whose session loads the declaring plugin. The launch turn only acknowledges;
every later batch is a system turn Casa starts itself once the previous turn
has ended and nothing else is queued or running (``start_next_batch``), and
the specialist reports each batch through ``report_job_progress``.

The job's counters live in the engagement record's ``origin["job"]`` so they
survive a restart; ``initial_job_state`` is their shape.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Casa tool granted to a specialist session only while it runs a job.
JOB_CASA_GRANTS: tuple[str, ...] = ("mcp__casa-framework__report_job_progress",)


@dataclass(frozen=True)
class JobDecl:
    """One ``casa.jobs`` entry of an installed plugin."""

    qualified_name: str            # "<manifest name>:<job name>"
    plugin: str                    # the declaring plugin's manifest name
    name: str                      # the job's own name
    skill: str                     # "<manifest name>:<skill>"
    title: str
    summary: str | None
    batches: int | None            # None = unlimited
    turns_per_batch: int | None    # None = the specialist's tools.max_turns


# ---------------------------------------------------------------------------
# Declaration and listing
# ---------------------------------------------------------------------------

def jobs_for_specialist(role: str) -> dict[str, JobDecl]:
    """The jobs declared by the plugins a specialist's session loads, keyed by
    qualified name. Any error resolving plugins yields an empty dict."""
    try:
        import plugin_registry
        from plugin_store import manifest_jobs

        jobs: dict[str, JobDecl] = {}
        for resolved in plugin_registry.resolve_for(f"specialist:{role}").plugins:
            manifest = resolved.manifest
            plugin = manifest["name"]
            for entry in manifest_jobs(manifest):
                qualified_name = f"{plugin}:{entry['name']}"
                jobs[qualified_name] = JobDecl(
                    qualified_name=qualified_name,
                    plugin=plugin,
                    name=entry["name"],
                    skill=f"{plugin}:{entry['skill']}",
                    title=entry["title"],
                    summary=entry.get("summary"),
                    batches=(None if entry["batches"] == "unlimited"
                             else entry["batches"]),
                    turns_per_batch=entry.get("turnsPerBatch"),
                )
        return jobs
    except Exception:
        logger.warning("Could not list background jobs for specialist %s", role,
                       exc_info=True)
        return {}


def startable_jobs(delegate_roles: Iterable[str]) -> list[tuple[str, JobDecl]]:
    """``(role, decl)`` for every job a caller with these delegates can start,
    in delegates order; a job declared for two roles appears once, first role."""
    found: list[tuple[str, JobDecl]] = []
    names: set[str] = set()
    for role in delegate_roles:
        for name, decl in jobs_for_specialist(role).items():
            if name not in names:
                names.add(name)
                found.append((role, decl))
    return found


def find_job_host(job: str, delegate_roles: Iterable[str]) -> tuple[str, JobDecl] | None:
    """The specialist (first in delegates order) that runs *job*, or None."""
    for role, decl in startable_jobs(delegate_roles):
        if decl.qualified_name == job:
            return role, decl
    return None


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def initial_job_state(decl: JobDecl) -> dict:
    """The ``origin["job"]`` a job engagement is created with."""
    return {
        "name": decl.qualified_name,
        "title": decl.title,
        "skill": decl.skill,
        "batches": decl.batches,
        "turns_per_batch": decl.turns_per_batch,
        "started": 0,
        "judged": 0,
        "stuck": 0,
        "reported": False,
        "remaining": None,
        "prev_remaining": None,
        "last_summary": None,
    }


def launch_prompt(decl: JobDecl, task: str, context: str, turns_per_batch: int) -> str:
    """The launch turn: state the job, acknowledge in one line, do no work."""
    return (
        f'You are starting the background job "{decl.title}" (skill {decl.skill}).\n'
        f"Request: {task}\n"
        f"Context: {context}\n"
        "In THIS turn do no work: reply with one short line saying what you are "
        "about to do, then end your turn.\n"
        "Casa then starts batch 1. How the job runs:\n"
        f"- Each batch is one turn of at most {turns_per_batch} turns. Load the skill, "
        "do one bounded batch, call report_job_progress (a one-line summary; "
        "done/remaining counts if the skill has them) as your last action, then end "
        "your turn. Casa starts the next batch. Running out of turns only ends the batch.\n"
        "- Messages the operator writes in this topic arrive as their own turns: "
        "answer briefly and end your turn; the job then continues.\n"
        "- When nothing is left, call emit_completion with a summary for the operator. "
        "If it is refused because a message is unread, end your turn, read it, then complete."
    )


def batch_prompt(n: int, title: str) -> str:
    return f'Batch {n} of "{title}": continue the job.'


# ---------------------------------------------------------------------------
# Live turn-delivery owners (per engagement)
# ---------------------------------------------------------------------------
#
# A turn is "in progress" from the moment its delivery owner (the launch owner
# or the channel's turn-delivery task) starts until that owner has finished its
# finalization and incomplete/error adjudication. Not the driver's per-turn
# lock, which is released before the final stream delivery.

_turn_owners: dict[str, int] = {}


def turn_owner_started(engagement_id: str) -> None:
    _turn_owners[engagement_id] = _turn_owners.get(engagement_id, 0) + 1


def turn_owner_finished(engagement_id: str) -> None:
    n = _turn_owners.get(engagement_id, 0) - 1
    if n > 0:
        _turn_owners[engagement_id] = n
    else:
        _turn_owners.pop(engagement_id, None)


def turn_owners(engagement_id: str) -> int:
    return _turn_owners.get(engagement_id, 0)


# ---------------------------------------------------------------------------
# The batch loop
# ---------------------------------------------------------------------------

async def job_after_turn(rec: Any, channel: Any, *, turn_cut_off: bool = False) -> None:
    """Runs after every turn of a job engagement, once that turn's own owner has
    finished (and called ``turn_owner_finished``)."""
    latest = channel._engagement_registry.get(rec.id)
    if latest is None or latest.status not in ("active", "idle"):
        return
    if turn_cut_off:
        from tools import _finalize_engagement
        await _finalize_engagement(
            latest, outcome="error", text="a batch stopped before finishing",
            artifacts=[], next_steps=[], driver=channel._engagement_driver)
        return
    await start_next_batch(latest, channel)


async def start_next_batch(rec: Any, channel: Any) -> None:
    """The only place a batch is counted, judged, limit-checked and started."""
    driver = channel._engagement_driver
    if driver.inbound_unread_depth(rec.id) > 0 or turn_owners(rec.id):
        return
    job = rec.origin["job"]
    if job["started"] > job["judged"]:
        progress = job["reported"] and (
            job["remaining"] is None or job["prev_remaining"] is None
            or job["remaining"] < job["prev_remaining"])
        job["stuck"] = 0 if progress else job["stuck"] + 1
        job["prev_remaining"] = job["remaining"]
        job["reported"] = False
        job["judged"] = job["started"]
    detail = None
    if job["stuck"] >= 3:
        detail = "no progress in 3 consecutive batches"
    elif isinstance(job["batches"], int) and job["started"] >= job["batches"]:
        detail = f'reached its limit of {job["batches"]} batches'
    if detail:
        from tools import _finalize_engagement
        task = asyncio.create_task(_finalize_engagement(
            rec, outcome="error", text=detail, artifacts=[], next_steps=[],
            driver=driver))
        channel._turn_tasks.add(task)
        task.add_done_callback(channel._turn_tasks.discard)
        return
    job["started"] += 1
    handed_off = await channel.deliver_system_turn(
        rec, batch_prompt(job["started"], job["title"]))
    await channel._engagement_registry.persist_origin(rec.id)
    if not handed_off:
        logger.info("job batch handoff refused for %s", rec.id)
