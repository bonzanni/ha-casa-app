"""#613 — the restart probe's M-9 step, and the boot window it stumbled on.

`test-local/e2e/test_mcp_restart_survival.sh` needs Docker and s6 and runs only
in the scheduled hardening tier, so nothing in the unit suite has ever executed
any of it. That is why M-9 could fail a CORRECT product run for months: the step
re-polls one immutable response file for a `"result"` that the product will
never write, because a redelivered turn's single tool call can legitimately be
answered with the designed retryable ``-32000 casa_temporarily_unavailable``
(``svc_casa_mcp.py``'s cold-boot window) and neither Casa nor the CLI re-issues
it.

The tests here run the REAL M-9 block — extracted from the real script by
anchors, with only the container I/O replaced — against response sets the test
writes. No Docker, no s6, no network.

They do NOT and cannot decide whether a given boot actually lands a call inside
the window: that is s6 spawn latency against casa-main's remaining boot work.
Nothing below asserts it.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "test-local" / "e2e" / "test_mcp_restart_survival.sh"
CASA_CORE = REPO / "casa" / "rootfs" / "opt" / "casa" / "casa_core.py"
BRIDGE = REPO / "casa" / "rootfs" / "opt" / "casa" / "svc_casa_mcp.py"

# The exact message the bridge returns when it cannot reach casa-main's socket.
# Kept as a literal HERE as well as in the probe and the bridge on purpose: the
# probe classifies on the whole message, not on the -32000 code, because the
# bridge answers -32000 for forwarding errors and timeouts too and neither of
# those justifies "the call never reached casa-main".
SOCKET_UNREACHABLE = (
    "casa_temporarily_unavailable: casa-main internal socket unreachable")


def _region(text: str, start_re: str, end_re: str, *, include_end: bool) -> str:
    """The lines of `text` from the line matching `start_re` to `end_re`.

    Anchored on lines that exist both before and after this change, so the
    extraction itself cannot be what makes an arm red.
    """
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(start_re, ln)), None)
    assert start is not None, f"anchor {start_re!r} not found in {PROBE}"
    end = next((i for i in range(start + 1, len(lines))
                if re.match(end_re, lines[i])), None)
    assert end is not None, f"anchor {end_re!r} not found after {start_re!r}"
    return "\n".join(lines[start:end + 1 if include_end else end])


def _helpers_and_m9() -> tuple[str, str]:
    text = PROBE.read_text(encoding="utf-8")
    helpers = _region(text, r"^in_c\(\) \{", r'^echo "=== M-8', include_end=False)
    m9 = _region(text, r'^echo "=== M-9', r'^pass "M-9 ', include_end=True)
    return helpers, m9


# --------------------------------------------------------------------------
# The harness: the REAL block, container I/O replaced, clock virtualised
# --------------------------------------------------------------------------

_FAKE_DOCKER = '''\
"""The container, played well enough that the block under test cannot tell.

The injection decoder is deliberately NOT a regex over the `docker exec` argv:
shell quoting and `printf` interpretation are distinct operations, and matching
the argv text captured the printf format's own trailing newline and quote as
part of the nonce. What the mock CLI parses is the LINE that reaches the FIFO,
so this reproduces exactly that — `shlex` for the quoting, a real `printf` for
the format (executed with the parsed words as data, and with the redirection
removed, so no FIFO is ever opened), then the mock CLI's own parsing statements.
"""
import json, os, shlex, subprocess, sys


def decode_injection(command):
    """The (tool, token) pairs the mock CLI would parse from `command`."""
    words = shlex.split(command)
    assert words and words[0] == "printf", ("unsupported injection", words)
    assert len(words) > 3 and words[-2] == ">", ("unsupported injection", words)
    assert words[-1].endswith("stdin.fifo"), ("unsupported injection", words)
    text = subprocess.run(
        ["sh", "-c", 'printf "$@"', "m9-printf", *words[1:-2]],
        capture_output=True, text=True, check=True,
    ).stdout
    calls = []
    for line in text.splitlines():
        text_ = line.rstrip("\\n")
        if text_.startswith("/mock casa_call"):
            tool, _, token = text_[len("/mock casa_call"):].strip().partition(" ")
            calls.append([tool.strip() or "list_engagement_workspaces",
                          token.strip()])
    return calls


args = sys.argv[1:]
if args[:1] == ["--decode"]:
    print(json.dumps(decode_injection(args[1])))
    sys.exit(0)

cfg = json.load(open(os.environ["M9_CFG"]))
ws = cfg["ws"]


def materialise(entries, token=""):
    for e in entries:
        body = dict(e["body"])
        if e.get("token") == "MATCH":
            body["_probe_token"] = token
        elif e.get("token"):
            body["_probe_token"] = e["token"]
        raw = e["raw"] if "raw" in e else json.dumps(body)
        with open(os.path.join(ws, e["name"]), "w") as fh:
            fh.write(raw)


if args[:1] != ["exec"]:
    sys.exit(0)
rest = args[2:]                                  # drop `exec <container>`
if rest[:1] == ["test"]:
    sys.exit(0 if cfg.get("socket_ready", True) else 1)   # `test -S <socket>`
if rest[:1] == ["s6-rc"] and "-u" in rest:
    materialise(cfg.get("on_restart", []))
    sys.exit(0)
if rest[:2] == ["sh", "-c"] and "stdin.fifo" in rest[2]:
    calls = decode_injection(rest[2])
    assert len(calls) == 1, ("expected exactly one turn per injection", calls)
    token = calls[0][1]
    with open(cfg["injections"], "a") as fh:
        fh.write(token + "\\n")
    materialise(cfg.get("on_inject", []), token)
    sys.exit(0)
sys.exit(0)
'''

_STUBS = r'''
# ---- harness stubs: ONLY container I/O, the clock, and the report sinks ----
in_c() {
    local a=() x
    for x in "$@"; do a+=("${x//\/data\/engagements\/$EID/$WS}"); done
    "${a[@]}"
}
fail() { echo "HARNESS-FAIL: $*" >&2; exit 9; }
pass() { echo "HARNESS-PASS: $*"; }
wait_for_text_in_log() { echo "HARNESS-LOGWAIT: $2"; return 0; }
# A VIRTUAL clock, so the block's real timeouts elapse without wall time and
# without changing a single one of its own numbers.
date() {
    if [ "${1:-}" = "+%s" ]; then cat "$CLOCK"; else command date "$@"; fi
}
sleep() { local n="${1:-1}"; echo $(( $(cat "$CLOCK") + ${n%.*} )) > "$CLOCK"; }
'''

ERR_SOCKET = {"jsonrpc": "2.0", "id": 1,
              "error": {"code": -32000, "message": SOCKET_UNREACHABLE}}
ERR_TIMEOUT = {"jsonrpc": "2.0", "id": 1,
               "error": {"code": -32000,
                         "message": "casa_temporarily_unavailable: "
                                    "forwarding timeout"}}
ERR_UNGRANTED = {"jsonrpc": "2.0", "id": 1,
                 "error": {"code": -32004, "message": "tool_not_granted"}}
ERR_NOT_LIVE = {"jsonrpc": "2.0", "id": 1,
                "error": {"code": -32006, "message": "engagement_not_live"}}
MOCK_ERROR = {"mock_error": "URLError: connection refused"}

EID = "e1e2e3e4-0000-4000-8000-000000000001"


def _own_workspace() -> dict:
    payload = {"workspaces": [{"engagement_id": EID}]}
    return {"jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text",
                                    "text": json.dumps(payload)}]}}


def _f(name: str, body: dict, token: str | None = None) -> dict:
    e: dict = {"name": name, "body": body}
    if token:
        e["token"] = token
    return e



def _fake_docker_dir(tmp_path) -> Path:
    """Write the fake container onto a directory fit for `PATH`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text("#!" + sys.executable + "\n" + _FAKE_DOCKER,
                      encoding="utf-8")
    docker.chmod(0o755)
    return bindir


def decode_injection(tmp_path, command: str) -> list[list[str]]:
    """Ask the fake container what the mock CLI would parse from `command`."""
    docker = _fake_docker_dir(tmp_path) / "docker"
    proc = subprocess.run([str(docker), "--decode", command],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class Run:
    def __init__(self, proc, injections: list[str], trace: str = ""):
        self.rc = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr
        self.injections = injections
        self.trace = trace

    @property
    def probe_nonce(self) -> str | None:
        """The nonce the PROBE generated, read out of bash's own xtrace.

        Independent of the fake container's decoder on purpose: the point of the
        comparison is that the token bash expanded and the token the container
        parsed are the same bytes.
        """
        hits = [ln.split("M9_TOKEN=", 1)[1].strip()
                for ln in self.trace.splitlines() if "M9_TOKEN=" in ln]
        return hits[0] if hits else None

    @property
    def m9_passes(self) -> int:
        return len([ln for ln in self.out.splitlines()
                    if ln.startswith("HARNESS-PASS: M-9 ")])

    @property
    def failures(self) -> int:
        # A substring count, not a line prefix: `assert_own_workspace` cats the
        # offending file to stderr and that file has no trailing newline, so the
        # failure lands at the END of that line.
        return self.err.count("HARNESS-FAIL:")

    def __repr__(self) -> str:
        return (f"rc={self.rc} m9_passes={self.m9_passes} "
                f"failures={self.failures} injections={self.injections}\n"
                f"--- stdout ---\n{self.out}\n--- stderr ---\n{self.err}")


def run_m9(tmp_path, *, on_restart, on_inject=(), socket_ready=True) -> Run:
    """Run the real M-9 block against a scripted response set."""
    helpers, m9 = _helpers_and_m9()
    ws = tmp_path / "workspace"
    ws.mkdir()
    bindir = _fake_docker_dir(tmp_path)
    injections = tmp_path / "injections.log"
    injections.write_text("", encoding="utf-8")
    clock = tmp_path / "clock"
    clock.write_text("1000\n", encoding="utf-8")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"ws": str(ws),
                               "injections": str(injections),
                               "socket_ready": socket_ready,
                               "on_restart": list(on_restart),
                               "on_inject": list(on_inject)}),
                   encoding="utf-8")

    trace = tmp_path / "xtrace"
    script = "\n".join([
        "set -euo pipefail",
        # bash's OWN trace of every expansion, on its own descriptor. It is how
        # the test learns the nonce the probe generated without asking the fake
        # container to tell it — the two must agree.
        f'exec 9>"{trace}"', "BASH_XTRACEFD=9", "set -x",
        'NAME="harness"', f'EID="{EID}"', f'WS="{ws}"', f'CLOCK="{clock}"',
        helpers, _STUBS, m9,
    ])
    script_path = tmp_path / "m9.sh"
    script_path.write_text(script, encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}" + env.get("PATH", "")
    env["M9_CFG"] = str(cfg)
    proc = subprocess.run(["bash", str(script_path)], capture_output=True,
                          text=True, env=env, timeout=300)
    return Run(proc,
               [ln for ln in
                injections.read_text(encoding="utf-8").splitlines() if ln],
               trace.read_text(encoding="utf-8", errors="replace"))


# --------------------------------------------------------------------------
# The red case: a CORRECT product run must not fail M-9
# --------------------------------------------------------------------------

def test_cold_boot_window_answer_does_not_fail_a_correct_run(tmp_path):
    """#613 — the red case.

    The redelivered turn's one tool call is answered with exactly the bridge's
    socket-unreachable ``-32000``; that file never changes, because the mock CLI
    records it and Casa never re-issues a tool call once ``turn_start`` has
    consumed the envelope. M-9 must not read that as a product failure — it must
    inject a nonce-bound turn now that the socket is serving and require THAT to
    dispatch as the caller's own workspace.

    At the base the block re-reads the error ten times and dies in
    ``assert_own_workspace``'s ``assert "error" not in d``: zero passes, one
    failure, no injection.
    """
    r = run_m9(tmp_path,
               on_restart=[_f(".mock_casa_call.000.json", ERR_SOCKET)],
               on_inject=[_f(".mock_casa_call.001.json", _own_workspace(),
                             token="MATCH")])
    assert r.rc == 0, r
    assert r.m9_passes == 1, r
    assert r.failures == 0, r
    assert len(r.injections) == 1, r
    # The token bash expanded and the token the container parsed are the same
    # bytes. Comparing against a token the fake reconstructed would prove
    # nothing: an extraction that mangles the nonce mangles both sides.
    assert r.probe_nonce, r
    assert r.injections == [r.probe_nonce], r


def test_dispatched_redelivered_turn_still_passes_without_any_injection(tmp_path):
    """The unchanged arm: the redelivered turn's own call dispatched."""
    r = run_m9(tmp_path,
               on_restart=[_f(".mock_casa_call.000.json", _own_workspace())])
    assert r.rc == 0, r
    assert r.m9_passes == 1, r
    assert r.injections == [], r


@pytest.mark.parametrize("body,label", [
    (ERR_UNGRANTED, "-32004 tool_not_granted"),
    (ERR_NOT_LIVE, "-32006 engagement_not_live"),
    (ERR_TIMEOUT, "-32000 with a message that is NOT socket-unreachable"),
    (MOCK_ERROR, "a transport failure recorded by the CLI"),
])
def test_any_other_recorded_answer_still_fails_m9(tmp_path, body, label):
    """The tolerance is exactly one message wide.

    ``#588``'s regression (``-32004``) and ``-32006`` must still redden the
    step, and so must the bridge's OTHER ``-32000`` shapes: a forwarding error
    or timeout does not justify "the call never reached casa-main".
    """
    r = run_m9(tmp_path, on_restart=[_f(".mock_casa_call.000.json", body)])
    assert r.rc == 9, (label, r)
    assert r.m9_passes == 0, (label, r)
    assert r.failures == 1, (label, r)
    assert r.injections == [], (label, r)


def test_unparseable_response_fails_m9(tmp_path):
    r = run_m9(tmp_path, on_restart=[{"name": ".mock_casa_call.000.json",
                                      "body": {}, "raw": "{not json"}])
    assert r.rc == 9, r
    assert r.m9_passes == 0, r


def test_window_arm_reads_its_own_nonce_not_the_newest_file(tmp_path):
    """A refused nonce turn beside a stranger's success must still fail.

    Written newest-last on purpose: replacing the nonce wait with
    ``casa_call_files | tail -1`` would read the stranger's dispatched result
    and pass. The step must read the answer to the turn IT injected.
    """
    r = run_m9(tmp_path,
               on_restart=[_f(".mock_casa_call.000.json", ERR_SOCKET)],
               on_inject=[_f(".mock_casa_call.001.json", ERR_NOT_LIVE,
                             token="MATCH"),
                          _f(".mock_casa_call.002.json", _own_workspace(),
                             token="somebody-elses-turn")])
    assert r.rc == 9, r
    assert r.m9_passes == 0, r
    assert len(r.injections) == 1, r
    # It must fail ON THE REFUSAL IT SELECTED, not by timing out having found
    # nothing: the failure is `assert_own_workspace`'s, and the refused file it
    # prints is the -32006 one that carried this turn's nonce.
    assert "M-9 expected a dispatched result listing only this engagement" \
        in r.err, r
    assert "-32006" in r.err, r


# --------------------------------------------------------------------------
# The boot window itself: measured, not asserted away
# --------------------------------------------------------------------------

def _await_line(func: ast.AST, name: str) -> int | None:
    """The line of the single awaited call to `name` inside `func`, or None."""
    hits = [n.lineno for n in ast.walk(func)
            if isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == name]
    assert len(hits) <= 1, (name, hits)
    return hits[0] if hits else None


def _main_awaits() -> tuple[int | None, int | None]:
    tree = ast.parse(CASA_CORE.read_text(encoding="utf-8"))
    main = next(n for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "main")
    return (_await_line(main, "replay_undergoing_engagements"),
            _await_line(main, "start_internal_unix_runner"))


def test_probe_classifies_on_the_message_the_bridge_actually_returns():
    """The probe's window arm turns on ONE message; drift would silence it.

    ``svc_casa_mcp`` answers ``-32000 casa_temporarily_unavailable`` for three
    distinct causes, and only the socket-unreachable one means the call never
    reached casa-main. The probe therefore matches the whole message — so the
    literal in the probe and the literal in the bridge must be the same bytes.
    """
    bridge = BRIDGE.read_text(encoding="utf-8")
    probe = PROBE.read_text(encoding="utf-8")
    assert '"casa_temporarily_unavailable: "\n' \
           '                    "casa-main internal socket unreachable"' in bridge
    assert SOCKET_UNREACHABLE in probe, (
        "the M-9 classifier no longer carries the bridge's socket-unreachable "
        "message verbatim")


def test_boot_await_order_is_measured_and_its_documented_consequence_holds():
    """A measurement of ``casa_core.main``, and the one claim it licenses.

    No test binds this ordering today, which is why #613's window survived
    sixteen green scheduled nights unmeasured. This is deliberately NOT a pin on
    the order: a reorder that creates the socket first would be a correct change
    and must not read here as a regression. What is asserted is the CONSEQUENCE
    that holds *while* the replay is awaited first — the bridge must keep
    documenting the cold-boot window it creates. If the order ever flips, this
    test records the observation and claims nothing about whether the window is
    closed; only running the probe in a container could show that.
    """
    replay, socket = _main_awaits()
    assert replay is not None, "main no longer awaits replay_undergoing_engagements"
    assert socket is not None, "main no longer awaits start_internal_unix_runner"
    if replay < socket:
        doc = ast.get_docstring(ast.parse(BRIDGE.read_text(encoding="utf-8")))
        assert doc is not None
        assert "Cold boot" in doc and "casa_temporarily_unavailable" in doc, (
            f"main awaits the boot replay at line {replay} and creates the "
            f"internal socket at line {socket}, so the cold-boot window is "
            "real; svc_casa_mcp must keep documenting it")


# --------------------------------------------------------------------------
# The harness's own decoder — the seam through which a nonce could be mangled
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,expected", [
    ("printf '/mock casa_call list_engagement_workspaces m9w-42-1000\\n' "
     "> /data/engagement-ctl/x/stdin.fifo",
     [["list_engagement_workspaces", "m9w-42-1000"]]),
    ("printf '/mock casa_call list_engagement_workspaces\\n' "
     "> /data/engagement-ctl/x/stdin.fifo",
     [["list_engagement_workspaces", ""]]),
    ("printf '%s\\n' '/mock casa_call list_engagement_workspaces m11-7-9' "
     "> /data/engagement-ctl/x/stdin.fifo",
     [["list_engagement_workspaces", "m11-7-9"]]),
])
def test_injection_decoder_reads_the_line_not_the_argv(tmp_path, command,
                                                       expected):
    """What reaches the FIFO is a LINE; the argv still holds a printf format.

    Matching the argv text captured the format's own trailing `\\n` and its
    closing quote as part of the nonce, and `wait_for_token_casa_call` then
    matched nothing. Shell quoting and `printf` interpretation are distinct
    operations and the decoder performs both.
    """
    assert decode_injection(tmp_path, command) == expected


def test_injection_decoder_refuses_a_form_it_cannot_reproduce(tmp_path):
    """An unsupported injection must fail loudly, never decode to nothing."""
    docker = _fake_docker_dir(tmp_path) / "docker"
    proc = subprocess.run(
        [str(docker), "--decode", "echo hello > /data/engagement-ctl/x/stdin.fifo"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0, proc.stdout
    assert "unsupported injection" in proc.stderr, proc.stderr


# --------------------------------------------------------------------------
# The window arm must not re-run the race it exists to excuse
# --------------------------------------------------------------------------
# Both added on terra's S2 finding in the gate-owned handback review of
# 97dfcd3b: the arm injected its replacement turn without establishing that the
# socket had begun serving, so a boot still incomplete after the preamble's
# bounded wait produced the cold-boot answer TWICE and failed a correct — merely
# slow — boot one indirection further down. The accepted red case above is
# unchanged; these are additional arms for a finding it did not cover.

def test_window_arm_refuses_to_inject_while_the_socket_is_not_serving(tmp_path):
    """A boot that has not finished serving is named, and nothing is injected."""
    r = run_m9(tmp_path, socket_ready=False,
               on_restart=[_f(".mock_casa_call.000.json", ERR_SOCKET)],
               on_inject=[_f(".mock_casa_call.001.json", _own_workspace(),
                             token="MATCH")])
    assert r.rc == 9, r
    assert r.m9_passes == 0, r
    assert r.injections == [], r
    assert "still not serving" in r.err, r


def test_window_arm_fails_if_the_injected_turn_also_takes_the_window_answer(
        tmp_path):
    """With the socket confirmed serving, a second cold answer is an anomaly.

    It must redden the step rather than be excused a second time: the tolerance
    is for ONE call that raced the socket, not for a bridge that cannot reach
    casa-main at all.
    """
    r = run_m9(tmp_path,
               on_restart=[_f(".mock_casa_call.000.json", ERR_SOCKET)],
               on_inject=[_f(".mock_casa_call.001.json", ERR_SOCKET,
                             token="MATCH")])
    assert r.rc == 9, r
    assert r.m9_passes == 0, r
    assert len(r.injections) == 1, r
    assert "M-9 expected a dispatched result listing only this engagement" \
        in r.err, r
