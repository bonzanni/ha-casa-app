"""Workspace-template rendering (§16.3, unified plugin arch §3.3): copy the
template subtree + generate .claude/settings.json with hooks + permissions
(NO enabledPlugins — executor plugins load via --plugin-dir), plus the
run-script --plugin-dir plumbing."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.unit


@pytest.fixture
def executor_defaults(tmp_path: Path) -> Path:
    """Minimal defaults tree with plugin-developer + test-fixture executors."""
    base = tmp_path / "defaults" / "agents" / "executors"
    for name in ("plugin-developer", "test-fixture"):
        root = base / name
        (root / "workspace-template" / ".claude").mkdir(parents=True)
        (root / "workspace-template" / "CLAUDE.md.tmpl").write_text(
            "# {executor_type} engagement\n\nTask: {task}\n\nContext: {context}\n\n"
            "World state:\n{world_state_summary}\n",
            encoding="utf-8",
        )
    return base


def _defn(exec_type="test-fixture", tools_allowed=("Read",),
          permission_mode="acceptEdits"):
    from config import ExecutorDefinition
    return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
        type=exec_type,
        description="test fixture twenty-character description here",
        model="sonnet",
        driver="claude_code",
        tools_allowed=list(tools_allowed),
        permission_mode=permission_mode,
    )


def _render(executor_defaults, exec_type, defn, dest, **kw):
    from drivers.workspace import render_workspace_template
    render_workspace_template(
        template_root=executor_defaults / exec_type / "workspace-template",
        dest=dest, defn=defn, executor_type=exec_type,
        task=kw.get("task", "t"), context=kw.get("context", "c"),
        world_state_summary=kw.get("world_state_summary", ""),
        hooks_yaml_data=kw.get("hooks_yaml_data", {}),
    )


def test_renders_claude_md(tmp_path, executor_defaults):
    dest = tmp_path / "engagement"
    _render(executor_defaults, "plugin-developer",
            _defn("plugin-developer"), dest,
            task="build face-rec", context="targets=tina,ellen",
            world_state_summary="(none)")
    claude_md = (dest / "CLAUDE.md").read_text(encoding="utf-8")
    assert "# plugin-developer engagement" in claude_md
    assert "Task: build face-rec" in claude_md
    assert "targets=tina,ellen" in claude_md


def test_generates_settings_json_without_enabled_plugins(tmp_path, executor_defaults):
    """§3.3: settings.json has hooks + permissions but NO enabledPlugins."""
    dest = tmp_path / "engagements" / "eng1"
    _render(executor_defaults, "test-fixture",
            _defn(tools_allowed=["Read", "Bash(git*)"]), dest)
    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert "enabledPlugins" not in settings
    assert "hooks" in settings
    assert settings["permissions"]["allow"] == ["Read", "Bash(git*)"]
    assert settings["permissions"]["defaultMode"] == "acceptEdits"


def test_template_path_filters_invalid_permissions(tmp_path, executor_defaults):
    dest = tmp_path / "engagements" / "eng2"
    _render(executor_defaults, "test-fixture",
            _defn(tools_allowed=["Read", "casa-internal-bogus", "Bash(git*)"],
                  permission_mode="bypassPermissions"), dest)
    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["allow"] == ["Read", "Bash(git*)"]
    assert settings["permissions"]["defaultMode"] == "bypassPermissions"


def test_template_keeps_broad_bash_and_web_tools(tmp_path, executor_defaults):
    dest = tmp_path / "engagements" / "eng3"
    _render(executor_defaults, "test-fixture",
            _defn(tools_allowed=["Bash", "WebFetch", "WebSearch", "Read",
                                 "casa-internal-bogus"],
                  permission_mode="auto"), dest)
    allow = json.loads((dest / ".claude" / "settings.json").read_text()
                       )["permissions"]["allow"]
    assert "Bash" in allow and "WebFetch" in allow and "WebSearch" in allow
    assert "Read" in allow
    assert "casa-internal-bogus" not in allow


def test_template_path_writes_translated_hooks(tmp_path, executor_defaults):
    dest = tmp_path / "engagements" / "eng4"
    _render(executor_defaults, "test-fixture", _defn(), dest,
            hooks_yaml_data={"pre_tool_use": [{"policy": "block_dangerous_bash"}]})
    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert "PreToolUse" in settings["hooks"]
    # Declared entry + the code-mandatory managed_component_guard entry
    # (round-4 Terra P0: yaml policies are additive-only) + the
    # code-mandatory path_scope floor entry (Task 4 #360: block_dangerous_bash
    # was declared but path_scope was not).
    pre = settings["hooks"]["PreToolUse"]
    commands = [e["hooks"][0]["command"] for e in pre]
    # #631: +1 — resident_prompt_write_guard joined the bridge's
    # code-mandatory set (file tools only; see hook_bridge.py for why
    # path_scope alone does not cover the resident tree).
    assert len(pre) == 4
    assert any(c.endswith("hook_proxy.sh managed_component_guard")
               for c in commands)
    assert any(c.endswith("hook_proxy.sh path_scope") for c in commands)


def test_template_path_handles_bundled_plugin_developer(tmp_path):
    """Regression: bundled plugin-developer definition.yaml + hooks.yaml +
    workspace-template/ flow through render_workspace_template cleanly, and
    settings.json carries NO enabledPlugins (§3.3)."""
    import yaml
    from config import ExecutorDefinition
    from drivers.workspace import render_workspace_template

    here = Path(__file__).resolve().parent.parent
    plugin_dev_dir = (
        here / "casa" / "rootfs" / "opt" / "casa" / "defaults"
        / "agents" / "executors" / "plugin-developer"
    )
    raw_defn = yaml.safe_load((plugin_dev_dir / "definition.yaml").read_text(encoding="utf-8"))
    raw_hooks = yaml.safe_load((plugin_dev_dir / "hooks.yaml").read_text(encoding="utf-8")) or {}
    tools = raw_defn.get("tools") or {}
    defn = ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
        type=raw_defn["type"], description=raw_defn["description"],
        model="sonnet", driver=raw_defn["driver"],
        tools_allowed=list(tools.get("allowed", [])),
        permission_mode=tools.get("permission_mode", "acceptEdits"),
    )
    dest = tmp_path / "engagements" / "eng-bundled"
    render_workspace_template(
        template_root=plugin_dev_dir / "workspace-template",
        dest=dest, defn=defn, hooks_yaml_data=raw_hooks,
        executor_type=defn.type, task="probe task", context="probe context",
        world_state_summary="",
    )
    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["allow"] == defn.tools_allowed
    assert settings["permissions"]["defaultMode"] == defn.permission_mode
    assert "PreToolUse" in settings["hooks"]
    assert len(settings["hooks"]["PreToolUse"]) >= 1
    assert "enabledPlugins" not in settings          # §3.3

    # Task 10 doctrine: first contact + as-directed brainstorming + completion accounting
    claude_md = (dest / "CLAUDE.md").read_text(encoding="utf-8")
    assert "## First contact" in claude_md
    assert "intended approach" in claude_md
    assert "process requirement" in claude_md

    # v0.79.0 (§6/T5) doctrine: turn discipline — one question at a time via
    # the ask tool, the inbound gate, and the redirect/STOP priority lane.
    assert "## Turn discipline" in claude_md
    assert "One question at a time" in claude_md
    assert "options: []" in claude_md
    assert "unread_inbound" in claude_md
    assert "[OPERATOR REDIRECT" in claude_md
    assert "doctrine/engagement-conduct.md" in claude_md
    doctrine_card = (
        plugin_dev_dir / "doctrine" / "engagement-conduct.md"
    ).read_text(encoding="utf-8")
    assert "STOP" in doctrine_card
    assert "redirect: <text>" in doctrine_card


# --- run-script --plugin-dir plumbing (§3.8) --------------------------------

def test_render_run_script_plugin_dir_flags():
    from drivers.workspace import render_run_script
    out = render_run_script(
        engagement_id="e" * 32, permission_mode="acceptEdits", extra_dirs=[],
        plugin_dirs=["/config/plugins/store/a/aaa",
                     "/config/plugins/store/b/bbb"], uid=200005, gid=200005)
    assert ("--plugin-dir /config/plugins/store/a/aaa "
            "--plugin-dir /config/plugins/store/b/bbb") in out


def test_render_run_script_rejects_relative_or_shell_special_plugin_dir():
    from drivers.workspace import render_run_script, WorkspaceConfigError
    for bad in ("relative/path", "/a;rm -rf /", "/a$(evil)", "/a|b"):
        with pytest.raises(WorkspaceConfigError):
            render_run_script(engagement_id="e" * 32,
                              permission_mode="acceptEdits", extra_dirs=[],
                              plugin_dirs=[bad], uid=200005, gid=200005)


def test_run_template_has_no_seed_or_cache_env():
    template = (Path(__file__).resolve().parent.parent / "casa" / "rootfs"
                / "opt" / "casa" / "scripts" / "engagement_run_template.sh"
                ).read_text(encoding="utf-8")
    assert "CLAUDE_CODE_PLUGIN_SEED_DIR" not in template
    assert "CLAUDE_CODE_PLUGIN_CACHE_DIR" not in template
    assert "{PLUGIN_DIR_FLAGS}" in template


def test_template_path_fires_without_plugins_yaml(tmp_path, executor_defaults):
    """§3.3: the template render path is selected by the template dir alone —
    no plugins.yaml needed."""
    import asyncio
    from drivers.workspace import provision_workspace

    exec_dir = executor_defaults / "test-fixture"
    defn = _defn()
    defn.hooks_path = ""
    defn.prompt_template_path = str(exec_dir / "prompt.md")
    defn.extra_dirs = []
    defn.mcp_server_names = []
    ws = asyncio.run(provision_workspace(
        engagements_root=str(tmp_path / "engagements"),
        engagement_id="f" * 32, engagement_auth_token="tok-ws-test",
        defn=defn, task="t", context="c",
        casa_framework_mcp_url="http://x",
        workspace_template_root=exec_dir / "workspace-template",
    ))
    # Template path fired → CLAUDE.md rendered from the .tmpl.
    assert (Path(ws) / "CLAUDE.md").read_text(encoding="utf-8").startswith(
        "# test-fixture engagement")
    settings = json.loads((Path(ws) / ".claude" / "settings.json").read_text())
    assert "enabledPlugins" not in settings


# --- run-script v2: explicit stream-json CLI + per-spawn epoch + ringlog ---
# (v0.75.0, W1/Sol B5/r5-B2/r5-B3/r6-B2/r6-B3/r7-B4/r8-B4/r9-B4/r10-B1). All
# of these tests drive the REAL repo copies of engagement_run_template.sh /
# ringlog.sh as subprocesses — skip cleanly if bash isn't on PATH.

BASH = shutil.which("bash")
_bash_required = pytest.mark.skipif(BASH is None, reason="bash not found on PATH")

# render the template with a KNOWN id so the paths are concrete (r3-B6:
# {ID} is already substituted by render_run_script — operate on the result).
_PROBE_ID = "probe0000"
_RINGLOG = os.path.abspath(
    "casa/rootfs/opt/casa/scripts/ringlog.sh")


@pytest.fixture
def rendered_run_script():
    from drivers.workspace import render_run_script
    return render_run_script(engagement_id="e" * 32, permission_mode="acceptEdits",
                              extra_dirs=[], plugin_dirs=[], uid=200005, gid=200005)


@pytest.fixture
def rendered_probe_script():
    from drivers.workspace import render_run_script
    return render_run_script(engagement_id=_PROBE_ID, permission_mode="acceptEdits",
                              extra_dirs=[], plugin_dirs=[], uid=200005, gid=200005)


@_bash_required
def test_rendered_run_script_is_valid_bash(tmp_path, rendered_run_script):
    p = tmp_path / "run"; p.write_text(rendered_run_script)
    r = subprocess.run([BASH, "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@_bash_required
def test_rendered_run_script_contract(rendered_run_script):
    s = rendered_run_script
    assert s.startswith("#!/command/with-contenv bash")
    assert "--print --verbose --output-format stream-json" in s
    assert '"casa_control": "spawn"' in s
    assert "MCP_TOOL_TIMEOUT=660000" in s
    # v0.131.0: preserve pre-CLI-2.1.219 nesting (default became 3) — the
    # CHANGELOG claims this pin for engagements; keep the claim honest.
    assert "export CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1" in s
    # v0.131.0: the resume-rejection notice must fire BEFORE the ringlog
    # stderr redirect so it reaches the container log, not just the
    # per-epoch stderr file.
    assert s.index("RESUME_ARGS=()") < s.index("ringlog.sh")
    assert "exec 2>&1" not in s
    # Task 6 (containment stage 2): the final exec wraps the CLI in a
    # setpriv uid+cap drop — `setpriv` is the literal token Task 8's
    # staleness marker keys on. The template line-continues with `\` +
    # newline + indent, so normalize whitespace before matching the
    # brief's contiguous-string contract.
    collapsed = re.sub(r"\s+", " ", s.replace("\\\n", " "))
    assert (
        "exec setpriv --reuid 200005 --regid 200005 --clear-groups "
        "--bounding-set -all --inh-caps -all --no-new-privs -- claude"
    ) in collapsed
    assert s.index('exec setpriv') > s.index('ringlog.sh')


@_bash_required
def test_ringlog_streams_before_eof_via_fifo(tmp_path):
    # r6-B3: PIN streaming — a buffer-whole impl writes nothing until EOF. Feed a
    # 3000-byte chunk (>1 CHUNK) through a FIFO, keep the write end OPEN, and
    # assert the log already has >= CHUNK bytes BEFORE we close (EOF). The old
    # read-whole-line impl would show 0 here.
    fifo = tmp_path / "f"; os.mkfifo(fifo)
    log = tmp_path / "e.log"
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    wfd = os.open(str(fifo), os.O_WRONLY)          # reader exists → won't block
    os.set_blocking(rfd, True)
    proc = subprocess.Popen([BASH, "scripts/ringlog.sh", str(log), "65536"],
                            stdin=rfd, cwd="casa/rootfs/opt/casa")
    os.close(rfd)                                  # child holds its own copy
    os.write(wfd, b"A" * 3000); time.sleep(0.3)
    assert log.stat().st_size >= 2048              # streamed BEFORE EOF, not buffered
    os.close(wfd); proc.wait(timeout=5)
    assert log.stat().st_size == 3000              # all bytes preserved


@_bash_required
def test_ringlog_byte_exact_multibyte_not_codepoint(tmp_path):
    # r6-B3: 4096 'é' = 8192 BYTES, MAX huge (no rotation). Byte-oriented
    # ingestion preserves ALL 8192 bytes; the OLD codepoint clamp (${line:0:2048})
    # would keep only ~2048 codepoints ≈ 4096 bytes — so == 8192 FAILS the old impl.
    log = tmp_path / "e.log"
    r = subprocess.run([BASH, "scripts/ringlog.sh", str(log), "1000000"],
                       input=("é" * 4096).encode("utf-8"),
                       capture_output=True, cwd="casa/rootfs/opt/casa")
    assert r.returncode == 0
    assert log.stat().st_size == 8192          # EXACT bytes, not codepoint-clamped


@_bash_required
def test_ringlog_large_no_newline_flood_bounded(tmp_path):
    # r5-B3/r6-B3: 1 MB no-newline flood → memory bounded per read; on-disk
    # window byte-bounded (each of FILE and FILE.1 ≤ MAX+CHUNK).
    log = tmp_path / "e.log"
    r = subprocess.run([BASH, "scripts/ringlog.sh", str(log), "65536"],
                       input=b"x" * 1_000_000, capture_output=True,
                       cwd="casa/rootfs/opt/casa")
    assert r.returncode == 0
    assert log.stat().st_size <= 65536 + 2048
    if (tmp_path / "e.log.1").exists():
        assert (tmp_path / "e.log.1").stat().st_size <= 65536 + 2048


@_bash_required
def test_ringlog_rotates_on_threshold(tmp_path):
    log = tmp_path / "e.log"
    r = subprocess.run([BASH, "scripts/ringlog.sh", str(log), "64"],
                       input=b"y" * 512, capture_output=True,
                       cwd="casa/rootfs/opt/casa")
    assert (tmp_path / "e.log.1").exists()


def _harness_script(rendered: str, tmp_path, final_exec: str) -> Path:
    """Rewrite the rendered run script (id already substituted to _PROBE_ID)
    for a NON-BLOCKING local run: real ws tmp dir, real control dir (Task 4:
    .spawn_epoch/.stderr.*/stdin.fifo all live there now, not the workspace),
    stdin </dev/null (not the FIFO), the ringlog path pointed at the REPO
    copy (the /opt/casa host path doesn't exist — r3-B6), and the final
    `exec claude …` replaced."""
    ws = tmp_path / "ws"; (ws / ".home").mkdir(parents=True, exist_ok=True)  # r6-B2: repeated calls
    ctl = tmp_path / "ctl"; ctl.mkdir(parents=True, exist_ok=True)          # r6-B2: repeated calls
    s = rendered.replace(f"/data/engagements/{_PROBE_ID}", str(ws))
    # Re-root the control dir by substituting the CTL="..." assignment's
    # literal value — every subsequent `$CTL/...` reference in the script
    # (spawn epoch, stderr ring, stdin fifo) re-roots for free.
    s = s.replace(f'CTL="/data/engagement-ctl/{_PROBE_ID}"', f'CTL="{ctl}"')
    s = s.replace("/opt/casa/scripts/ringlog.sh", _RINGLOG)
    s = s.replace('exec <"$CTL/stdin.fifo"', "exec </dev/null")
    # Task 6: the final line is now `exec setpriv ... -- claude ...` — match
    # from `exec setpriv` (the wrapper's own exec) through to the end of the
    # invocation, same anchor logic as before.
    s = re.sub(r"exec setpriv .*?(?=\n[A-Z#]|\Z)", final_exec, s, flags=re.S)
    p = tmp_path / "run"; p.write_text(s); return p


@_bash_required
def test_run_script_exit_status_propagates(tmp_path, rendered_probe_script):
    """r2/r3-B6: process substitution keeps claude the exec'd child, so a
    fake 'claude' exit code surfaces as the script's exit code."""
    p = _harness_script(rendered_probe_script, tmp_path, "exec bash -c 'exit 7'")
    r = subprocess.run([BASH, str(p)])
    assert r.returncode == 7


@_bash_required
def test_run_script_signal_propagates(tmp_path, rendered_probe_script):
    """A SIGTERM to the exec'd child surfaces as -SIGTERM (r3-B6: Python
    reports signal death as a NEGATIVE returncode, not 143)."""
    p = _harness_script(rendered_probe_script, tmp_path,
                        "exec bash -c 'kill -TERM $$; sleep 5'")
    r = subprocess.run([BASH, str(p)])
    assert r.returncode == -signal.SIGTERM        # == -15


@_bash_required
def test_run_script_epoch_unique_files_and_sweep_prunes(
        tmp_path, rendered_probe_script):
    """r5-B2/r6-B2: N spawns → epochs 1..N, each a UNIQUE .stderr.<E>.log; the
    run script SWEEP-prunes every file <= E-4 at spawn, so total .stderr.*.log
    count stays bounded (~4). Deterministic (all writers exit before assert)."""
    p = _harness_script(rendered_probe_script, tmp_path,
                        "exec bash -c 'echo err >&2; true'")
    for expect in range(1, 8):
        r = subprocess.run([BASH, str(p)], capture_output=True, text=True)
        assert f'{{"casa_control": "spawn", "epoch": {expect}}}' in r.stdout
    ctl = tmp_path / "ctl"                              # Task 4: control dir
    assert (ctl / ".spawn_epoch").read_text().strip() == "7"
    _wait_ringlogs_exit(str(tmp_path))                  # r9-B4: writer barrier here too
    logs = sorted(f.name for f in ctl.glob(".stderr.*.log"))
    assert logs == [".stderr.4.log", ".stderr.5.log",
                    ".stderr.6.log", ".stderr.7.log"]   # <=E-4 all swept, bounded


def _await_file_bytes(path, n, timeout=5.0):
    # r9-B4: explicit opened/written barrier — poll until the file exists with
    # >= n bytes (replaces bare sleep(0.2) races).
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if path.stat().st_size >= n:
                return
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    raise AssertionError(f"{path} never reached {n} bytes")


@_bash_required
def test_ringlog_stale_epoch_fence_delayed_open(tmp_path):
    # r7-B4: a ringlog whose epoch is ALREADY stale (current spawn >= epoch+4)
    # when it starts must NOT create its file at all (delayed-open case). Drive
    # ringlog directly with MY_EPOCH=1 and .spawn_epoch=5.
    (tmp_path / ".spawn_epoch").write_text("5")
    log = tmp_path / ".stderr.1.log"
    r = subprocess.run([BASH, _RINGLOG, str(log), "65536", "1"],
                       input=b"hello\n", capture_output=True, cwd=str(tmp_path))
    assert r.returncode == 0
    assert not log.exists()                          # fenced — never created


@_bash_required
def test_ringlog_stale_epoch_fence_rotation_after_prune(tmp_path):
    # r7-B4: ringlog opens fresh (epoch current), then the epoch advances and the
    # sweep prunes its path; a FORCED rotation must NOT recreate the stale path.
    # Explicitly await the process (no sleep-race).
    (tmp_path / ".spawn_epoch").write_text("1")
    fifo = tmp_path / "f"; os.mkfifo(fifo)
    log = tmp_path / ".stderr.1.log"
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    wfd = os.open(str(fifo), os.O_WRONLY); os.set_blocking(rfd, True)
    # r10-B1: MAX=3000 > CHUNK=2048 — `read -N 2048` emits nothing until a FULL
    # chunk (or EOF), so the barrier must be a whole chunk, not 2 bytes.
    proc = subprocess.Popen([BASH, _RINGLOG, str(log), "3000", "1"],
                            stdin=rfd, cwd=str(tmp_path))
    os.close(rfd)
    os.write(wfd, b"a" * 2048)                       # one FULL chunk → emitted
    _await_file_bytes(log, 2048)                     # opened/written barrier
    (tmp_path / ".spawn_epoch").write_text("5")      # epoch 1 now stale
    os.remove(log)                                   # sweep prunes it (file existed — barrier)
    os.write(wfd, b"z" * 2048)                       # 2nd chunk → 4096 > 3000 → rotation
    os.close(wfd); proc.wait(timeout=5)              # explicit await
    assert proc.returncode == 0
    assert not log.exists()                          # fence stopped recreation


@_bash_required
def test_ringlog_stale_fence_self_unlinks_without_sweep(tmp_path):
    # r8-B4/r9-B4 (concrete — the SWEEP NEVER RUNS): epoch flips stale while
    # ringlog holds its file open; the rotation's post-check makes ringlog REMOVE
    # ITS OWN paths — the invariant doesn't depend on winning a race with the
    # sweeper.
    (tmp_path / ".spawn_epoch").write_text("1")
    fifo = tmp_path / "f"; os.mkfifo(fifo)
    log = tmp_path / ".stderr.1.log"
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    wfd = os.open(str(fifo), os.O_WRONLY); os.set_blocking(rfd, True)
    # r10-B1: full-chunk barrier (read -N 2048 emits nothing until a full chunk).
    proc = subprocess.Popen([BASH, _RINGLOG, str(log), "3000", "1"],
                            stdin=rfd, cwd=str(tmp_path))
    os.close(rfd)
    os.write(wfd, b"a" * 2048)                       # one FULL chunk → emitted
    _await_file_bytes(log, 2048)                     # opened/written barrier
    (tmp_path / ".spawn_epoch").write_text("5")      # stale — but NO sweep runs
    os.write(wfd, b"z" * 2048)                       # 4096 > 3000 → rotation → fence
    os.close(wfd); proc.wait(timeout=5)
    assert proc.returncode == 0
    assert not log.exists()                          # self-unlinked own FILE
    assert not (tmp_path / ".stderr.1.log.1").exists()   # …and own FILE.1


@_bash_required
def test_template_ringlog_stale_fence_reads_control_dir_not_cwd(
        tmp_path, rendered_probe_script):
    """Regression (Task 4 fix-loop round 1): ringlog's stale-epoch self-unlink
    fence (r7-B4/r8-B4) must read ``.spawn_epoch`` from the CONTROL dir, not
    cwd — the run template's cwd is the WORKSPACE (Task 4 moved
    ``.spawn_epoch`` to the control dir), so a naive ``cat .spawn_epoch``
    always misses there and falls back to ``$MY_EPOCH`` (never stale), which
    would silently reopen the bug class the fence exists to close.

    Unlike the three ``test_ringlog_stale_*`` tests above — which invoke
    ``ringlog.sh`` DIRECTLY with ``cwd`` == the very directory holding
    ``.spawn_epoch`` and so would pass even with the cwd-relative bug — this
    drives the REAL rendered+harnessed run template (whose cwd is the
    workspace, distinct from its control dir) end to end, bumps
    ``.spawn_epoch`` in the control dir the template itself computed to
    simulate later spawns WITHOUT ever running a later spawn's sweep loop
    (so any removal below can only be ringlog's own fence, not the sweep),
    and asserts the ring self-unlinks on rotation."""
    fifo = tmp_path / "release"; os.mkfifo(fifo)
    # A tiny helper script (not inline `bash -c '...'`) sidesteps quoting the
    # child's own logic through the harness's regex substitution.
    child = tmp_path / "child.sh"
    child.write_text(
        "#!/bin/bash\n"
        "head -c 2048 /dev/zero | tr '\\0' 'a' >&2\n"
        f"cat {fifo} >/dev/null\n"                    # blocks until released
        "head -c 2048 /dev/zero | tr '\\0' 'z' >&2\n"  # 2nd chunk → rotation
    )
    child.chmod(0o755)

    p = _harness_script(rendered_probe_script, tmp_path, f"exec {child}")
    # Shrink ringlog's MAX (the template hardcodes 65536) so a rotation is
    # reachable within a small, deterministic byte count — the ONE template
    # constant this test overrides; everything else (the CTL wiring,
    # STDERR_LOG derivation, and the ringlog invocation itself) is exercised
    # exactly as rendered.
    text = p.read_text()
    patched = text.replace('"$STDERR_LOG" 65536 "$EPOCH"',
                            '"$STDERR_LOG" 3000 "$EPOCH"')
    assert patched != text, "MAX substitution missed — template line changed?"
    p.write_text(patched)

    ctl = tmp_path / "ctl"
    proc = subprocess.Popen([BASH, str(p)], stdout=subprocess.DEVNULL)
    log = ctl / ".stderr.1.log"
    _await_file_bytes(log, 2048)                      # ringlog opened + wrote chunk 1
    assert (ctl / ".spawn_epoch").read_text().strip() == "1"
    # Simulate 4 LATER spawns advancing the epoch — without ever running a
    # later spawn (so its sweep loop never executes for this file).
    (ctl / ".spawn_epoch").write_text("5")
    with open(fifo, "w") as f:
        f.write("go\n")                               # release the child → rotation
    proc.wait(timeout=5)
    assert proc.returncode == 0
    # r8-B4: the exec'd child's exit does NOT imply the process-substitution
    # ringlog consumer has finished — real writer barrier before asserting.
    _wait_ringlogs_exit(str(tmp_path))
    assert not log.exists(), (
        "ringlog's rotation-time stale check must read .spawn_epoch from the "
        "control dir (via dirname of its own FILE argument), not cwd — cwd "
        "is the workspace, where .spawn_epoch no longer lives (Task 4)"
    )


@_bash_required
def test_sweep_removes_slipped_stale_file_next_spawn(tmp_path, rendered_probe_script):
    # r8-B4 (check→open TOCTOU backstop): a file that slips through the tiny
    # pre-check→open window while going stale persists ONLY until the next
    # spawn — the SWEEP visits ALL files <= E-4 every spawn (not just exact
    # E-4), and the exited writer holds no fd, so removal is final. Simulate
    # the slipped file directly, run one template spawn, assert it is gone.
    p = _harness_script(rendered_probe_script, tmp_path,
                        "exec bash -c 'true'")
    ctl = tmp_path / "ctl"                                    # Task 4: control dir
    subprocess.run([BASH, str(p)], capture_output=True)      # epoch 1
    (ctl / ".stderr.0.log").write_text("slipped stale file")  # simulate the window
    subprocess.run([BASH, str(p)], capture_output=True)      # epoch 2 → sweeps <= -2…
    # advance until the sweep window covers it (epochs 3,4 → E-4 >= 0)
    subprocess.run([BASH, str(p)], capture_output=True)
    subprocess.run([BASH, str(p)], capture_output=True)
    _wait_ringlogs_exit(str(tmp_path))                        # real writer barrier
    assert not (ctl / ".stderr.0.log").exists()              # swept by a later spawn


def _wait_ringlogs_exit(marker: str, timeout: float = 5.0) -> None:
    # r8-B4: subprocess.run does NOT wait for process-substitution consumers —
    # the run script's exec'd child can exit while its ringlog still drains.
    # Poll pgrep for ringlog processes whose argv contains this test's tmp dir.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = subprocess.run(["pgrep", "-f", f"ringlog.sh.*{marker}"],
                           capture_output=True)
        if r.returncode != 0:            # no matches → all writers exited
            return
        time.sleep(0.05)
    raise AssertionError("ringlog writers did not exit in time")


@_bash_required
def test_run_script_lingering_writer_no_resurrection(
        tmp_path, rendered_probe_script):
    """r6-B2/r7-B4/r8-B4 end-to-end: run several spawns via the real template;
    WAIT for every ringlog consumer to actually exit (poll pgrep — the exec'd
    child's exit does NOT imply the proc-sub consumer exited), then assert the
    file set is the bounded un-pruned window and nothing stale lingers."""
    p = _harness_script(rendered_probe_script, tmp_path,
                        "exec bash -c 'echo err >&2; true'")
    for _ in range(7):
        subprocess.run([BASH, str(p)], capture_output=True, text=True)
    _wait_ringlogs_exit(str(tmp_path))               # r8-B4: real writer barrier
    ctl = tmp_path / "ctl"                           # Task 4: control dir
    assert not (ctl / ".stderr.1.log").exists()      # swept, not resurrected
    assert len(list(ctl.glob(".stderr.*.log"))) <= 4  # bounded total


def test_render_run_script_refuses_to_shadow_its_own_exports():
    """#429 r2 (Terra): {EXTRA_EXPORT} is interpolated AFTER the template's
    own exports, so an extra_env entry naming one silently overrides it for
    the whole engagement. Refused at the collision point, independently of
    the manifest validator upstream — this covers every caller."""
    import pytest as _pytest
    from drivers.workspace import WorkspaceConfigError, render_run_script
    with _pytest.raises(WorkspaceConfigError) as exc:
        render_run_script(
            engagement_id="e" * 32, permission_mode="acceptEdits",
            extra_dirs=[], extra_env={"MCP_TOOL_TIMEOUT": ""}, uid=200005, gid=200005)
    assert "MCP_TOOL_TIMEOUT" in str(exc.value)


def test_render_run_script_still_accepts_an_ordinary_extra_env():
    from drivers.workspace import render_run_script
    out = render_run_script(
        engagement_id="e" * 32, permission_mode="acceptEdits",
        extra_dirs=[], extra_env={"CASA_BANKFEED_EB_CP_TOKEN": ""}, uid=200005, gid=200005)
    assert "export CASA_BANKFEED_EB_CP_TOKEN=''" in out
