"""A pending wipe intent in casa-main, and what may touch memory around the
container-start step (§14 tests 9c and 9g; INV-MEM-036).

* No attempt recorded: memory is untouched, Casa boots normally and asks for
  the app restart again once its channels are up.
* Any attempt recorded, from this container start or an earlier one:
  notification-only mode. Nothing that reads memory starts, no socket is
  bound, no restart is requested, and the operator gets one line per
  container start, recorded before it is sent.

The casa-main runs here are the real ``casa_core.main()`` in a process the
test spawns and stops with SIGTERM."""
from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import memory_wipe_support as w
from memory import maintenance

pytestmark = [pytest.mark.unit]

CASA_CORE = w.CODE_ROOT / "casa_core.py"


# --- casa-main, for real --------------------------------------------------------

_MAIN = """
import sys, json, asyncio
sys.path.insert(0, {code!r})
opens, binds = [], []
_WATCH = ("/data", "/config", "/root/.claude", "/run/casa")
def _audit(event, args):
    if event == "open" and isinstance(args[0], str) and args[0].startswith(_WATCH):
        opens.append(args[0])
    elif event == "socket.bind":
        binds.append(str(args[1]))
sys.addaudithook(_audit)
import casa_core, supervisor_restart
from memory import maintenance as m
m.INTENT_PATH = {intent!r}
m.container_id = lambda proc_root="/proc": {cid!r}
sends = []
async def _send(text):
    sends.append(text)
    with open({sent!r}, "a") as fh:
        fh.write(text + "\\n")
    return True
m.send_operator_line = _send
restarts = []
async def _restart():
    restarts.append(1)
    return {{"supervisor_status": 200}}
supervisor_restart.request_app_restart = _restart
casa_core.verify_effective_cli = lambda: "0.0.0 (stub)"
{prelude}
asyncio.run(casa_core.main())
print("RESULT " + json.dumps({{"opens": opens, "binds": binds,
      "restarts": len(restarts),
      "sends": len(sends)}}))
"""


_RUNS = [0]


def run_main(tmp: Path, intent: dict | bytes | None, *, cid: str,
             prelude: str = "") -> tuple[dict, list[str]]:
    """``casa_core.main()`` in its own process until it idles, then SIGTERM."""
    path = tmp / "memory" / "maintenance.json"
    if isinstance(intent, bytes):
        w.write(path, intent)
    elif intent is not None:
        w.write(path, json.dumps(intent))
    _RUNS[0] += 1
    sent = tmp / f"sent-{_RUNS[0]}.txt"
    script = _MAIN.format(code=str(w.CODE_ROOT), intent=str(path), cid=cid,
                          prelude=prelude,
                          sent=str(sent))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    child = subprocess.Popen([sys.executable, "-c", script], env=env, cwd="/",
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and child.poll() is None:
            if "Casa is not taking messages" in _peek(child):
                break
            time.sleep(0.05)
        time.sleep(0.2)
        assert child.poll() is None, "casa-main exited instead of idling"
        child.send_signal(signal.SIGTERM)
        out, _err = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    assert child.returncode == 0, _err
    result = json.loads(out.split("RESULT ", 1)[1])
    lines = sent.read_text().splitlines() if sent.exists() else []
    return result, lines


_STDOUT: dict[int, str] = {}


def _peek(child: subprocess.Popen) -> str:
    """The child's stdout so far (logs go there), without blocking."""
    import selectors
    sel = selectors.DefaultSelector()
    sel.register(child.stdout, selectors.EVENT_READ)
    buf = _STDOUT.setdefault(child.pid, "")
    while sel.select(timeout=0):
        chunk = os.read(child.stdout.fileno(), 65536).decode()
        if not chunk:
            break
        buf += chunk
    _STDOUT[child.pid] = buf
    return buf


def _attempted(error=None, *, kind="wipe", cid="c1"):
    return {"kind": kind, "requested_at": "2026-11-02T10:00:00Z",
            "attempts": [{"container_id": cid, "at": "t", "error": error}],
            "notified_container_ids": []}


def test_a_recorded_attempt_puts_casa_main_in_notification_only_mode(tmp_path):
    result, lines = run_main(tmp_path, _attempted(), cid="c2")
    assert lines == [maintenance.INTERRUPTED_LINES["wipe"]]
    assert result == {"opens": [], "binds": [], "restarts": 0,
                      "sends": 1}
    intent = json.loads((tmp_path / "memory" / "maintenance.json").read_text())
    assert intent["notified_container_ids"] == ["c2"]


def test_the_line_carries_the_error_or_the_fixed_interrupted_literal(tmp_path):
    reason = "no wipe rule for 'spool'"
    _, lines = run_main(tmp_path / "a", _attempted(reason), cid="c2")
    assert lines == [
        f"The memory wipe failed at startup ({reason}). I'm not taking "
        "messages until it succeeds; restart the app to retry."]
    _, lines = run_main(tmp_path / "b", _attempted(kind="restore"), cid="c2")
    assert lines == ["The restore was interrupted before it finished; restart "
                     "the app to retry."]
    _, lines = run_main(tmp_path / "c", _attempted(), cid="c2")
    assert lines == ["The memory wipe was interrupted before it finished; "
                     "restart the app to retry."]


def test_one_line_per_container_start(tmp_path):
    _, first = run_main(tmp_path, _attempted(), cid="c2")
    _, respawn = run_main(tmp_path, None, cid="c2")          # same container
    _, restart = run_main(tmp_path, None, cid="c3")          # a new start
    assert (len(first), len(respawn), len(restart)) == (1, 0, 1)


def test_no_attempt_boots_normally_and_requests_the_restart_again():
    """Read from ``main``'s own code: the check returns only on a recorded
    attempt, and a pending intent with none asks for the restart once the
    channels are up. (A full normal boot is out of reach of a unit test.)"""
    fn = _main_node(CASA_CORE.read_text(encoding="utf-8"))
    calls = _calls_in_order(fn)
    assert calls.index("notification_only") < calls.index("start_all") < (
        calls.index("request_restart"))
    guard = next(n for n in ast.walk(fn) if isinstance(n, ast.If)
                 and any(isinstance(c, ast.Call) and _name(c) ==
                         "notification_only" for c in ast.walk(n)))
    assert "attempts" in ast.unparse(guard.test)
    assert isinstance(guard.body[-1], ast.Return)


# --- the order in casa_core.main --------------------------------------------------

#: Everything that reads memory or lets anything run, which must come after
#: the intent check.
CONSUMERS = ("_read_plugin_env", "MemoryStore", "start_all",
             "start_internal_unix_runner", "AppRunner", "start_agent_loop")


def _name(call: ast.Call) -> str:
    f = call.func
    return getattr(f, "attr", None) or getattr(f, "id", None) or ""


def _main_node(source: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(source)
    return next(n for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")


def _calls_in_order(fn: ast.AST) -> list[str]:
    """Call names in source order; the intent reader counts where it is
    named, since ``main`` hands it to a thread rather than calling it."""
    marks = [(n.lineno, n.col_offset, _name(n)) for n in ast.walk(fn)
             if isinstance(n, ast.Call)]
    marks += [(n.lineno, n.col_offset, n.attr) for n in ast.walk(fn)
              if isinstance(n, ast.Attribute) and n.attr == "read_intent"]
    return [name for *_pos, name in sorted(marks)]


def order_violations(source: str) -> list[str]:
    calls = _calls_in_order(_main_node(source))
    if "read_intent" not in calls:
        return ["no intent check"]
    check = calls.index("read_intent")
    out = []
    if calls.index("_resolve_password_options") > check:
        out.append("before option and secret resolution")
    out += [f"after {c}" for c in CONSUMERS if c in calls
            and calls.index(c) < check]
    return out


def test_the_check_comes_before_every_memory_consumer():
    source = CASA_CORE.read_text(encoding="utf-8")
    calls = _calls_in_order(_main_node(source))
    assert all(c in calls for c in CONSUMERS), [
        c for c in CONSUMERS if c not in calls]
    assert order_violations(source) == []


def test_the_order_check_reports_a_check_moved_after_start_all():
    source = CASA_CORE.read_text(encoding="utf-8")
    moved = source.replace(
        "memory_maintenance.read_intent)", "memory_maintenance.read_intent_x)"
    ).replace("    await channel_manager.start_all()\n",
              "    await channel_manager.start_all()\n"
              "    memory_maintenance.read_intent()\n", 1)
    assert "after start_all" in order_violations(moved)


# --- the notification order, with real kills --------------------------------------

def _notify_child(tmp: Path, *, kill: str | None) -> subprocess.CompletedProcess:
    hook = {
        None: "",
        "after_write": "_real = m.write_intent\n"
                       "def _wi(*a, **k):\n    _real(*a, **k)\n    _die()\n"
                       "m.write_intent = _wi\n",
        "before_write": "def _wi(*a, **k):\n    _die()\n"
                        "m.write_intent = _wi\n",
    }[kill]
    return w.run_child("\n".join([
        "import asyncio",
        "from memory import maintenance as m",
        f"m.INTENT_PATH = {str(tmp / 'memory' / 'maintenance.json')!r}",
        "m.container_id = lambda proc_root='/proc': 'c2'",
        "async def _send(text):",
        f"    open({str(tmp / 'sent.txt')!r}, 'a').write(text + '\\n')",
        "m.send_operator_line = _send",
        hook,
        "intent = m.read_intent()",
        "stop = asyncio.Event(); stop.set()",
        "asyncio.run(m.notification_only(intent, stop=stop))",
    ]))


def _sent(tmp: Path) -> int:
    p = tmp / "sent.txt"
    return len(p.read_text().splitlines()) if p.exists() else 0


@pytest.mark.parametrize(("kill", "sends_after_respawn"),
                         [("after_write", 0), ("before_write", 1)])
def test_the_container_is_recorded_before_the_line_is_sent(
        tmp_path, kill, sends_after_respawn):
    w.write(tmp_path / "memory" / "maintenance.json",
            json.dumps(_attempted()))
    assert w.killed(_notify_child(tmp_path, kill=kill))
    assert _sent(tmp_path) == 0
    assert _notify_child(tmp_path, kill=None).returncode == 0
    assert _sent(tmp_path) == sends_after_respawn


# --- attempts, from the step itself --------------------------------------------------

def test_a_step_killed_after_its_first_deletion_leaves_its_attempt(tmp_path):
    """The attempt is written before the first deletion: a run killed right
    after it deleted something leaves an attempt with no error, which is
    notification-only with the interrupted line."""
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    w.write(w.under(root, "/data/memory/memory.db"), "a store")
    w.write(w.under(root, "/data/sessions.json"), '{"k": 1}')
    assert maintenance.write_wipe_intent(str(root)) == "written"
    p = w.run_oneshot(root, proc, kill_at="_remove")
    assert w.killed(p)
    assert not w.under(root, "/data/memory/memory.db").exists()
    intent = maintenance.read_intent(str(root))
    assert [a["error"] for a in intent["attempts"]] == [None]
    assert intent["attempts"][0]["container_id"] == w.container_id_of(proc)
    assert maintenance.notification_line(intent) == (
        maintenance.INTERRUPTED_LINES["wipe"])


def test_an_earlier_containers_attempt_alone_is_still_an_attempt(tmp_path):
    """H3 (Astra S2): container C1's step records its attempt, deletes
    memory.db and is killed; C2's step dies before it appends its own. C2's
    casa-main must not boot over the partial wipe."""
    root = tmp_path / "root"
    c1 = w.fake_proc(tmp_path / "c1", start=100)
    c2 = w.fake_proc(tmp_path / "c2", start=200)
    w.write(w.under(root, "/data/memory/memory.db"), "a store")
    assert maintenance.write_wipe_intent(str(root)) == "written"
    assert w.killed(w.run_oneshot(root, c1, kill_at="_remove"))
    intent = maintenance.read_intent(str(root))
    ids = [a["container_id"] for a in intent["attempts"]]
    assert ids == [w.container_id_of(c1)]
    assert w.container_id_of(c2) not in ids
    result, lines = run_main(tmp_path / "main", intent, cid=w.container_id_of(c2))
    assert lines == [maintenance.INTERRUPTED_LINES["wipe"]]
    assert result["restarts"] == 0


def test_container_id_tracks_pid_1s_start_and_the_boot():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        a = maintenance.container_id(str(w.fake_proc(base / "a", start=5)))
        again = maintenance.container_id(str(w.fake_proc(base / "b", start=5)))
        later = maintenance.container_id(str(w.fake_proc(base / "c", start=6)))
        reboot = maintenance.container_id(str(w.fake_proc(
            base / "d", start=5, boot_id="c0ffee00-0000-4000-8000-0000000000ff")))
    assert a == again and len({a, later, reboot}) == 3


def test_an_unreadable_intent_is_an_attempt_that_deletes_nothing(tmp_path):
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    w.write(w.under(root, "/data/memory/memory.db"), "a store")
    w.write(w.under(root, "/data/memory/maintenance.json"), "{not json")
    assert w.run_oneshot(root, proc).stdout.strip() == "failed"
    assert w.under(root, "/data/memory/memory.db").exists()
    intent = maintenance.read_intent(str(root))
    assert intent["attempts"][-1]["error"] == maintenance.UNREADABLE_REASON
    # A restart cannot fix it, so the line names the one action that does.
    expected = ("Memory maintenance could not read its record, so nothing was "
                "deleted. Remove /data/memory/maintenance.json, then restart "
                "the app.")
    assert maintenance.notification_line(intent) == expected
    # the next container start deletes nothing either, and says the same
    assert w.run_oneshot(root, proc).stdout.strip() == "failed"
    assert w.under(root, "/data/memory/memory.db").exists()
    intent = maintenance.read_intent(str(root))
    assert [a["error"] for a in intent["attempts"]] == [
        maintenance.UNREADABLE_REASON] * 2
    assert maintenance.notification_line(intent) == expected
    # casa-main finding the file unreadable itself sends the same line
    assert maintenance.notification_line(maintenance.unreadable_intent()) == (
        expected)


# --- 9g: the services that start without casa-main --------------------------------

# --- 9g: the projects migration while an intent exists -------------------------------

def _migration_block(script: str, root: Path) -> str:
    start = script.index('PERSIST_PROJECTS="$CONFIG_DIR/cc-home/.claude/projects"')
    end = script.index("\nfi\n", start) + 4
    return (f'DATA_DIR={str(root / "data")!r}\nCONFIG_DIR={str(root / "config")!r}\n'
            + script[start:end].replace("/root/.claude", str(root / "root/.claude")))


def _run_migration(root: Path, script: str) -> None:
    subprocess.run(["/bin/bash", "-c", _migration_block(script, root)],
                   check=True, capture_output=True, text=True)


SETUP_CONFIGS = (w.REPO / "casa/rootfs/etc/s6-overlay/scripts/setup-configs.sh")


@pytest.mark.parametrize("pending", [True, False])
def test_setup_configs_leaves_a_real_projects_dir_alone_while_pending(
        tmp_path, pending):
    canary = "d1" + os.urandom(8).hex()
    w.write(tmp_path / "root/.claude/projects/-root/t.jsonl", canary)
    if pending:
        w.write(tmp_path / "data/memory/maintenance.json",
                json.dumps(_attempted("the step failed")))
    _run_migration(tmp_path, SETUP_CONFIGS.read_text(encoding="utf-8"))
    cc_home = tmp_path / "config/cc-home"
    real = tmp_path / "root/.claude/projects"
    if pending:
        assert w.hits(cc_home, [canary]) == []
        assert real.is_dir() and not real.is_symlink()
        assert w.hits(real, [canary]) == ["-root/t.jsonl"]
    else:          # no intent: the migration runs as it always has
        assert w.hits(cc_home, [canary]) == [".claude/projects/-root/t.jsonl"]
        assert real.is_symlink()


def test_without_the_guard_the_migration_copies_during_a_pending_wipe(tmp_path):
    canary = "d2" + os.urandom(8).hex()
    w.write(tmp_path / "root/.claude/projects/-root/t.jsonl", canary)
    w.write(tmp_path / "data/memory/maintenance.json", json.dumps(_attempted()))
    script = SETUP_CONFIGS.read_text(encoding="utf-8").replace(
        'if [ ! -e "$DATA_DIR/memory/maintenance.json" ]; then',
        "if true; then")
    _run_migration(tmp_path, script)
    assert w.hits(tmp_path / "config/cc-home", [canary]) != []


INVALID_UTF8 = b'{"kind": "wipe", "requested_at": "\xff", "attempts": []}'


def test_an_intent_that_is_not_utf8_is_unreadable_in_the_step(tmp_path):
    """r1 Astra S1: a byte that does not decode is an unreadable record,
    not a crash: the step deletes nothing and records why."""
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    w.write(w.under(root, "/data/memory/memory.db"), "a store")
    w.write(w.under(root, "/data/memory/maintenance.json"), INVALID_UTF8)
    p = w.run_oneshot(root, proc)
    assert (p.returncode, p.stdout.strip()) == (0, "failed"), p.stderr
    assert w.under(root, "/data/memory/memory.db").exists()
    intent = maintenance.read_intent(str(root))
    assert intent["attempts"][-1]["error"] == maintenance.UNREADABLE_REASON


def test_casa_main_meeting_an_intent_that_is_not_utf8_tells_and_waits(tmp_path):
    """The real startup check, on the undecodable file itself: notification
    only, with the line that names the fix, on every start."""
    result, lines = run_main(tmp_path, INVALID_UTF8, cid="c2")
    assert lines == ["Memory maintenance could not read its record, so "
                     "nothing was deleted. Remove /data/memory/maintenance.json, "
                     "then restart the app."]
    assert result == {"opens": [], "binds": [], "restarts": 0,
                      "sends": 1}
    _, again = run_main(tmp_path, None, cid="c3")
    assert len(again) == 1


#: A file-size limit for the child only, set just before main() runs: every
#: write past one byte fails with EFBIG (the signal is ignored, as a
#: process that writes would see it), so the intent cannot be rewritten.
EFBIG = """
import resource, signal
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (1, 1))
"""


def test_a_failing_bookkeeping_write_never_crashes_notification_only(tmp_path):
    """r5 Astra S1: after a recorded failed wipe, casa-main cannot record
    that it notified (EFBIG). It must not crash into a respawn loop: it logs,
    sends nothing it could not record, and idles; the intent is kept, and
    the next start does the same."""
    intent = _attempted("the step failed")
    path = tmp_path / "memory" / "maintenance.json"
    for start in ("c2", "c3"):
        result, lines = run_main(tmp_path, intent if start == "c2" else None,
                                 cid=start, prelude=EFBIG)
        assert lines == []
        assert result == {"opens": [], "binds": [], "restarts": 0, "sends": 0}
    kept = json.loads(path.read_text())
    assert kept["attempts"] == intent["attempts"]
    assert kept["notified_container_ids"] == []


def test_an_intent_path_that_cannot_be_read_never_boots_normally(tmp_path):
    """A record that exists but cannot be opened (here a directory where the
    file should be) is not a normal boot and not a crash: casa-main idles in
    notification-only mode."""
    (tmp_path / "memory" / "maintenance.json").mkdir(parents=True)
    result, lines = run_main(tmp_path, None, cid="c2")
    assert lines == []
    assert result == {"opens": [], "binds": [], "restarts": 0,
                      "sends": 0}
