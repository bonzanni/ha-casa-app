"""Browser executor §5.4–§5.6 (Plan 3 C4, C8, C11): continuing a plugin's long
work in fresh turns, and the driver sweep that finds work nothing is driving.

A browsing run outlives the turn that started it: it waits for the operator's
tap, a live-view login, or simply runs longer than one turn should. The plugin
ends the turn and the run is continued by a FRESH turn of its specialist, bound
to the run's binding (``plugin_bindings``):

* **The queue (C8).** ``CONTINUATIONS.request(binding, run_ref, reason)`` keeps
  at most one pending continuation per (plugin, operator, ``run_ref``). Its
  worker waits for the specialist's desk in the binding's chat (one desk use at
  a time, so it runs after any turn still driving the run), then runs ONE
  delegated turn of the specialist in a fresh session whose task is the fixed
  text ``Continue browsing run <run_ref>.`` under an origin rebuilt from the
  binding: the chain of requesters, the chat and operator, Casa's
  ``synthetic: "continuation"`` marker — so the turn has the operator's grant
  identity but no operator words. Once the worker has started the turn, a new
  request for the same run queues again.
* **The relay (C4).** The continuation's answer goes back up the binding's
  chain: to the resident directly, as a delegation completion it narrates (the
  durable announcement every async delegation has); or, when a specialist
  asked, first to a turn of that specialist with the answer as its task, whose
  own answer goes on up the same way. A relay's task is Casa's text around
  another role's answer and is never banked as the requester's words
  (INV-MEM-024: only a resident's own task on a turn Casa did not compose is).
  A continuation answer that is exactly ``<silent/>`` (an empty one is not) is the specialist's own
  declaration of progress: it climbs no further and is announced to nobody
  (C4a, INV-PLUG-054).
* **The sweep (C11).** Once a minute Casa calls, for every resolved plugin
  that declares ``casa.driverSweep``, that tool directly — Casa starts the
  plugin's stdio server itself, with no model and no session — and queues a
  continuation (reason ``sweep``) for every ``{run_ref, binding_ref}`` it
  returns whose binding belongs to that plugin. The plugin's own predicate
  decides which runs need a driver; the sweep is what makes a lost hand-over
  (a turn cut at its ceiling, a crash, a Casa restart) cost a minute rather
  than the run.

Restart: the queue is memory-only; the sweep re-finds anything it loses."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from output_boundary import SILENCE_SENTINEL

logger = logging.getLogger(__name__)

CONTINUE_TASK = "Continue browsing run {run_ref}."
CONTINUATION_MARKER = "continuation"
SWEEP_INTERVAL_S = 60
SWEEP_CALL_TIMEOUT_S = 30.0
SWEEP_MAX_RESULTS = 50
# A pending continuation that cannot get its desk or a concurrency slot is
# retried at this pace, and dropped after the bound (the sweep re-finds it).
RETRY_EVERY_S = 10.0
GIVE_UP_AFTER_S = 600.0
# How long a cancelled continuation waits for its turn's bounded teardown
# before releasing the desk (the runner's own teardown bound plus its reap).
CANCEL_TEARDOWN_S = 45.0
RELAY_TASK = (
    "{name} has finished part of the work you handed it earlier (its run "
    "{run_ref}). Its answer:\n\n{answer}\n\nUse it to finish your part of that "
    "request, and answer as you would have.")


@dataclasses.dataclass
class Pending:
    binding: Any
    run_ref: str
    reason: str
    queued_at: float


class Continuations:
    def __init__(self) -> None:
        self._pending: dict[tuple, Pending] = {}
        self._tasks: set[asyncio.Task] = set()

    def request(self, binding, run_ref: str, reason: str) -> str:
        """Queue one continuation of *run_ref* for *binding* — ``queued``, or
        ``already_queued`` when one is pending and not yet started."""
        key = (binding.plugin, int(binding.operator_id), run_ref)
        if key in self._pending:
            return "already_queued"
        self._pending[key] = Pending(binding, run_ref, reason, time.monotonic())
        task = asyncio.get_running_loop().create_task(self._run(key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info("continuation queued: run_ref=%s reason=%s", run_ref[:24], reason)
        return "queued"

    def pending_count(self) -> int:
        return len(self._pending)

    async def drain(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(self, key) -> None:
        pending = self._pending.get(key)
        if pending is None:
            return
        def _forget() -> None:
            # only this worker's own entry — a request queued since is the next
            if self._pending.get(key) is pending:
                del self._pending[key]
        try:
            await run_continuation(pending, on_start=_forget)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — logged; the sweep re-finds the run
            logger.exception("continuation of run_ref=%s failed", pending.run_ref[:24])
        finally:
            _forget()


CONTINUATIONS = Continuations()


def continuation_origin(binding, *, executing: str | None = None) -> dict:
    """The parent origin a continuation (or relay) turn of the chain's role
    at ``executing`` runs under — rebuilt from the binding alone."""
    chain = list(binding.chain)
    role = executing or chain[-1]
    upto = chain[:chain.index(role)] if role in chain else chain[:-1]
    stored = binding.origin
    origin = {
        "role": stored.get("role") or chain[0],
        "execution_role": upto[-1] if upto else chain[0],
        "channel": "telegram", "source": "telegram", "message_type": "channel_in",
        "chat_id": binding.chat_id, "user_id": binding.operator_id,
        "user_name": stored.get("user_name"),
        "cid": "cont-" + uuid.uuid4().hex[:12],
        "user_text": "",
        "synthetic": CONTINUATION_MARKER,
        "_origin_route": stored.get("_origin_route", "telegram"),
        "_origin_clearance": stored.get("_origin_clearance", "private"),
        "delegation_depth": max(len(upto) - 1, 0),
        "_handoff_chain": tuple(upto) or (chain[0],),
        "_binding_ref": binding.ref,
    }
    return origin


async def _run_turn(cfg, task: str, origin: dict, *, quota_key: str):
    """One delegated turn of *cfg* under *origin*, through the bounded runner;
    *quota_key* is its send-quota and post-echo owner."""
    import agent as agent_mod
    import tools as tools_mod
    qk = tools_mod._delegation_quota_key.set(quota_key)
    ov = agent_mod.origin_var.set(origin)
    try:
        run = asyncio.create_task(tools_mod._run_delegated_agent_bounded(cfg, task, ""))
    finally:
        agent_mod.origin_var.reset(ov)
        tools_mod._delegation_quota_key.reset(qk)
    return await run


class ContinuationDropped(Exception):
    """A continuation's turn could not get its desk or a slot in time, or the
    desk faulted while it waited; the sweep re-finds the run."""


class RelayInterrupted(RuntimeError):
    """Casa stopped while a finished continuation's answer was still climbing
    to a specialist requester; the resident is told so, durably, instead of
    the climb ending in silence (its notice is Casa's text, never the answer;
    the browser app's run record keeps the run's result)."""


class RequesterUnavailable(RuntimeError):
    """A specialist the answer must climb through is no longer loaded: the
    relay fails explicitly rather than passing one role's answer off as
    another's (its notice is Casa's text, never the answer)."""


@contextlib.asynccontextmanager
async def _desk_turn(chat_id: int, role: str, *, deadline: float):
    """One use of *role*'s desk in *chat_id* with a concurrency permit for
    it: waits for the queue and the lock (bounded by *deadline*), rechecks the
    fault under the lock, takes the permit after the lock as a desk use does."""
    import specialist_desk
    import tools as tools_mod
    desk = specialist_desk.DESKS.get_or_create(int(chat_id), role)
    reservation = None
    while reservation is None:
        if desk.faulted is None:
            reservation = desk.reserve()
        if reservation is None:
            if time.monotonic() > deadline:
                raise ContinuationDropped(f"{role}'s desk stayed unavailable")
            await asyncio.sleep(RETRY_EVERY_S)
    permit = None
    runs: list = []
    try:
        async with desk.use(reservation):
            if desk.faulted is not None:
                raise ContinuationDropped(f"{role}'s desk faulted while it waited")
            limiter = tools_mod._specialist_limiter
            while limiter is not None and permit is None:
                permit = limiter.try_acquire(f"{chat_id}:{role}")
                if permit is None:
                    if time.monotonic() > deadline:
                        raise ContinuationDropped(f"no slot for {role}")
                    await asyncio.sleep(RETRY_EVERY_S)
            # #1197's guard, as every desk use has it: a run the bounded
            # runner gave up waiting for keeps the desk refused until it ends
            sink_tok = tools_mod._desk_run_sink.set(runs)
            try:
                yield desk
            finally:
                tools_mod._desk_run_sink.reset(sink_tok)
                desk.hold_unwinding(runs)
    finally:
        if permit is not None:
            permit.release()


async def _run_turn_cancellable(cfg, task: str, origin: dict, *, quota_key: str):
    """``_run_turn``, cancelled with its caller: the bounded runner's own
    teardown runs before the caller's desk and permit are released."""
    inner = asyncio.ensure_future(_run_turn(cfg, task, origin, quota_key=quota_key))
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError:
        inner.cancel()
        await asyncio.wait({inner}, timeout=CANCEL_TEARDOWN_S)
        raise


async def _relay_up(binding, run_ref: str, output, *, deadline: float):
    """One turn per specialist requester, nearest first, each its own desk
    use; returns the output the resident is told."""
    import tools as tools_mod
    chain = list(binding.chain)
    for i in range(len(chain) - 2, 0, -1):
        below, requester = chain[i + 1], chain[i]
        req_cfg = tools_mod._agent_role_map.get(requester)
        if req_cfg is None:
            raise RequesterUnavailable(
                f"{requester} is not loaded, so the answer of the work it handed on "
                f"(run {run_ref}) could not be passed back to it")
        answer = (output.text if not output.run_aborted
                  else f"(its turn did not complete: {output.run_subtype or 'aborted'})")
        async with _desk_turn(binding.chat_id, requester, deadline=deadline):
            output = await _run_turn_cancellable(
                req_cfg, RELAY_TASK.format(name=tools_mod._display_name_for_role(below),
                                           run_ref=run_ref, answer=answer),
                continuation_origin(binding, executing=requester),
                quota_key="relay-" + uuid.uuid4().hex)
    return output


async def run_continuation(pending: Pending, *, on_start) -> None:
    """The continuation turn under its role's desk, registered as one async
    delegation of the chain's first specialist whose announced answer is the
    end of the relay — durable, replayed after a restart like any
    delegation's. Started only for a binding of the operator as configured
    now."""
    import tools as tools_mod
    from operator_calls import is_configured_operator
    from specialist_registry import DelegationRecord

    binding = pending.binding
    if not is_configured_operator(binding.chat_id, binding.operator_id):
        logger.warning("continuation: binding is not the configured operator's — dropped")
        return
    role = binding.chain[-1]
    cfg = tools_mod._agent_role_map.get(role)
    if cfg is None:
        logger.warning("continuation: role %s is not loaded — dropped", role)
        return
    deadline = time.monotonic() + GIVE_UP_AFTER_S
    first = binding.chain[1] if len(binding.chain) > 1 else role
    fido_done = asyncio.Event()
    progress_only: list = []
    try:
        async with _desk_turn(binding.chat_id, role, deadline=deadline):
            on_start()
            record = DelegationRecord(
                id=str(uuid.uuid4()), agent=first, started_at=time.time(),
                origin={"role": binding.chain[0], "channel": "telegram",
                        "chat_id": binding.chat_id, "user_id": binding.operator_id,
                        "cid": "cont-" + uuid.uuid4().hex[:12], "user_text": "",
                        "_origin_route": binding.origin.get("_origin_route", "telegram"),
                        "_origin_clearance": binding.origin.get("_origin_clearance",
                                                                "private")})
            await tools_mod._specialist_registry.register_delegation(record)

            async def _chain():
                try:
                    out = await _run_turn(cfg, CONTINUE_TASK.format(run_ref=pending.run_ref),
                                          continuation_origin(binding), quota_key=record.id)
                finally:
                    fido_done.set()
                if not out.run_aborted and (out.text or "").strip() == SILENCE_SENTINEL:
                    progress_only.append(out)
                    return out       # progress only (C4a): nothing climbs the chain
                try:
                    return await _relay_up(binding, pending.run_ref, out,
                                           deadline=time.monotonic() + GIVE_UP_AFTER_S)
                except asyncio.CancelledError:
                    if len(binding.chain) <= 2:
                        raise
                    raise RelayInterrupted(
                        f"Casa stopped before the answer of run {pending.run_ref} was "
                        f"passed back to {binding.chain[-2]}; ask it to check the "
                        "run again") from None

            task = asyncio.create_task(_chain())
            # quiet only when THIS turn declared progress — never inferred
            # from a relay requester's own answer
            tools_mod._attach_completion_callback(
                task, record, quiet_if=lambda out: any(o is out for o in progress_only))
            waiter = asyncio.ensure_future(fido_done.wait())
            try:
                # the desk is held for the continuation turn only; the relay
                # turns take their own requesters' desks
                await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.wait({task}, timeout=CANCEL_TEARDOWN_S)
                raise
            finally:
                waiter.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.wait({task}, timeout=CANCEL_TEARDOWN_S)
            raise
        except Exception:  # noqa: BLE001 — the completion callback announces it
            pass
    except ContinuationDropped as exc:
        logger.warning("continuation of run_ref=%s dropped: %s", pending.run_ref[:24], exc)


# ---------------------------------------------------------------------------
# The driver sweep (C11)
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def plugin_data_dir(manifest_name: str, *, environ=os.environ) -> str:
    """The directory the CLI gives a locally loaded plugin as
    ``CLAUDE_PLUGIN_DATA``: ``<config dir>/plugins/data/<id>-inline``, ``<id>``
    the plugin name with every character but letters, digits, ``_`` and ``-``
    turned into ``-`` (measured on the CLI the SDK spawns; re-measured per CLI
    pin, see the plan). The sweep's direct call must see the same directory as
    the plugin's sessions."""
    base = environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        environ.get("HOME") or os.path.expanduser("~"), ".claude")
    pid = re.sub(r"[^A-Za-z0-9_-]", "-", manifest_name)
    return os.path.join(base, "plugins", "data", f"{pid}-inline")


def _expand(value: str, env: dict) -> str:
    return _VAR_RE.sub(lambda m: env.get(m.group(1), m.group(2) or ""), value)


def sweep_server(rp) -> "dict | None":
    """The plugin's one stdio server, expanded the way the CLI would, or None."""
    from plugin_store import mcp_servers_map
    servers = mcp_servers_map(Path(rp.path) / ".mcp.json")
    stdio = [cfg for cfg in servers.values() if isinstance(cfg, dict) and cfg.get("command")]
    if len(stdio) != 1:
        return None
    cfg = stdio[0]
    manifest = rp.manifest if isinstance(getattr(rp, "manifest", None), dict) else {}
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(rp.path)
    env["CLAUDE_PLUGIN_DATA"] = plugin_data_dir(str(manifest.get("name") or rp.name))
    server_env = {k: _expand(str(v), env) for k, v in (cfg.get("env") or {}).items()}
    return {"command": _expand(str(cfg["command"]), env),
            "args": [_expand(str(a), env) for a in (cfg.get("args") or [])],
            "env": {**env, **server_env}, "cwd": str(rp.path)}


async def call_sweep_tool(server: dict, tool: str) -> list[dict]:
    """Start the server, call *tool* with no arguments, read its JSON text."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=server["command"], args=server["args"],
                                   env=server["env"], cwd=server["cwd"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            if tool not in {t.name for t in listed.tools}:
                raise LookupError("the declared sweep tool is not served")
            result = await session.call_tool(tool, {})
    text = "".join(getattr(c, "text", "") for c in (result.content or []))
    data = json.loads(text) if text else {}
    runs = data.get("runs") if isinstance(data, dict) else None
    return [r for r in (runs or [])[:SWEEP_MAX_RESULTS] if isinstance(r, dict)]


async def sweep_once() -> int:
    """One pass over every loaded specialist's resolved plugins; returns how
    many continuations it queued."""
    import tools as tools_mod
    from operator_calls import is_configured_operator
    from plugin_bindings import BINDINGS
    from plugin_grants import withhold_env_unresolved
    from plugin_store import manifest_driver_sweep
    from text_util import sanitize_segment

    queued = 0
    seen: set[str] = set()
    for role, cfg in list((tools_mod._agent_role_map or {}).items()):
        if getattr(cfg, "kind", "") != "specialist":
            continue
        try:
            resolution = await asyncio.to_thread(tools_mod._delegated_resolution, cfg)
            resolution, _ = withhold_env_unresolved(resolution, context="driver sweep")
        except Exception:  # noqa: BLE001
            logger.warning("driver sweep: could not resolve %s's plugins", role,
                           exc_info=True)
            continue
        for rp in resolution.plugins:
            manifest = rp.manifest if isinstance(getattr(rp, "manifest", None), dict) else {}
            try:
                tool = manifest_driver_sweep(manifest)
            except Exception:  # noqa: BLE001 — an invalid declaration sweeps nothing
                tool = None
            if not tool or rp.artifact_id in seen:
                continue
            seen.add(rp.artifact_id)
            server = sweep_server(rp)
            if server is None:
                continue
            from plugin_registry import runtime_name
            seg = sanitize_segment(runtime_name(rp))
            try:
                runs = await asyncio.wait_for(call_sweep_tool(server, tool),
                                              SWEEP_CALL_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                logger.warning("driver sweep of %s failed (%s)", seg, type(exc).__name__)
                continue
            for r in runs:
                run_ref, ref = r.get("run_ref"), r.get("binding_ref")
                if not isinstance(run_ref, str) or not 0 < len(run_ref) <= 64:
                    continue
                binding = BINDINGS.peek(ref)
                if binding is None or binding.plugin != seg:
                    continue
                if not is_configured_operator(binding.chat_id, binding.operator_id):
                    continue
                if CONTINUATIONS.request(binding, run_ref, "sweep") == "queued":
                    queued += 1
    return queued


async def sweep_job() -> None:
    try:
        n = await sweep_once()
        if n:
            logger.info("driver sweep queued %d continuation(s)", n)
    except Exception:  # noqa: BLE001 — a failed pass waits for the next
        logger.warning("driver sweep pass failed", exc_info=True)


def register_sweep(scheduler) -> None:
    scheduler.add_job(sweep_job, trigger="interval", id="plugin_driver_sweep",
                      seconds=SWEEP_INTERVAL_S, replace_existing=True, coalesce=True,
                      max_instances=1, misfire_grace_time=SWEEP_INTERVAL_S)
