"""The scratchpad's ``run`` operation: a model's script, run once over its pad's sections in
a process that can reach nothing but its own input and output, and its result written back
to the pad all at once.

The box is ``/opt/casa/run/child.py`` under the AppArmor child profile ``pyrun``, entered by
``setpriv`` as one of two slot uids (``docs/architecture/script-runner.md``). This module is
Casa's side: the snapshot of the pad with each section's header line kept aside, the spawn
with an empty environment and descriptors 0, 1, 2 only, the bounded readers, the wall clock,
the kill of the slot's uid after every run, the reading of what came back, the pad's apply
under its stamp, and the reply. ``boot`` runs a preflight through the whole box and makes
``run`` unavailable, with one ERROR and one operator notice, when it fails; a run whose box
fails later (no ready frame, whatever the exit status) does the same.

Every refusal the box relies on is checked by the prelude on every run; nothing here trusts
the preflight later. The pad is identified by the key the tool was built with and nothing
the box receives names a pad.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import uid_kill
from scratchpad import (PADS, PAGE_NOTE, READ_CHARS, READ_CHARS_MAX, READ_CHARS_MIN,
                        SECTION_MARK, _clean, _named, _Refused, _sections, _shape, _split)

logger = logging.getLogger(__name__)

SLOT_UIDS = (199000, 199001)
INTERPRETER = "/opt/casa/run/python3"
PRELUDE = "/opt/casa/run/child.py"
SETPRIV = "/usr/bin/setpriv"
WALL_S = 30.0
MiB = 1024 * 1024
STDOUT_CAP = 8 * MiB          # the result's bound is 6.23 MiB for a full pad and the printed cap
STDERR_KEPT = 4096
STDERR_SHOWN = 2_000
FRAME = b'{"box": "ready"}'
LABEL_PATHS = ("/proc/self/attr/apparmor/current", "/proc/self/attr/current")
CHILD_PROFILE = "pyrun"
PARENT_DEATH = 104            # the prelude's code for a parent that is not Casa: Casa is gone

UNAVAILABLE = "run is unavailable on this install; Casa's log says why."
NOT_READY = "run is not ready yet: Casa is still starting."
BUSY_PAD = "A run is already going on this pad."
BUSY_SLOTS = "Two runs are already going on; try again in a moment."
UNREADABLE = "The result could not be read."

SPAWN: Callable[..., Awaitable[Any]] = asyncio.create_subprocess_exec
LADDER: Callable[..., Awaitable[uid_kill.QuiesceResult]] = uid_kill.kill_uid_until_empty


def own_label() -> str:
    """Casa's own confinement label, as the kernel reports it."""
    last: OSError | None = None
    for path in LABEL_PATHS:
        try:
            with open(path, "rb") as fh:
                return fh.read().rstrip(b"\0\n").decode("ascii", "replace")
        except OSError as exc:
            last = exc
    raise OSError(f"cannot read Casa's confinement label ({last.strerror if last else '?'})")


def machine() -> str:
    return os.uname().machine


def expected_label(own: str) -> str:
    """The child label derived from Casa's own: ``<name> (enforce)`` becomes
    ``<name>//pyrun (enforce)``; anything else means the box cannot be enforced."""
    suffix = " (enforce)"
    if not own.endswith(suffix) or not own[:-len(suffix)] or "//" in own:
        raise ValueError(f"Casa's own label is {own!r}, not '<name> (enforce)'")
    return f"{own[:-len(suffix)]}//{CHILD_PROFILE}{suffix}"


@dataclass
class _State:
    ready: bool = False               # boot() returned
    available: bool = False
    reason: str = ""
    label: str = ""
    slots: dict[int, str] = field(default_factory=lambda: {uid: "free" for uid in SLOT_UIDS})
    in_flight: set[str] = field(default_factory=set)
    notify: Callable[[str], Awaitable[Any]] | None = None
    notified: bool = False


_state = _State()


def reset() -> None:
    """A fresh state, for a test."""
    global _state
    _state = _State()


async def _unavailable(reason: str, fix: str) -> None:
    """``run`` answers unavailable from now on; one ERROR, and one operator notice per process."""
    _state.available = False
    _state.reason = reason
    logger.error("scratchpad run unavailable: %s (%s)", reason, fix)
    if _state.notified or _state.notify is None:
        return
    _state.notified = True
    try:
        await _state.notify(f"The scratchpad's run operation is unavailable: {fix}. "
                            "Casa's log has the detail.")
    except Exception:  # noqa: BLE001 — a notice fault never stops a boot or a run
        logger.warning("run unavailability notice not sent", exc_info=True)


# --- the box's outcome ---------------------------------------------------------------------


@dataclass
class Outcome:
    status: int                     # waitpid's: negative for a signal
    frame: bool                     # stdout's first line was the ready frame
    stdout: bytes                   # at most STDOUT_CAP; the rest discarded
    overflow: bool
    stderr: str                     # the last STDERR_KEPT bytes
    seconds: float
    wall_killed: bool
    spawn_error: str = ""           # the spawn itself failed: no process ran


async def _read_stdout(stream) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    overflow = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return b"".join(chunks), overflow
        if size + len(chunk) > STDOUT_CAP:
            overflow = True        # keep draining so the box is never blocked on its pipe
            chunk = chunk[:STDOUT_CAP - size]
        chunks.append(chunk)
        size += len(chunk)


async def _read_stderr(stream) -> str:
    tail = b""
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return tail.decode("utf-8", "replace")
        tail = (tail + chunk)[-STDERR_KEPT:]


async def _feed(stdin, payload: bytes) -> None:
    try:
        stdin.write(payload)
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass                        # the box refused before reading: its exit says why
    finally:
        try:
            stdin.close()
        except OSError:
            pass


async def execute(payload: bytes, uid: int, label: str) -> Outcome:
    """One run of the box over *payload* as *uid*, with the readers, the wall clock and the
    kill of its uid; never raises."""
    argv = [SETPRIV, "--reuid", str(uid), "--regid", str(uid), "--clear-groups",
            "--bounding-set", "-all", "--inh-caps", "-all", "--",
            INTERPRETER, "-I", "-S", "-B", PRELUDE, str(os.getpid()), label, str(uid)]
    started = time.monotonic()
    try:
        proc = await SPAWN(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                           stderr=asyncio.subprocess.PIPE, env={}, close_fds=True, cwd="/",
                           start_new_session=True)
    except OSError as exc:
        return Outcome(-1, False, b"", False, "", time.monotonic() - started, False,
                       spawn_error=f"{type(exc).__name__}: {exc}")
    readers = asyncio.gather(_read_stdout(proc.stdout), _read_stderr(proc.stderr))
    feeder = asyncio.ensure_future(_feed(proc.stdin, payload))
    wall_killed = False
    try:
        await asyncio.wait_for(proc.wait(), WALL_S)
    except asyncio.TimeoutError:
        wall_killed = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
    seconds = time.monotonic() - started
    try:
        (stdout, overflow), stderr = await asyncio.wait_for(readers, 5.0)
    except asyncio.TimeoutError:
        readers.cancel()
        stdout, overflow, stderr = b"", True, ""
    feeder.cancel()
    try:
        await feeder
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — the feeder's fate is irrelevant
        pass
    frame = stdout.split(b"\n", 1)[0] == FRAME
    return Outcome(proc.returncode, frame, stdout, overflow, stderr, seconds, wall_killed)


def _well_formed(doc: Any) -> bool:
    """Casa's own check of the result's shape, whatever the child said: a dict of exactly
    ``printed`` (str), ``printed_total`` (int) and ``pad`` (null, or a list of [str, str])."""
    if type(doc) is not dict or set(doc) != {"printed", "printed_total", "pad"}:
        return False
    if type(doc["printed"]) is not str or type(doc["printed_total"]) is not int:
        return False
    pad = doc["pad"]
    if pad is None:
        return True
    return type(pad) is list and all(
        type(pair) is list and len(pair) == 2 and type(pair[0]) is str and type(pair[1]) is str
        for pair in pad)


def _result(outcome: Outcome) -> tuple[dict | None, int]:
    """The parsed result document after the frame, or None, and the stray bytes a script
    wrote to the real stdout between the frame and the result line (the box writes a newline
    of its own before the result, which is not counted)."""
    if not outcome.frame or outcome.overflow:
        return None, 0
    rest = outcome.stdout[len(FRAME) + 1:]
    if not rest.endswith(b"\n"):
        return None, len(rest)
    cut = rest.rfind(b"\n", 0, len(rest) - 1) + 1
    try:
        doc = json.loads(rest[cut:])
    except ValueError:
        return None, len(rest)
    return (doc if _well_formed(doc) else None), max(cut - 1, 0)


# --- the pad's sections, there and back ----------------------------------------------------


def snapshot_sections(lines: list[str]) -> list[tuple[str, str | None, str]]:
    """``(name, header line or None, text)`` for each section of *lines* as ``_sections``
    cuts them, the top first and always present; the text is the section's lines each ended
    by a newline. A name two sections share refuses, as every operation on it does."""
    found = _sections(lines)
    counts = collections.Counter(s.name for s in found if s.header >= 0)
    for name, count in counts.items():
        if count > 1:
            _named(lines, name)         # raises with the operations' own wording
    out: list[tuple[str, str | None, str]] = []
    if not found or found[0].header >= 0:
        out.append(("", None, ""))
    for s in found:
        out.append((s.name, lines[s.header] if s.header >= 0 else None,
                    "".join(line + "\n" for line in lines[s.first:s.end])))
    return out


def lines_from_result(pairs: list, snapshot: list[tuple[str, str | None, str]]) -> list[str]:
    """The pad's lines after a run: a key the script kept takes its section's header line
    exactly as it was, a key that cleans to the empty name is the top, any other key gets a
    normalised header; a line of a value beginning the mark is kept with one leading space."""
    headers = {name: header for name, header, _ in snapshot if header is not None}
    lines: list[str] = []
    for key, text in pairs:
        body = [f" {line}" if line.startswith(SECTION_MARK) else line for line in _split(text)]
        if key in headers:
            lines += [headers[key], *body]
        elif _clean(key) == "":
            lines[0:0] = body
        else:
            if "\n" in key or "\r" in key:
                raise _Refused("A section name is one line.", "bad_section")
            lines += [SECTION_MARK + _clean(key), *body]
    return lines


# --- slots -----------------------------------------------------------------------------------


async def _retry_parked() -> None:
    for uid, state in list(_state.slots.items()):
        if state == "parked":
            result = await LADDER(uid)
            if result.extinct:
                _state.slots[uid] = "free"
                logger.info("run slot uid %d cleared after %d passes", uid, result.passes)


async def _take_slot() -> int | None:
    await _retry_parked()
    for uid, state in _state.slots.items():
        if state == "free":
            _state.slots[uid] = "busy"
            return uid
    return None


async def _release_slot(uid: int) -> None:
    """After every run: the slot's uid is killed until verified empty, else parked."""
    result = await LADDER(uid)
    if result.extinct:
        _state.slots[uid] = "free"
        return
    _state.slots[uid] = "parked"
    logger.error("run slot uid %d parked: %s (survivors %s)", uid, result.reason,
                 list(result.survivors))


# --- boot ------------------------------------------------------------------------------------


def _box_failure(outcome: Outcome) -> tuple[str, str]:
    """(reason for the log, the fix for the operator) of a run whose box failed."""
    tail = outcome.stderr.strip().splitlines()
    line = tail[-1] if tail else ""
    if outcome.spawn_error:
        return f"the box could not be started: {outcome.spawn_error}", "update the app"
    if outcome.status == 100:
        return line, "update the app so the profile loads"
    if outcome.status == 106:
        return line, "remove the stale child profile, or update the app so the profile loads"
    if outcome.status in (126, 127):
        return (f"setpriv exited {outcome.status}: {line}",
                "update the app so the profile loads")
    if outcome.wall_killed:
        return "the box did not answer within the wall clock", "the box's self-check hung"
    return (f"exit {outcome.status}: {line}" if line else f"exit {outcome.status}",
            "the box's self-check failed")


async def boot(*, notify: Callable[[str], Awaitable[Any]] | None = None) -> bool:
    """Casa's label, the architecture, the kill of both slot uids, then one run of ``pass``
    over no sections through the whole box. Any failure leaves ``run`` unavailable with the
    reason; ``run`` is not ready until this returns."""
    _state.notify = notify
    try:
        try:
            _state.label = expected_label(own_label())
        except (OSError, ValueError) as exc:
            await _unavailable(str(exc), "update the app so the profile loads")
            return False
        logger.info("scratchpad run: expecting the box under %r", _state.label)
        if machine() != "x86_64":
            await _unavailable(f"the host is {machine()}, and the box is built for x86_64 only",
                               "x86_64 only")
            return False
        for uid in SLOT_UIDS:
            await _release_slot(uid)
        if all(state == "parked" for state in _state.slots.values()):
            await _unavailable("both slot uids still hold processes", "restart the app")
            return False
        uid = await _take_slot()
        if uid is None:
            await _unavailable("no slot uid is free", "restart the app")
            return False
        try:
            payload = json.dumps({"script": "pass", "sections": []}, ensure_ascii=True).encode("ascii")
            outcome = await execute(payload, uid, _state.label)
        finally:
            await _release_slot(uid)
        doc, _ = _result(outcome)
        if not outcome.frame or outcome.status != 0 or doc is None or doc["pad"] != []:
            reason, fix = _box_failure(outcome)
            if outcome.frame:
                reason = f"the preflight returned exit {outcome.status} with {outcome.stdout[:200]!r}"
            await _unavailable(reason, fix)
            return False
        _state.available = True
        logger.info("scratchpad run: the box passed its preflight in %.2f s", outcome.seconds)
        return True
    except Exception as exc:  # noqa: BLE001 — a boot never stops Casa's
        logger.exception("scratchpad run: boot failed")
        await _unavailable(f"{type(exc).__name__}: {exc}", "the box's self-check failed")
        return False
    finally:
        _state.ready = True


# --- the operation ---------------------------------------------------------------------------


def _signal_line(status: int, wall_killed: bool) -> str:
    if wall_killed:
        return f"Killed after {WALL_S:.0f} s."
    number = -status
    if number == signal.SIGXCPU:
        return "Stopped after 20 s of CPU."
    if number == signal.SIGSYS:
        return f"Killed by signal {number} (SIGSYS: a system call outside x86_64's table)."
    try:
        name = f" ({signal.Signals(number).name})"
    except ValueError:
        name = ""
    return f"Killed by signal {number}{name}."


def _status_line(outcome: Outcome) -> str:
    status = outcome.status
    if status < 0:
        return _signal_line(status, outcome.wall_killed)
    if status == 0:
        return f"Ran {outcome.seconds:.1f} s, exit 0."
    if status == 1:
        return "Exit 1: the script raised."
    if status < 100:
        return f"Exit {status}: the script's own sys.exit."
    return f"Exit {status}: the script's own exit."


def _printed_block(doc: dict | None, stray: int, chars: int) -> str:
    parts = []
    if doc is not None and doc["printed_total"] > 0:
        text = doc["printed"][:chars]
        head = (f"Printed (first {len(text):,} of {doc['printed_total']:,} characters):"
                if len(text) < doc["printed_total"] else f"Printed ({len(text):,} characters):")
        parts.append(f"{head}\n{text}")
    if stray:
        parts.append(f"(and {stray:,} stray bytes on stdout)")
    return "\n".join(parts)


async def run(key: str, args: dict) -> tuple[str, bool]:
    """The tool's ``run`` under *key*: the reply's text and whether it is an error."""
    if args.get("section") is not None:
        return "run takes text and chars. Nothing changed.", True
    script = args.get("text")
    if script is None:
        return "run needs text. Nothing changed.", True
    chars = max(READ_CHARS_MIN, min(READ_CHARS_MAX, int(args.get("chars", READ_CHARS))))
    if not _state.ready:
        return NOT_READY, True
    if not _state.available:
        return UNAVAILABLE, True
    if key in _state.in_flight:
        return BUSY_PAD, True
    snap = PADS.snapshot(key)
    if snap is None:
        return PADS.respond(key, {"op": "read"})
    stamp, lines = snap
    try:
        snapshot = snapshot_sections(lines)
    except _Refused as refused:
        return f"{_shape(lines)}\n{refused} Nothing changed.", True
    uid = await _take_slot()
    if uid is None:
        if any(state == "busy" for state in _state.slots.values()):
            return f"{_shape(lines)}\n{BUSY_SLOTS} Nothing changed.", True
        return UNAVAILABLE, True
    _state.in_flight.add(key)
    try:
        payload = json.dumps({"script": script, "sections": [[n, t] for n, _, t in snapshot]},
                             ensure_ascii=True).encode("ascii")
        outcome = await execute(payload, uid, _state.label)
    finally:
        _state.in_flight.discard(key)
        await _release_slot(uid)
    return await _reply(key, stamp, lines, snapshot, outcome, chars)


async def _reply(key: str, stamp: int, lines: list[str], snapshot, outcome: Outcome,
                 chars: int) -> tuple[str, bool]:
    if not outcome.frame:
        # The box failed, whatever the status: a refusal before the frame, setpriv's own
        # exit, a kill during the prelude. Not when the prelude found another parent, which
        # means this process is gone.
        reason, fix = _box_failure(outcome)
        if outcome.status == PARENT_DEATH:
            logger.error("scratchpad run: %s", reason)
        else:
            await _unavailable(reason, fix)
        return f"{_shape(lines)}\n{PAGE_NOTE}\n{UNAVAILABLE} Nothing changed.", True
    doc, stray = _result(outcome)
    head = f"{_shape(lines)}\n{PAGE_NOTE}\n"
    extra = _printed_block(doc, stray, chars)
    if outcome.status != 0:
        tail = outcome.stderr[-STDERR_SHOWN:]
        parts = [f"{_status_line(outcome)} Nothing changed."]
        if extra:
            parts.append(extra)
        if tail.strip():
            parts.append(f"Stderr:\n{tail}")
        return head + "\n".join(parts), True
    if doc is None or doc["pad"] is None:
        parts = [f"{UNREADABLE} Nothing changed."]
        if extra:
            parts.append(extra)
        return head + "\n".join(parts), True
    try:
        after = lines_from_result(doc["pad"], snapshot)
    except _Refused as refused:
        return head + "\n".join(p for p in (f"{refused} Nothing changed.", extra) if p), True
    said, refused = PADS.apply(key, stamp, after)
    if refused:
        return head + "\n".join(p for p in (f"{said} Nothing changed.", extra) if p), True
    body = [f"{_status_line(outcome)}", said]
    if extra:
        body.append(extra)
    return f"{_shape(after)}\n{PAGE_NOTE}\n" + "\n".join(body), False
