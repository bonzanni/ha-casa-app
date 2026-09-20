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
import time
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Casa tool granted to a specialist session only while it runs a job.
JOB_CASA_GRANTS: tuple[str, ...] = ("mcp__casa-framework__report_job_progress",)

# The COMPLETE casa-framework surface of a plugin-job (resident-hosted) session:
# it reports and it finishes, and the bridge admits exactly this — no union with
# MANDATORY_BRIDGE_CASA_GRANTS, so a worker never holds query_engager (C3).
PLUGIN_JOB_CASA_GRANTS: tuple[str, ...] = (
    "mcp__casa-framework__report_job_progress",
    "mcp__casa-framework__emit_completion",
)

# A job that has not advanced for this long, with no turn queued and no delivery
# owner live, is stalled and the sweep continues it (C7a).
_JOB_STALL_S: float = 180.0

# Consecutive failed continuations before the sweep ends the job (C7a).
_JOB_MAX_STALLS: int = 3

# A launch creates its engagement only after a few awaits.  Keep the small
# in-process claim separate from durable records so two starts cannot pass
# that window for the same installed plugin.
_pending_plugin_job_starts: dict[str, tuple[str, str]] = {}


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


@dataclass(frozen=True)
class JobHost:
    """Who runs a job: a specialist the caller delegates to, or the calling
    resident itself (the declaring plugin is installed on it)."""

    kind: str                  # "specialist" | "resident"
    role: str                  # the specialist's role, or the hosting resident's
    decl: "JobDecl"
    plugin: Any                # the declaring ResolvedPlugin


def running_job_for_plugin(registry: Any, plugin: str) -> Any | None:
    """Return the live job record for an installed plugin, if any.

    New plugin jobs persist their registry identity in ``plugin_job``.  The
    phase-one records have no such marker, so recover it from the pinned
    artifact whose manifest name declares the qualified job.
    """
    for rec in registry.active_and_idle():
        origin = getattr(rec, "origin", None) or {}
        job = origin.get("job")
        if not isinstance(job, dict):
            continue
        meta = origin.get("plugin_job")
        if isinstance(meta, dict):
            recorded = (meta.get("registry_name") or meta.get("plugin")
                        or meta.get("name"))
            if recorded == plugin:
                return rec
            continue
        # No recorded identity (a phase-1 record): the qualified job name
        # carries the declaring plugin's manifest name, which is what a
        # phase-1 declaration is keyed by. Prefer a pinned artifact when one
        # names this installed plugin, else fall back to that prefix.
        qualified = job.get("name")
        manifest_name = qualified.split(":", 1)[0] if isinstance(qualified, str) else ""
        recorded_names = {artifact.get("name") for artifact in
                          (getattr(rec, "plugin_artifacts", ()) or ())
                          if isinstance(artifact, dict)}
        for artifact in getattr(rec, "plugin_artifacts", ()) or ():
            if not isinstance(artifact, dict):
                continue
            if artifact.get("manifest_name") == manifest_name and artifact.get("name") == plugin:
                return rec
        # Only when the record names no installed plugin at all does the
        # qualified job name stand in for its identity: a record that DOES name
        # one and did not match above is a different installed plugin, and
        # refusing on its manifest prefix would block two distinct plugins from
        # running side by side (diff review r1).
        if manifest_name and manifest_name == plugin and not recorded_names:
            return rec
    return None


def pending_plugin_job_start(plugin: str) -> tuple[str, str] | None:
    """The qualified job and title currently claiming *plugin*, if any."""
    return _pending_plugin_job_starts.get(plugin)


def claim_plugin_job_start(plugin: str, job: str, title: str) -> bool:
    """Synchronously claim an installed plugin's short pre-record window."""
    if plugin in _pending_plugin_job_starts:
        return False
    _pending_plugin_job_starts[plugin] = (job, title)
    return True


def release_plugin_job_start(plugin: str) -> None:
    _pending_plugin_job_starts.pop(plugin, None)


def _job_busy_refusal(plugin: str, job: str, title: str, rec: Any | None) -> dict:
    """C6's refusal: a live record names its ids, a pending start does not."""
    if rec is None:
        return {"status": "error", "kind": "job_busy", "plugin": plugin,
                "job": job,
                "message": f"{plugin} is already starting {title}."}
    return {"status": "error", "kind": "job_busy", "plugin": plugin, "job": job,
            "engagement_id": rec.id, "topic_id": rec.topic_id,
            "message": (f"{plugin} already has a running job: {title}. "
                        "Wait for it to finish or /cancel it in its topic.")}


def claim_job_start(host: "JobHost", registry: Any) -> dict | None:
    """One job per installed plugin (A4): refuse when one is live or starting,
    else claim the pre-record window. The caller releases in a `finally`."""
    plugin = host_plugin_name(host)
    running = (running_job_for_plugin(registry, plugin)
               if registry is not None else None)
    if running is not None:
        job = (getattr(running, "origin", None) or {}).get("job") or {}
        return _job_busy_refusal(
            plugin, job.get("name") or host.decl.qualified_name,
            job.get("title") or getattr(running, "task", "")[:80], running)
    if not claim_plugin_job_start(plugin, host.decl.qualified_name, host.decl.title):
        pending = pending_plugin_job_start(plugin) or (
            host.decl.qualified_name, host.decl.title)
        return _job_busy_refusal(plugin, pending[0], pending[1], None)
    return None


def release_job_start(plugin: str) -> None:
    release_plugin_job_start(plugin)


def host_plugin_name(host: "JobHost") -> str:
    """The installed plugin's registry identity — the concurrency key (C6)."""
    return getattr(getattr(host, "plugin", None), "name", None) or host.decl.plugin


def acquire_job_permit(host: "JobHost", limiter: Any):
    """The per-plugin slot plus the shared global cap (C6). Never the
    resident's engagement scope, so a job cannot make its host unavailable."""
    if limiter is None:
        return None, None
    permit = limiter.try_acquire(f"plugin-job:{host_plugin_name(host)}")
    if permit is None:
        return None, {"status": "error", "kind": "busy",
                      "message": "Casa is at its concurrent-work limit. "
                                 "Try again shortly."}
    return permit, None


# ---------------------------------------------------------------------------
# Declaration and listing
# ---------------------------------------------------------------------------

def jobs_for_target(scope: str) -> dict[str, tuple[JobDecl, Any]]:
    """The jobs declared by the plugins resolved for *scope*
    (``specialist:<role>`` or ``resident:<role>``), keyed by qualified name."""
    try:
        import plugin_registry
        from plugin_store import manifest_jobs

        jobs: dict[str, tuple[JobDecl, Any]] = {}
        for resolved in plugin_registry.resolve_for(scope).plugins:
            manifest = resolved.manifest
            plugin = manifest["name"]
            for entry in manifest_jobs(manifest):
                qualified_name = f"{plugin}:{entry['name']}"
                jobs[qualified_name] = (JobDecl(
                    qualified_name=qualified_name,
                    plugin=plugin,
                    name=entry["name"],
                    skill=f"{plugin}:{entry['skill']}",
                    title=entry["title"],
                    summary=entry.get("summary"),
                    batches=(None if entry["batches"] == "unlimited"
                             else entry["batches"]),
                    turns_per_batch=entry.get("turnsPerBatch"),
                ), resolved)
        return jobs
    except Exception:
        logger.warning("Could not list background jobs for %s", scope,
                       exc_info=True)
        return {}


def jobs_for_specialist(role: str) -> dict[str, JobDecl]:
    """The jobs declared by the plugins a specialist's session loads, keyed by
    qualified name. Any error resolving plugins yields an empty dict."""
    return {name: decl for name, (decl, _plugin) in
            jobs_for_target(f"specialist:{role}").items()}


def startable_jobs(caller_role: str, delegate_roles: Iterable[str]) -> list[JobHost]:
    """Every job a caller can start, with own plugins before delegates.

    A duplicate qualified name is hosted by the first scope that declares it.
    """
    found: list[JobHost] = []
    names: set[str] = set()

    for name, (decl, plugin) in jobs_for_target(f"resident:{caller_role}").items():
        names.add(name)
        found.append(JobHost("resident", caller_role, decl, plugin))
    for role in delegate_roles:
        for name, (decl, plugin) in jobs_for_target(f"specialist:{role}").items():
            if name not in names:
                names.add(name)
                found.append(JobHost("specialist", role, decl, plugin))
    return found


def find_job_host(job: str, caller_role: str,
                  delegate_roles: Iterable[str]) -> JobHost | None:
    """The first eligible host for *job*, or None."""
    for host in startable_jobs(caller_role, delegate_roles):
        if host.decl.qualified_name == job:
            return host
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
        # Whether the current batch reported moving the job toward completion.
        # The specialist says so itself (#1031): counts are the worker's own
        # invented unit — a job can be uncountable, or its total unknown — so
        # they are shown to the operator and decide nothing.
        "advanced": False,
        "last_summary": None,
        # C7a: the sweep's inputs. `last_advance` is written whenever a batch is
        # admitted and whenever a progress report lands; `stalls` counts
        # consecutive failed continuations and resets on a successful admission.
        "last_advance": None,
        "stalls": 0,
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
        "do one bounded batch, call report_job_progress as your last action, then end "
        "your turn. Casa starts the next batch. Running out of turns only ends the batch.\n"
        "- report_job_progress takes a one-line summary and `progressed`: whether this "
        "batch moved the job toward completion, whatever that means for this work. "
        "Say false when it did not — a batch that found nothing to do, or that could "
        "not proceed. Three batches in a row without progress end the job.\n"
        "- The done/remaining counts are optional and only shown to the operator: pass "
        "them when this job's work has a real unit and a known total, leave them out "
        "otherwise. Nothing left to do means the job is finished, not a batch with "
        "0 remaining: complete it instead.\n"
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
    # This hook runs after every turn of a job, the launch turn first: its first
    # run is the moment launch ownership ends and continuation begins. Recording
    # it is what lets the sweep tell "still launching" from "running but not
    # advancing" — three review rounds showed that inferring it from the session
    # pointer and the rebuild flag misreads a post-launch state (diff review r4).
    if turn_cut_off:
        from tools import _finalize_engagement
        await _finalize_engagement(
            latest, outcome="error", text="a batch stopped before finishing",
            artifacts=[], next_steps=[], driver=channel._engagement_driver)
        return
    await start_next_batch(latest, channel)


async def sweep_jobs(registry: Any, channel: Any) -> None:
    """Periodic owner of job liveness (C7a): continue a stalled job, and end one
    that could not be continued `_JOB_MAX_STALLS` times running."""
    if channel is None or channel._stopping:
        return
    driver = channel._engagement_driver
    for rec in registry.active_and_idle():
        if channel._stopping:
            return
        job = rec.origin.get("job")
        if not job or driver.inbound_unread_depth(rec.id) > 0 or turn_owners(rec.id):
            continue
        # The launch owns the record until its acknowledgement turn ends, and
        # until then there is no turn owner to see (diff review r1). This is a
        # guard against acting, not a claim about ownership: a record can also
        # leave its launch without ever reaching here — which is why the sweep
        # below only ever CONTINUES a job it cannot prove is running, and needs
        # three failed attempts of its own before it ends one.
        if getattr(registry, "launch_in_flight", None) and registry.launch_in_flight(rec.id):
            continue
        # A job that has never advanced is measured from when it was created,
        # not from the epoch.
        since = job.get("last_advance") or getattr(rec, "started_at", None) or 0
        if time.time() - since < _JOB_STALL_S:
            continue
        try:
            admitted = await start_next_batch(rec, channel)
        except Exception:
            logger.warning("job continuation failed for %s", rec.id, exc_info=True)
            admitted = False
        if channel._stopping:
            return
        if admitted or rec.status not in ("active", "idle"):
            continue
        # A refused continuation is the ONLY thing that counts toward ending a
        # job here. The sweep cannot tell a job that is still launching from one
        # that is stuck — seven review rounds established that no combination of
        # live state says so reliably — so it never ends a job on a state read:
        # it continues one, harmlessly if it was launching, and only its own
        # three refused attempts end it (INV-BGJOB-003).
        job["stalls"] = job.get("stalls", 0) + 1
        await registry.persist_origin(rec.id)
        if job["stalls"] >= _JOB_MAX_STALLS:
            from tools import _finalize_engagement
            await _finalize_engagement(
                rec, outcome="error",
                text=f"could not be continued after {_JOB_MAX_STALLS} attempts",
                artifacts=[], next_steps=[], driver=driver)


async def start_next_batch(rec: Any, channel: Any) -> bool:
    """The only place a batch is counted, judged, limit-checked and started.

    Returns whether it ADMITTED a turn: False for a quiet skip, a refused
    hand-off, or a finalize. The batch number is chosen and the previous batch
    judged synchronously (the operator's "count when a batch starts"), but
    `started` is committed and persisted only once the hand-off succeeded — a
    refused continuation costs nothing (C7a, review r3)."""
    driver = channel._engagement_driver
    if driver.inbound_unread_depth(rec.id) > 0 or turn_owners(rec.id):
        return False
    # Stage the judgment: a refused hand-off must cost no batch or progress.
    job = dict(rec.origin["job"])
    if "advanced" not in job:
        # A job launched before #1031 persisted `reported`/`remaining`/
        # `prev_remaining` instead. Its one in-flight batch ran under the count
        # rule and is judged under it, so an upgrade mid-job neither invents a
        # stuck batch nor throws away one the old rule had credited.
        job["advanced"] = bool(job.get("reported")) and (
            job.get("remaining") is None or job.get("prev_remaining") is None
            or job["remaining"] < job["prev_remaining"])
    if job["started"] > job["judged"]:
        # A batch made progress when it said so through report_job_progress.
        # A batch that reported no progress, or ended without reporting at all
        # (out of turns, an error, or it simply did not call the tool), did not.
        job["stuck"] = 0 if job.get("advanced") else job["stuck"] + 1
        job["advanced"] = False
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
        await task
        return False
    job["started"] += 1
    handed_off = await channel.deliver_system_turn(
        rec, batch_prompt(job["started"], job["title"]))
    if not handed_off:
        logger.info("job batch handoff refused for %s", rec.id)
        return False
    rec.origin["job"].update(
        {key: job[key] for key in ("started", "judged", "stuck", "advanced")})
    rec.origin["job"].update(last_advance=time.time(), stalls=0)
    await channel._engagement_registry.persist_origin(rec.id)
    return True
