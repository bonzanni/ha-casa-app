"""Casa's side of the scratchpad's ``run`` operation (``pad_run``), with an injected spawner
and an injected kill ladder: the slots, the wall clock, the frame rule, the status mapping,
the result's shape check, the lines back from a result, the apply under the pad's stamp,
the reply's bound, and ``boot``. Nothing here spawns a process or signals one; the real box
is ``tests/test_run_child.py`` and, for the confinement, the host.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

import pad_run
import scratchpad
from scratchpad import MAX_BYTES, PAGE_NOTE
from uid_kill import QuiesceResult

try:
    from tests.test_scratchpad import _call, _client
except ImportError:
    from test_scratchpad import _call, _client

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "child", REPO / "casa" / "rootfs" / "opt" / "casa" / "run" / "child.py")
child = importlib.util.module_from_spec(_spec)
sys.dont_write_bytecode = True      # the prelude's directory in the tree stays as shipped
_spec.loader.exec_module(child)
sys.dont_write_bytecode = False

FRAME = b'{"box": "ready"}\n'
LABEL = "local_casa-test (enforce)"
CHILD_LABEL = "local_casa-test//pyrun (enforce)"


# --- the fakes ---------------------------------------------------------------------------------


class _Stream:
    def __init__(self, data: bytes, chunk: int = 65536):
        self._data = data
        self._chunk = chunk

    async def read(self, n: int = -1) -> bytes:
        out, self._data = self._data[:self._chunk], self._data[self._chunk:]
        return out


class _Stdin:
    def __init__(self):
        self.received = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.received += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """What the spawner returns: ``wait`` returns after *delay*, or when killed."""

    def __init__(self, status: int = 0, stdout: bytes = b"", stderr: bytes = b"",
                 delay: float = 0.0, hang: bool = False):
        self.returncode = None
        self._status = status
        self.stdout = _Stream(stdout)
        self.stderr = _Stream(stderr)
        self.stdin = _Stdin()
        self._delay = delay
        self._hang = hang
        self._killed = asyncio.Event()
        self.killed = False

    async def wait(self) -> int:
        if self._hang:
            await self._killed.wait()
            self.returncode = -9
        else:
            if self._delay:
                await asyncio.sleep(self._delay)
            self.returncode = self._status
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._killed.set()


def _result_line(pad, printed: str = "", total: int | None = None) -> bytes:
    doc = {"printed": printed, "printed_total": len(printed) if total is None else total, "pad": pad}
    return b"\n" + json.dumps(doc, ensure_ascii=True).encode("ascii") + b"\n"


class Spawner:
    """Records every spawn; answers with the next scripted process, or runs *script*
    through the real ``child.run_script`` in this process when no process is scripted."""

    def __init__(self):
        self.calls: list[tuple[tuple, dict]] = []
        self.processes: list[FakeProcess] = []
        self.spawned: list[FakeProcess] = []

    async def __call__(self, *argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.processes:
            proc = self.processes.pop(0)
        else:
            proc = _Deferred()
        self.spawned.append(proc)
        return proc


class _Deferred(FakeProcess):
    """A process that runs the payload it is fed through the real prelude's script half,
    once Casa has written it, so the pad's encoding is exercised end to end."""

    def __init__(self):
        super().__init__()
        self._fed = asyncio.Event()
        self.stdin = _DeferredStdin(self)
        self.stdout = _LazyStream(self)

    async def wait(self) -> int:
        await self._fed.wait()
        return self.returncode

    def feed(self, payload: bytes) -> None:
        doc = json.loads(payload)
        status, result = child.run_script(doc["script"], doc["sections"])
        self.stdout.data = FRAME + b"\n" + json.dumps(result, ensure_ascii=True).encode("ascii") + b"\n"
        self.returncode = status
        self._fed.set()


class _LazyStream:
    """The deferred process's stdout: nothing to read until it has been fed."""

    def __init__(self, proc):
        self._proc = proc
        self.data = b""

    async def read(self, n: int = -1) -> bytes:
        await self._proc._fed.wait()
        out, self.data = self.data, b""
        return out


class _DeferredStdin(_Stdin):
    def __init__(self, proc: _Deferred):
        super().__init__()
        self._proc = proc

    def close(self) -> None:
        super().close()
        self._proc.feed(self.received)


class Ladder:
    def __init__(self, extinct: bool = True):
        self.calls: list[int] = []
        self.extinct = extinct
        self.per_uid: dict[int, list[bool]] = {}

    async def __call__(self, uid: int, **kwargs) -> QuiesceResult:
        self.calls.append(uid)
        queue = self.per_uid.get(uid)
        extinct = queue.pop(0) if queue else self.extinct
        return (QuiesceResult(extinct=True, passes=2) if extinct
                else QuiesceResult(extinct=False, reason="still populated", survivors=(4242,)))


@pytest.fixture(autouse=True)
def runner(monkeypatch):
    """A fresh runner state, ready and available under the casa-test label, a recording
    spawner and a ladder that always verifies, and a fresh pad store."""
    pad_run.reset()
    pad_run._state.ready = True
    pad_run._state.available = True
    pad_run._state.label = CHILD_LABEL
    spawner = Spawner()
    ladder = Ladder()
    monkeypatch.setattr(pad_run, "SPAWN", spawner)
    monkeypatch.setattr(pad_run, "LADDER", ladder)
    store = scratchpad.PadStore()
    monkeypatch.setattr(scratchpad, "PADS", store)
    monkeypatch.setattr(pad_run, "PADS", store)
    return spawner, ladder, store


def _pad(store, text: str, key: str = "k") -> None:
    store.respond(key, {"op": "write", "text": text})


def _scripted(spawner, *, status=0, stdout=b"", stderr=b"", delay=0.0, hang=False) -> FakeProcess:
    proc = FakeProcess(status, stdout, stderr, delay, hang)
    spawner.processes.append(proc)
    return proc


# --- the spawn ---------------------------------------------------------------------------------


async def test_the_spawn_line_is_exactly_the_boxs(runner):
    spawner, _, store = runner
    _pad(store, "x")
    _scripted(spawner, stdout=FRAME + _result_line([["", "x\n"]]))
    import os
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is False, reply
    ((argv, kwargs),) = spawner.calls
    assert argv == ("/usr/bin/setpriv", "--reuid", "199000", "--regid", "199000", "--clear-groups",
                    "--bounding-set", "-all", "--inh-caps", "-all", "--",
                    "/opt/casa/run/python3", "-I", "-S", "-B", "/opt/casa/run/child.py",
                    str(os.getpid()), CHILD_LABEL, "199000")
    assert kwargs == {"stdin": asyncio.subprocess.PIPE, "stdout": asyncio.subprocess.PIPE,
                      "stderr": asyncio.subprocess.PIPE, "env": {}, "close_fds": True,
                      "cwd": "/", "start_new_session": True}
    assert "--no-new-privs" not in argv


async def test_the_payload_is_ascii_and_carries_the_sections_as_the_box_expects(runner):
    spawner, _, store = runner
    _pad(store, "top\n## a\nx\udcff\n\n## ## b\ny")
    _scripted(spawner, stdout=FRAME + _result_line(None))
    await pad_run.run("k", {"op": "run", "text": "print(1)"})
    fed = spawner.spawned[0].stdin.received
    assert fed.isascii() and spawner.spawned[0].stdin.closed
    assert json.loads(fed) == {"script": "print(1)", "sections": [
        ["", "top\n"], ["a", "x\udcff\n\n"], ["## b", "y\n"]]}


async def test_a_pad_with_two_sections_of_one_name_refuses_before_the_spawn(runner):
    spawner, _, store = runner
    store.put("k", ["## a", "1", "## a", "2"])
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is True and "2 sections are named" in reply and "Nothing changed." in reply
    assert spawner.calls == []


# --- slots and the wall clock -------------------------------------------------------------------


async def test_a_pad_with_an_empty_section_name_refuses_before_the_spawn(runner):
    """Red case: the top and a `## ` header are one key to the script, so a pass run would
    report success with the top's lines gone."""
    spawner, _, store = runner
    for lines in (["top", "## ", "body"], ["## ", "foo"], ["x", "##   ", "y", "## a", "z"]):
        store.put("k", lines)
        reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
        assert error is True, lines
        assert 'A section named "" and the top of the pad are one name to a run.' in reply
        assert reply.endswith("Nothing changed.")
        assert store.lines("k") == lines
    assert spawner.calls == []


async def test_a_result_that_names_a_section_twice_is_refused_in_one_pass(runner):
    """Red case: a result of many pairs that all prepend to the top did quadratic work on
    the event loop; now two keys naming one section refuse before anything is built."""
    spawner, _, store = runner
    _pad(store, "t\n## a\nx")
    cases = [
        ([["", "x\n"]] * 300_000, "the top of the pad 300000 times"),
        ([["", "x\n"], [" ", "y\n"]], "the top of the pad 2 times"),
        ([["", "x\n"], ["## a", "y\n"], ["a", "z\n"]], '"a" 2 times'),
        ([["", ""], ["a", "x\n"], ["## a", "y\n"]], '"a" 2 times'),
        ([["", ""], ["b", "x\n"], ["##  b  ", "y\n"]], '"b" 2 times'),
    ]
    for pairs, said in cases:
        _scripted(spawner, stdout=FRAME + _result_line(pairs))
        reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
        assert error is True and f"The result names {said}; a name is one section. Nothing changed." in reply
        assert store.lines("k") == ["t", "## a", "x"]
    snapshot = pad_run.snapshot_sections(["t", "## a", "x"])
    lines = pad_run.lines_from_result([["b", "1\n"], ["", "top\n"], ["a", "x\n"]], snapshot)
    assert lines == ["top", "## b", "1", "## a", "x"]


async def test_two_overlapping_takes_never_share_a_uid(runner):
    """Red case: both takes read the parked slot as parked, both ran the ladder, and the
    second write freed the slot the first had already taken."""
    _, ladder, _ = runner
    pad_run._state.slots = {199000: "parked", 199001: "busy"}
    slow = Ladder()

    async def slow_ladder(uid, **kwargs):
        await asyncio.sleep(0.01)
        return await slow(uid)
    pad_run.LADDER = slow_ladder
    taken = await asyncio.gather(pad_run._take_slot(), pad_run._take_slot())
    got = [uid for uid in taken if uid is not None]
    assert len(got) == len(set(got)) == 1 and got == [199000]
    assert pad_run._state.slots == {199000: "busy", 199001: "busy"}
    assert slow.calls == [199000]


async def test_a_recovery_cancelled_mid_ladder_leaves_the_slot_parked_not_claimed(runner):
    """Red case: a take cancelled while the ladder ran left the slot `recovering`, which no
    later take revisits, so the slot was lost for the rest of the process."""
    _, ladder, _ = runner
    pad_run._state.slots = {199000: "parked", 199001: "busy"}
    started = asyncio.Event()

    async def stuck_ladder(uid, **kwargs):
        started.set()
        await asyncio.Event().wait()
    pad_run.LADDER = stuck_ladder
    take = asyncio.ensure_future(pad_run._take_slot())
    await started.wait()
    take.cancel()
    with pytest.raises(asyncio.CancelledError):
        await take
    assert pad_run._state.slots == {199000: "parked", 199001: "busy"}
    pad_run.LADDER = ladder
    assert await pad_run._take_slot() == 199000
    assert ladder.calls == [199000]


async def test_two_runs_at_a_time_one_per_pad(runner):
    spawner, ladder, store = runner
    for key in ("a", "b", "c"):
        _pad(store, key, key)
    first = _scripted(spawner, hang=True)
    second = _scripted(spawner, hang=True)
    t1 = asyncio.ensure_future(pad_run.run("a", {"op": "run", "text": "pass"}))
    t2 = asyncio.ensure_future(pad_run.run("b", {"op": "run", "text": "pass"}))
    await asyncio.sleep(0.01)
    assert sorted(pad_run._state.slots.values()) == ["busy", "busy"]
    reply, error = await pad_run.run("c", {"op": "run", "text": "pass"})
    assert error is True and pad_run.BUSY_SLOTS in reply and "Nothing changed." in reply
    reply, error = await pad_run.run("a", {"op": "run", "text": "pass"})
    assert (reply, error) == (pad_run.BUSY_PAD, True)
    assert len(spawner.calls) == 2
    assert {c[0][2] for c in spawner.calls} == {"199000", "199001"}
    first.kill()
    second.kill()
    await asyncio.gather(t1, t2)
    assert sorted(pad_run._state.slots.values()) == ["free", "free"]
    assert sorted(ladder.calls) == [199000, 199001]


async def test_the_wall_clock_kills_and_nothing_changes(runner, monkeypatch):
    spawner, ladder, store = runner
    _pad(store, "x")
    monkeypatch.setattr(pad_run, "WALL_S", 0.05)
    proc = _scripted(spawner, stdout=FRAME, hang=True)
    reply, error = await pad_run.run("k", {"op": "run", "text": "import time; time.sleep(60)"})
    assert proc.killed
    assert error is True and "Killed after 0 s. Nothing changed." in reply
    assert store.lines("k") == ["x"]
    assert ladder.calls == [199000]
    assert pad_run._state.available


async def test_every_run_ends_with_the_ladder_and_a_survivor_parks_the_slot(runner, caplog):
    spawner, ladder, store = runner
    _pad(store, "x")
    ladder.per_uid[199000] = [False]
    _scripted(spawner, stdout=FRAME + _result_line([["", "x\n"]]))
    with caplog.at_level(logging.ERROR, logger="pad_run"):
        reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is False
    assert pad_run._state.slots == {199000: "parked", 199001: "free"}
    assert [r for r in caplog.records if "parked" in r.getMessage() and "4242" in r.getMessage()]
    # The next run retries the ladder on the parked slot first, then takes a free one.
    ladder.per_uid[199000] = [True]
    _scripted(spawner, stdout=FRAME + _result_line([["", "x\n"]]))
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is False
    assert ladder.calls == [199000, 199000, 199000]
    assert pad_run._state.slots == {199000: "free", 199001: "free"}


async def test_both_slots_parked_is_unavailable_until_one_clears(runner):
    spawner, ladder, store = runner
    _pad(store, "x")
    ladder.extinct = False
    _scripted(spawner, stdout=FRAME + _result_line([["", "x\n"]]))
    await pad_run.run("k", {"op": "run", "text": "pass"})
    _scripted(spawner, stdout=FRAME + _result_line([["", "x\n"]]))
    await pad_run.run("k", {"op": "run", "text": "pass"})
    assert pad_run._state.slots == {199000: "parked", 199001: "parked"}
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert (reply, error) == (pad_run.UNAVAILABLE, True)
    assert len(spawner.calls) == 2
    ladder.extinct = True
    _scripted(spawner, stdout=FRAME + _result_line([["", "x\n"]]))
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is False


# --- the frame rule and the status mapping --------------------------------------------------------


@pytest.mark.parametrize("status, stderr", [
    (100, b"box: confinement is 'local_casa-test (enforce)', expected 'local_casa-test//pyrun (enforce)'\n"),
    (106, b"box: the loaded child profile is not this text's (Permission denied)\n"),
    (126, b"setpriv: failed to execute /opt/casa/run/python3: Permission denied\n"),
    (127, b"setpriv: failed to execute /opt/casa/run/python3: No such file or directory\n"),
    (1, b"Traceback (most recent call last):\n  File \"/opt/casa/run/child.py\"\nNameError\n"),
    (-9, b""),
])
async def test_no_frame_is_the_boxs_failure_whatever_the_status(runner, status, stderr, caplog):
    """Red case: a status read as the script's would let a refused box look like a bad script
    while the next run tries again."""
    spawner, _, store = runner
    _pad(store, "x")
    notices = []

    async def notify(text):
        notices.append(text)
    pad_run._state.notify = notify
    _scripted(spawner, status=status, stderr=stderr)
    with caplog.at_level(logging.ERROR, logger="pad_run"):
        reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is True
    assert reply == f"{scratchpad._shape(['x'])}\n{PAGE_NOTE}\n{pad_run.UNAVAILABLE} Nothing changed."
    assert store.lines("k") == ["x"]
    assert not pad_run._state.available
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "unavailable" in errors[0]
    if stderr:
        assert stderr.decode().strip().splitlines()[-1] in errors[0]
    assert len(notices) == 1 and "unavailable" in notices[0]
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert (reply, error) == (pad_run.UNAVAILABLE, True)
    assert len(spawner.calls) == 1 and len(notices) == 1


async def test_the_operator_is_told_once_per_process(runner):
    """Red case: a notice per failure would page the operator on every run of a broken box."""
    spawner, _, store = runner
    _pad(store, "x")
    notices = []

    async def notify(text):
        notices.append(text)
    pad_run._state.notify = notify
    for _ in range(3):
        pad_run._state.available = True
        _scripted(spawner, status=100, stderr=b"box: confinement is 'x', expected 'y'\n")
        await pad_run.run("k", {"op": "run", "text": "pass"})
    assert len(spawner.calls) == 3 and len(notices) == 1


async def test_a_parent_death_refusal_does_not_retire_run(runner, caplog):
    spawner, _, store = runner
    _pad(store, "x")
    _scripted(spawner, status=104, stderr=b"box: parent is 7, expected 6\n")
    with caplog.at_level(logging.ERROR, logger="pad_run"):
        reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is True and pad_run.UNAVAILABLE in reply
    assert pad_run._state.available
    assert any("parent is 7" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("status, line", [
    (100, "Exit 100: the script's own exit."),
    (3, "Exit 3: the script's own sys.exit."),
    (1, "Exit 1: the script raised."),
    (-9, "Killed by signal 9 (SIGKILL)."),
    (-24, "Stopped after 20 s of CPU."),
    (-31, "Killed by signal 31 (SIGSYS: a system call outside x86_64's table)."),
    (-15, "Killed by signal 15 (SIGTERM)."),
])
async def test_after_the_frame_every_status_is_the_scripts(runner, status, line):
    spawner, _, store = runner
    _pad(store, "x")
    _scripted(spawner, status=status, stdout=FRAME, stderr=b"the script's own line\n")
    reply, error = await pad_run.run("k", {"op": "run", "text": "import os; os._exit(100)"})
    assert error is True
    assert reply.startswith(f"{scratchpad._shape(['x'])}\n{PAGE_NOTE}\n{line} Nothing changed.")
    assert "Stderr:\nthe script's own line" in reply
    assert store.lines("k") == ["x"]
    assert pad_run._state.available
    assert len(spawner.calls) == 1


async def test_a_success_replies_with_the_shape_the_note_the_time_and_the_summary(runner):
    spawner, _, store = runner
    _pad(store, "top\n## a\nold")
    _scripted(spawner, stdout=FRAME + _result_line([["", "top\n"], ["a", "new\n"], ["b", "b\n"]],
                                                   printed="hello"))
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is False
    head, note, status, summary, printed, text = reply.split("\n")
    assert head == scratchpad._shape(["top", "## a", "new", "## b", "b"])
    assert note == PAGE_NOTE
    assert status.startswith("Ran 0.") and status.endswith(" s, exit 0.")
    assert summary == 'Sections: 1 new ("b"), 1 changed ("a"). Lines 3 -> 5. Sections 1 -> 2: a line beginning "## " starts a section.'
    assert (printed, text) == ("Printed (5 characters):", "hello")
    assert store.lines("k") == ["top", "## a", "new", "## b", "b"]


async def test_printed_output_is_cut_at_chars_with_the_true_total(runner):
    spawner, _, store = runner
    _pad(store, "x")
    _scripted(spawner, stdout=FRAME + _result_line([["", "x\n"]], printed="z" * 40_000, total=10_010_000))
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass", "chars": 300})
    assert error is False
    assert "Printed (first 300 of 10,010,000 characters):\n" + "z" * 300 in reply
    assert reply.count("z") == 300
    _scripted(spawner, stdout=FRAME + _result_line([["", "x\n"]], printed="z" * 40_000, total=10_010_000))
    reply, _ = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert "Printed (first 8,000 of 10,010,000 characters):" in reply


async def test_stray_bytes_on_stdout_are_counted_and_the_result_still_read(runner):
    spawner, _, store = runner
    _pad(store, "t")
    _scripted(spawner, stdout=FRAME + b"stray\n" + _result_line([["", "t\n"], ["a", "x\n"]]))
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is False and "(and 6 stray bytes on stdout)" in reply
    assert store.lines("k") == ["t", "## a", "x"]


async def test_the_stderr_tail_is_shown_on_a_failure(runner):
    spawner, _, store = runner
    _pad(store, "x")
    traceback = b"T" * 1_000_000 + b"\nZeroDivisionError: division by zero\n"
    _scripted(spawner, status=1, stdout=FRAME + _result_line(None), stderr=traceback)
    reply, error = await pad_run.run("k", {"op": "run", "text": "1/0"})
    assert error is True
    shown = reply.split("Stderr:\n", 1)[1]
    assert len(shown) == 2_000 and shown.endswith("ZeroDivisionError: division by zero\n")


# --- what comes back: Casa's own shape check ----------------------------------------------------


@pytest.mark.parametrize("last_line", [
    b'{"printed": "", "printed_total": 0, "pad": [[1, 2]]}',
    b'{"printed": "", "printed_total": 0, "pad": [["a", 1]]}',
    b'{"printed": "", "printed_total": 0, "pad": [[1, "a"]]}',
    b"[" * 100_000 + b"]" * 100_000,
    b'{"printed": "", "printed_total": 0, "pad": [["a", "x", "y"]]}',
    b'{"printed": "", "printed_total": 0, "pad": [["a"]]}',
    b'{"printed": "", "printed_total": 0, "pad": [{"a": "x"}]}',
    b'{"printed": "", "printed_total": 0, "pad": {"a": "x"}}',
    b'{"printed": "", "pad": [["a", "x"]]}',
    b'{"printed": "", "printed_total": 0, "pad": [["a", "x"]], "extra": 1}',
    b'{"printed": "", "printed_total": "0", "pad": [["a", "x"]]}',
    b'{"printed": "", "printed_total": true, "pad": [["a", "x"]]}',
    b'{"printed": 0, "printed_total": 0, "pad": [["a", "x"]]}',
    b'{"printed": "", "printed_total": 0, "pad": null}',
    b'[["a", "x"]]',
    b'not json',
    b'',
])
async def test_a_result_casa_does_not_accept_is_unreadable_and_applies_nothing(runner, last_line):
    """Whatever the child said at status 0: a forged last line, a pad that is not pairs of
    strings, a missing or extra key, a number as a string or a bool, null. No exception
    reaches the tool."""
    spawner, _, store = runner
    _pad(store, "x")
    _scripted(spawner, stdout=FRAME + b"\n" + last_line + b"\n")
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is True
    assert f"{pad_run.UNREADABLE} Nothing changed." in reply
    assert store.lines("k") == ["x"]
    assert pad_run._state.available


async def test_past_the_cap_nothing_is_kept_not_even_a_record_of_a_chunk(monkeypatch):
    """Red case: an entry per discarded chunk, even an empty one, grows with what the
    script prints for as long as the wall clock lets it."""
    import tracemalloc
    monkeypatch.setattr(pad_run, "STDOUT_CAP", 65536)
    chunk = b"z" * 4096
    post_cap = 200_000

    class Endless:
        def __init__(self):
            self.left = 16 + post_cap

        async def read(self, n=-1):
            if self.left == 0:
                return b""
            self.left -= 1
            return chunk
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    kept, overflow = await pad_run._read_stdout(Endless())
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert (len(kept), overflow) == (65536, True)
    assert peak - base < 2 * 65536 + 16384, peak - base


async def test_a_result_with_no_final_newline_or_past_the_read_cap_is_unreadable(runner, monkeypatch):
    spawner, _, store = runner
    _pad(store, "x")
    _scripted(spawner, stdout=FRAME + b'\n{"printed": "", "printed_total": 0, "pad": [["", "y\\n"]]}')
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is True and pad_run.UNREADABLE in reply and store.lines("k") == ["x"]
    monkeypatch.setattr(pad_run, "STDOUT_CAP", 1000)
    _scripted(spawner, stdout=FRAME + b"\n" + b"x" * 2000 + _result_line([["", "y\n"]]))
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is True and pad_run.UNREADABLE in reply and store.lines("k") == ["x"]
    # Red case: a well-formed line exactly at the cap, with more after it, is not the
    # result: what was discarded past the cap may have been the real last line.
    first = FRAME + _result_line([["", "forged\n"]])
    monkeypatch.setattr(pad_run, "STDOUT_CAP", len(first))
    _scripted(spawner, stdout=first + _result_line([["", "real\n"]]))
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is True and pad_run.UNREADABLE in reply and store.lines("k") == ["x"]


# --- the apply under the stamp -----------------------------------------------------------------


async def test_a_pad_written_during_the_run_refuses_the_result(runner):
    spawner, _, store = runner
    _pad(store, "x")
    proc = _scripted(spawner, stdout=FRAME + _result_line([["", "from the run\n"]]), hang=False)
    proc._delay = 0.02
    task = asyncio.ensure_future(pad_run.run("k", {"op": "run", "text": "pass"}))
    await asyncio.sleep(0.005)
    store.respond("k", {"op": "write", "text": "x"})       # the same text: still a change
    reply, error = await task
    assert error is True and "The pad changed during the run. Nothing changed." in reply
    assert store.lines("k") == ["x"]


async def test_a_pad_deleted_during_the_run_refuses_the_result(runner):
    spawner, _, store = runner
    _pad(store, "x")
    proc = _scripted(spawner, stdout=FRAME + _result_line([["", "from the run\n"]]))
    proc._delay = 0.02
    task = asyncio.ensure_future(pad_run.run("k", {"op": "run", "text": "pass"}))
    await asyncio.sleep(0.005)
    store.discard("k")
    reply, error = await task
    assert error is True and "The pad changed during the run." in reply
    assert store.lines("k") is None


async def test_the_apply_passes_the_stores_door(runner):
    spawner, _, store = runner
    _pad(store, "## ## a\nx")
    _scripted(spawner, stdout=FRAME + _result_line([["", ""], ["## a", "x\n"], ["## ## a", "y\n"]]))
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is True and 'The result names "## a" 2 times' in reply
    assert store.lines("k") == ["## ## a", "x"]
    _scripted(spawner, stdout=FRAME + _result_line([["", ""], ["## a", "x\n"], ["b\nc", "y\n"]]))
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is True and "A section name is one line. Nothing changed." in reply
    _scripted(spawner, stdout=FRAME + _result_line([["", "z" * (MAX_BYTES + 1)]]))
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is True and "the cap is" in reply and "Nothing changed." in reply
    assert store.lines("k") == ["## ## a", "x"]


# --- the encoding, through the real script half ------------------------------------------------


@pytest.mark.parametrize("lines", [
    ["## ## a", "x"],
    ["## a   ", "x"],
    ["## a", "x", ""],
    [""],
    [],
    ["top", "", "## a", "", "## b", "y", "", ""],
    ["t", "", ""],
    ["x\udcff", "## e", "\x00"],
    ["top", "## ## a", "x", "", "##  b  ", "y"],
])
async def test_pass_is_a_byte_identical_no_op(runner, lines):
    """Header spelling, blank lines, lone surrogates and NULs all come back as they were."""
    _, _, store = runner
    store.put("k", lines)
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is False, reply
    assert store.lines("k") == lines
    assert "Sections: none new, changed or removed." in reply


async def test_a_renamed_or_new_key_gets_a_normalised_header_and_a_kept_one_keeps_its_spelling(runner):
    _, _, store = runner
    store.put("k", ["## ## a", "x"])
    await pad_run.run("k", {"op": "run", "text": "pad['b'] = pad.pop('## a')"})
    assert store.lines("k") == ["## b", "x"]
    store.put("k", ["## ## a", "x"])
    await pad_run.run("k", {"op": "run", "text": "pad['## b'] = 'n'"})
    assert store.lines("k") == ["## ## a", "x", "## b", "n"]
    store.put("k", ["## ## a", "x"])
    reply, error = await pad_run.run("k", {"op": "run", "text": "pad['a'] = 'collide'"})
    assert error is False and store.lines("k") == ["## ## a", "x", "## a", "collide"]
    store.put("k", ["## ## a", "x"])
    reply, error = await pad_run.run("k", {"op": "run", "text": "pad['## ## a'] = 'collide'"})
    assert error is True and 'The result names "## a" 2 times' in reply
    assert store.lines("k") == ["## ## a", "x"]


async def test_a_value_reads_as_its_lines_and_a_mark_line_in_one_stays_a_value(runner):
    _, _, store = runner
    store.put("k", ["t"])
    await pad_run.run("k", {"op": "run", "text": "pad['a'] = 'a\\nb'; pad[''] = 'x\\n## not\\ny'"})
    assert store.lines("k") == ["x", " ## not", "y", "## a", "a", "b"]
    store.put("k", ["t"])
    await pad_run.run("k", {"op": "run", "text": "pad = {'': 'one\\n', 'n': ''}"})
    assert store.lines("k") == ["one", "## n"]
    store.put("k", ["t"])
    await pad_run.run("k", {"op": "run", "text": "pad['x'] = 'a\\r\\nb\\rc'"})
    assert store.lines("k") == ["t", "## x", "a", "b", "c"]


async def test_the_scripts_exit_and_its_printed_output_through_the_real_script_half(runner):
    _, _, store = runner
    store.put("k", ["t"])
    reply, error = await pad_run.run("k", {"op": "run", "text": "print(len(pad['']))\nimport sys; sys.exit(3)"})
    assert error is True and "Exit 3: the script's own sys.exit. Nothing changed." in reply
    assert "Printed (2 characters):\n2\n" in reply + "\n"
    assert store.lines("k") == ["t"]
    reply, error = await pad_run.run("k", {"op": "run", "text": "pad = None"})
    assert error is True and "Exit 1: the script raised." in reply
    assert store.lines("k") == ["t"]


# --- the reply's bound -------------------------------------------------------------------------


async def test_no_reply_reaches_the_clis_save_to_file_threshold(runner):
    spawner, _, store = runner
    long_name = "n" * 500
    before = ["t"] + [f"## {long_name}{i}" for i in range(10_000)]
    store.put("k", before)
    _scripted(spawner, stdout=FRAME + b"s" * 100 + _result_line([["", "t\n"]], printed="p" * 40_000, total=10**9))
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass", "chars": 40_000})
    assert error is False and store.lines("k") == ["t"]
    assert len(reply) < 50_000
    summary = reply.split("\n")[3]
    assert summary.startswith("Sections: 10000 removed (") and len(summary) <= 800
    store.put("k", before)
    _scripted(spawner, status=1, stdout=FRAME + b"s" * 100 + _result_line(None, printed="p" * 40_000, total=10**9),
              stderr=b"T" * 1_000_000)
    reply, error = await pad_run.run("k", {"op": "run", "text": "1/0", "chars": 40_000})
    assert error is True and len(reply) < 50_000


# --- availability -------------------------------------------------------------------------------


async def test_run_is_not_ready_before_boot_returns_and_unavailable_when_it_failed(runner):
    spawner, _, store = runner
    _pad(store, "x")
    pad_run._state.ready = False
    assert await pad_run.run("k", {"op": "run", "text": "pass"}) == (pad_run.NOT_READY, True)
    pad_run._state.ready = True
    pad_run._state.available = False
    assert await pad_run.run("k", {"op": "run", "text": "pass"}) == (pad_run.UNAVAILABLE, True)
    assert spawner.calls == []


async def test_run_takes_text_and_chars_and_needs_a_pad(runner):
    spawner, _, store = runner
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass", "section": "a"})
    assert (reply, error) == ("run takes text and chars. Nothing changed.", True)
    reply, error = await pad_run.run("k", {"op": "run"})
    assert (reply, error) == ("run needs text. Nothing changed.", True)
    reply, error = await pad_run.run("k", {"op": "run", "text": "pass"})
    assert error is True and reply.startswith("No pad for this conversation")
    assert spawner.calls == []


async def test_run_reaches_the_tool_through_the_sdk_bridge(runner):
    spawner, _, store = runner
    client = _client("S1")
    _, error = await _call(client, op="write", text="x")
    assert error is False
    _scripted(spawner, stdout=FRAME + _result_line([["", "x\n"], ["a", "y\n"]]))
    reply, error = await _call(client, op="run", text="pad['a'] = 'y\\n'")
    assert error is False and PAGE_NOTE in reply
    assert store.lines("S1") == ["x", "## a", "y"]
    reply, error = await _call(client, op="run", text="pass", section="a")
    assert error is True and "run takes text and chars" in reply


# --- boot --------------------------------------------------------------------------------------


def _preflight(spawner):
    _scripted(spawner, stdout=FRAME + _result_line([]))


async def test_boot_derives_the_child_label_kills_both_uids_and_runs_the_preflight(runner, monkeypatch):
    spawner, ladder, _ = runner
    pad_run.reset()
    monkeypatch.setattr(pad_run, "own_label", lambda: LABEL)
    monkeypatch.setattr(pad_run, "machine", lambda: "x86_64")
    _preflight(spawner)
    notices = []

    async def notify(text):
        notices.append(text)
    assert await pad_run.boot(notify=notify) is True
    assert pad_run._state.ready and pad_run._state.available
    assert pad_run._state.label == CHILD_LABEL
    assert ladder.calls[:2] == [199000, 199001] and len(ladder.calls) == 3
    ((argv, _),) = spawner.calls
    assert argv[-2] == CHILD_LABEL
    assert json.loads(spawner.spawned[0].stdin.received) == {"script": "pass", "sections": []}
    assert notices == []
    assert pad_run.expected_label("abc123_casa (enforce)") == "abc123_casa//pyrun (enforce)"


@pytest.mark.parametrize("own", ["unconfined", "local_casa-test (complain)", "local_casa-test",
                                 " (enforce)", "local_casa-test//x (enforce)"])
async def test_boot_refuses_a_label_that_is_not_an_enforced_name(runner, monkeypatch, own, caplog):
    spawner, _, _ = runner
    pad_run.reset()
    monkeypatch.setattr(pad_run, "own_label", lambda: own)
    notices = []

    async def notify(text):
        notices.append(text)
    with caplog.at_level(logging.ERROR, logger="pad_run"):
        assert await pad_run.boot(notify=notify) is False
    assert pad_run._state.ready and not pad_run._state.available
    assert spawner.calls == []
    assert len(notices) == 1 and "profile" in notices[0]
    assert any(own in r.getMessage() for r in caplog.records)
    assert await pad_run.run("k", {"op": "run", "text": "pass"}) == (pad_run.UNAVAILABLE, True)


async def test_a_boot_failure_before_the_channel_is_ready_is_told_once_it_is(runner, monkeypatch):
    """Red case: the notice went to a channel that had not connected yet, was refused, and
    counted as sent, so the operator never heard of it."""
    spawner, _, _ = runner
    pad_run.reset()
    monkeypatch.setattr(pad_run, "own_label", lambda: "unconfined")
    ready = asyncio.Event()
    notices = []

    async def notify(text):
        if not ready.is_set():
            return False
        notices.append(text)
        return True
    assert await pad_run.boot(notify=notify, ready=ready) is False
    await asyncio.sleep(0)
    assert notices == [] and pad_run._state.ready and spawner.calls == []
    ready.set()
    await pad_run._state.notice_task
    assert len(notices) == 1 and "profile" in notices[0]
    await pad_run._unavailable("again", "x")
    assert len(notices) == 1


async def test_boot_refuses_an_unreadable_label_and_another_architecture(runner, monkeypatch):
    spawner, _, _ = runner
    pad_run.reset()

    def unreadable():
        raise OSError("cannot read Casa's confinement label (Invalid argument)")
    monkeypatch.setattr(pad_run, "own_label", unreadable)
    assert await pad_run.boot() is False
    assert "Invalid argument" in pad_run._state.reason
    pad_run.reset()
    monkeypatch.setattr(pad_run, "own_label", lambda: LABEL)
    monkeypatch.setattr(pad_run, "machine", lambda: "aarch64")
    assert await pad_run.boot() is False
    assert "aarch64" in pad_run._state.reason and spawner.calls == []


async def test_boot_refuses_a_preflight_that_does_not_pass(runner, monkeypatch, caplog):
    spawner, _, _ = runner
    monkeypatch.setattr(pad_run, "own_label", lambda: LABEL)
    monkeypatch.setattr(pad_run, "machine", lambda: "x86_64")
    for proc, fix in (
            (FakeProcess(100, b"", b"box: confinement is 'local_casa-test (enforce)', expected 'local_casa-test//pyrun (enforce)'\n"),
             "update the app so the profile loads"),
            (FakeProcess(106, b"", b"box: the loaded child profile is not this text's (Permission denied)\n"),
             "remove the stale child profile"),
            (FakeProcess(109, b"", b"box: shmget succeeded: the syscall filter is not in place\n"),
             "the box's self-check failed"),
            (FakeProcess(126, b"", b"setpriv: failed to execute /opt/casa/run/python3: Permission denied\n"),
             "update the app so the profile loads"),
            (FakeProcess(0, FRAME + _result_line([["", "x\n"]]), b""), "the box's self-check failed"),
            (FakeProcess(3, FRAME + _result_line(None), b""), "the box's self-check failed")):
        pad_run.reset()
        spawner.processes.append(proc)
        notices = []

        async def notify(text):
            notices.append(text)
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="pad_run"):
            assert await pad_run.boot(notify=notify) is False
        assert not pad_run._state.available and pad_run._state.ready
        assert len(notices) == 1 and fix in notices[0], (fix, notices)
        assert any(fix in r.getMessage() for r in caplog.records)


async def test_boot_with_a_spawn_that_fails_is_unavailable_not_an_exception(runner, monkeypatch):
    pad_run.reset()
    monkeypatch.setattr(pad_run, "own_label", lambda: LABEL)
    monkeypatch.setattr(pad_run, "machine", lambda: "x86_64")

    async def missing(*argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])
    monkeypatch.setattr(pad_run, "SPAWN", missing)
    assert await pad_run.boot() is False
    assert "could not be started" in pad_run._state.reason


async def test_boot_parks_a_slot_whose_uid_still_holds_processes(runner, monkeypatch):
    spawner, ladder, _ = runner
    pad_run.reset()
    monkeypatch.setattr(pad_run, "own_label", lambda: LABEL)
    monkeypatch.setattr(pad_run, "machine", lambda: "x86_64")
    ladder.per_uid[199000] = [False, False]
    _preflight(spawner)
    assert await pad_run.boot() is True
    assert pad_run._state.slots == {199000: "parked", 199001: "free"}
    pad_run.reset()
    ladder.extinct = False
    assert await pad_run.boot() is False
    assert "both slot uids" in pad_run._state.reason
