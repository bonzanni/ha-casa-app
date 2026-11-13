"""Background jobs: a plugin-declared skill run as one long session.

A plugin declares jobs in its manifest's ``casa.jobs`` block
(``plugin_store.manifest_jobs`` validates them). The assistant starts one with
the ``start_job`` tool; it runs as an engagement of the specialist whose
session loads the declaring plugin, or of a resident's plugin worker. Casa
writes the job's brief into its session once; everything later (an operator's
message, a tapped answer, Casa's own nudge) is written in as it arrives. The
model decides when to report progress, when to ask and when to finish.

Casa never ends a job on its own judgement. When a turn ends without the job
finishing, the turn-end guard (:meth:`JobRunner.turn_ended`) continues it,
nudges it once, or parks it: a parked job waits for the operator, who is told
in their direct chat and can reply to continue it or Cancel. Only Cancel, a
failed resume and the stale-job reap end one.

The job's durable state lives in the engagement record's ``origin["job"]``;
``initial_job_state`` is its shape.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import scratchpad
from channels.tg_richtext import TIME_LINK_GUIDE

logger = logging.getLogger(__name__)

# Casa tools granted to a specialist session only while it runs a job: its
# progress lines, and its questions to the operator.
JOB_CASA_GRANTS: tuple[str, ...] = ("mcp__casa-framework__report_job_progress",
                                    "mcp__casa-framework__ask_operator")

# The COMPLETE casa-framework surface of a plugin-job (resident-hosted) session:
# it reports progress and it finishes, and the bridge admits exactly this — no union with
# MANDATORY_BRIDGE_CASA_GRANTS, so a worker never holds query_engager (C3).
PLUGIN_JOB_CASA_GRANTS: tuple[str, ...] = (
    "mcp__casa-framework__report_job_progress",
    "mcp__casa-framework__emit_completion",
)

# The step ceiling of one write into a job's session. Measured: ``max_turns``
# applies per ``query()``, and a message absorbed mid-turn neither resets nor
# extends the running turn's count. The ceiling is high on purpose: reaching
# it is a continuation boundary, not a stop, so it only bounds a loop.
JOB_MAX_TURNS: int = 1000

# A job's scratchpad: one file per job, written through on every change, kept
# while the job lives and deleted when it ends.
JOB_PAD_ROOT = Path("/data/job-pads")
JOB_PROMPT_LINE = (
    "You have a scratchpad for this job. Keep your list and your progress "
    "there and read it after a summary; it lasts through compaction and a "
    "Casa restart, and goes when the job ends.")
# The tool's own text, with the lifetime clause a job's pad does not have.
JOB_DESCRIPTION = scratchpad.DESCRIPTION.replace(
    "they go when it ends or Casa restarts",
    "they last through a Casa restart and go when the job ends")


def job_pad_path(engagement_id: str) -> Path:
    """The file a job's scratchpad is written through to."""
    return JOB_PAD_ROOT / f"{engagement_id}.pad"


def job_session_options(options: Any, rec: Any, *,
                        brief: "tuple[str, str | None] | None" = None) -> Any:
    """*options* as every job session runs: the high step ceiling, the
    long-session auto-compaction window, the CLI's echo of each written
    message at take-up, the ``SessionStart`` hook that re-states the job
    after a compaction, and the job's scratchpad. Applied by the job options
    builders (launch and resume, for a specialist and for a resident-hosted
    worker alike), so a resumed session runs exactly as the launched one did.

    *brief* is ``(task, operator words)``, passed only at a launch: it opens
    a pad that does not exist yet. A resume passes none and finds its pad in
    memory or in its file, or starts an empty one bound to the file."""
    import dataclasses
    from claude_runtime import REPLAY_USER_MESSAGES_ARG, long_session_env
    hooks = dict(options.hooks or {})
    hooks["SessionStart"] = [*hooks.get("SessionStart", []),
                             compaction_hook_matcher(rec)]
    options = dataclasses.replace(
        options,
        max_turns=JOB_MAX_TURNS,
        env={**(options.env or {}), **long_session_env()},
        extra_args={**(options.extra_args or {}), REPLAY_USER_MESSAGES_ARG: None},
        hooks=hooks,
    )
    task, words = brief if brief is not None else (None, None)
    return scratchpad.attach(
        options, rec.id, eager=True, task=task, words=words,
        path=job_pad_path(rec.id), prompt_line=JOB_PROMPT_LINE,
        description=JOB_DESCRIPTION)


def job_header(rec: Any) -> str:
    """Who the job is, from its record: what a session re-reads after its
    context was summarised. ``Started by:`` follows the first ``Job id:`` line
    directly, as in the brief."""
    job = (getattr(rec, "origin", None) or {}).get("job") or {}
    return (
        f'You are running the background job "{job.get("title")}" '
        f'(skill {job.get("skill")}).\n'
        f"Job id: {rec.id}\n"
        + starter_line(job.get("started_by"))
        + f"Request: {getattr(rec, 'task', '')}\n"
        "Your context was just summarised; carry on."
    )


def compaction_hook_matcher(rec: Any) -> Any:
    """The ``SessionStart`` SDK callback hook of a job session. The SDK's
    ``HookEvent`` type does not list ``SessionStart``, but the pinned CLI
    accepts it as a callback hook, fires it with ``source: "compact"`` after
    every compaction (manual and automatic), and puts its
    ``additionalContext`` into the model's context (measured). Any other
    source adds nothing.

    The job's scratchpad is re-stated after the header: its shape and its
    brief (:func:`scratchpad.reinject_text`)."""
    from claude_agent_sdk import HookMatcher

    async def re_state_job(hook_input: Any, tool_use_id: Any, context: Any) -> dict:
        source = hook_input.get("source") if isinstance(hook_input, dict) else None
        if source != "compact":
            return {}
        logger.info("job %s context compacted: job header re-stated", rec.id[:8])
        text = job_header(rec)
        if pad := scratchpad.reinject_text(rec.id):
            text = f"{text}\n\n{pad}"
        return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": text}}

    return HookMatcher(matcher=None, hooks=[re_state_job])


# A launch creates its engagement only after a few awaits.  Keep the small
# in-process claim separate from durable records so two starts cannot pass
# that window for the same installed plugin.
_pending_plugin_job_starts: dict[str, tuple[str, str]] = {}
# The same window keyed by the job's manifest name (INV-BGJOB-006), mapped to
# the installed plugin holding the claim, so two installations sharing a
# manifest name cannot both pass it either.
_pending_manifest_job_starts: dict[str, str] = {}


def job_manifest_name(qualified: Any) -> str:
    """The manifest name a qualified job name carries: the text before its
    first ``:``. Every job record has the qualified name in
    ``origin["job"]["name"]``, whatever its host kind or its age."""
    return qualified.split(":", 1)[0] if isinstance(qualified, str) else ""


@dataclass(frozen=True)
class JobDecl:
    """One ``casa.jobs`` entry of an installed plugin."""

    qualified_name: str            # "<manifest name>:<job name>"
    plugin: str                    # the declaring plugin's manifest name
    name: str                      # the job's own name
    skill: str                     # "<manifest name>:<skill>"
    title: str
    summary: str | None
    host: str | None = None        # "specialist" = never hosted by a resident
    # #1301: a run the scheduler started is quiet — no start line in the
    # direct chat and no telling of a completed end. Its log, when a log group
    # is set up, is written like any job's.
    quiet_when_scheduled: bool = False


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

    "Live" here is ``registry.job_occupants()``: a record whose strict
    terminal write is still pending counts, since a failed write puts it
    back live (#1173).
    """
    for rec in registry.job_occupants():
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


def running_job_for_manifest(registry: Any, manifest_name: str) -> Any | None:
    """Return a live job record whose qualified job name carries
    *manifest_name*, whichever plugin installation and host kind runs it —
    a record whose strict terminal write is pending included (#1173)."""
    if not manifest_name:
        return None
    for rec in registry.job_occupants():
        job = (getattr(rec, "origin", None) or {}).get("job")
        if isinstance(job, dict) and job_manifest_name(job.get("name")) == manifest_name:
            return rec
    return None


def pending_plugin_job_start(plugin: str) -> tuple[str, str] | None:
    """The qualified job and title currently claiming *plugin*, if any."""
    return _pending_plugin_job_starts.get(plugin)


def pending_manifest_job_start(manifest_name: str) -> tuple[str, str] | None:
    """The qualified job and title currently claiming *manifest_name*, if any."""
    holder = _pending_manifest_job_starts.get(manifest_name)
    return _pending_plugin_job_starts.get(holder) if holder is not None else None


def claim_plugin_job_start(plugin: str, job: str, title: str) -> bool:
    """Synchronously claim the short pre-record window of an installed plugin
    AND of its job's manifest name; both or neither."""
    manifest_name = job_manifest_name(job)
    if (plugin in _pending_plugin_job_starts
            or manifest_name in _pending_manifest_job_starts):
        return False
    _pending_plugin_job_starts[plugin] = (job, title)
    _pending_manifest_job_starts[manifest_name] = plugin
    return True


def release_plugin_job_start(plugin: str) -> None:
    claimed = _pending_plugin_job_starts.pop(plugin, None)
    if claimed is not None:
        manifest_name = job_manifest_name(claimed[0])
        if _pending_manifest_job_starts.get(manifest_name) == plugin:
            del _pending_manifest_job_starts[manifest_name]


def _job_busy_refusal(plugin: str, job: str, title: str, rec: Any | None) -> dict:
    """C6's refusal: a live record names its ids, a pending start does not."""
    if rec is None:
        return {"status": "error", "kind": "job_busy", "plugin": plugin,
                "job": job,
                "message": f"{plugin} is already starting {title}."}
    if is_quiet_run(rec):
        # #1301: a quiet scheduled run has no start line to cancel it from.
        return {"status": "error", "kind": "job_busy", "plugin": plugin,
                "job": job, "engagement_id": rec.id,
                "message": (f"{plugin} is already running {title} on its "
                            "schedule. Wait for it to finish.")}
    return {"status": "error", "kind": "job_busy", "plugin": plugin, "job": job,
            "engagement_id": rec.id,
            "message": (f"{plugin} already has a running job: {title}. "
                        "Wait for it to finish, or cancel it by asking me.")}


def claim_job_start(host: "JobHost", registry: Any) -> dict | None:
    """One job per installed plugin (A4) and one per plugin manifest name
    (INV-BGJOB-006): refuse when one is live or starting, else claim the
    pre-record window. The caller releases in a `finally`.

    The manifest rule reads only ``origin["job"]["name"]``, which every job
    record carries, so a record whose artifact rows cannot name its plugin (an
    older record, or one whose declaring row was lost when the plugin was
    unassigned mid-launch) still blocks. Its accepted cost: installations
    sharing a manifest name run their jobs one after the other."""
    plugin = host_plugin_name(host)
    manifest_name = job_manifest_name(host.decl.qualified_name)
    running = None
    if registry is not None:
        running = (running_job_for_plugin(registry, plugin)
                   or running_job_for_manifest(registry, manifest_name))
    if running is not None:
        job = (getattr(running, "origin", None) or {}).get("job") or {}
        return _job_busy_refusal(
            plugin, job.get("name") or host.decl.qualified_name,
            job.get("title") or getattr(running, "task", "")[:80], running)
    if not claim_plugin_job_start(plugin, host.decl.qualified_name, host.decl.title):
        pending = (pending_plugin_job_start(plugin)
                   or pending_manifest_job_start(manifest_name)
                   or (host.decl.qualified_name, host.decl.title))
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

def _declared_jobs(plugins: Iterable[Any]) -> dict[str, tuple[JobDecl, Any]]:
    """The jobs declared by *plugins* (resolved plugins), keyed by qualified
    name."""
    from plugin_store import manifest_jobs

    jobs: dict[str, tuple[JobDecl, Any]] = {}
    for resolved in plugins:
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
                host=entry.get("host"),
                quiet_when_scheduled=entry.get("quietWhenScheduled") is True,
            ), resolved)
    return jobs


def jobs_for_target(scope: str) -> dict[str, tuple[JobDecl, Any]]:
    """The jobs declared by the plugins resolved for *scope*
    (``specialist:<role>`` or ``resident:<role>``), keyed by qualified name."""
    try:
        import plugin_registry

        return _declared_jobs(plugin_registry.resolve_for(scope).plugins)
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
    A job declaring ``host: "specialist"`` is skipped in the caller's own
    scope WITHOUT claiming its name, so the first delegate declaring it hosts it.
    """
    found: list[JobHost] = []
    names: set[str] = set()

    for name, (decl, plugin) in jobs_for_target(f"resident:{caller_role}").items():
        if decl.host == "specialist":
            continue
        names.add(name)
        found.append(JobHost("resident", caller_role, decl, plugin))
    for role in delegate_roles:
        for name, (decl, plugin) in jobs_for_target(f"specialist:{role}").items():
            if name not in names:
                names.add(name)
                found.append(JobHost("specialist", role, decl, plugin))
    return found


def own_job_hosts(role: str) -> list[JobHost]:
    """S7a (INV-BGJOB-008): the jobs a SPECIALIST may start itself — those
    declared by the plugins assigned to it and loadable now, i.e. after the
    env withholding every session build applies (the set the job's own
    session loads; ruling O), each hosted by the specialist. Never the
    resident's scope, never a delegate's."""
    try:
        import plugin_registry
        from plugin_grants import withhold_env_unresolved

        resolution, _ = withhold_env_unresolved(
            plugin_registry.resolve_for(f"specialist:{role}"),
            context=f"specialist {role} start_job")
        return [JobHost("specialist", role, decl, plugin)
                for decl, plugin in _declared_jobs(resolution.plugins).values()]
    except Exception:
        logger.warning("Could not list own background jobs for %s", role,
                       exc_info=True)
        return []


def offers_start_job(cfg: Any, resolution: Any, *, engagement: bool) -> bool:
    """S7a §3.1: a specialist's delegated build (a delegation or a pinned
    stored-call turn; not a job or engagement build — the same condition as
    S6's inbox grant) is offered
    ``start_job`` when the plugins it loads declare a job. Visibility only:
    which job may start is decided at the call (``own_job_hosts``)."""
    if engagement or getattr(cfg, "kind", "") != "specialist":
        return False
    try:
        return bool(_declared_jobs(getattr(resolution, "plugins", ()) or ()))
    except Exception:  # noqa: BLE001 — a declaration error offers nothing
        logger.warning("Could not read job declarations for %s",
                       getattr(cfg, "role", "?"), exc_info=True)
        return False


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

# #1277: who started a job, as Casa knows it. Evaluated at launch from the
# launching turn's origin, recorded in ``origin["job"]["started_by"]`` and
# rendered from the record ever after — never re-derived, since a record's
# origin can change after launch and a record written before the field
# existed must say nothing.
STARTED_BY_TOKENS = ("operator", "scheduled", "agent")


def job_started_by(origin: dict) -> str:
    """``scheduled`` for a job trigger's fire; ``operator`` for a turn Casa
    attributes to the operator's authenticated act — their own message or
    tap (``_operator_turn``), their answer to a scheduled ask
    (``ANSWERED_BY_OPERATOR``), or a turn of a task the operator started
    (``OPERATOR_TASK_KEY``); ``agent`` for everything else."""
    from provenance import ANSWERED_BY_OPERATOR, OPERATOR_TASK_KEY
    if origin.get("_scheduled_job") is True:
        return "scheduled"
    if origin.get(OPERATOR_TASK_KEY) is True:
        return "operator"
    if origin.get("_operator_turn") is True or origin.get(ANSWERED_BY_OPERATOR) is True:
        return "operator"
    return "agent"


def starter_line(started_by: Any) -> str:
    """The line right after ``Job id:`` in the launch prompt and the brief;
    nothing for a record that recorded no token."""
    return f"Started by: {started_by}\n" if started_by in STARTED_BY_TOKENS else ""


def initial_job_state(decl: JobDecl, *, started_by: str | None = None,
                      context: str = "", self_host: bool = False) -> dict:
    """The ``origin["job"]`` a job engagement is created with.

    ``brief_uuid`` is the identity of the job's brief: the brief is written
    into the session with this uuid, and the CLI records a uuid in the
    session transcript only when it takes the message into a turn. A resume
    after a restart or a session death therefore knows, from the transcript
    alone, whether the job ever read its brief. ``context`` is the launch
    context, kept so the brief can be written again when it was not.
    ``self_host`` marks a job its own host started from a turn of its own
    (``start_job`` called by a specialist), as opposed to one a resident
    started on it."""
    state = {
        "name": decl.qualified_name,
        "title": decl.title,
        "skill": decl.skill,
        "brief_uuid": str(uuid.uuid4()),
        "context": context,
        "last_summary": None,
    }
    if started_by is not None:
        state["started_by"] = started_by
    if decl.quiet_when_scheduled and started_by == "scheduled":
        # #1301: decided once, here, and read from the record ever after.
        state["quiet"] = True
    if self_host:
        state["self_host"] = True
    return state


def is_quiet_run(rec: Any) -> bool:
    """Whether *rec* is a quiet run (#1301): a job declaring
    ``quietWhenScheduled`` that the scheduler started. A completed end is told
    nowhere in the direct chat; the plugin's delivered slots still reach the
    operator, and the job's log is written like any job's."""
    return is_quiet_origin(getattr(rec, "origin", None))


def was_quiet_run(rec: Any) -> bool:
    """Whether *rec* carries a quiet stamp at all — a quiet run, or one whose
    quietness an interaction ended (or is ending: a promotion clears the stamp
    in memory before its write settles, and rolls it back if the write
    fails). An interaction with such a run goes through the registry's
    promotion, which decides under its lock."""
    job = (getattr(rec, "origin", None) or {}).get("job")
    return isinstance(job, dict) and "quiet" in job


def is_quiet_origin(origin: Any) -> bool:
    """``is_quiet_run`` over a record's origin (or a frozen copy of it)."""
    job = origin.get("job") if isinstance(origin, dict) else None
    return isinstance(job, dict) and job.get("quiet") is True


# ---------------------------------------------------------------------------
# The agent log
# ---------------------------------------------------------------------------

def job_log_key(rec: Any) -> Any:
    """Whose log a job's lines go to: the specialist running it, or, for a
    job a resident's plugin worker runs, that resident's plugin log. ``None``
    for any other record (a resident's own turns never log)."""
    from agent_logs import LogKey
    role = getattr(rec, "role_or_type", None)
    if not isinstance(role, str) or not role:
        return None
    kind = getattr(rec, "kind", None)
    if kind == "specialist":
        return LogKey("agent", role)
    if kind == "plugin":
        return LogKey("plugins", role)
    return None


def job_run_name(rec: Any) -> str:
    """The run's name in its log lines: the job's title, else its task."""
    job = (getattr(rec, "origin", None) or {}).get("job") or {}
    return str(job.get("title") or getattr(rec, "task", "") or "job")


def log_job_line(logs: Any, rec: Any, text: str) -> None:
    """One line in the job's log. Synchronous: it queues the line and never
    waits on Telegram. Nothing happens without a log sink or a log key."""
    if logs is None:
        return
    try:
        key = job_log_key(rec)
        if key is not None:
            logs.log(key, job_run_name(rec), text)
    except Exception:  # noqa: BLE001 — a log line is never worth a job
        logger.debug("job log line not queued", exc_info=True)


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------

def launch_prompt(decl: JobDecl, task: str, context: str, *, job_id: str,
                  started_by: str | None, can_ask: bool = True) -> str:
    """The job's brief: the one message Casa writes at launch, which starts
    the work.

    ``job_id`` is the engagement id: a plugin can claim work by it and match
    the job-end notice, whose delegation id is the same id. ``started_by`` is
    the record's token (#1277), rendered on the line after it. ``can_ask``:
    the job runs on a specialist and may ask the operator; a resident's
    plugin worker has no question tool."""
    return (
        f'You are running the background job "{decl.title}" (skill {decl.skill}).\n'
        f"Job id: {job_id}\n"
        + starter_line(started_by)
        + f"Request: {task}\n"
        f"Context: {context or '(none)'}\n"
        "How the job runs:\n"
        + _job_rules(can_ask=can_ask)
    )


def job_brief(rec: Any) -> str:
    """The brief rendered from the record — what a resume writes when the
    session transcript shows the job never read the one written at launch."""
    job = (getattr(rec, "origin", None) or {}).get("job") or {}
    decl = JobDecl(qualified_name=str(job.get("name") or ""), plugin="",
                   name="", skill=str(job.get("skill") or ""),
                   title=str(job.get("title") or ""), summary=None)
    return launch_prompt(
        decl, getattr(rec, "task", "") or "", str(job.get("context") or ""),
        job_id=rec.id, started_by=job.get("started_by"),
        can_ask=getattr(rec, "kind", "") == "specialist")


def _job_rules(*, can_ask: bool = True) -> str:
    """How a job runs. ``can_ask`` adds how a specialist's job asks the
    operator; a resident's plugin worker has no question tool."""
    return (
        "- Load the skill and do the work. The job is one long session: keep "
        "going until it is finished.\n"
        "- report_job_progress records a one-line progress note; it appears "
        "in your log when a log group is set up. Use it when there is "
        "something worth telling; pass done and remaining only when the work "
        "has a real unit and a known total.\n"
        + ("- When something needs the operator's decision or information only "
           "they have, call ask_operator with the question and, when there are "
           "clear choices, the options. Carry on with other work if there is "
           "any; if there is none, end your turn and wait. Their answer reaches "
           "you as a message. Do not ask in your own text: only ask_operator "
           "reaches them.\n" if can_ask else
           "- You cannot ask the operator anything: set aside an item that "
           "needs their answer and say so in your progress or your summary.\n")
        + "- The operator's messages reach you as they arrive, between your "
        "steps: read them, act on them, and carry on.\n"
        "- When nothing is left, call emit_completion with a summary for the "
        "operator. If it is refused because a message has not reached you "
        "yet, read the message that arrives, then call it again.\n"
        "- " + TIME_LINK_GUIDE
    )


# ---------------------------------------------------------------------------
# Turn ends, parking, recovery
# ---------------------------------------------------------------------------

CONTINUE_LINE = "Continue the job."

_NUDGE_ASK = ("You ended your turn but the job is not finished. Continue, ask "
              "the operator with ask_operator, or call emit_completion.")
_NUDGE_NO_ASK = ("You ended your turn but the job is not finished. Continue, "
                 "or call emit_completion.")

CASA_RESTART_LINE = (
    "Casa restarted. Your conversation is intact up to the restart; a tool "
    "call that was running may or may not have finished. Continue the job.")
SESSION_RESTART_LINE = (
    "Your session restarted after its process ended unexpectedly. Your "
    "conversation is intact up to that point; a tool call that was running "
    "may or may not have finished. Continue the job.")
CASA_RESTART_PREFIX = "(Sent before Casa restarted:)"
SESSION_RESTART_PREFIX = "(Sent before the job's session restarted:)"

# ``origin["job"]["awaiting"]``: the job is waiting for the operator — it
# asked (ask_operator), an approval is pending, or it was parked. Persisted
# because boot reads it: a waiting job with nothing journaled resumes lazily,
# on the operator's next input.
AWAITING_KEY = "awaiting"
AWAITING_ASKED = "asked"
AWAITING_APPROVAL = "approval"
AWAITING_STOPPED = "stopped"

# The wait before the one "continue" after a turn ended on an API fault.
FAULT_RETRY_S: float = 60.0


@dataclass
class _JobState:
    """A live job's in-memory guard state. Lost on a restart, by design."""

    stops: int = 0              # turn ends without finishing since operator input
    faults: int = 0             # consecutive API-fault turn ends
    progress: bool = False      # report_job_progress since the last ceiling
    auto_resumed: bool = False  # the one automatic resume after a session death


_states: dict[str, _JobState] = {}
_runner: "JobRunner | None" = None


def _state(engagement_id: str) -> _JobState:
    return _states.setdefault(engagement_id, _JobState())


def configure(runner: "JobRunner | None") -> None:
    """Install the process's job runner (casa_core, once the driver exists)."""
    global _runner
    _runner = runner


def note_progress(engagement_id: str) -> None:
    """``report_job_progress`` was called: the next step-ceiling stop is a
    continuation boundary, not a loop."""
    _state(engagement_id).progress = True


def note_operator_input(rec: Any) -> None:
    """An operator message was written into the job's session: the guard
    starts over (stops, faults, progress since the last ceiling) and the job
    is no longer waiting for the operator."""
    st = _state(rec.id)
    st.stops = 0
    st.faults = 0
    st.progress = False
    job = (getattr(rec, "origin", None) or {}).get("job")
    if isinstance(job, dict) and job.pop(AWAITING_KEY, None) is not None:
        _persist_later(rec.id)


def mark_awaiting(engagement_id: str, kind: str) -> None:
    """The job is now waiting for the operator (*kind*: asked, approval or
    stopped). SYNCHRONOUS; the write is scheduled."""
    reg = _runner.registry if _runner is not None else None
    rec = reg.get(engagement_id) if reg is not None else None
    job = (getattr(rec, "origin", None) or {}).get("job")
    if not isinstance(job, dict):
        return
    job[AWAITING_KEY] = kind
    _persist_later(engagement_id)


def awaiting(rec: Any) -> str | None:
    job = (getattr(rec, "origin", None) or {}).get("job")
    value = job.get(AWAITING_KEY) if isinstance(job, dict) else None
    return value if isinstance(value, str) else None


def _persist_later(engagement_id: str) -> None:
    if _runner is not None:
        _runner.spawn(_runner.persist(engagement_id))


def format_elapsed(seconds: float) -> str:
    """``40 min``, ``1 h 5 min``, ``2 h`` — the job's running time as the
    operator reads it on the job's end notice."""
    minutes = max(1, int(seconds // 60))
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def _title(rec: Any) -> str:
    job = (getattr(rec, "origin", None) or {}).get("job") or {}
    title = str(job.get("title") or getattr(rec, "task", "") or "job")
    return (title.splitlines() or ["job"])[0][:80]


class JobRunner:
    """What Casa does around a job's session: the turn-end guard, parking,
    the one automatic resume after a session death, and the resume at boot.

    Wired into the in_casa driver as its turn-end and session-lost handlers.
    ``channel`` is the Telegram transport (the parking post, the start
    line): a new post goes to the job's delivery (``rec.delivery``, decided
    when the job was born; a job with none posts nothing) through the bot it
    names, and a post is edited through the bot that posted it, never through
    a bot it picks; ``agent_logs`` is the agent log sink (the resume line),
    when there is one.
    """

    def __init__(self, *, registry: Any, channel: Any = None,
                 driver: Any = None, agent_logs: Any = None) -> None:
        self.registry = registry
        self.channel = channel
        self.driver = driver
        self.agent_logs = agent_logs
        self._tasks: set[asyncio.Task] = set()

    # -- small helpers -----------------------------------------------------

    def spawn(self, coro: Any) -> "asyncio.Task | None":
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:        # no loop: nothing can run now
            coro.close()
            return None
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def persist(self, engagement_id: str) -> None:
        try:
            await self.registry.persist_origin(engagement_id)
        except Exception:  # noqa: BLE001 — the in-memory value stands
            logger.warning("job %s: state write failed", engagement_id[:8],
                           exc_info=True)

    def _live(self, rec: Any) -> bool:
        latest = self.registry.get(rec.id) if self.registry is not None else rec
        if latest is None or latest.status not in ("active", "idle"):
            return False
        pending = getattr(self.registry, "terminal_pending", None)
        return not (callable(pending) and pending(rec.id) is True)

    def _input_pending(self, engagement_id: str) -> bool:
        drv = self.driver
        try:
            return (drv.inbound_unread_depth(engagement_id) > 0
                    or drv.inbound_reservations(engagement_id) > 0)
        except Exception:  # noqa: BLE001 — no reading, no input assumed
            return False

    def _delivery_method(self, rec: Any, name: str) -> Any:
        """The method *name* of the endpoint at the job's delivery, or
        ``None``: a job with no delivery posts nothing, anywhere."""
        delivery = getattr(rec, "delivery", None)
        endpoint = getattr(self.channel, "endpoint", None)
        if delivery is None or not callable(endpoint):
            return None
        try:
            return getattr(endpoint(delivery.bot_id), name, None)
        except Exception:  # noqa: BLE001 — no endpoint for that bot: nothing is posted
            logger.warning("job %s: no endpoint for its delivery", rec.id[:8])
            return None

    def _write(self, rec: Any, text: str) -> None:
        self.driver.submit_casa(rec, text)

    # -- the turn-end guard ------------------------------------------------

    def turn_ended(self, rec: Any, result: Any, api_error: Any = None) -> None:
        """At every ResultMessage of a job's session, SYNCHRONOUSLY, in the
        reader. Continue, nudge once, or park — never end the job. Also
        persists the job's cost (the driver updated it in memory)."""
        job = (getattr(rec, "origin", None) or {}).get("job")
        if not isinstance(job, dict):
            return                  # an interactive record an older Casa left
        if not self._live(rec):
            _states.pop(rec.id, None)
            return
        self.spawn(self._after_turn(rec))
        st = _state(rec.id)
        if api_error is not None:
            kind = getattr(api_error, "value", api_error)
            if st.faults == 0:
                st.faults = 1
                logger.info("job %s turn ended on an API fault (%s); continuing "
                            "in %.0fs", rec.id[:8], kind, FAULT_RETRY_S)
                self.spawn(self._continue_after_fault(rec))
            else:
                self.park(rec, f"«{_title(rec)}» stopped on an error ({kind}). "
                               f"Reply to continue it, or Cancel.")
            return
        st.faults = 0
        if self._input_pending(rec.id):
            return                  # the CLI runs the queued input as the next turn
        if awaiting(rec) is not None:
            return                  # waiting for the operator
        from drivers.in_casa_driver import RESULT_SUBTYPE_MAX_TURNS
        if getattr(result, "subtype", None) == RESULT_SUBTYPE_MAX_TURNS:
            if st.progress:
                st.progress = False
                logger.info("job %s reached its step ceiling with progress; "
                            "continuing", rec.id[:8])
                self._write(rec, CONTINUE_LINE)
            else:
                self.park(rec, f"«{_title(rec)}» stopped without finishing. "
                               "Reply to continue it, or Cancel.")
            return
        if st.stops == 0:
            st.stops = 1
            logger.info("job %s ended its turn unfinished; nudging once", rec.id[:8])
            self._write(rec, _NUDGE_ASK if getattr(rec, "kind", "") == "specialist"
                        else _NUDGE_NO_ASK)
            return
        self.park(rec, f"«{_title(rec)}» stopped without finishing. "
                       "Reply to continue it, or Cancel.")

    async def _after_turn(self, rec: Any) -> None:
        await self.persist(rec.id)

    async def _continue_after_fault(self, rec: Any) -> None:
        await asyncio.sleep(FAULT_RETRY_S)
        if self._live(rec) and _state(rec.id).faults == 1:
            # The delivery worker resumes the session first if it died.
            self._write(rec, CONTINUE_LINE)

    # -- parking -------------------------------------------------------------

    def park(self, rec: Any, line: str) -> None:
        """The job waits for the operator: ``awaiting = "stopped"`` and one
        line in their direct chat with the job's Cancel button. The operator's
        reply to it continues the job. Nothing here ends the job.

        The line is Casa's own text only (the job's title, as on its start
        line): the job's progress summaries are its words and stay in its
        log."""
        logger.info("job %s parked: %s", rec.id[:8], line)
        st = _state(rec.id)
        st.stops = 0
        mark_awaiting(rec.id, AWAITING_STOPPED)
        self.spawn(self._post_parked(rec, line))

    async def _post_parked(self, rec: Any, text: str) -> None:
        if was_quiet_run(rec):
            # A quiet run that parks needs the operator now: it stops being
            # quiet, so its end is told like any job's.
            try:
                await self.registry.promote_from_quiet(rec.id)
            except Exception:  # noqa: BLE001
                logger.warning("job %s: quiet promotion failed", rec.id[:8],
                               exc_info=True)
        poster = self._delivery_method(rec, "post_job_parked")
        if not callable(poster):
            return
        try:
            await poster(rec, text)
        except Exception:  # noqa: BLE001 — the record still says it waits
            logger.warning("job %s parking line not posted", rec.id[:8],
                           exc_info=True)

    # -- session death -------------------------------------------------------

    async def session_lost(self, rec: Any) -> None:
        """The job's session ended without Casa's teardown. Resume it once,
        by the boot path, with the restart line and every journaled message
        the transcript lacks. A failed resume, or a second death, ends the
        job ``error`` with its last progress."""
        st = _state(rec.id)
        if st.auto_resumed:
            await self._end_error(rec, "its session ended unexpectedly")
            return
        st.auto_resumed = True
        try:
            await self.driver.recover(
                rec, restart_line=SESSION_RESTART_LINE, brief=job_brief(rec),
                redelivery_prefix=SESSION_RESTART_PREFIX)
        except Exception as exc:  # noqa: BLE001 — the job ends below
            logger.warning("job %s could not be resumed after its session "
                           "ended: %s", rec.id[:8], exc)
            await self._end_error(rec, "its session ended unexpectedly")

    async def resume_at_boot(self, rec: Any) -> None:
        """Boot, for one live job. A job waiting for the operator with nothing
        journaled resumes lazily, on their next input. Otherwise: the log
        line, the resume, the journal reconcile, then the brief (when the
        transcript shows it was never read) or the restart line, then every
        journaled message the transcript lacks. One attempt: a failed resume
        ends the job ``error``."""
        import job_inbox
        # The boot walk can reach a job after it ended (a Cancel landed while
        # earlier jobs were being resumed): the live record decides.
        rec = self.registry.get(rec.id) or rec
        if getattr(rec, "status", None) not in ("active", "idle"):
            return
        if awaiting(rec) is not None and not job_inbox.load(rec.id):
            return
        log_job_line(self.agent_logs, rec, "↻ Resuming after a restart")
        try:
            await self.driver.recover(
                rec, restart_line=CASA_RESTART_LINE, brief=job_brief(rec),
                redelivery_prefix=CASA_RESTART_PREFIX)
        except Exception as exc:  # noqa: BLE001 — the job ends below
            logger.warning("job %s could not be resumed after a restart: %s",
                           rec.id[:8], exc)
            await self._end_error(rec, "could not be resumed after a restart")

    async def _end_error(self, rec: Any, reason: str) -> None:
        """End the job ``error`` through the finalize funnel, which adds the
        job's last progress to the text itself."""
        from tools import _finalize_engagement
        latest = self.registry.get(rec.id) or rec
        if latest.status not in ("active", "idle"):
            return
        await _finalize_engagement(
            latest, outcome="error", text=reason,
            next_steps=[], driver=self.driver)
