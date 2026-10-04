"""S5 §5 — the pinned one-call turn's controller: the one build input a tap
captured under the desk lock, the watch the hooks resolve with the executed
call's own result, and (``PinnedRun``) the owner of the run — the pin's
state, the capture, the SDK client and the processes the run started.

Leaf module: stdlib only; ``stored_calls`` for the canonical form; the SDK
imported lazily where the client is entered and where the reaper set lives.

Why a controller of its own (design §5.2.4, rounds 6–13): the delegated
runner owns its client through ``async with ClaudeSDKClient`` and the
bounded wrapper releases after the SDK's teardown bound with the inner task
possibly alive. A stored call's turn must release its desk only when the
CLI and the plugin servers it started are confirmed gone — so this
controller depends on NOTHING inside the SDK's teardown: it pins pidfds at
start, signals them directly, confirms exit by pidfd readability under one
deadline, seals its own hook callbacks and drains them, and abandons the
SDK client to a detached, never-awaited close.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import select
import signal
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class BuildInput:
    """What a tap's re-check judged and the pinned builder consumes UNCHANGED
    (design §4.2.3): the role config, the resolution AFTER the session
    builder's own env withholding, the withheld list, the protected map, the
    contract map and the profile plan — captured ONCE, under the desk lock,
    in the builder's own order (plan §1.5)."""
    cfg: Any
    resolution: Any
    withheld: tuple
    protected: dict
    contract_map: Any
    plan: Any
    target: str


@dataclasses.dataclass(frozen=True)
class Capture:
    """How the watched call ended (design §5.4), as the hooks resolve it at
    their END with their OWN effective result:

    - ``receipt``   — a ``safe`` tool's receipt: its response's ``receipt``
      sentence when it carries one, else the response text (#1200);
    - ``delivered`` — the ``More`` exception's proposal landed (the hook's
      delivery receipt): the landed proposal IS the operator-visible receipt;
    - ``withheld``  — the ``More`` result was refused or its post not proven
      (``text`` = the reason);
    - ``no_post``   — the ``More`` tool's contract no-post shape: its own
      ``receipt`` sentence, else its own text, is the receipt;
    - ``error``     — the failure hook: the error's CLASS only;
    - ``no_call``   — the turn ended with no executed call;
    - ``timed_out`` — the ceiling, with termination confirmed and nothing
      captured.

    ``rewritten`` is the trust-and-tell flag (§14.7): the CLI's reported
    post-hook input differed from the stored canonical."""
    kind: str
    text: str = ""
    rewritten: bool = False


# ---------------------------------------------------------------------------
# processes: /proc and pidfds
# ---------------------------------------------------------------------------

def _ppid(pid: int) -> int | None:
    """The parent pid from ``/proc/<pid>/stat`` (the comm may hold spaces and
    parentheses: split after the LAST ``)``). None when unreadable."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        rest = raw.rpartition(b")")[2].split()
        return int(rest[1])              # state, ppid, ...
    except (IndexError, ValueError):
        return None


def _descendants(pid: int) -> list[int]:
    """Every live descendant of *pid*, read from ``/proc`` by parent pid
    (transitively), in BFS order."""
    children: dict[int, list[int]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        child = int(name)
        parent = _ppid(child)
        if parent is not None:
            children.setdefault(parent, []).append(child)
    out: list[int] = []
    queue = [pid]
    while queue:
        cur = queue.pop(0)
        for child in sorted(children.get(cur, [])):
            if child not in out:
                out.append(child)
                queue.append(child)
    return out


# pidfd_open(2) / pidfd_send_signal(2): ``os``/``signal`` expose them when the
# interpreter was built against a libc that declares them; a standalone build
# (the dev venv's) does not, so the raw syscalls are the fallback. The numbers
# are asm-generic — identical on x86_64 and aarch64, Casa's two images.
_SYS_PIDFD_SEND_SIGNAL = 424
_SYS_PIDFD_OPEN = 434


def _libc_syscall(*args: int) -> int:
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    res = libc.syscall(*[ctypes.c_long(a) for a in args])
    if res < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(res)


def _pidfd_open(pid: int) -> int:
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    return _libc_syscall(_SYS_PIDFD_OPEN, pid, 0)


def _pidfd_send_signal(fd: int, sig: int) -> None:
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, sig)
        return
    _libc_syscall(_SYS_PIDFD_SEND_SIGNAL, fd, int(sig), 0, 0)


def _send_signal(fd: int, sig: int) -> None:
    """Signal through the pinned fd — never a numeric pid (a reused pid is
    not this process)."""
    try:
        _pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass                              # already gone


def _active_children() -> set:
    """The SDK transport's module-level reaper set (its atexit handler
    SIGTERMs every member by its live numeric pid)."""
    try:
        from claude_agent_sdk._internal.transport import subprocess_cli
        return subprocess_cli._ACTIVE_CHILDREN
    except Exception:  # noqa: BLE001 — a differently laid out SDK: nothing to discard from
        return set()


class _Pinned:
    """One pinned process: its pid at pin time, the pidfd kept open for the
    run's life, (the CLI only) the SDK's process object, and whether its
    descent from the CLI was PROVEN while pinned — an unprovable one is
    awaited but never signalled (it may be a reused pid), and it keeps the
    run from ever being confirmed."""

    def __init__(self, pid: int, fd: int, proc: Any = None, ours: bool = True) -> None:
        self.pid, self.fd, self.proc, self.ours = pid, fd, proc, ours

    def exited(self) -> bool:
        """A pidfd becomes readable when its process has exited (reaped or
        not) — a zero-time poll is the final readiness sweep."""
        poller = select.poll()
        poller.register(self.fd, select.POLLIN)
        return bool(poller.poll(0))

    def signal(self, sig: int) -> None:
        _send_signal(self.fd, sig)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class ProcessTree:
    """The processes one delegated run's CLI started, owned by Casa (#1212):
    the CLI pinned by pidfd the moment its session exists, its descendants
    pinned with the parent chain validated while pinned, re-walked when a
    termination begins, signalled through the pinned fds and confirmed gone
    by pidfd readability — never through the SDK's own teardown. ``PinnedRun``
    (a stored-call tap) is one; the bounded runner (a desk reply, a
    delegation) owns a bare one for each run."""

    GRACE_S = 5.0            # (b) SIGTERM → SIGKILL
    EXIT_WAIT_S = 10.0       # (d)+(g) one deadline: pidfd exits and the callback drain

    def __init__(self, *, run_id: str) -> None:
        self.run_id = run_id
        self._cli: _Pinned | None = None
        self._children: list[_Pinned] = []
        self._unconfirmed_identity = False
        self._client: Any = None
        # "none" → "entering" (the client is being entered: a CLI may already
        # be running) → "entered" | "failed"; while "entering", nothing is
        # pinned yet and a termination must find the process itself
        self._entry = "none"
        self._closing = False       # a bare tree: Casa began ending the run (§3.3)
        self.reaping = False        # a bare tree: its reap owns closing the fds

    def _close_started(self) -> bool:
        """Whether Casa has begun ending the run — a pinned server found dead
        after that is explained, not an anomaly (``_repin``)."""
        return self._closing

    # -- a bare tree's client (#1212) ------------------------------------------
    def attach(self, client: Any) -> None:
        """The client is about to be entered: a termination from here on can
        late-pin the CLI from its transport's process."""
        self._client = client
        self._entry = "entering"

    def pin_client(self) -> None:
        """The client's session exists: pin its CLI and descendants."""
        self._pin_tree(*self._process_of(self._client))
        self._entry = "entered"

    @staticmethod
    def _process_of(client: Any) -> tuple:
        proc = getattr(getattr(client, "_transport", None), "_process", None)
        return getattr(proc, "pid", None), proc

    def _late_pin(self) -> None:
        """A termination or finish reached before the entry completed: pin
        whatever process the transport already holds; an entry still pending
        with NO process to pin cannot be confirmed (the CLI may be starting);
        an entry that FAILED before any process started has nothing to
        confirm."""
        if self._cli is not None or self._entry not in ("entering", "cancelled", "failed"):
            return
        pid, proc = self._process_of(self._client)
        if pid is not None:
            self._pin_tree(pid, proc)
        elif self._entry != "failed":
            self._unconfirmed_identity = True

    def _pin_tree(self, pid: Any, proc: Any) -> None:
        if not isinstance(pid, int) or pid <= 0:
            self._unconfirmed_identity = True        # no process to pin: not established
            return
        try:
            fd = _pidfd_open(pid)
        except ProcessLookupError:
            return                                  # extinct already: nothing runs
        except OSError:
            self._unconfirmed_identity = True
            return
        self._cli = _Pinned(pid, fd, proc)
        self._pin_descendants(pid)

    def _pin_descendants(self, root: int) -> None:
        """Pin every live descendant of *root* not pinned yet, with a pidfd
        each and the parent chain validated while pinned. Called once at
        enter for the CLI, and again at terminate for every pinned process
        still alive (#1205: a child a tool call starts AFTER enter — a plugin
        server shelling out — was never pinned, so it survived the kill,
        reparented to init, and was not counted)."""
        try:
            descendants = _descendants(root)
        except OSError:
            self._unconfirmed_identity = True
            return
        known = self.pinned_pids()
        roots = {p.pid for p in self._all() if p.ours}   # only a PROVEN ancestor vouches
        for child in descendants:
            if child in known:
                continue
            try:
                cfd = _pidfd_open(child)
            except ProcessLookupError:
                continue                            # extinct
            except OSError:
                self._unconfirmed_identity = True
                continue
            # validate the parent chain WHILE pinned: an extinct child is
            # dropped; a proven descendant is ours to signal; a chain that
            # cannot be proven (the CLI left and the child was reparented, or
            # the pid was reused since the listing) is KEPT and awaited but
            # never signalled, and it keeps the run from being confirmed
            # (Astra, diff round 1: dropping it released a live server)
            verdict = self._chain_verdict(child, roots, cfd)
            if verdict == "extinct":
                os.close(cfd)
                continue
            self._children.append(_Pinned(child, cfd, ours=(verdict == "ours")))
            known.add(child)
            if verdict == "ours":
                roots.add(child)
            else:
                self._unconfirmed_identity = True

    def _repin(self) -> None:
        """#1205: re-walk the descendants of every pinned process that is
        still running and pin what was started since enter — twice during a
        termination: when it begins (before the execution task is cancelled,
        since a server may exit during that wait and reparent its child) and
        again just before the signals (a child started during the wait). A
        worker that detached itself (setsid, a double fork) is not a
        descendant and stays out of scope (design §14.8).

        An INCOMPLETE scan is not a confirmation (diff round 2, Astra and
        Terra; coordinator's ruling under §14.8 "bounded best effort, told"):
        a PROVEN pinned process other than the CLI that the run itself never
        signalled and that is found exited — before the walk began, or while
        /proc was being enumerated — may have left a child reparented before
        any walk could see it, so the run is marked unconfirmed and the desk
        is faulted and told rather than released over a silent survivor. Only
        the walks set this, and they run before any signal; a server that
        exits because Casa signalled the CLI never reaches here. (An
        unprovable pin already leaves the run unconfirmed, so the flag looks
        at proven ones only.)"""
        cli = self._cli
        proven = [p for p in self._all() if p.ours and p is not cli]
        for pinned in list(self._all()):
            if not pinned.exited():
                self._pin_descendants(pinned.pid)
        # ONE check, after the enumeration, over EVERY proven pin (diff round 3,
        # Astra: a death between two separate polls escaped a before/after
        # comparison) — and only for a death Casa did not cause: once Casa has
        # started the SDK's close (the normal end's teardown, which stops the
        # servers) or signalled, a dead server is explained, not an anomaly
        if not self._close_started() and any(p.exited() for p in proven):
            logger.warning("pinned run %s: a pinned process exited before Casa signalled or "
                           "closed anything; the scan cannot be complete", self.run_id)
            self._unconfirmed_identity = True

    def _chain_verdict(self, child: int, roots: "set[int] | int", fd: int) -> str:
        """``ours`` / ``extinct`` / ``unprovable``. Only the child's OWN pidfd
        proves an exit: a ``/proc`` read that fails (the entry gone, EMFILE, a
        permission error) says nothing about the process, so a child that is
        unreadable but not exited is kept, unprovable (Astra, diff round 2)."""
        probe = _Pinned(child, fd)

        def _unprovable() -> str:
            # the child's OWN fd decides extinction, read at the moment of the
            # verdict: a child that is dead — before the walk, or having left
            # between a poll and a /proc read — is extinct whatever /proc says
            # of its ancestry; only a LIVE child is unprovable (Astra, diff
            # rounds 2–4)
            return "extinct" if probe.exited() else "unprovable"

        if isinstance(roots, int):
            roots = {roots}
        parent = _ppid(child)
        if parent is None:
            return _unprovable()                    # unreadable (EMFILE, a permission error) — or gone
        seen = set()
        cur = child
        while cur not in seen and cur > 1:
            seen.add(cur)
            parent = _ppid(cur)
            if parent is None:
                return _unprovable()                # an ancestor vanished mid-walk
            if parent in roots:
                return "ours"                       # descends from a pinned process
            cur = parent
        return _unprovable()

    def _all(self) -> list[_Pinned]:
        return ([self._cli] if self._cli is not None else []) + list(self._children)

    def pinned_pids(self) -> set[int]:
        return {p.pid for p in self._all()}

    def pinned_fds(self) -> list[int]:
        return [p.fd for p in self._all()]

    def alive(self) -> bool:
        """Whether any pinned process is still running (pidfd not readable)."""
        return any(not p.exited() for p in self._all())

    async def _wait_exit(self, pinned: _Pinned, deadline: float) -> bool:
        loop = asyncio.get_running_loop()
        if pinned.exited():
            return True
        fut: asyncio.Future = loop.create_future()
        loop.add_reader(pinned.fd, lambda: fut.done() or fut.set_result(True))
        try:
            await asyncio.wait({fut}, timeout=max(0.0, deadline - loop.time()))
        finally:
            loop.remove_reader(pinned.fd)
        return pinned.exited()                      # the final zero-time sweep

    async def _signal(self) -> None:
        """(b) the CLI: SIGTERM, a grace, SIGKILL — through the pinned fd;
        (c) every retained PROVEN descendant: SIGKILL (an unprovable one is
        only awaited — it may not be ours)."""
        loop = asyncio.get_running_loop()
        cli = self._cli
        if cli is not None and not cli.exited():
            cli.signal(signal.SIGTERM)
            await self._wait_exit(cli, loop.time() + self.GRACE_S)
            if not cli.exited():
                cli.signal(signal.SIGKILL)
        for child in self._children:
            if child.ours and not child.exited():
                child.signal(signal.SIGKILL)

    async def _confirm_exits(self, deadline: float) -> bool:
        """(d): every pinned fd readable by *deadline*."""
        confirmed = True
        for pinned in self._all():
            if not await self._wait_exit(pinned, deadline):
                confirmed = False
        return confirmed

    def _discard_reaper(self) -> None:
        """(f): a confirmed-dead CLI leaves the SDK's atexit reaper set, which
        would otherwise signal its numeric pid — perhaps reused — at exit."""
        cli = self._cli
        if cli is not None and cli.exited() and cli.proc is not None:
            _active_children().discard(cli.proc)          # idempotent

    def begin_termination(self) -> None:
        """#1212 §3.3 step 1, a bare tree's: re-walk while the CLI still lives
        (before the run is cancelled, so the SDK's close cannot reparent a
        child first), then mark the run as being ended by Casa. A run
        cancelled while its client is still being entered has pinned nothing
        yet: its CLI is late-pinned here, before the SDK's cleanup can clear
        the transport (Astra, diff round 2)."""
        self._late_pin()
        self._repin()
        self._closing = True

    async def reap(self, role: str) -> bool:
        """#1212 §3.3 steps 3–6, after the run's own teardown: the late pin,
        the re-walk, the signals and the confirmation under one deadline; the
        fds are closed whatever happens. False is logged at ERROR, naming the
        role and the pids still pinned."""
        loop = asyncio.get_running_loop()
        try:
            self._closing = True
            self._late_pin()
            self._repin()
            await self._signal()
            confirmed = await self._confirm_exits(loop.time() + self.EXIT_WAIT_S)
            self._discard_reaper()
            ok = confirmed and not self._unconfirmed_identity
            if not ok:
                logger.error("delegated run %s (role %s): its processes could not be "
                             "confirmed gone (processes_exited=%s identity_ok=%s pids=%s)",
                             self.run_id, role, confirmed, not self._unconfirmed_identity,
                             sorted(p.pid for p in self._all() if not p.exited()))
            return ok
        except Exception as exc:  # noqa: BLE001 — a detached task: say so, never vanish
            logger.error("delegated run %s (role %s): ending its processes failed: %s",
                         self.run_id, role, type(exc).__name__)
            return False
        finally:
            self.close_fds()

    def close_fds(self) -> None:
        for pinned in self._all():
            pinned.close()


class PinnedRun(ProcessTree):
    """One tap's run (design §5.2.4). ``handle_tap`` creates it under the desk
    lock, sets it on ``tools._pinned_run`` around the runner's task, and the
    builder, the hooks and the runner read it from there.

    The watch is one-shot: the first ``resolve`` wins, later ones are
    ignored — the ceiling never resolves it (a receipt landing during the
    hold is still the receipt, §5.2.5); ``settle`` reads it after the run
    ended or its termination was confirmed."""

    TASK_WAIT_S = 5.0        # (a) the execution task's cancellation wait
    LATE_WAIT_S = 3600.0     # a detached waiter's period between its "still alive" reports
    GRACE_S = 5.0            # (b) SIGTERM → SIGKILL
    EXIT_WAIT_S = 10.0       # (d)+(g) one deadline: pidfd exits and the callback drain

    def __init__(self, *, run_id: str, runtime_name: str, canonical: str, label: str,
                 build_input: BuildInput) -> None:
        super().__init__(run_id=run_id)
        self.runtime_name = runtime_name
        self.canonical = canonical
        self.label = label
        self.build_input = build_input
        self.fired = False          # the pin allowed the one call
        self.rewritten = False      # §14.7: set by the capture at the hook's entry
        self.sealed = False         # §5.2.4 (g): the irreversible entry cutoff
        self.entered = 0            # S5 callbacks inside (incremented only unsealed)
        self._drain: asyncio.Future | None = None
        self._capture: Capture | None = None
        self.close_task: asyncio.Task | None = None
        self.transcript: tuple[str, str] | None = None   # (session id, cwd) the runner records

    def _close_started(self) -> bool:
        # the SDK's close (the normal end's teardown, which stops the servers)
        # started by Casa
        return self.close_task is not None

    # -- the watch -----------------------------------------------------------
    def resolve(self, capture: Capture) -> None:
        if self._capture is None:
            self._capture = capture

    @property
    def captured(self) -> Capture | None:
        return self._capture

    def settle(self, default_kind: str, text: str = "") -> Capture:
        """The capture, or the default outcome when nothing was captured."""
        return self._capture if self._capture is not None else Capture(default_kind, text)

    # -- the pin (§5.3) --------------------------------------------------------
    def pin(self, tool_name: Any, tool_input: Any) -> str | None:
        """The deny reason, or None when this is THE call: the stored tool,
        the stored arguments in canonical JSON, not yet allowed. The
        comparison never normalises (§2.6 refused at deposit what the CLI
        would change)."""
        from stored_calls import canonical_json
        if self.sealed:
            return "the run is sealed"
        if tool_name != self.runtime_name:
            return "not the stored call"
        try:
            reported = canonical_json(tool_input)
        except (TypeError, ValueError):
            return "not the stored arguments"
        if reported != self.canonical:
            return "not the stored arguments"
        if self.fired:
            return "the stored call already ran"
        self.fired = True
        return None

    async def pin_hook(self, input_data: Any, tool_use_id: Any, context: Any) -> dict:
        """The ``HookMatcher(matcher=None)`` callback: for every NON-plugin tool
        a deny in the CLI's shape (built-ins, ToolSearch, Skill, the messaging
        and desk tools — none of which has a Casa hook with an operator-visible
        side effect). A plugin tool is NOT judged here: ``matcher=None`` matches
        it too and the CLI runs both matchers concurrently, so judging it in
        both would consume the one-shot pin in whichever ran first and the
        plugin admission hook — where the one call is really judged, as its
        first check — would then deny the stored call as already run (Terra,
        diff round 1). Guarded: sealed ⇒ no effect."""
        async def _body(input_data, tool_use_id, context):
            name = (input_data or {}).get("tool_name")
            from result_broker import PLUGIN_TOOL_PREFIX
            if isinstance(name, str) and name.startswith(PLUGIN_TOOL_PREFIX):
                return {}                       # the admission hook's pin owns plugin tools
            why = self.pin(name, (input_data or {}).get("tool_input") or {})
            if why is None:
                return {}
            from hooks import _deny
            return _deny(why)
        return await self.guard(_body)(input_data, tool_use_id, context)

    # -- the guard (§5.2.4 (g)) ------------------------------------------------
    def guard(self, body: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap an S5 callback: the seal is read FIRST and a sealed callback
        returns ``{}`` with no effect; otherwise the counter is incremented
        with no await between the read and the increment, the body runs, and
        the counter is decremented synchronously in ``finally``."""
        async def _guarded(input_data, tool_use_id, context):
            if self.sealed:
                return {}
            self.entered += 1
            try:
                return await body(input_data, tool_use_id, context)
            finally:
                self.entered -= 1
                if self.entered == 0 and self._drain is not None and not self._drain.done():
                    self._drain.set_result(True)
        return _guarded

    # -- ownership (§5.2.4) ------------------------------------------------------
    async def enter(self, client_options: Any, *, client_factory: Callable[..., Any] | None = None) -> Any:
        """Create and ENTER the SDK client, pin the CLI's pidfd from the
        transport's process the moment the session is started, snapshot its
        descendants with a pidfd each (parent chain validated while pinned),
        and hand the entered client over. The execution task never enters,
        exits or disconnects it; this controller owns it to the end."""
        if client_factory is None:
            from claude_agent_sdk import ClaudeSDKClient
            client_factory = ClaudeSDKClient
        client = client_factory(client_options)
        # ownership BEFORE the entry is awaited: a ceiling that fires while
        # the CLI's initialisation hangs must still find the client to close
        # and the process to pin (Astra, diff round 1)
        self._client = client
        self._entry = "entering"
        try:
            await client.__aenter__()
        except asyncio.CancelledError:
            self._entry = "cancelled"               # unresolved: a CLI may be starting
            raise
        except BaseException:
            self._entry = "failed"                  # the SDK raised before a session existed
            raise
        self._pin_tree(*self._process_of(client))
        self._entry = "entered"
        return client

    async def _drain_callbacks(self, deadline: float) -> bool:
        if self.entered == 0:
            return True
        loop = asyncio.get_running_loop()
        self._drain = loop.create_future()
        await asyncio.wait({self._drain}, timeout=max(0.0, deadline - loop.time()))
        return self.entered == 0

    async def terminate(self, task: "asyncio.Task | None" = None) -> bool:
        """§5.2.4 (a)–(g). Returns whether EVERY pinned process is confirmed
        exited and every S5 callback has left — False is the faulted desk."""
        loop = asyncio.get_running_loop()
        # #1205 (Astra, diff round 1): walk the tree BEFORE the cancellation
        # wait too — a server can exit while (a) waits, reparenting a child
        # it started, which the later walk would then not even find
        self._repin()
        # (a) the execution task: cancelled, waited for at most TASK_WAIT_S, then abandoned
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=self.TASK_WAIT_S)
            if not task.done():
                task.add_done_callback(_consume)
        self._late_pin()
        self._repin()                                # #1205: children started since enter, or during (a)
        # (b) the CLI: SIGTERM, a grace, SIGKILL; (c) every proven descendant
        await self._signal()
        # (g)(1) the entry cutoff, right after the kill and before any wait
        self.sealed = True
        # (e) the SDK client is abandoned to a detached, never-awaited close
        self._start_close()
        # (d) + (g)(2): one deadline for every pidfd exit and the callback drain
        ok = await self._confirm(loop.time() + self.EXIT_WAIT_S)
        if not ok:
            logger.error("pinned run %s: termination unconfirmed at the deadline",
                         self.run_id)
        return ok

    async def finish(self) -> bool:
        """The run's NORMAL end (the execution task returned): the SDK
        client's orderly close as a detached task — never awaited, nothing
        inside the SDK's teardown is waited on —, every pinned fd's exit
        confirmed by readability under EXIT_WAIT_S, then the seal and the
        callback drain under the same deadline, and the reaper discard. False
        means a pinned process is still alive or a callback still inside:
        the caller takes the termination path."""
        loop = asyncio.get_running_loop()
        self._late_pin()
        self._start_close()
        return await self._confirm(loop.time() + self.EXIT_WAIT_S)

    def abandon(self) -> None:
        """A cancelled desk use: the detached close is started and the fds
        are closed when it ends; nothing is waited for."""
        self._start_close()
        if self.close_task is not None:
            self.close_task.add_done_callback(lambda _t: self.close_fds())
        else:
            self.close_fds()

    def _start_close(self) -> None:
        if self._client is not None and self.close_task is None:
            self.close_task = asyncio.create_task(self._client.__aexit__(None, None, None))
            self.close_task.add_done_callback(_consume)

    async def _confirm(self, deadline: float) -> bool:
        """(d)+(g): every pinned fd readable and the callbacks drained under
        ONE deadline; (f) the confirmed-dead CLI leaves the reaper set."""
        confirmed = await self._confirm_exits(deadline)
        self.sealed = True
        drained = await self._drain_callbacks(deadline)
        self._discard_reaper()
        ok = confirmed and drained and not self._unconfirmed_identity
        if not ok:
            logger.warning("pinned run %s: not confirmed (processes_exited=%s "
                           "callbacks_drained=%s identity_ok=%s)", self.run_id, confirmed,
                           drained, not self._unconfirmed_identity)
        return ok

    async def _wait_process_object(self, deadline: float) -> bool:
        """The unpinned writer's exit, read from the transport's process
        object (``returncode`` set) by polling until *deadline*; True at once
        when the entry never produced a process (nothing can write)."""
        _pid, proc = self._process_of(self._client)
        if proc is None:
            return True
        loop = asyncio.get_running_loop()
        while getattr(proc, "returncode", None) is None:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(min(0.1, max(0.0, deadline - loop.time())))
        return True

    def schedule_after_exit(self, coro_factory: Callable[[], Any]) -> "asyncio.Task":
        """An unconfirmed run's deferred cleanup (the transcript delete: its
        writer may still flush, Astra diff round 3): a DETACHED task — never
        awaited by the desk, whose bounded hold is over — waits for every
        pinned fd to report exit (capped at LATE_WAIT_S), runs *coro_factory*
        once, then closes the fds; the fds stay owned by the waiter until then."""
        async def _later() -> None:
            loop = asyncio.get_running_loop()
            try:
                # the period expiring is NOT exit evidence (Astra, diff round
                # 5: a delete before the exit lets a later flush recreate the
                # transcript for good) — keep the obligation, say so at every
                # period, and run the cleanup only once the exit is confirmed
                while True:
                    deadline = loop.time() + self.LATE_WAIT_S
                    exited = True
                    for pinned in self._all():
                        if not await self._wait_exit(pinned, deadline):
                            exited = False
                    if not self._all():
                        # nothing could be pinned (pidfd_open failed) yet the
                        # client holds a process that may still write: its exit
                        # is read from the retained process object (diff round 4)
                        exited = await self._wait_process_object(deadline)
                    if exited:
                        break
                    logger.error("pinned run %s: a pinned process is still alive after %.0f s; "
                                 "its deferred cleanup keeps waiting", self.run_id, self.LATE_WAIT_S)
                await coro_factory()
            finally:
                self.close_fds()
        task = asyncio.create_task(_later())
        _LATE_TASKS.add(task)
        task.add_done_callback(_LATE_TASKS.discard)
        task.add_done_callback(_consume)
        return task


_LATE_TASKS: set = set()


def _consume(task: "asyncio.Task") -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        pass
