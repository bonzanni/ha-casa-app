"""#1248: the engagement service pair runs no program from the plugin tools
directory while it is still root.

`setup-configs.sh` prepends `/config/tools/bin` — where a plugin's
`verify_bin` executables are published, under any name — to the PATH of every
s6 service. The engagement run script and its log script run as root under
that PATH until the run script's final `exec setpriv …`, so a same-named file
there (`cat`, `mkdir`, `s6-log`, `setpriv`, ringlog's `bash`…) was what ran.

These tests run the REAL rendered scripts with the REAL bash/dash, against
real executables in directories that stand in for the plugin tools directory
(`tools/`, injected through `s6_rc.PLUGIN_TOOLS_BIN`) and for the image PATH
(`trusted/`). Every stub records one line and then forwards to the host's own
program, so choosing the wrong one shows up as a COUNT, never as a later
failure. The with-contenv interpreter lookup itself happens before any script
line runs and is only reproducible in the image; here it is pinned as text.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from drivers import s6_rc
from drivers.workspace import cli_model_flags, render_log_run_script, render_run_script

REPO_SCRIPTS = Path(__file__).resolve().parent.parent / "casa/rootfs/opt/casa/scripts"
TEMPLATE = REPO_SCRIPTS / "engagement_run_template.sh"
RINGLOG = REPO_SCRIPTS / "ringlog.sh"

BASH = "/bin/bash"
SH = "/bin/sh"
ENG_ID = "h1248000000000000000000000000000"
UID = 200005

# Programs the root phase of either script runs by name, and the host program
# each forwarding stub hands over to (resolved now, before any PATH is faked).
FORWARDED = ("cat", "mv", "rm", "dirname", "mkdir", "env", "bash")
REAL = {name: shutil.which(name) for name in FORWARDED}

pytestmark = pytest.mark.skipif(
    not all(REAL.values()) or not os.path.exists(BASH) or not os.path.exists(SH),
    reason="needs /bin/bash, /bin/sh and coreutils on the host")


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _forwarder(directory: Path, name: str, tag: str, log: Path) -> None:
    """Record `<tag> <name>` with shell builtins, then exec the host program."""
    _write_exec(directory / name,
                "#!/bin/sh\n"
                f'printf "%s %s\\n" "{tag}" "{name}" >> "{log}"\n'
                f'exec "{REAL[name]}" "$@"\n')


def _recorder(directory: Path, name: str, tag: str, log: Path) -> None:
    """Record `<tag> <name> PATH=<its PATH>` and succeed (no host program)."""
    _write_exec(directory / name,
                "#!/bin/sh\n"
                f'printf "%s %s PATH=%s\\n" "{tag}" "{name}" "$PATH" >> "{log}"\n'
                "exit 0\n")


def _fake_setpriv(directory: Path, tag: str, log: Path) -> None:
    """Record its PATH, drop setpriv's options through `--`, exec the rest."""
    _write_exec(directory / "setpriv",
                "#!/bin/sh\n"
                f'printf "%s %s PATH=%s\\n" "{tag}" setpriv "$PATH" >> "{log}"\n'
                'while [ "$#" -gt 0 ]; do a=$1; shift; [ "$a" = "--" ] && break; done\n'
                'exec "$@"\n')


def _populate(directory: Path, tag: str, log: Path) -> None:
    """Every program either script runs by name, recording under `tag`."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("cat", "mv", "rm", "dirname", "mkdir"):
        _forwarder(directory, name, tag, log)
    _recorder(directory, "s6-log", tag, log)
    _fake_setpriv(directory, tag, log)


def _lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def _count(log: Path, tag: str, name: str) -> int:
    return sum(1 for line in _lines(log)
               if line.split(" ", 2)[:2] == [tag, name])


def _observed_paths(log: Path, tag: str, name: str) -> list[str]:
    prefix = f"{tag} {name} PATH="
    return [line[len(prefix):] for line in _lines(log) if line.startswith(prefix)]


def _wait_ringlog_exit(marker: str, timeout: float = 10.0) -> None:
    # The exec'd child's exit does not wait for the process-substitution
    # consumer; poll until no ringlog naming this test's tmp dir remains.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = subprocess.run(["pgrep", "-f", f"ringlog.sh.*{marker}"],
                           capture_output=True)
        if r.returncode != 0:
            return
        time.sleep(0.05)
    raise AssertionError("ringlog did not exit in time")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """tools/ (the injected plugin tools dir) and trusted/ (the image PATH),
    both carrying every program; one shared invocation log."""
    log = tmp_path / "ran.log"
    tools, trusted = tmp_path / "tools", tmp_path / "trusted"
    _populate(tools, "shadow", log)
    _populate(trusted, "trusted", log)
    _recorder(trusted, "claude", "trusted", log)
    monkeypatch.setattr(s6_rc, "PLUGIN_TOOLS_BIN", str(tools))
    return tmp_path, tools, trusted, log


def _render_log_script(log_dir: Path) -> str:
    from drivers.workspace import engagement_log_dir
    script = render_log_run_script(engagement_id=ENG_ID)
    real_dir = engagement_log_dir(ENG_ID)
    assert script.count(real_dir) == 2, script
    return script.replace(real_dir, str(log_dir))


def _render_run_script(tmp_path: Path, final: str | None = None) -> Path:
    """The rendered run script with ONLY fixture paths substituted (workspace,
    control dir, ringlog, the stdin FIFO); `final`, when given, replaces the
    whole final invocation."""
    ws, ctl = tmp_path / "ws", tmp_path / "ctl"
    (ws / ".home").mkdir(parents=True, exist_ok=True)
    ctl.mkdir(parents=True, exist_ok=True)
    s = render_run_script(model="claude-sonnet-5", engagement_id=ENG_ID, permission_mode="acceptEdits",
                          extra_dirs=[], plugin_dirs=[], uid=UID, gid=UID)
    for old, new in (
            (f"/data/engagements/{ENG_ID}", str(ws)),
            (f'CTL="/data/engagement-ctl/{ENG_ID}"', f'CTL="{ctl}"'),
            ("/opt/casa/scripts/ringlog.sh", str(RINGLOG)),
            ('exec <"$CTL/stdin.fifo"', "exec </dev/null")):
        assert old in s, old
        s = s.replace(old, new)
    if final is not None:
        s = s[:s.index("exec setpriv")] + final + "\n"
    p = tmp_path / "run"
    p.write_text(s)
    return p


def _base_path(tools: Path, trusted: Path) -> str:
    return f"{tools}:{trusted}:/usr/bin:/bin"


# --- R1: the log script ------------------------------------------------------

def test_log_script_runs_mkdir_and_s6_log_from_outside_the_tools_dir(world):
    tmp_path, tools, trusted, log = world
    log_dir = tmp_path / "logs" / "casa-engagement-x"
    script = tmp_path / "log-run"
    script.write_text(_render_log_script(log_dir))

    r = subprocess.run([SH, str(script)], env={"PATH": _base_path(tools, trusted)},
                       cwd=tmp_path, capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    assert _count(log, "shadow", "mkdir") == 0, _lines(log)
    assert _count(log, "shadow", "s6-log") == 0, _lines(log)
    assert _count(log, "trusted", "mkdir") == 1, _lines(log)
    assert _count(log, "trusted", "s6-log") == 1, _lines(log)
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700


# --- R2: the run template's root phase, ringlog included -----------------------

def test_run_script_root_phase_and_ringlog_run_nothing_from_the_tools_dir(world):
    tmp_path, tools, trusted, log = world
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    (ctl / ".session_id").write_text("0123abcd-0123-4567-89ab-0123456789ab\n")
    (ctl / ".spawn_epoch").write_text("4\n")
    (ctl / ".stderr.1.log").write_text("old\n")
    (ctl / ".stderr.1.log.1").write_text("older\n")
    # One byte past ringlog's 65536 ceiling: exactly one rotation.
    script = _render_run_script(
        tmp_path, final="printf '%65537s' '' >&2\nexec true")

    r = subprocess.run([BASH, str(script)], env={"PATH": _base_path(tools, trusted)},
                       cwd=tmp_path, capture_output=True, text=True)
    _wait_ringlog_exit(str(ctl))

    assert r.returncode == 0, r.stderr
    for name in ("cat", "mv", "rm", "dirname"):
        assert _count(log, "shadow", name) == 0, (name, _lines(log))
    assert {n: _count(log, "trusted", n) for n in ("cat", "mv", "rm", "dirname")} \
        == {"cat": 6, "mv": 2, "rm": 2, "dirname": 1}, _lines(log)
    assert r.stdout.count('{"casa_control": "spawn", "epoch": 5}') == 1, r.stdout
    assert not (ctl / ".stderr.1.log").exists()
    assert not (ctl / ".stderr.1.log.1").exists()
    assert (ctl / ".stderr.5.log.1").stat().st_size == 65537


# --- R3: the final exec — setpriv from outside, the CLI with the inherited PATH --

def test_final_exec_resolves_setpriv_outside_and_hands_claude_the_inherited_path(world):
    tmp_path, tools, trusted, log = world
    script = _render_run_script(tmp_path)
    inherited = _base_path(tools, trusted)

    r = subprocess.run([BASH, str(script)], env={"PATH": inherited},
                       cwd=tmp_path, capture_output=True, text=True)
    _wait_ringlog_exit(str(tmp_path / "ctl"))

    assert r.returncode == 0, r.stderr
    assert _count(log, "shadow", "setpriv") == 0, _lines(log)
    assert _count(log, "trusted", "setpriv") == 1, _lines(log)
    assert _observed_paths(log, "trusted", "claude") == [inherited], _lines(log)
    [root_path] = _observed_paths(log, "trusted", "setpriv")
    entries = root_path.split(":")
    assert [e for e in entries
            if not e.startswith("/")
            or os.path.realpath(e) == os.path.realpath(tools)] == []


# --- R4 (fresh launch): the preflight resolves setpriv the way the script does --

def _setpriv_only_in_tools(tmp_path: Path, monkeypatch, *, also_trusted: bool):
    tools, trusted = tmp_path / "tools", tmp_path / "trusted"
    tools.mkdir()
    trusted.mkdir()
    log = tmp_path / "ran.log"
    _fake_setpriv(tools, "shadow", log)
    if also_trusted:
        _fake_setpriv(trusted, "trusted", log)
    monkeypatch.setattr(s6_rc, "PLUGIN_TOOLS_BIN", str(tools))
    monkeypatch.setenv("PATH", f"{tools}:{trusted}")


def _preflight_ready_record(tmp_path: Path, monkeypatch):
    """A record whose every OTHER uid-drop precondition holds for the
    unprivileged test process: its own uid owns the workspace and has a
    passwd entry, nothing private is exposed, no plugin dirs."""
    from drivers import claude_code_driver as ccd
    from engagement_registry import EngagementRecord
    monkeypatch.setattr(ccd, "UID_BASE", 0)
    monkeypatch.setattr(ccd.private_state, "credential_modes_ok", lambda: [])
    return EngagementRecord(
        id="h1248abcdef01234", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=999,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None,
        origin={"channel": "telegram", "chat_id": "42"}, task="say hello",
        allocated_uid=os.getuid())


def test_fresh_launch_preflight_refuses_a_setpriv_only_in_the_tools_dir(
        tmp_path, monkeypatch):
    from drivers import claude_code_driver as ccd
    _setpriv_only_in_tools(tmp_path, monkeypatch, also_trusted=False)
    rec = _preflight_ready_record(tmp_path, monkeypatch)

    with pytest.raises(ccd.UidDropRefused, match="setpriv"):
        ccd._preflight_uid_drop(rec, str(tmp_path))


def test_fresh_launch_preflight_passes_with_a_setpriv_outside_the_tools_dir(
        tmp_path, monkeypatch):
    """Positive control: the same fixture with a setpriv on the image PATH
    passes every check, so the refusal above is the setpriv lookup's."""
    from drivers import claude_code_driver as ccd
    _setpriv_only_in_tools(tmp_path, monkeypatch, also_trusted=True)
    rec = _preflight_ready_record(tmp_path, monkeypatch)

    ccd._preflight_uid_drop(rec, str(tmp_path))   # must not raise


# --- R5: absolute interpreters -------------------------------------------------

def test_the_three_interpreters_are_absolute():
    log_script = render_log_run_script(engagement_id=ENG_ID)
    firsts = [TEMPLATE.read_text().split("\n", 1)[0],
              log_script.split("\n", 1)[0],
              RINGLOG.read_text().split("\n", 1)[0]]
    assert firsts == ["#!/command/with-contenv /bin/bash",
                      "#!/command/with-contenv /bin/sh",
                      "#!/bin/bash"]


def test_ringlog_run_through_its_shebang_ignores_a_bash_in_the_tools_dir(world):
    tmp_path, tools, trusted, log = world
    _forwarder(tools, "bash", "shadow", log)
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    (ctl / ".spawn_epoch").write_text("1\n")
    out = ctl / ".stderr.1.log"

    r = subprocess.run([str(RINGLOG), str(out), "65536", "1"], input="hello\n",
                       env={"PATH": _base_path(tools, trusted)},
                       capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    assert _count(log, "shadow", "bash") == 0, _lines(log)
    assert out.read_text() == "hello\n"


# --- R6: a persisted pre-fix script reads stale ---------------------------------

_PRE_FIX = (
    "#!/command/with-contenv bash\nset -e\n"
    'printf \'{"casa_control": "spawn", "epoch": %s}\\n\' 1\n'
    "exec setpriv --reuid 200005 --regid 200005 --clear-groups \\\n"
    "             --bounding-set -all --inh-caps -all --no-new-privs \\\n"
    "             -- claude --print --verbose --output-format stream-json\n")


def _stale(tmp_path: Path, text: str) -> bool:
    run = tmp_path / "svc" / s6_rc._main_service_name(ENG_ID) / "run"
    run.parent.mkdir(parents=True, exist_ok=True)
    run.write_text(text)
    return s6_rc.run_script_is_stale(svc_root=str(tmp_path / "svc"),
                                     engagement_id=ENG_ID,
                                     model_flags=cli_model_flags("claude-sonnet-5"))


@pytest.mark.parametrize("decoy", [
    "",                                               # the plain pre-fix script
    "# PATH=$_casa_root_path\n",                      # only in a comment
    "echo ' PATH=$_casa_root_path'\n",                # only mid-line, quoted
])
def test_a_pre_fix_run_script_reads_stale(tmp_path, decoy):
    head, tail = _PRE_FIX.split("set -e\n", 1)
    assert _stale(tmp_path, head + "set -e\n" + decoy + tail) is True


def test_a_fresh_render_is_not_stale(tmp_path):
    """Positive control for the case above."""
    text = render_run_script(model="claude-sonnet-5", engagement_id=ENG_ID, permission_mode="acceptEdits",
                             extra_dirs=[], plugin_dirs=[], uid=UID, gid=UID)
    assert _stale(tmp_path, text) is False


# --- R7: the scripts' filter and the preflight's PATH agree, entry by entry -----

def _entry_cases(tools: Path, tmp_path: Path) -> dict[str, str]:
    link = tmp_path / "tools-link"
    if not link.exists():
        link.symlink_to(tools)
    return {
        "exact": str(tools),
        "trailing-slash": str(tools) + "/",
        "leading-double-slash": "/" + str(tools),
        "dot-segment": str(tools.parent / "." / "tools"),
        "dotdot-segment": str(tools / ".." / "tools"),
        "symlink": str(link),
    }


_CASES = ("exact", "trailing-slash", "leading-double-slash", "dot-segment",
          "dotdot-segment", "symlink",
          "empty-leading", "empty-interior", "empty-trailing", "relative")
_UNTRUSTED_TAGS = ("shadow", "cwd", "rel")


def _case_path(case: str, tools: Path, trusted: Path, tmp_path: Path) -> str:
    safe = f"{trusted}:/usr/bin"
    if case == "empty-leading":
        return ":" + safe
    if case == "empty-interior":
        return f"{trusted}::/usr/bin"
    if case == "empty-trailing":
        return safe + ":"
    if case == "relative":
        return "rel:" + safe
    return _entry_cases(tools, tmp_path)[case] + ":" + safe


def _untrusted_runs(log: Path) -> list[str]:
    return [line for line in _lines(log) if line.split(" ", 1)[0] in _UNTRUSTED_TAGS]


@pytest.mark.parametrize("script_kind", ["run", "log"])
@pytest.mark.parametrize("case", _CASES)
def test_script_filter_and_trusted_path_agree(world, monkeypatch, case, script_kind):
    tmp_path, tools, trusted, log = world
    ws = tmp_path / "ws"
    _populate(ws, "cwd", log)               # what an empty entry would find
    _populate(ws / "rel", "rel", log)       # what the relative entry names
    path = _case_path(case, tools, trusted, tmp_path)
    expected = f"{trusted}:/usr/bin"

    if script_kind == "run":
        script, observer = _render_run_script(tmp_path), "setpriv"
        argv = [BASH, str(script)]
    else:
        script = tmp_path / "log-run"
        script.write_text(_render_log_script(tmp_path / "logs" / "x"))
        observer, argv = "s6-log", [SH, str(script)]
    r = subprocess.run(argv, env={"PATH": path}, cwd=ws,
                       capture_output=True, text=True)
    _wait_ringlog_exit(str(tmp_path / "ctl"))

    # The script: the observer saw exactly the safe entries, nothing ran
    # from the tools dir, the working directory or the relative directory.
    assert r.returncode == 0, r.stderr
    assert _untrusted_runs(log) == []
    assert _observed_paths(log, "trusted", observer) == [expected], _lines(log)

    # The preflight's PATH: the same entries, and setpriv resolves to the same file.
    monkeypatch.setenv("PATH", path)
    monkeypatch.chdir(ws)
    trusted_path = s6_rc._trusted_env()["PATH"]
    assert trusted_path == expected
    assert shutil.which("setpriv", path=trusted_path) == str(trusted / "setpriv")


# --- R8: nothing trusted left → refuse before running anything ------------------

@pytest.mark.parametrize("script_kind", ["run", "log"])
def test_a_path_with_no_trusted_entry_runs_nothing(world, script_kind):
    tmp_path, tools, trusted, log = world
    ws = tmp_path / "ws"
    _populate(ws, "cwd", log)
    _populate(ws / "rel", "rel", log)
    path = f"{tools}::rel:{tools}/"

    if script_kind == "run":
        argv = [BASH, str(_render_run_script(tmp_path))]
    else:
        script = tmp_path / "log-run"
        script.write_text(_render_log_script(tmp_path / "logs" / "x"))
        argv = [SH, str(script)]
    r = subprocess.run(argv, env={"PATH": path}, cwd=ws,
                       capture_output=True, text=True)
    _wait_ringlog_exit(str(tmp_path / "ctl"))

    assert r.returncode != 0
    assert _lines(log) == []
