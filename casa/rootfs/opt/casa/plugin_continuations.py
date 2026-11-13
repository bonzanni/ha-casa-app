"""Browser executor §5.4–§5.6 (Plan 3 C4, C8, C11): continuing a plugin's long
work in later turns, and the driver sweep that finds work nothing is driving.

A browsing run outlives the turn that started it: it waits for the operator's
tap, a live-view login, or simply runs longer than one turn should. The plugin
ends the turn and the run is continued by a new turn of its specialist, bound
to the run's binding (``plugin_bindings``):

* **The queue (C8).** ``CONTINUATIONS.request(binding, run_ref, reason)`` keeps
  at most one pending continuation per (plugin, bot, operator, ``run_ref``):
  the bot is the binding's (``plugin_bindings.binding_target``: its delivery's,
  or its route's for work that posts nothing), since one plugin can serve
  agents on two bots and its run refs may be equal on both. Its worker waits
  for the specialist's lane (``agent_lane``: one use at a time per executing
  agent, so it runs after any turn still driving the run, and never while the
  agent is faulted), then runs ONE delegated turn of the specialist, resuming
  the binding's conversation (``_conversation_for``), whose task is the fixed
  text ``Continue browsing run <run_ref>.`` under an origin rebuilt from the
  binding: the chain of requesters, the chat and operator, the route, Casa's
  ``synthetic: "continuation"`` marker — so the turn has the operator's grant
  identity but no operator words — and the binding's delivery, so the turn
  posts exactly where the work it continues posted, or nowhere. Once the
  worker has started the turn, a new request for the same run queues again.
  Only this turn resumes: relay turns, and any turn nested in it, stay fresh.
* **The relay (C4).** The continuation's answer goes back up the binding's
  chain to its **requester**, the chain's root (:func:`continuation_requester`):
  the resident, when its delegation started the run, or the executing agent's
  own conversation, when its direct turn did — as a delegation completion it
  narrates (the durable announcement every async delegation has); when a
  specialist asked on the way, first to a turn of that specialist with the
  answer as its task, whose own answer goes on up the same way. A requester
  other than the executing agent's own conversation leaves that conversation
  one note (``chat_notes``) of what the continuation answered. A relay's task
  is Casa's text around another role's answer and is never captured as the
  requester's words
  (INV-MEM-032: only a resident's own task on a turn Casa did not compose is).
  A continuation answer that is exactly ``<silent/>`` (an empty one is not) is the specialist's own
  declaration of progress: it climbs no further and is announced to nobody
  (C4a, INV-PLUG-054). A specialist requester that closes its relay turn the
  same way declares the same: more work is coming back to it, so no requester
  above it is told. A resumed turn that raises is quiet the same way: the sweep
  carries the run on (operator ruling 2026-11-06).
* **The sweep (C11).** Once a minute Casa calls, for every resolved plugin
  that declares ``casa.driverSweep``, that tool directly — Casa starts the
  plugin's stdio server itself, with no model and no session — and queues a
  continuation (reason ``sweep``) for every ``{run_ref, binding_ref}`` it
  returns whose binding belongs to that plugin. The plugin's own predicate
  decides which runs need a driver; the sweep is what makes a lost hand-over
  (a turn cut at its ceiling, a crash, a Casa restart) cost a minute rather
  than the run. An entry may name the deposits whose delivery the plugin has
  not confirmed yet (``unless_delivered``): Casa delivered them, so it skips
  the run when each reached the operator. An answer may ask for one more pass
  soon (``sweep_again_s``, slice 5k: the operator's Done in a live view reaches
  no one, so the plugin looks again every few seconds while a view is open).

Restart: the queue is memory-only; the sweep re-finds anything it loses."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from output_boundary import SILENCE_SENTINEL
from provenance import carry_route, carry_task_proof, task_delivery_marker

logger = logging.getLogger(__name__)

CONTINUE_TASK = "Continue browsing run {run_ref}."
CONTINUATION_MARKER = "continuation"
SWEEP_INTERVAL_S = 60
SWEEP_CALL_TIMEOUT_S = 30.0
SWEEP_MAX_RESULTS = 50
# Slice 5k: an extra pass a plugin asks for comes at the earliest this soon; a request of a
# minute or more is the minute pass itself. At most this many deposits name one entry's hold.
SWEEP_AGAIN_MIN_S = 5.0
UNLESS_DELIVERED_MAX = 4
# A pending continuation that cannot get its lane or a concurrency slot is
# retried at this pace, and dropped after the bound (the sweep re-finds it).
RETRY_EVERY_S = 10.0
GIVE_UP_AFTER_S = 600.0
# How long a cancelled continuation waits for its turn's bounded teardown
# before releasing the lane (the runner's own teardown bound plus its reap).
CANCEL_TEARDOWN_S = 45.0
# Raised resumed turns in a row after which the next starts fresh.
MAX_RESUME_FAILURES = 2
RELAY_TASK = (
    "{name} has finished part of the work you handed it earlier (its run "
    "{run_ref}). Its answer:\n\n{answer}\n\nUse it to finish your part of that "
    "request, and answer as you would have. If you have just started more work "
    "whose end will come back to you, close with exactly <silent/> instead; "
    "otherwise answer.")


@dataclasses.dataclass
class Pending:
    binding: Any
    run_ref: str
    reason: str
    queued_at: float
    bot_id: int                 # the binding's bot (plugin_bindings.binding_target) when queued
    dropped: bool = False       # its work was stopped before it started (``drop``)
    # the card the operator approved the binding's newest request on, read when queued:
    # the same sweep pass may forget it (``plugin_stop.settle_ended``) before the turn starts
    card: Any = None


class Continuations:
    def __init__(self) -> None:
        self._pending: dict[tuple, Pending] = {}
        self._tasks: set[asyncio.Task] = set()

    def request(self, binding, run_ref: str, reason: str) -> str:
        """Queue one continuation of *run_ref* for *binding* — ``queued``, or
        ``already_queued`` when one is pending and not yet started. Raises
        ``plugin_bindings.ChannelUnavailable``, queueing nothing, when the
        binding's bot cannot be named now."""
        from plugin_bindings import binding_target
        _ep, bot_id = binding_target(binding)
        key = (binding.plugin, bot_id, int(binding.operator_id), run_ref)
        if key in self._pending:
            return "already_queued"
        import run_admission
        self._pending[key] = Pending(
            binding, run_ref, reason, time.monotonic(), bot_id=bot_id,
            card=run_admission.CONFIRMATIONS.card_for_binding(binding.ref))
        task = asyncio.get_running_loop().create_task(self._run(key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info("continuation queued: run_ref=%s reason=%s", run_ref[:24], reason)
        return "queued"

    def pending_count(self) -> int:
        return len(self._pending)

    def pending_on_bot(self, bot_id: int) -> int:
        """How many continuations are queued on bot *bot_id* and not yet started."""
        return sum(1 for p in self._pending.values() if p.bot_id == int(bot_id))

    def drop(self, plugin: str, bot_id: int, operator_id: int, refs) -> int:
        """The operator stopped, on bot *bot_id*, the work these references name
        (``plugin_stop``): every pending continuation of one of them on that bot that has
        not started is dropped — its worker returns without a turn once it holds the lane.
        A started one is the running session, left to end itself. Returns how many were
        dropped."""
        wanted = {r for r in refs if isinstance(r, str)}
        n = 0
        for key, pending in list(self._pending.items()):
            if (key[0] == plugin and key[1] == int(bot_id) and key[2] == int(operator_id)
                    and key[3] in wanted):
                pending.dropped = True
                del self._pending[key]
                n += 1
        if n:
            logger.info("continuations dropped: %d", n)
        return n

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


def continuation_requester(binding):
    """Who receives a continuation's result (``task_model.Requester``): the
    root of the binding's chain — the resident, when its delegation started
    the run; the executing agent's own conversation, when its direct turn did
    — on the route of the turn that asked."""
    from task_model import Requester
    return Requester(role=binding.chain[0], route=carry_route(binding.origin))


def _delivery_for_turn(binding, executing: "str | None"):
    """The delivery of a turn rebuilt from *binding*: the binding's own for the
    turn of the agent that runs the work; nothing for a relay turn of an
    agent that asked on the way, whose posts the binding does not know."""
    from plugin_bindings import delivery_of
    if executing is not None and executing != binding.chain[-1]:
        return None
    return delivery_of(binding)


def continuation_origin(binding, *, executing: str | None = None) -> dict:
    """The parent origin a continuation (or relay) turn of the chain's role
    at ``executing`` runs under — rebuilt from the binding alone, with the
    work's delivery (:func:`_delivery_for_turn`)."""
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
        **carry_route(stored),
        **carry_task_proof(stored),
        **task_delivery_marker(_delivery_for_turn(binding, executing)),
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
    """A continuation's turn could not get its agent's lane or a slot in time,
    or the agent faulted while it waited; the sweep re-finds the run."""


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
async def _lane_turn(chat_id: int, role: str, *, deadline: float):
    """One use of *role*'s lane (``agent_lane``) with a concurrency permit
    for it in *chat_id*: waits for the queue and the lock (bounded by
    *deadline*), rechecks the fault under the lock, takes the permit after the
    lock as every use of the lane does. A lane still busy at *deadline* drops
    the turn, and the place it held in the queue is given back."""
    import agent_lane
    import tools as tools_mod
    lane = agent_lane.LANES.get(role)
    reservation = None
    while reservation is None:
        if lane.faulted is None:
            reservation = lane.reserve()
        if reservation is None:
            if time.monotonic() > deadline:
                raise ContinuationDropped(f"{role}'s lane stayed unavailable")
            await asyncio.sleep(RETRY_EVERY_S)
    permit = None
    runs: list = []
    try:
        async with lane.use(reservation, deadline) as admitted:
            if not admitted:
                raise ContinuationDropped(f"{role}'s lane stayed busy")
            if lane.faulted is not None:
                raise ContinuationDropped(f"{role} faulted while it waited")
            limiter = tools_mod._specialist_limiter
            while limiter is not None and permit is None:
                permit = limiter.try_acquire(f"{chat_id}:{role}")
                if permit is None:
                    if time.monotonic() > deadline:
                        raise ContinuationDropped(f"no slot for {role}")
                    await asyncio.sleep(RETRY_EVERY_S)
            # #1197's guard, as every use of the lane has it: a run the bounded
            # runner gave up waiting for keeps the lane refused until it ends
            sink_tok = tools_mod._lane_run_sink.set(runs)
            try:
                yield lane
            finally:
                tools_mod._lane_run_sink.reset(sink_tok)
                lane.hold_unwinding(runs)
    finally:
        reservation.release()
        if permit is not None:
            permit.release()


async def _run_turn_cancellable(cfg, task: str, origin: dict, *, quota_key: str):
    """``_run_turn``, cancelled with its caller: the bounded runner's own
    teardown runs before the caller's lane and permit are released."""
    inner = asyncio.ensure_future(_run_turn(cfg, task, origin, quota_key=quota_key))
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError:
        inner.cancel()
        await asyncio.wait({inner}, timeout=CANCEL_TEARDOWN_S)
        raise


async def _relay_up(binding, run_ref: str, output, *, deadline: float,
                    progress_only: list | None = None):
    """One turn per specialist requester, nearest first, each its own use of
    that requester's lane; returns the output the requester is told.

    A requester whose completed turn closes with exactly ``<silent/>`` declared
    progress only: its output is recorded in *progress_only* (when given) and
    returned at once, so no requester above it gets a turn."""
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
        async with _lane_turn(binding.chat_id, requester, deadline=deadline):
            output = await _run_turn_cancellable(
                req_cfg, RELAY_TASK.format(name=tools_mod._display_name_for_role(below),
                                           run_ref=run_ref, answer=answer),
                continuation_origin(binding, executing=requester),
                quota_key="relay-" + uuid.uuid4().hex)
        if (not output.run_aborted
                and tools_mod._closing_text(output).strip() == SILENCE_SENTINEL):
            if progress_only is not None:
                progress_only.append(output)
            return output
    return output


async def _conversation_for(binding, cfg) -> "str | None":
    """The session this continuation resumes, or None for a fresh one: the binding keeps
    none, two resumed turns in a row raised, or its transcript is gone (retention, a lost
    disk), read with the SDK's own lookup in the folder the turn launches in."""
    import claude_agent_sdk as sdk
    import tools as tools_mod
    sid = binding.session_id
    if not sid or binding.resume_failures >= MAX_RESUME_FAILURES:
        return None
    found = await asyncio.to_thread(sdk.get_session_info, sid,
                                    directory=tools_mod.specialist_cwd(cfg))
    return sid if found is not None else None


def _knowledge_line(requester: str, task: str, out=None, *, failed: "str | None" = None
                    ) -> str:
    """The body-free note an agent's conversation gets of a continuation it
    did not start: who asked, what, and how it ended."""
    import tools as tools_mod
    brief = task if len(task) <= 80 else task[:79] + "…"
    who = tools_mod._display_name_for_role(requester)
    if failed is not None:
        return f"{who} asked you: {brief} — you couldn't finish it ({failed})"
    if getattr(out, "run_aborted", False):
        return (f"{who} asked you: {brief} — you couldn't finish it "
                f"({out.run_subtype or 'aborted'})")
    return f"{who} asked you: {brief} — you answered"


def _note_knowledge(binding, requester, role: str, line: str) -> None:
    """*line* for *role*'s own conversation, when someone else asked: in the
    chat the work posts in. Work that posts nothing has no chat to note in."""
    if requester.role == role:
        return
    import chat_notes
    from plugin_bindings import delivery_of
    delivery = delivery_of(binding)
    if delivery is not None:
        chat_notes.record(delivery.bot_id, delivery.chat_id, line)


async def run_continuation(pending: Pending, *, on_start) -> None:
    """The continuation turn under its role's lane, registered as one async
    delegation of the chain's first specialist whose announced answer, the
    end of the relay, goes to the requester (:func:`continuation_requester`)
    — durable, replayed after a restart like any delegation's. Started only
    for a binding of the operator as configured now."""
    import tools as tools_mod
    from operator_calls import is_configured_operator
    from plugin_bindings import BINDINGS, delivery_of
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
    if getattr(cfg, "kind", "") != "specialist":
        logger.warning("continuation: role %s is not a specialist — dropped", role)
        return
    deadline = time.monotonic() + GIVE_UP_AFTER_S
    first = binding.chain[1] if len(binding.chain) > 1 else role
    fido_done = asyncio.Event()
    progress_only: list = []
    requester = continuation_requester(binding)
    task_text = CONTINUE_TASK.format(run_ref=pending.run_ref)
    try:
        async with _lane_turn(binding.chat_id, role, deadline=deadline):
            if pending.dropped:
                return          # its work was stopped while it waited: no turn
            on_start()
            # the completion is announced to the requester, on its route: the
            # route of the turn that asked (``continuation_requester``)
            record = DelegationRecord(
                id=str(uuid.uuid4()), agent=first, started_at=time.time(),
                origin={"role": requester.role, "channel": "telegram",
                        "chat_id": binding.chat_id, "user_id": binding.operator_id,
                        "cid": "cont-" + uuid.uuid4().hex[:12], "user_text": "",
                        "_origin_route": binding.origin.get("_origin_route", "telegram"),
                        "_origin_clearance": binding.origin.get("_origin_clearance",
                                                                "private"),
                        **carry_route(binding.origin),
                        **task_delivery_marker(delivery_of(binding)),
                        # the requester's narration replies to the card the operator approved
                        # (sent as a reply only in that card's own chat)
                        **({"_reply_to": pending.card.to_json()} if pending.card else {})},
                # the continuation as a task: run by the chain's last agent,
                # its result to the requester, its posts at the binding's
                # delivery — persisted, so a restart replays it whole
                executor=role, requester=requester, delivery=delivery_of(binding))
            await tools_mod._specialist_registry.register_delegation(record)
            # In its executor's own chat the continuation is a task like any
            # other: its post index and its input destination open before its
            # turn runs, so a reply to any of its posts — queued while the
            # session starts — reaches it, never the conversation. Its run is
            # this record's (the turn's ``_delegation_id``); the input ends
            # with the work, however it ends.
            birth = tools_mod.TaskBirth(task_id=record.id, brief=task_text, executor=role,
                                        requester=requester, delivery=record.delivery)
            input_open = birth.in_own_chat
            if input_open:
                tools_mod._open_task(birth)

            async def _chain():
                resume, tok = None, None
                try:
                    resume = await _conversation_for(binding, cfg)
                    # around this turn only: relays and the requesters' turns stay fresh
                    tok = tools_mod._conversation.set(
                        tools_mod.ConversationTurn(binding.ref, resume))
                    out = await _run_turn(cfg, task_text,
                                          continuation_origin(binding), quota_key=record.id)
                except Exception as exc:  # noqa: BLE001 — CancelledError is not one
                    if resume is None:
                        from error_kinds import _classify_error
                        _note_knowledge(binding, requester, role, _knowledge_line(
                            requester.role, task_text, failed=_classify_error(exc).value))
                        raise       # a fresh turn that raises is announced as always
                    BINDINGS.turn_ended(binding.ref, raised=True)
                    logger.warning("continuation of run_ref=%s: its resumed turn raised (%s); "
                                   "the sweep carries it on", pending.run_ref[:24],
                                   type(exc).__name__)
                    out = tools_mod.DelegatedOutput(text=SILENCE_SENTINEL)
                    progress_only.append(out)
                    return out
                else:
                    if resume is not None:
                        BINDINGS.turn_ended(binding.ref, raised=False)
                finally:
                    if tok is not None:
                        tools_mod._conversation.reset(tok)
                    fido_done.set()
                # the message the turn closed with, whole, is exactly the sentinel
                if (not out.run_aborted
                        and tools_mod._closing_text(out).strip() == SILENCE_SENTINEL):
                    progress_only.append(out)
                    return out       # progress only (C4a): nothing climbs the chain
                _note_knowledge(binding, requester, role,
                                _knowledge_line(requester.role, task_text, out))
                try:
                    return await _relay_up(binding, pending.run_ref, out,
                                           deadline=time.monotonic() + GIVE_UP_AFTER_S,
                                           progress_only=progress_only)
                except asyncio.CancelledError:
                    if len(binding.chain) <= 2:
                        raise
                    raise RelayInterrupted(
                        f"Casa stopped before the answer of run {pending.run_ref} was "
                        f"passed back to {binding.chain[-2]}; ask it to check the "
                        "run again") from None

            task = asyncio.create_task(_chain())
            if input_open:
                task.add_done_callback(
                    functools.partial(tools_mod._end_task_input, record.id))
            # quiet only when a turn of this chain declared progress: the
            # continuation's own, or a relay requester's
            tools_mod._attach_completion_callback(
                task, record, quiet_if=lambda out: any(o is out for o in progress_only))
            waiter = asyncio.ensure_future(fido_done.wait())
            try:
                # the lane is held for the continuation turn only; the relay
                # turns take their own requesters' lanes
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


async def call_plugin_tool(server: dict, tool: str, args: dict, *,
                           handed: "asyncio.Future | None" = None) -> str:
    """Start the server, call *tool* with *args*, return the result's text. With *handed*,
    the text is also set on that future from inside the client session, so a caller waiting
    on it never waits for the stdio teardown, which goes on in this coroutine."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=server["command"], args=server["args"],
                                   env=server["env"], cwd=server["cwd"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            if tool not in {t.name for t in listed.tools}:
                raise LookupError("the declared tool is not served")
            result = await session.call_tool(tool, args)
            text = "".join(getattr(c, "text", "") for c in (result.content or []))
            if handed is not None and not handed.done():
                handed.set_result(text)
    return text


async def list_plugin_tools(server: dict, *, timeout_s: float = 20.0) -> list[dict]:
    """Start the server, ask it which tools it serves, and stop it: ``[{name, description,
    inputSchema}]``. It runs ``initialize`` and ``list_tools`` and nothing else, never a tool,
    inside one overall budget of *timeout_s* (``TimeoutError`` when it is spent). The budget
    bounds the listing, not the return: once it fires, the MCP stdio client still shuts the
    server down (it closes stdin and waits about two seconds, then terminates the process and
    waits about two more before it kills it), so the call returns up to about four seconds
    after *timeout_s*."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=server["command"], args=server["args"],
                                   env=server["env"], cwd=server["cwd"])
    async with asyncio.timeout(timeout_s):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
    return [{"name": t.name, "description": getattr(t, "description", None),
             "inputSchema": getattr(t, "inputSchema", None)} for t in listed.tools]


async def call_sweep_tool(server: dict, tool: str
                          ) -> tuple[list[dict], float | None, list[dict]]:
    """Start the server, call *tool* with no arguments, read its JSON text: the entries, the
    extra pass it asks for (seconds, or None), and the requests it names as ended on their own
    (``ended``)."""
    text = await call_plugin_tool(server, tool, {})
    data = json.loads(text) if text else {}
    runs = data.get("runs") if isinstance(data, dict) else None
    again = data.get("sweep_again_s") if isinstance(data, dict) else None
    ended = data.get("ended") if isinstance(data, dict) else None
    if (not isinstance(again, int) or isinstance(again, bool)
            or not 0 < again < SWEEP_INTERVAL_S):
        again = None
    entries = runs if isinstance(runs, list) else []
    ended = ended if isinstance(ended, list) else []
    return ([r for r in entries[:SWEEP_MAX_RESULTS] if isinstance(r, dict)],
            None if again is None else max(float(again), SWEEP_AGAIN_MIN_S),
            [e for e in ended[:SWEEP_MAX_RESULTS] if isinstance(e, dict)])


def _held_by_delivery(refs, *, plugin: str, operator_id: int, artifact_id: str) -> bool:
    """Slice 5k: True when *refs* names 1..UNLESS_DELIVERED_MAX deposits and Casa delivered
    every one of them, for this plugin and operator, from this artifact. Anything else — one
    pending, withheld or forgotten (a Casa restart), a malformed field — holds nothing."""
    import result_broker
    if not isinstance(refs, list) or not 0 < len(refs) <= UNLESS_DELIVERED_MAX:
        return False
    return all(result_broker.is_reference(ref) and result_broker.DELIVERY_OUTCOMES.status(
        ref, plugin=plugin, operator_id=operator_id, artifact_id=artifact_id
    ).get("status") == "delivered" for ref in refs)


async def sweep_once() -> int:
    """One pass over every loaded specialist's resolved plugins; returns how
    many continuations it queued."""
    queued, _again = await _sweep_pass()
    return queued


async def declaring_plugins(declared, *, context: str) -> list[tuple]:
    """``(role, cfg, rp, seg, tool)`` for every loaded specialist's resolved plugin whose
    manifest declares a tool by *declared* (``plugin_store.manifest_driver_sweep`` or
    ``manifest_stop_tool``) — the sessions' own resolution, with unresolved env withheld —
    deduplicated by artifact id. A role whose plugins cannot be resolved, or an invalid
    declaration, contributes nothing."""
    import tools as tools_mod
    from plugin_grants import withhold_env_unresolved
    from plugin_registry import runtime_name
    from text_util import sanitize_segment

    out: list[tuple] = []
    seen: set[str] = set()
    for role, cfg in list((tools_mod._agent_role_map or {}).items()):
        if getattr(cfg, "kind", "") != "specialist":
            continue
        try:
            resolution = await asyncio.to_thread(tools_mod._delegated_resolution, cfg)
            resolution, _ = withhold_env_unresolved(resolution, context=context)
        except Exception:  # noqa: BLE001
            logger.warning("%s: could not resolve %s's plugins", context, role,
                           exc_info=True)
            continue
        for rp in resolution.plugins:
            manifest = rp.manifest if isinstance(getattr(rp, "manifest", None), dict) else {}
            try:
                tool = declared(manifest)
            except Exception:  # noqa: BLE001 — an invalid declaration declares nothing
                tool = None
            if not tool or rp.artifact_id in seen:
                continue
            seen.add(rp.artifact_id)
            out.append((role, cfg, rp, sanitize_segment(runtime_name(rp)), tool))
    return out


async def _sweep_pass() -> tuple[int, float | None]:
    """One pass: the continuations it queued, and the soonest extra pass a plugin asked for."""
    from operator_calls import is_configured_operator
    from plugin_bindings import BINDINGS, ChannelUnavailable
    from plugin_store import manifest_driver_sweep

    queued = 0
    again: float | None = None
    for _role, _cfg, rp, seg, tool in await declaring_plugins(manifest_driver_sweep,
                                                              context="driver sweep"):
        server = sweep_server(rp)
        if server is None:
            continue
        try:
            runs, soon, ended = await asyncio.wait_for(call_sweep_tool(server, tool),
                                                       SWEEP_CALL_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            logger.warning("driver sweep of %s failed (%s)", seg, type(exc).__name__)
            continue
        if soon is not None:
            again = soon if again is None else min(again, soon)
        for r in runs:
            run_ref, ref = r.get("run_ref"), r.get("binding_ref")
            if not isinstance(run_ref, str) or not 0 < len(run_ref) <= 64:
                continue
            binding = BINDINGS.peek(ref)
            if binding is None or binding.plugin != seg:
                continue
            if not is_configured_operator(binding.chat_id, binding.operator_id):
                continue
            if "unless_delivered" in r and _held_by_delivery(
                    r["unless_delivered"], plugin=seg, operator_id=binding.operator_id,
                    artifact_id=str(getattr(rp, "artifact_id", "") or "")):
                continue        # the operator has the card or link it waits on
            try:
                if CONTINUATIONS.request(binding, run_ref, "sweep") == "queued":
                    queued += 1
            except ChannelUnavailable:
                continue        # its bot has no id yet: a later pass queues it
        await _settle_ended(seg, ended)
    return queued, again


async def _settle_ended(seg: str, ended: list[dict]) -> None:
    """The requests the plugin names as ended on their own: each one's start or status card,
    for a binding of this plugin and the operator as configured now, is marked ended and
    loses its Stop (``plugin_stop.settle_ended``), once."""
    import plugin_stop
    from operator_calls import is_configured_operator
    from plugin_bindings import BINDINGS, ChannelUnavailable, binding_target

    for e in ended:
        ref = e.get("ref")
        if (not isinstance(ref, str) or not ref.startswith(plugin_stop.REF_PREFIX)
                or len(ref) > plugin_stop.REF_MAX):
            continue
        binding = BINDINGS.peek(e.get("binding_ref"))
        if binding is None or binding.plugin != seg:
            continue
        if not is_configured_operator(binding.chat_id, binding.operator_id):
            continue
        try:
            _ep, bot_id = binding_target(binding)
        except ChannelUnavailable:
            continue            # its bot has no id yet: the next pass names it again
        try:
            await plugin_stop.settle_ended(seg, bot_id, int(binding.operator_id), ref)
        except Exception as exc:  # noqa: BLE001 — the next pass names it again
            logger.warning("driver sweep: marking an ended request failed (%s)",
                           type(exc).__name__)


# One pass at a time (the minute pass and an extra pass); at most one extra pass pending;
# none at all once Casa is stopping (a minute pass still in flight then arms nothing).
_passing = False
_stopping = False
_extra: dict[str, Any] = {"handle": None, "when": None, "loop": None}
_extra_tasks: set[asyncio.Task] = set()


def _arm_extra_pass(delay: float) -> None:
    """Arm one extra pass *delay* seconds from now, unless one is already armed sooner."""
    if _stopping:
        return
    loop = asyncio.get_running_loop()
    when = loop.time() + delay
    handle = _extra["handle"]
    if handle is not None and _extra["loop"] is loop and not handle.cancelled():
        if _extra["when"] <= when:
            return
        handle.cancel()
    _extra.update(handle=loop.call_at(when, _start_extra_pass), when=when, loop=loop)


def _start_extra_pass() -> None:
    _extra.update(handle=None, when=None, loop=None)
    task = asyncio.get_running_loop().create_task(sweep_job())
    _extra_tasks.add(task)
    task.add_done_callback(_extra_tasks.discard)


def stop_sweep() -> None:
    """Casa's stop: no pass starts or arms another from now on, and the armed extra pass and
    one already running are cancelled."""
    global _stopping
    _stopping = True
    cancel_extra_pass()


def cancel_extra_pass() -> None:
    """The armed extra pass, and one already running, are cancelled."""
    handle = _extra["handle"]
    if handle is not None:
        handle.cancel()
    _extra.update(handle=None, when=None, loop=None)
    for task in list(_extra_tasks):
        task.cancel()


async def sweep_job() -> None:
    """One pass, unless one is running (that one arms any extra pass still asked for)."""
    global _passing
    if _passing or _stopping:
        return
    _passing = True
    again = None
    try:
        n, again = await _sweep_pass()
        if n:
            logger.info("driver sweep queued %d continuation(s)", n)
    except Exception:  # noqa: BLE001 — a failed pass waits for the next
        logger.warning("driver sweep pass failed", exc_info=True)
    finally:
        _passing = False
    if again is not None:
        _arm_extra_pass(again)


def register_sweep(scheduler) -> None:
    scheduler.add_job(sweep_job, trigger="interval", id="plugin_driver_sweep",
                      seconds=SWEEP_INTERVAL_S, replace_existing=True, coalesce=True,
                      max_instances=1, misfire_grace_time=SWEEP_INTERVAL_S)
