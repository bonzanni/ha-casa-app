"""Unit tests for drivers.workspace — engagement workspace provisioner."""

from __future__ import annotations

import json
import os
import sys

import pytest

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestRenderRunScript:
    def test_substitutes_all_placeholders(self):
        from drivers.workspace import render_run_script

        out = render_run_script(
            engagement_id="abc12345def67890",
            permission_mode="acceptEdits",
            extra_dirs=["/share/casa-plugins-repo"],
         uid=200005, gid=200005)

        assert "{ID}" not in out
        assert "{ID_SHORT}" not in out
        assert "{PERMISSION_MODE}" not in out
        assert "{ADD_DIR_FLAGS}" not in out
        assert "{EXTRA_UNSET}" not in out

        assert 'HOME="/data/engagements/abc12345def67890/.home"' in out
        assert "--permission-mode acceptEdits" in out
        assert "--add-dir /data/engagements/abc12345def67890/" in out
        assert "--add-dir /share/casa-plugins-repo" in out

    def test_default_extra_dirs_still_includes_workspace(self):
        from drivers.workspace import render_run_script

        out = render_run_script(
            engagement_id="xxxxxxxxxxxxxxxx",
            permission_mode="dontAsk",
            extra_dirs=[],
         uid=200005, gid=200005)
        assert "--add-dir /data/engagements/xxxxxxxxxxxxxxxx/" in out
        assert "--permission-mode dontAsk" in out

    def test_extra_unset_names_appear_in_unset_line(self):
        from drivers.workspace import render_run_script

        out = render_run_script(
            engagement_id="xxxxxxxxxxxxxxxx",
            permission_mode="dontAsk",
            extra_dirs=[],
            extra_unset=["MY_SECRET", "ANOTHER_TOKEN"],
         uid=200005, gid=200005)
        # The template unsets base secrets then "{EXTRA_UNSET}" — after
        # rendering, the extras should appear in the unset command.
        assert "MY_SECRET" in out
        assert "ANOTHER_TOKEN" in out
        assert "{EXTRA_UNSET}" not in out


    def test_render_emits_setpriv_with_uid(self):
        """Task 6 (containment stage 2): the final exec drops privileges via
        setpriv before handing off to claude — reuid/regid come straight
        from the allocated uid/gid, bounding set and inh-caps cleared,
        no_new_privs set."""
        import re as _re
        from drivers.workspace import render_run_script

        script = render_run_script(
            engagement_id="abcd1234-eng-id", permission_mode="acceptEdits",
            extra_dirs=[], uid=200005, gid=200005)
        collapsed = _re.sub(r"\s+", " ", script.replace("\\\n", " "))
        assert (
            "exec setpriv --reuid 200005 --regid 200005 --clear-groups "
            "--bounding-set -all --inh-caps -all --no-new-privs -- claude"
        ) in collapsed

    def test_render_refuses_unallocated_uid(self):
        """Fail-closed: UNALLOCATED_UID (-1) or root (0) must never reach
        --reuid — either would silently skip or defeat the privilege drop."""
        from drivers.workspace import render_run_script
        with pytest.raises(ValueError):
            render_run_script(
                engagement_id="abcd1234-eng-id", permission_mode="acceptEdits",
                extra_dirs=[], uid=-1, gid=-1)
        with pytest.raises(ValueError):
            render_run_script(
                engagement_id="abcd1234-eng-id", permission_mode="acceptEdits",
                extra_dirs=[], uid=0, gid=0)

    def test_render_log_run_script(self):
        from drivers.workspace import render_log_run_script

        script = render_log_run_script(engagement_id="xxxxxxxxxxxxxxxx")
        assert script.startswith("#!/command/with-contenv sh\n")
        assert "mkdir -p /var/log/casa-engagement-xxxxxxxxxxxxxxxx" in script
        assert "exec s6-log n20 s1000000 /var/log/casa-engagement-xxxxxxxxxxxxxxxx" in script
        # GHSA-569r-7crq-xr43: the umask must precede the mkdir, or the dir is
        # created 0755 and only later-rotated files are private. Asserting the
        # ORDER, not merely the presence of the line.
        assert script.index("umask 077") < script.index("mkdir -p")
        assert script.index("umask 077") < script.index("exec s6-log")

    def test_engagement_log_dir_helper(self):
        """v0.64.0: one owner for the per-engagement log location — the
        render script, the log relay, the retention sweep, and the delete
        tool all derive from it."""
        from drivers.workspace import ENGAGEMENT_LOG_ROOT, engagement_log_dir

        assert engagement_log_dir("abc") == (
            f"{ENGAGEMENT_LOG_ROOT}/casa-engagement-abc"
        )
        assert engagement_log_dir("abc", root="/x") == "/x/casa-engagement-abc"

    def test_render_run_script_contains_channels_flag(self):
        """E-12 (v0.37.0): --channels server:casa-engagement-channel."""
        from drivers.workspace import render_run_script
        script = render_run_script(
            engagement_id="abcd1234-eng-id",
            permission_mode="acceptEdits",
            extra_dirs=[],
         uid=200005, gid=200005)
        assert "--channels server:casa-engagement-channel" in script
        # v0.64.0: --remote-control dropped — inert headless (non-TTY stdout
        # degrades the CLI to one-shot --print; no interactive/remote session
        # ever starts), and passing it advertised a surface that doesn't exist.
        assert "--remote-control" not in script

    def test_render_run_script_consumes_persisted_session_id(self):
        """O-5 (v0.37.9): the run script must read ``.session_id`` from
        the workspace and pass it via ``--resume`` so a Casa restart
        mid-engagement does NOT lose conversation context.

        Pairs with TestSessionIdCapture in test_claude_code_driver.py —
        the writer task captures the UUID from s6-log; this test asserts
        the run-script half of the contract reads it back.
        """
        from drivers.workspace import render_run_script
        eid = "abcd1234567890123456789012345678"
        script = render_run_script(
            engagement_id=eid,
            permission_mode="acceptEdits",
            extra_dirs=[],
         uid=200005, gid=200005)
        # The shell idiom (hardened v0.131.0): if .session_id exists AND its
        # content is an exact UUID, pass it as a single =-joined argv token.
        # The old unquoted `--resume $(cat ...)` word-split arbitrary file
        # content into extra CLI flags. Task 4 (containment stage 2):
        # .session_id lives in the root-only control dir, never the
        # engagement's own workspace.
        assert f'CTL="/data/engagement-ctl/{eid}"' in script
        assert '$CTL/.session_id' in script
        assert '=~ ^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$' in script
        assert 'RESUME_ARGS=("--resume=$SID")' in script
        assert '"${RESUME_ARGS[@]}"' in script
        # The injectable idiom must never come back.
        assert "--resume $(cat" not in script
        assert "$RESUME_FLAG" not in script


class TestRunScriptResumeArgvBehavior:
    """v0.131.0 hardening, behavioral half (codex review): execute the
    rendered run script under real bash with a stub ``claude`` on PATH and
    assert the FINAL argv the CLI receives. String assertions alone can't
    prove the safe fragments are the ones actually wired into the exec.

    ``.session_id`` is attacker-influenced (it lives inside the engagement
    workspace, which the engagement's own CLI can write), so the contract
    is: exact UUID → exactly one ``--resume=<uuid>`` argv element; anything
    else → no resume argv at all (fresh session), never extra flags.
    """

    EID = "abcd1234567890123456789012345678"
    UUID = "a1b2c3d4-e5f6-7890-abcd-ef0123456789"

    def _run_rendered(self, tmp_path, session_id_content: str | None) -> list[str]:
        import stat
        import subprocess as sp
        from drivers.workspace import render_run_script

        script = render_run_script(
            engagement_id=self.EID,
            permission_mode="acceptEdits",
            extra_dirs=[],
         uid=200005, gid=200005)
        ws = tmp_path / "ws"
        (ws / ".home").mkdir(parents=True)
        ctl = tmp_path / "ctl"
        ctl.mkdir(parents=True)
        # Re-root the workspace AND control-dir paths, and neutralize the two
        # infra excs that need container facilities (the stdin FIFO and the
        # ringlog stderr pipeline) — the contract under test is the resume
        # argv, not I/O plumbing. The exec'd `claude` resolves via PATH to our
        # stub. Task 4: .session_id/.spawn_epoch/stdin.fifo/stderr now live
        # under $CTL — substituting the CTL="..." assignment's literal value
        # re-roots every `$CTL/...` reference for free.
        script = script.replace(f"/data/engagements/{self.EID}", str(ws))
        script = script.replace(f'CTL="/data/engagement-ctl/{self.EID}"', f'CTL="{ctl}"')
        script = script.replace(
            'exec <"$CTL/stdin.fifo"', "exec </dev/null"
        )
        script = script.replace(
            'exec 2> >(/opt/casa/scripts/ringlog.sh "$STDERR_LOG" 65536 "$EPOCH")',
            'exec 2>>"$STDERR_LOG"',
        )
        assert "/opt/casa/scripts/ringlog.sh" not in script, (
            "ringlog substitution missed — template line changed?"
        )
        if session_id_content is not None:
            (ctl / ".session_id").write_text(session_id_content)

        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        argv_file = tmp_path / "argv.txt"
        stub = stub_dir / "claude"
        stub.write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "$@" > "{argv_file}"\n'
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        # Task 6: the final line now wraps the exec in `setpriv --reuid ...
        # -- claude ...`. Real setpriv requires CAP_SETUID (root) to actually
        # change identity — this test process is an ordinary unprivileged
        # test-runner user, and the contract under test is the resume argv,
        # not the privilege drop itself (that's Task 7/9's territory). Stub
        # setpriv as a transparent pass-through: skip past its own flags to
        # the `--` separator and exec whatever follows.
        setpriv_stub = stub_dir / "setpriv"
        setpriv_stub.write_text(
            "#!/bin/bash\n"
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "--" ]; then shift; exec "$@"; fi\n'
            "  shift\n"
            "done\n"
            'exec "$@"\n'
        )
        setpriv_stub.chmod(setpriv_stub.stat().st_mode | stat.S_IEXEC)

        script_path = tmp_path / "run.sh"
        script_path.write_text(script)
        env = {
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "TELEGRAM_BOT_TOKEN": "x", "WEBHOOK_SECRET": "x",
            "SUPERVISOR_TOKEN": "x", "HASSIO_TOKEN": "x",
        }
        proc = sp.run(
            ["bash", str(script_path)],
            capture_output=True, text=True, timeout=30, env=env, cwd=ws,
        )
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        return argv_file.read_text().splitlines()

    def test_valid_uuid_becomes_single_resume_token(self, tmp_path):
        argv = self._run_rendered(tmp_path, self.UUID)
        assert argv.count(f"--resume={self.UUID}") == 1
        assert "--resume" not in argv          # never the two-token form

    def test_injected_flags_never_reach_argv(self, tmp_path):
        payload = f"{self.UUID} --permission-mode bypassPermissions"
        argv = self._run_rendered(tmp_path, payload)
        assert not any(a.startswith("--resume") for a in argv)
        assert "bypassPermissions" not in argv
        # the injected mode must not even ride along inside another token
        assert not any("bypassPermissions" in a for a in argv)
        # exactly one --permission-mode: the rendered legitimate one
        assert argv.count("--permission-mode") == 1
        assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"

    def test_missing_session_file_starts_fresh(self, tmp_path):
        argv = self._run_rendered(tmp_path, None)
        assert not any(a.startswith("--resume") for a in argv)
        assert "--print" in argv               # sanity: stub got the real argv

    @pytest.mark.parametrize("sid", [
        "a1b2c3d4-e5f6-7890-abcd-ef0123456789",
        "A1B2C3D4-E5F6-7890-ABCD-EF0123456789",   # driver regex is IGNORECASE
        "00000000-0000-0000-0000-000000000000",
    ])
    def test_bash_grammar_accepts_what_the_writer_writes(self, tmp_path, sid):
        """The writer half (`claude_code_driver._UUID_REGEX`) and the reader
        half (the template's bash regex) are two spellings of one grammar.
        If they diverge in the writer-accepts/reader-rejects direction, every
        engagement silently loses history on restart — so bind them: any sid
        the driver would persist must produce a resume argv."""
        from drivers.claude_code_driver import _UUID_REGEX
        assert _UUID_REGEX.match(sid), "sample must be writer-accepted"
        # The writer persists `sid + "\n"` (claude_code_driver
        # `_capture_session_id`) — bind against the actual on-disk bytes.
        argv = self._run_rendered(tmp_path, sid + "\n")
        assert argv.count(f"--resume={sid}") == 1


class TestRenderRunScriptShellInjection:
    """Bug 4 + Bug 5 (v0.14.6): extra_dirs and extra_env keys must not
    inject shell into the rendered run script."""

    def test_extra_dir_with_semicolon_rejected(self):
        from drivers.workspace import WorkspaceConfigError, render_run_script
        with pytest.raises(WorkspaceConfigError, match="shell-special"):
            render_run_script(
                engagement_id="x" * 16,
                permission_mode="dontAsk",
                extra_dirs=["/tmp; rm -rf /data"],
             uid=200005, gid=200005)

    def test_extra_dir_with_quote_rejected(self):
        from drivers.workspace import WorkspaceConfigError, render_run_script
        with pytest.raises(WorkspaceConfigError, match="shell-special"):
            render_run_script(
                engagement_id="x" * 16,
                permission_mode="dontAsk",
                extra_dirs=["/tmp/'; touch /tmp/pwned ;#"],
             uid=200005, gid=200005)

    def test_extra_dir_with_newline_rejected(self):
        from drivers.workspace import WorkspaceConfigError, render_run_script
        with pytest.raises(WorkspaceConfigError, match="shell-special"):
            render_run_script(
                engagement_id="x" * 16,
                permission_mode="dontAsk",
                extra_dirs=["/tmp\nrm -rf /data"],
             uid=200005, gid=200005)

    def test_relative_extra_dir_rejected(self):
        from drivers.workspace import WorkspaceConfigError, render_run_script
        with pytest.raises(WorkspaceConfigError, match="absolute path"):
            render_run_script(
                engagement_id="x" * 16,
                permission_mode="dontAsk",
                extra_dirs=["relative/path"],
             uid=200005, gid=200005)

    def test_extra_dir_with_space_quoted_via_shlex(self):
        """Spaces in absolute paths are allowed but rendered shlex-quoted."""
        from drivers.workspace import render_run_script
        out = render_run_script(
            engagement_id="x" * 16,
            permission_mode="dontAsk",
            extra_dirs=["/share/with space"],
         uid=200005, gid=200005)
        # Either shlex.quote'd or single-quoted — never bare.
        assert "/share/with space" in out
        # The bare unquoted form would be a defect.
        assert "--add-dir /share/with space\n" not in out

    def test_extra_env_key_with_newline_rejected(self):
        """Bug 5: a newline in the key escapes the export statement."""
        from drivers.workspace import WorkspaceConfigError, render_run_script
        with pytest.raises(WorkspaceConfigError, match="extra_env keys"):
            render_run_script(
                engagement_id="x" * 16,
                permission_mode="dontAsk",
                extra_dirs=[],
                extra_env={"FOO\nrm -rf /data": "harmless"},
             uid=200005, gid=200005)

    def test_extra_env_key_with_dollar_rejected(self):
        from drivers.workspace import WorkspaceConfigError, render_run_script
        with pytest.raises(WorkspaceConfigError, match="extra_env keys"):
            render_run_script(
                engagement_id="x" * 16,
                permission_mode="dontAsk",
                extra_dirs=[],
                extra_env={"$(whoami)": "harmless"},
             uid=200005, gid=200005)

    def test_extra_env_lowercase_key_rejected(self):
        """Lowercase keys also rejected — convention is upper-snake."""
        from drivers.workspace import WorkspaceConfigError, render_run_script
        with pytest.raises(WorkspaceConfigError):
            render_run_script(
                engagement_id="x" * 16,
                permission_mode="dontAsk",
                extra_dirs=[],
                extra_env={"foo": "bar"},
             uid=200005, gid=200005)

    def test_extra_env_value_with_quote_escaped(self):
        """Embedded single-quote in value is escaped via '\\'' idiom."""
        from drivers.workspace import render_run_script
        out = render_run_script(
            engagement_id="x" * 16,
            permission_mode="dontAsk",
            extra_dirs=[],
            extra_env={"GITHUB_TOKEN": "abc'def"},
         uid=200005, gid=200005)
        assert "export GITHUB_TOKEN='abc'\\''def'" in out

    def test_valid_extra_env_renders(self):
        from drivers.workspace import render_run_script
        out = render_run_script(
            engagement_id="x" * 16,
            permission_mode="dontAsk",
            extra_dirs=[],
            extra_env={"GITHUB_TOKEN": "ghp_abc", "OP_TOKEN": "ops_xyz"},
         uid=200005, gid=200005)
        assert "export GITHUB_TOKEN='ghp_abc'" in out
        assert "export OP_TOKEN='ops_xyz'" in out


class TestProvisionWorkspace:
    def _make_defn(self, tmp_path, executor_type="hello-driver", plugins=None):
        """Build an ExecutorDefinition stub for workspace tests.

        Note: the default ``executor_type='hello-driver'`` is an incidental
        label only — no on-disk hello-driver definition is loaded. Tests
        construct the dataclass directly. The label could be any string;
        kept for diff-minimal historical continuity.
        """
        from config import ExecutorDefinition

        exec_dir = tmp_path / "defaults-executors" / executor_type
        exec_dir.mkdir(parents=True)
        (exec_dir / "prompt.md").write_text(
            "You are the {executor_type} executor. Task: {task}. Context: {context}."
        )

        plugins_dir = ""
        if plugins is not None:
            pdir = exec_dir / "plugins"
            pdir.mkdir()
            for pname in plugins:
                (pdir / pname).mkdir()
            plugins_dir = str(pdir)

        return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
            type=executor_type,
            description="test executor with twenty characters exactly today",
            model="sonnet",
            driver="claude_code",
            prompt_template_path=str(exec_dir / "prompt.md"),
            plugins_dir=plugins_dir,
            mcp_server_names=["casa-framework"],
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo/symlink not meaningful on Windows")
    async def test_creates_workspace_tree(self, tmp_path):
        import json
        from pathlib import Path
        from drivers.workspace import provision_workspace

        defn = self._make_defn(tmp_path)

        ws = tmp_path / "engagements"
        ws.mkdir()

        path = await provision_workspace(
            engagements_root=str(ws),
            engagement_id="eng1",
            engagement_auth_token="tok-ws-test",
            defn=defn,
            task="do a thing",
            context="because",
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )

        p = Path(path)
        assert p == ws / "eng1"
        assert (p / "CLAUDE.md").exists()
        claude_md = (p / "CLAUDE.md").read_text()
        assert "hello-driver" in claude_md
        assert "do a thing" in claude_md
        assert "because" in claude_md

        assert (p / ".mcp.json").exists()
        mcp = json.loads((p / ".mcp.json").read_text())
        assert "casa-framework" in mcp["mcpServers"]

        assert (p / ".claude" / "settings.json").exists()
        # Plugin symlinks removed in v0.14.x (Plan 4b §16.2); HOME dir still created.
        assert (p / ".home" / ".claude" / "plugins").is_dir()

        # FIFO — Task 4: lives in the control dir, not the workspace.
        from drivers.workspace import fifo_path
        fifo = fifo_path("eng1")
        assert not os.path.exists(p / "stdin.fifo")
        assert os.path.exists(fifo)
        import stat as _stat
        mode = os.stat(fifo).st_mode
        assert _stat.S_ISFIFO(mode)


    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo not meaningful on Windows")
    async def test_per_executor_plugins_no_symlinks(self, tmp_path):
        """Plugin symlinks removed in v0.14.x (Plan 4b §16.2); HOME dir exists."""
        from pathlib import Path
        from drivers.workspace import provision_workspace

        defn = self._make_defn(tmp_path, plugins=["superpowers", "plugin-dev"])


        ws = tmp_path / "engagements"
        ws.mkdir()

        path = await provision_workspace(
            engagements_root=str(ws),
            engagement_id="eng2",
            engagement_auth_token="tok-ws-test",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="http://x",
        )

        plugins_dir = Path(path) / ".home" / ".claude" / "plugins"
        # Dir exists but contains no symlinks — symlink assembly was removed.
        assert plugins_dir.is_dir()
        assert list(plugins_dir.iterdir()) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo/symlink not meaningful on Windows")
    async def test_mcp_json_carries_engagement_id_header(self, tmp_path):
        """Plan 4a.1 + #335: .mcp.json.mcpServers.casa-framework.headers
        carries X-Casa-Engagement-Id AND X-Casa-Engagement-Token so the HTTP
        bridge can authenticate + bind engagement_var."""
        from pathlib import Path
        from drivers.workspace import provision_workspace

        defn = self._make_defn(tmp_path)

        ws = tmp_path / "engagements"
        ws.mkdir()

        await provision_workspace(
            engagements_root=str(ws),
            engagement_id="eng-hdr-test",
            engagement_auth_token="tok-ws-test",
            defn=defn,
            task="t", context="c",
            casa_framework_mcp_url="http://127.0.0.1:8099/mcp/casa-framework",
        )

        mcp = json.loads((Path(ws) / "eng-hdr-test" / ".mcp.json").read_text())
        server_cfg = mcp["mcpServers"]["casa-framework"]
        assert server_cfg["headers"] == {
            "X-Casa-Engagement-Id": "eng-hdr-test",
            "X-Casa-Engagement-Token": "tok-ws-test",
        }

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo not meaningful on Windows")
    async def test_legacy_path_writes_permissions_allow_filtered(self, tmp_path):
        """L-1: tools_allowed flows to settings.json::permissions.allow, filtered."""
        import json
        from pathlib import Path
        from drivers.workspace import provision_workspace

        defn = self._make_defn(tmp_path)
        defn.tools_allowed = [
            "Bash(git*)", "Read", "mcp__casa-framework__emit_completion",
            "casa-internal-tool", "",
        ]
        defn.permission_mode = "acceptEdits"

        ws = tmp_path / "engagements"
        ws.mkdir()
        path = await provision_workspace(
            engagements_root=str(ws),
            engagement_id="eng-perm",
            engagement_auth_token="tok-ws-test",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="http://x",
        )
        settings = json.loads(
            (Path(path) / ".claude" / "settings.json").read_text()
        )
        assert "permissions" in settings
        assert settings["permissions"]["allow"] == [
            "Bash(git*)", "Read", "mcp__casa-framework__emit_completion",
        ]
        assert settings["permissions"]["defaultMode"] == "acceptEdits"

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo not meaningful on Windows")
    async def test_legacy_path_default_mode_from_defn(self, tmp_path):
        """L-1: defn.permission_mode flows to settings.json::permissions.defaultMode."""
        import json
        from pathlib import Path
        from drivers.workspace import provision_workspace

        defn = self._make_defn(tmp_path)
        defn.tools_allowed = ["Read"]
        defn.permission_mode = "bypassPermissions"

        ws = tmp_path / "engagements"
        ws.mkdir()
        path = await provision_workspace(
            engagements_root=str(ws),
            engagement_id="eng-perm-mode",
            engagement_auth_token="tok-ws-test",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="http://x",
        )
        settings = json.loads(
            (Path(path) / ".claude" / "settings.json").read_text()
        )
        assert settings["permissions"]["defaultMode"] == "bypassPermissions"

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo not meaningful on Windows")
    async def test_legacy_path_preserves_hooks_alongside_permissions(self, tmp_path):
        """L-1: existing hooks block coexists with new permissions block."""
        import json
        from pathlib import Path
        from drivers.workspace import provision_workspace

        defn = self._make_defn(tmp_path)
        defn.tools_allowed = ["Read"]
        # Write a hooks.yaml so translate_hooks_to_settings has something to do.
        hooks_path = tmp_path / "defaults-executors" / "hello-driver" / "hooks.yaml"
        hooks_path.write_text(
            "schema_version: 1\n"
            "pre_tool_use:\n"
            "  - policy: block_dangerous_bash\n"
        )
        defn.hooks_path = str(hooks_path)

        ws = tmp_path / "engagements"
        ws.mkdir()
        path = await provision_workspace(
            engagements_root=str(ws),
            engagement_id="eng-both",
            engagement_auth_token="tok-ws-test",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="http://x",
        )
        settings = json.loads(
            (Path(path) / ".claude" / "settings.json").read_text()
        )
        assert "hooks" in settings
        assert "permissions" in settings
        assert "PreToolUse" in settings["hooks"]

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo not meaningful on Windows")
    async def test_home_dir_created_via_template_path(self, tmp_path):
        """L-1: HOME dir must be created for both legacy and template paths."""
        from pathlib import Path
        from drivers.workspace import provision_workspace

        # Build a minimal workspace-template/ so the template path fires
        # (§3.3: selection is independent of any plugins.yaml).
        defn = self._make_defn(tmp_path, executor_type="tpl-fixture")
        exec_dir = tmp_path / "defaults-executors" / "tpl-fixture"
        tpl_root = exec_dir / "workspace-template"
        tpl_root.mkdir()
        (tpl_root / "CLAUDE.md.tmpl").write_text(
            "Tpl: type={executor_type} task={task}"
        )

        ws = tmp_path / "engagements"
        ws.mkdir()
        path = await provision_workspace(
            engagements_root=str(ws),
            engagement_id="eng-tpl-home",
            engagement_auth_token="tok-ws-test",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="http://x",
            workspace_template_root=tpl_root,
        )
        # Regression: HOME dir must exist even when template path fired.
        assert (Path(path) / ".home" / ".claude" / "plugins").is_dir()

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo not meaningful on Windows")
    async def test_provision_workspace_renders_casa_engagement_channel_mcp(
        self, tmp_path,
    ):
        """E-12 (v0.37.0): .mcp.json contains casa-engagement-channel stdio entry."""
        import json
        from pathlib import Path
        from drivers.workspace import provision_workspace
        eng_id = "abcd1234-test-uuid-segment-padding"  # any string; treated opaque
        defn = self._make_defn(tmp_path)
        ws = tmp_path / "engagements"
        ws.mkdir()
        path = await provision_workspace(
            engagements_root=str(ws),
            engagement_id=eng_id,
            engagement_auth_token="tok-ws-test",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="http://127.0.0.1:8100/mcp/casa-framework",
        )
        mcp = json.loads((Path(path) / ".mcp.json").read_text())
        assert "casa-framework" in mcp["mcpServers"]  # existing entry untouched
        assert "casa-engagement-channel" in mcp["mcpServers"]
        entry = mcp["mcpServers"]["casa-engagement-channel"]
        assert entry["command"] == "/opt/casa/venv/bin/python"
        assert entry["args"] == [
            "/opt/casa/channels/casa_engagement_channel.py",
            "--engagement-id", eng_id,
        ]
        assert entry["env"]["CASA_INTERNAL_SOCKET"] == "/run/casa/internal.sock"
        # #335: the per-engagement auth token rides into the stdio channel env.
        assert entry["env"]["CASA_ENGAGEMENT_TOKEN"] == "tok-ws-test"


class TestChownLastProvisioning:
    """Containment stage 2, Task 8: identity + chown are the LAST writes
    provision_workspace makes, and only when given a real allocated uid.

    Real ``os.chown`` to an arbitrary uid requires root — the unit runner is
    not — so the ownership assertions are gated on ``os.geteuid() == 0``;
    everywhere else the CALL SEQUENCE (ensure_identity before chown_workspace,
    both after every other write) is asserted via a recording monkeypatch,
    per the task brief.
    """

    # Same executor-definition fixture as TestProvisionWorkspace — kept as a
    # plain (non-inherited) copy so this class's tests don't also re-run
    # every TestProvisionWorkspace test under a second name.
    _make_defn = TestProvisionWorkspace._make_defn

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo/symlink not meaningful on Windows")
    async def test_default_uid_skips_identity_and_chown(self, tmp_path, monkeypatch):
        """Every existing caller of provision_workspace (this file's other
        tests included) omits uid/gid — the default (UNALLOCATED_UID) must
        leave the workspace untouched by identity/chown, exactly as before
        Task 8."""
        from drivers import workspace as ws_mod
        from drivers.workspace import provision_workspace

        calls: list[str] = []
        monkeypatch.setattr(
            ws_mod, "ensure_identity",
            lambda uid, home: calls.append("ensure_identity"))
        monkeypatch.setattr(
            ws_mod, "chown_workspace",
            lambda ws, uid, gid: calls.append("chown_workspace"))

        defn = self._make_defn(tmp_path)
        ws_root = tmp_path / "engagements"
        ws_root.mkdir()
        await provision_workspace(
            engagements_root=str(ws_root),
            engagement_id="eng-no-uid",
            engagement_auth_token="tok",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="http://x",
        )
        assert calls == []

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo/symlink not meaningful on Windows")
    async def test_identity_before_chown_and_chown_is_last(self, tmp_path, monkeypatch):
        from pathlib import Path

        from drivers import workspace as ws_mod
        from drivers.workspace import provision_workspace
        from engagement_uids import UID_BASE

        calls: list[tuple] = []
        monkeypatch.setattr(
            ws_mod, "ensure_identity",
            lambda uid, home: calls.append(("ensure_identity", uid, home)))
        monkeypatch.setattr(
            ws_mod, "chown_workspace",
            lambda ws, uid, gid: calls.append(("chown_workspace", ws, uid, gid)))

        defn = self._make_defn(tmp_path)
        ws_root = tmp_path / "engagements"
        ws_root.mkdir()
        path = await provision_workspace(
            engagements_root=str(ws_root),
            engagement_id="eng-uid-order",
            engagement_auth_token="tok",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="http://x",
            uid=UID_BASE, gid=UID_BASE,
        )

        assert [c[0] for c in calls] == ["ensure_identity", "chown_workspace"], (
            "ensure_identity must run before chown_workspace, and both "
            "must be the only two identity/chown calls"
        )
        assert calls[0][1] == UID_BASE
        assert calls[0][2] == str(Path(path) / ".home")
        assert calls[1][1] == path
        assert calls[1][2] == UID_BASE
        assert calls[1][3] == UID_BASE

        # Top-dir mode 0700 is set for real (chmod needs no special
        # privilege on a directory the test process itself owns) — even
        # though chown_workspace above was stubbed.
        import stat as _stat
        mode = os.stat(path).st_mode
        assert _stat.S_IMODE(mode) == 0o700

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo/symlink not meaningful on Windows")
    async def test_provision_propagates_ensure_identity_failure(self, tmp_path, monkeypatch):
        """Fail-closed: a failure appending the passwd/group entry must
        abort provisioning — never hand back a workspace the caller
        believes was chowned when it wasn't even given an identity."""
        from drivers import workspace as ws_mod
        from drivers.workspace import provision_workspace
        from engagement_uids import UID_BASE

        def boom(uid, home):
            raise OSError("cannot write /etc/passwd")
        monkeypatch.setattr(ws_mod, "ensure_identity", boom)
        chown_calls = []
        monkeypatch.setattr(
            ws_mod, "chown_workspace",
            lambda ws, uid, gid: chown_calls.append(1))

        defn = self._make_defn(tmp_path)
        ws_root = tmp_path / "engagements"
        ws_root.mkdir()
        with pytest.raises(OSError, match="passwd"):
            await provision_workspace(
                engagements_root=str(ws_root),
                engagement_id="eng-id-fail",
                engagement_auth_token="tok",
                defn=defn, task="t", context="c",
                casa_framework_mcp_url="http://x",
                uid=UID_BASE, gid=UID_BASE,
            )
        assert chown_calls == [], "chown must never run after a failed identity append"

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo/symlink not meaningful on Windows")
    async def test_provision_propagates_chown_failure(self, tmp_path, monkeypatch):
        """Fail-closed: a chown failure (e.g. PermissionError under a
        non-root spawner) must abort provisioning rather than hand back a
        still-root-owned workspace silently."""
        from drivers import workspace as ws_mod
        from drivers.workspace import provision_workspace
        from engagement_uids import UID_BASE

        monkeypatch.setattr(ws_mod, "ensure_identity", lambda uid, home: None)

        def boom(ws, uid, gid):
            raise PermissionError("chown requires root")
        monkeypatch.setattr(ws_mod, "chown_workspace", boom)

        defn = self._make_defn(tmp_path)
        ws_root = tmp_path / "engagements"
        ws_root.mkdir()
        with pytest.raises(PermissionError, match="root"):
            await provision_workspace(
                engagements_root=str(ws_root),
                engagement_id="eng-chown-fail",
                engagement_auth_token="tok",
                defn=defn, task="t", context="c",
                casa_framework_mcp_url="http://x",
                uid=UID_BASE, gid=UID_BASE,
            )

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo/symlink not meaningful on Windows")
    async def test_provision_workspace_provisions_engagement_outbox(
        self, tmp_path, monkeypatch,
    ):
        """Fix-loop round 1, finding 1 (wiring-site coverage): the private
        per-engagement outbox must be provisioned as part of
        provision_workspace's uid-drop step — the eager path a producer
        plugin depends on existing before the CLI ever starts. Real
        ownership is asserted under root; everywhere else the recorded
        provisioning call is asserted (same split as ensure_identity/
        chown_workspace above)."""
        import plugin_outbox
        from drivers import workspace as ws_mod
        from drivers.workspace import provision_workspace
        from engagement_uids import UID_BASE

        monkeypatch.setattr(ws_mod, "ensure_identity", lambda uid, home: None)
        monkeypatch.setattr(ws_mod, "chown_workspace", lambda ws, uid, gid: None)
        calls: list[int] = []
        real_provision = plugin_outbox.provision_engagement_outbox

        def _recording_provision(uid, **kw):
            calls.append(uid)
            return real_provision(uid, **kw)
        monkeypatch.setattr(
            plugin_outbox, "provision_engagement_outbox", _recording_provision)

        defn = self._make_defn(tmp_path)
        ws_root = tmp_path / "engagements"
        ws_root.mkdir()
        await provision_workspace(
            engagements_root=str(ws_root),
            engagement_id="eng-outbox-provision",
            engagement_auth_token="tok",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="http://x",
            uid=UID_BASE, gid=UID_BASE,
        )
        assert calls == [UID_BASE]
        d = plugin_outbox.engagement_outbox_dir(UID_BASE)
        assert os.path.isdir(d)
        if os.geteuid() == 0:
            assert os.stat(d).st_uid == UID_BASE

    @pytest.mark.skipif(
        os.geteuid() != 0 if hasattr(os, "geteuid") else True,
        reason="real chown requires root",
    )
    async def test_workspace_owned_by_uid_after_provision_real_root(self, tmp_path):
        """Real end-to-end ownership check — only runs under root (e.g. a
        privileged CI lane); everywhere else the ordering tests above cover
        the invariant."""
        from pathlib import Path

        from drivers.workspace import chown_workspace  # noqa: F401 (imported for parity)
        from drivers.workspace import control_dir, provision_workspace
        from engagement_uids import UID_BASE

        defn = self._make_defn(tmp_path)
        ws_root = tmp_path / "engagements"
        ws_root.mkdir()
        target_uid = UID_BASE + 1234
        path = await provision_workspace(
            engagements_root=str(ws_root),
            engagement_id="eng-real-root",
            engagement_auth_token="tok",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="http://x",
            uid=target_uid, gid=target_uid,
        )
        p = Path(path)
        for f in p.rglob("*"):
            st = os.lstat(f)
            assert st.st_uid == target_uid, f"{f} not owned by {target_uid}"
        assert os.lstat(p).st_uid == target_uid
        import stat as _stat
        assert _stat.S_IMODE(os.stat(p).st_mode) == 0o700

        # Control dir stays root-owned even though the workspace was chowned.
        ctl = Path(control_dir("eng-real-root"))
        assert os.stat(ctl).st_uid == 0

        import pwd as _pwd
        _pwd.getpwuid(target_uid)  # must not raise — NSS identity created

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink not meaningful on Windows")
    async def test_chown_workspace_never_follows_symlinks(self, tmp_path, monkeypatch):
        """A symlink planted inside the tree must be chowned itself
        (follow_symlinks=False) — never have its TARGET chowned. Asserted
        via the exact os.chown kwarg used, since chowning to an arbitrary
        uid for real requires root."""
        from drivers.workspace import chown_workspace

        ws = tmp_path / "ws"
        ws.mkdir()
        outside_target = tmp_path / "outside.txt"
        outside_target.write_text("do not touch")
        link = ws / "escape-link"
        link.symlink_to(outside_target)
        (ws / "regular.txt").write_text("y")
        (ws / "subdir").mkdir()
        (ws / "subdir" / "nested.txt").write_text("z")

        calls: list[tuple] = []

        def fake_chown(path, uid, gid, *, follow_symlinks=True):
            calls.append((str(path), uid, gid, follow_symlinks))

        monkeypatch.setattr(os, "chown", fake_chown)

        chown_workspace(str(ws), 424242, 424242)

        assert calls, "chown_workspace must actually chown something"
        assert all(c[3] is False for c in calls), (
            "every os.chown call must pass follow_symlinks=False"
        )
        called_paths = {c[0] for c in calls}
        assert str(ws) in called_paths
        assert str(link) in called_paths
        assert str(ws / "regular.txt") in called_paths
        assert str(ws / "subdir") in called_paths
        assert str(ws / "subdir" / "nested.txt") in called_paths
        # The symlink's TARGET is never in the call set — only the link
        # itself (its own path, under ws/) is chowned.
        assert str(outside_target) not in called_paths

    @pytest.mark.skipif(
        os.geteuid() != 0 if hasattr(os, "geteuid") else True,
        reason="real chown requires root",
    )
    async def test_chown_workspace_symlink_target_owner_unchanged_real(self, tmp_path):
        """Real-root variant: after chown_workspace, the symlink's TARGET
        (living outside the workspace) keeps its original owner — only the
        link itself (inside the tree) is reassigned."""
        from drivers.workspace import chown_workspace

        ws = tmp_path / "ws"
        ws.mkdir()
        outside_target = tmp_path / "outside.txt"
        outside_target.write_text("do not touch")
        link = ws / "escape-link"
        link.symlink_to(outside_target)

        before_target_uid = os.stat(outside_target).st_uid
        chown_workspace(str(ws), 0, 0)  # any distinct uid works as root

        assert os.stat(outside_target).st_uid == before_target_uid
        assert os.lstat(link).st_uid == 0


class TestCasaMeta:
    def test_write_and_load_roundtrip(self, tmp_path):
        from drivers.workspace import (
            write_casa_meta, load_casa_meta, provision_control_dir,
        )

        # load_casa_meta derives the engagement id from the workspace dir's
        # BASENAME (provision_workspace always names it that way) — name the
        # dir "e1" to match, rather than widen load_casa_meta's contract.
        ws = tmp_path / "e1"
        ws.mkdir()
        # Task 4: .casa-meta.json lives in the control dir now — provisioned
        # separately from the workspace (provision_workspace does this for a
        # real engagement; this unit test does it explicitly).
        provision_control_dir("e1")
        write_casa_meta(
            workspace_path=str(ws),
            engagement_id="e1", executor_type="hello-driver",
            status="UNDERGOING", created_at="2026-04-23T10:00:00Z",
            finished_at=None, retention_until=None,
        )

        meta = load_casa_meta(str(ws))
        assert meta["engagement_id"] == "e1"
        assert meta["status"] == "UNDERGOING"
        assert meta["finished_at"] is None

    def test_load_returns_none_when_missing(self, tmp_path):
        from drivers.workspace import load_casa_meta
        ws = tmp_path / "w"
        ws.mkdir()
        assert load_casa_meta(str(ws)) is None

    def test_load_falls_back_to_and_migrates_legacy_workspace_path(
            self, tmp_path):
        """Fix-loop round 1 (Important 2): a `.casa-meta.json` written
        BEFORE this release deploys lives only at the legacy workspace path
        (no control-dir copy exists yet). load_casa_meta must still find it
        — dropping it would orphan plugin_artifacts/created_at on finalize
        and permanently leak the workspace past the retention sweep — and
        opportunistically migrate it into the control dir so later reads
        (and any write_casa_meta rewrite) land on the canonical location."""
        import json
        from pathlib import Path
        from drivers.workspace import (
            casa_meta_path, control_dir, load_casa_meta,
        )

        ws = tmp_path / "e-legacy"
        ws.mkdir()
        legacy = ws / ".casa-meta.json"
        meta = {
            "engagement_id": "e-legacy", "executor_type": "hello-driver",
            "status": "COMPLETED", "created_at": "2026-04-23T10:00:00Z",
            "finished_at": "2026-04-23T10:05:00Z",
            "retention_until": "2099-01-01T00:00:00Z",
            "plugin_artifacts": [{"name": "x", "artifact_id": "a" * 64,
                                   "path": "/config/plugins/store/x/" + "a" * 64}],
        }
        legacy.write_text(json.dumps(meta), encoding="utf-8")
        # No control dir at all yet — the pre-deploy state.
        assert not Path(control_dir("e-legacy")).exists()

        loaded = load_casa_meta(str(ws))
        assert loaded == meta

        # Migrated forward: a second load (and the sweep/finalize callers
        # that key off the control-dir path) now find it there too.
        ctl_copy = Path(casa_meta_path("e-legacy"))
        assert ctl_copy.exists()
        assert json.loads(ctl_copy.read_text(encoding="utf-8")) == meta
        # The legacy file is left in place (harmless — it's removed with the
        # rest of the workspace at retention time); re-loading still works
        # and doesn't re-migrate destructively.
        assert legacy.exists()
        assert load_casa_meta(str(ws)) == meta

    @pytest.mark.parametrize("has_openat2", [True, False])
    def test_legacy_fallback_refuses_symlinked_casa_meta(
            self, tmp_path, monkeypatch, has_openat2):
        """Containment stage 2, Task 5: the legacy-path fallback
        (pre-Task-4 ``.casa-meta.json`` still under the WORKSPACE root) is a
        root read of a uid-owned workspace path — once Task 8 chowns the
        workspace, a symlink there is a live sibling-exfiltration primitive.
        A workspace whose ``.casa-meta.json`` is a SYMLINK (e.g. into a
        sibling engagement's control dir) must be refused, not followed —
        treated as absent, never migrated forward, and never returned as if
        it were this engagement's own metadata."""
        import json
        from pathlib import Path

        import safe_fs
        from drivers.workspace import (
            casa_meta_path, control_dir, load_casa_meta,
        )

        monkeypatch.setattr(safe_fs, "HAS_OPENAT2", has_openat2)

        # Sibling engagement's own (legitimate) legacy meta file.
        sibling_ws = tmp_path / "sibling"
        sibling_ws.mkdir()
        sibling_meta = {
            "engagement_id": "sibling", "executor_type": "hello-driver",
            "status": "COMPLETED", "created_at": "2026-04-23T10:00:00Z",
            "finished_at": "2026-04-23T10:05:00Z",
            "retention_until": "2099-01-01T00:00:00Z",
            "plugin_artifacts": [],
        }
        (sibling_ws / ".casa-meta.json").write_text(
            json.dumps(sibling_meta), encoding="utf-8")

        # This engagement's workspace: .casa-meta.json is a SYMLINK to the
        # sibling's file instead of its own.
        ws = tmp_path / "e-legacy-symlink"
        ws.mkdir()
        (ws / ".casa-meta.json").symlink_to(sibling_ws / ".casa-meta.json")
        assert not Path(control_dir("e-legacy-symlink")).exists()

        loaded = load_casa_meta(str(ws))

        assert loaded is None, (
            "a symlinked legacy .casa-meta.json must be refused (treated "
            f"as absent), never followed to a sibling's metadata; got {loaded!r}"
        )
        # Never migrated forward — a refused read must not write the
        # sibling's content into this engagement's own control dir.
        assert not Path(casa_meta_path("e-legacy-symlink")).exists()


class TestProvisionWithHooks:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_settings_json_contains_translated_hooks(self, tmp_path):
        from pathlib import Path
        from drivers.workspace import provision_workspace
        from config import ExecutorDefinition

        # Fake executor dir with hooks.yaml
        exec_dir = tmp_path / "defaults-executors" / "cfg"
        exec_dir.mkdir(parents=True)
        (exec_dir / "prompt.md").write_text("p")
        # Casa hooks.yaml INPUT schema is snake_case (hooks.v1.json /
        # translate_hooks_to_settings); PascalCase is the CC OUTPUT shape.
        (exec_dir / "hooks.yaml").write_text(
            "pre_tool_use:\n"
            "  - policy: casa_config_guard\n"
            "    matcher: Write|Edit\n"
        )
        # Task 4 (#360): provision_workspace now reads the load-time
        # snapshot (ExecutorDefinition.hooks_document, Task 3), not
        # hooks_path — populate it exactly as the loader would.
        hooks_document = {
            "pre_tool_use": [
                {"policy": "casa_config_guard", "matcher": "Write|Edit"},
            ],
        }
        defn = ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
            type="cfg",
            description="A config executor with twenty chars",
            model="sonnet", driver="claude_code",
            prompt_template_path=str(exec_dir / "prompt.md"),
            hooks_path=str(exec_dir / "hooks.yaml"),
            hooks_document=hooks_document,
        )

        (tmp_path / "eng").mkdir()
        path = await provision_workspace(
            engagements_root=str(tmp_path / "eng"),
            engagement_id="e42",
            engagement_auth_token="tok-ws-test",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="x",
        )

        settings = json.loads(
            (Path(path) / ".claude" / "settings.json").read_text()
        )
        assert "PreToolUse" in settings["hooks"]
        entry = settings["hooks"]["PreToolUse"][0]
        # Task 4 (#360): the matcher is forced to the registry canonical
        # value ("Write|Edit|Bash"), not the yaml-declared "Write|Edit".
        assert entry["matcher"] == "Write|Edit|Bash"
        assert entry["hooks"][0]["type"] == "command"
        assert entry["hooks"][0]["command"].endswith(
            "hook_proxy.sh casa_config_guard"
        )

    async def test_settings_json_ignores_post_load_hooks_yaml_mutation(
        self, tmp_path,
    ):
        """TOCTOU red case (#360): provision_workspace must derive settings
        from ``defn.hooks_document`` (the load-time snapshot), never by
        re-reading ``defn.hooks_path`` at provisioning time. Simulate the
        loader having captured a snapshot, then mutate the on-disk
        hooks.yaml afterward (as a config-editable hooks_file: repoint
        would) — the mutation must NOT reach the emitted settings.json."""
        from pathlib import Path
        from drivers.workspace import provision_workspace
        from config import ExecutorDefinition

        exec_dir = tmp_path / "defaults-executors" / "cfg2"
        exec_dir.mkdir(parents=True)
        (exec_dir / "prompt.md").write_text("p")
        hooks_path = exec_dir / "hooks.yaml"
        # Snapshot captured at "load time" (what agent_loader would have
        # produced from the original file).
        snapshot = {
            "pre_tool_use": [
                {"policy": "casa_config_guard", "matcher": "Write|Edit"},
            ],
        }
        hooks_path.write_text(
            "pre_tool_use:\n"
            "  - policy: casa_config_guard\n"
            "    matcher: Write|Edit\n"
        )
        defn = ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
            type="cfg2",
            description="A config executor with twenty chars",
            model="sonnet", driver="claude_code",
            prompt_template_path=str(exec_dir / "prompt.md"),
            hooks_path=str(hooks_path),
            hooks_document=snapshot,
        )

        # Post-load mutation of the file: an attacker (or a config-editable
        # hooks_file: repoint) hollows the policy list to shed the guard.
        hooks_path.write_text("pre_tool_use: []\n")

        (tmp_path / "eng2").mkdir()
        path = await provision_workspace(
            engagements_root=str(tmp_path / "eng2"),
            engagement_id="e43",
            engagement_auth_token="tok-ws-test",
            defn=defn, task="t", context="c",
            casa_framework_mcp_url="x",
        )

        settings = json.loads(
            (Path(path) / ".claude" / "settings.json").read_text()
        )
        commands = [
            e["hooks"][0]["command"]
            for e in settings["hooks"]["PreToolUse"]
        ]
        assert any(c.endswith("casa_config_guard") for c in commands), (
            "settings.json must reflect the load-time snapshot, not the "
            "post-load on-disk mutation"
        )


class TestBuildCcPermissions:
    """L-1 fix: filter defn.tools_allowed to valid CC permission patterns."""

    def _make_minimal_defn(self, tools_allowed, permission_mode="acceptEdits"):
        from config import ExecutorDefinition
        return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
            type="test-fixture",
            description="test fixture twenty-character description here",
            model="sonnet",
            driver="claude_code",
            tools_allowed=list(tools_allowed),
            permission_mode=permission_mode,
        )

    def test_keeps_bash_parameterized(self):
        from drivers.workspace import _build_cc_permissions
        defn = self._make_minimal_defn(["Bash(git*)", "Bash(gh*)"])
        out = _build_cc_permissions(defn)
        assert out["allow"] == ["Bash(git*)", "Bash(gh*)"]

    def test_keeps_bare_tool_names(self):
        from drivers.workspace import _build_cc_permissions
        defn = self._make_minimal_defn(
            ["Read", "Write", "Edit", "Glob", "Grep", "Skill"]
        )
        out = _build_cc_permissions(defn)
        assert out["allow"] == [
            "Read", "Write", "Edit", "Glob", "Grep", "Skill",
        ]

    def test_keeps_mcp_prefixed(self):
        from drivers.workspace import _build_cc_permissions
        defn = self._make_minimal_defn(
            ["mcp__casa-framework__emit_completion",
             "mcp__casa-framework__query_engager"]
        )
        out = _build_cc_permissions(defn)
        assert out["allow"] == [
            "mcp__casa-framework__emit_completion",
            "mcp__casa-framework__query_engager",
        ]

    def test_drops_invalid_with_warning(self, caplog):
        import logging
        from drivers.workspace import _build_cc_permissions
        defn = self._make_minimal_defn(
            ["Bash(git*)", "casa-internal-tool", "", "Read"]
        )
        with caplog.at_level(logging.WARNING, logger="drivers.workspace"):
            out = _build_cc_permissions(defn)
        assert out["allow"] == ["Bash(git*)", "Read"]
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 2

    def test_default_mode_from_defn(self):
        from drivers.workspace import _build_cc_permissions
        defn = self._make_minimal_defn(["Read"], permission_mode="bypassPermissions")
        out = _build_cc_permissions(defn)
        assert out["defaultMode"] == "bypassPermissions"

    def test_default_mode_falls_through_when_empty(self):
        from drivers.workspace import _build_cc_permissions
        defn = self._make_minimal_defn(["Read"], permission_mode="")
        out = _build_cc_permissions(defn)
        assert out["defaultMode"] == "acceptEdits"


class TestDoctrineProvisioning:
    """v0.74.2 (live finding 2026-07-13): the rendered CLAUDE.md references
    doctrine/*.md, but claude_code workspaces never received the executor's
    doctrine/ — the plugin-developer read missing files and proceeded
    without its conventions."""

    @pytest.mark.asyncio
    async def test_doctrine_dir_copied_into_workspace(self, tmp_path):
        from pathlib import Path
        from config import ExecutorDefinition
        from drivers.workspace import provision_workspace

        exec_dir = tmp_path / "executors" / "probe"
        exec_dir.mkdir(parents=True)
        (exec_dir / "prompt.md").write_text("Task: {task}")
        doctrine = exec_dir / "doctrine"
        doctrine.mkdir()
        (doctrine / "casa-conventions.md").write_text("# conventions")
        (doctrine / "recipes").mkdir()
        (doctrine / "recipes" / "r.md").write_text("# recipe")

        defn = ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
            type="probe", description="d", model="m", driver="claude_code",
            enabled=True, tools_allowed=["Read"], tools_disallowed=[],
            permission_mode="acceptEdits",
            prompt_template_path=str(exec_dir / "prompt.md"),
            doctrine_dir=str(doctrine),
            mcp_server_names=[],
        )
        ws = tmp_path / "engagements"
        ws.mkdir()
        path = await provision_workspace(
            engagements_root=str(ws), engagement_id="engd", defn=defn,
            engagement_auth_token="tok-ws-test",
            task="t", context="c",
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework")
        p = Path(path)
        assert (p / "doctrine" / "casa-conventions.md").read_text() == "# conventions"
        assert (p / "doctrine" / "recipes" / "r.md").exists()

    @pytest.mark.asyncio
    async def test_declared_but_missing_doctrine_fails_closed(self, tmp_path):
        """Sol design review: a declared-but-absent doctrine dir must abort
        provisioning, never silently recreate the degradation."""
        from config import ExecutorDefinition
        from drivers.workspace import provision_workspace

        exec_dir = tmp_path / "executors" / "probe"
        exec_dir.mkdir(parents=True)
        (exec_dir / "prompt.md").write_text("Task: {task}")
        defn = ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
            type="probe", description="d", model="m", driver="claude_code",
            enabled=True, tools_allowed=["Read"], tools_disallowed=[],
            permission_mode="acceptEdits",
            prompt_template_path=str(exec_dir / "prompt.md"),
            doctrine_dir=str(exec_dir / "nope"),
            mcp_server_names=[],
        )
        ws = tmp_path / "engagements"
        ws.mkdir()
        with pytest.raises(FileNotFoundError):
            await provision_workspace(
                engagements_root=str(ws), engagement_id="engm", defn=defn,
                engagement_auth_token="tok-ws-test",
                task="t", context="c",
                casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework")

    @pytest.mark.asyncio
    async def test_empty_doctrine_dir_is_the_opt_out(self, tmp_path):
        from config import ExecutorDefinition
        from drivers.workspace import provision_workspace

        exec_dir = tmp_path / "executors" / "probe"
        exec_dir.mkdir(parents=True)
        (exec_dir / "prompt.md").write_text("Task: {task}")
        defn = ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
            type="probe", description="d", model="m", driver="claude_code",
            enabled=True, tools_allowed=["Read"], tools_disallowed=[],
            permission_mode="acceptEdits",
            prompt_template_path=str(exec_dir / "prompt.md"),
            doctrine_dir="",
            mcp_server_names=[],
        )
        ws = tmp_path / "engagements"
        ws.mkdir()
        path = await provision_workspace(
            engagements_root=str(ws), engagement_id="engo", defn=defn,
            engagement_auth_token="tok-ws-test",
            task="t", context="c",
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework")
        assert not (__import__("pathlib").Path(path) / "doctrine").exists()


class TestRefreshClaudeMd:
    """W3/Sol r8-B5 + r10-B4: refresh_claude_md re-derives CLAUDE.md from the
    engagement record's VERBATIM origin["brief"], re-running the SAME whole-file
    interpolation as provision (both template paths)."""

    def _defn_legacy(self, tmp_path, executor_type="hello-driver"):
        from config import ExecutorDefinition
        exec_dir = tmp_path / "defaults-executors" / executor_type
        exec_dir.mkdir(parents=True)
        (exec_dir / "prompt.md").write_text(
            "You are the {executor_type} executor.\nTASK:\n{task}\n"
            "CTX:{context}\nMEM:{executor_memory}\n"
        )
        return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
            type=executor_type, description="test executor twenty chars ok!!",
            model="sonnet", driver="claude_code",
            prompt_template_path=str(exec_dir / "prompt.md"),
            mcp_server_names=["casa-framework"],
        )

    def _defn_template(self, tmp_path, executor_type="tmpl-driver"):
        from config import ExecutorDefinition
        exec_dir = tmp_path / "defaults-executors" / executor_type
        (exec_dir / "workspace-template").mkdir(parents=True)
        (exec_dir / "prompt.md").write_text("unused legacy prompt")
        (exec_dir / "workspace-template" / "CLAUDE.md.tmpl").write_text(
            "EXEC:{executor_type}\nTASK:\n{task}\n"
            "CTX:{context}\nWORLD:{world_state_summary}\nMEM:{executor_memory}\n"
        )
        return ExecutorDefinition(role_artifact=STUB_ROLE_ARTIFACT,
            type=executor_type, description="template executor twenty chars",
            model="sonnet", driver="claude_code",
            prompt_template_path=str(exec_dir / "prompt.md"),
            mcp_server_names=["casa-framework"],
        )

    def _rec(self, brief=None, task="canon task", context="", world=""):
        from engagement_registry import EngagementRecord
        origin = {"context": context, "world_state_summary": world}
        if brief is not None:
            origin["brief"] = brief
        return EngagementRecord(
            id="eng-refresh", kind="executor", role_or_type="hello-driver",
            driver="claude_code", status="active", topic_id=1,
            started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None, origin=origin, task=task,
        )

    async def test_provision_carries_brief_envelope(self, tmp_path):
        """A claude_code workspace provisioned with the rendered brief task →
        CLAUDE.md carries objective + criteria + verbatim process strings +
        completion accounting (not just the bare objective)."""
        from drivers.workspace import provision_workspace
        from drivers.brief import brief_task_for, COMPLETION_ACCOUNTING_LINE

        defn = self._defn_legacy(tmp_path)
        rec = self._rec(brief={
            "objective": "Ship the release",
            "acceptance_criteria": ["CI is green"],
            "process_requirements": ["Squash-merge only"],
        })
        ws = tmp_path / "engagements"
        ws.mkdir()
        path = await provision_workspace(
            engagements_root=str(ws), engagement_id=rec.id, defn=defn,
            engagement_auth_token="tok-ws-test",
            task=brief_task_for(rec, defn), context="",
            casa_framework_mcp_url="http://x",
        )
        claude_md = (__import__("pathlib").Path(path) / "CLAUDE.md").read_text()
        assert "Ship the release" in claude_md
        assert "CI is green" in claude_md
        assert "Squash-merge only" in claude_md
        assert COMPLETION_ACCOUNTING_LINE in claude_md

    async def test_refresh_regression_template_all_sections_return(self, tmp_path):
        """Provision (template path) with non-empty context/world-state/memory,
        blank the CLAUDE.md, refresh → brief sections AND
        context/world_state/memory sections are ALL present again (r10-B4)."""
        from pathlib import Path
        from drivers.workspace import provision_workspace, refresh_claude_md
        from drivers.brief import brief_task_for, COMPLETION_ACCOUNTING_LINE

        defn = self._defn_template(tmp_path)
        brief = {
            "objective": "Rebuild the index",
            "acceptance_criteria": ["index size < 1GB"],
            "process_requirements": ["Take a snapshot first"],
        }
        rec = self._rec(brief=brief, context="ctx-marker",
                        world="world-marker")
        rec.role_or_type = defn.type
        ws = tmp_path / "engagements"
        ws.mkdir()
        path = await provision_workspace(
            engagements_root=str(ws), engagement_id=rec.id, defn=defn,
            engagement_auth_token="tok-ws-test",
            task=brief_task_for(rec, defn), context="ctx-marker",
            world_state_summary="world-marker", executor_memory="mem-marker",
            casa_framework_mcp_url="http://x",
        )
        ws_dir = Path(path)
        # Memory was cached for a later boot refresh — Task 4: control dir.
        from drivers.workspace import executor_memory_path
        assert Path(executor_memory_path(rec.id)).read_text() == "mem-marker"

        # Blank CLAUDE.md (simulate a wiped workspace file), then refresh.
        (ws_dir / "CLAUDE.md").write_text("")
        refresh_claude_md(str(ws_dir), defn=defn, rec=rec)

        claude_md = (ws_dir / "CLAUDE.md").read_text()
        # Brief sections back.
        assert "Rebuild the index" in claude_md
        assert "index size < 1GB" in claude_md
        assert "Take a snapshot first" in claude_md
        assert COMPLETION_ACCOUNTING_LINE in claude_md
        # Every pre-existing placeholder section survived the refresh.
        assert "CTX:ctx-marker" in claude_md
        assert "WORLD:world-marker" in claude_md
        assert "MEM:mem-marker" in claude_md

    async def test_refresh_legacy_path_briefless_uses_task_fallback(self, tmp_path):
        from pathlib import Path
        from drivers.workspace import provision_workspace, refresh_claude_md

        defn = self._defn_legacy(tmp_path)
        rec = self._rec(brief=None, task="plain fallback task", context="c1")
        ws = tmp_path / "engagements"
        ws.mkdir()
        path = await provision_workspace(
            engagements_root=str(ws), engagement_id=rec.id, defn=defn,
            engagement_auth_token="tok-ws-test",
            task=rec.task, context="c1", executor_memory="",
            casa_framework_mcp_url="http://x",
        )
        (Path(path) / "CLAUDE.md").write_text("")
        refresh_claude_md(str(Path(path)), defn=defn, rec=rec)
        claude_md = (Path(path) / "CLAUDE.md").read_text()
        assert "plain fallback task" in claude_md
        assert "CTX:c1" in claude_md


class TestExtraDirContainment:
    """#344: extra_dirs is part of an operator-editable executor definition
    (a mutable trust surface, #312 family) — entries must be contained to
    the approved shared roots, never arbitrary host paths like "/" or
    "/config" (--add-dir grants the engagement CLI read/write there)."""

    @pytest.mark.parametrize("bad", [
        "/", "/config", "/data", "/data/engagements", "/etc",
        "/opt/casa", "/root", "/sharex",
    ])
    def test_outside_approved_roots_rejected(self, bad):
        from drivers.workspace import WorkspaceConfigError, render_run_script
        with pytest.raises(WorkspaceConfigError, match="approved root"):
            render_run_script(
                engagement_id="x" * 16,
                permission_mode="dontAsk",
                extra_dirs=[bad],
             uid=200005, gid=200005)

    @pytest.mark.parametrize("ok", ["/share", "/share/foo", "/media/nas"])
    def test_under_approved_roots_allowed(self, ok):
        from drivers.workspace import render_run_script
        out = render_run_script(
            engagement_id="x" * 16,
            permission_mode="dontAsk",
            extra_dirs=[ok],
         uid=200005, gid=200005)
        assert f"--add-dir {ok}" in out

    def test_dotdot_traversal_rejected(self):
        from drivers.workspace import WorkspaceConfigError, render_run_script
        with pytest.raises(WorkspaceConfigError):
            render_run_script(
                engagement_id="x" * 16,
                permission_mode="dontAsk",
                extra_dirs=["/share/../config"],
             uid=200005, gid=200005)

    def test_symlink_escaping_approved_root_rejected(self, tmp_path, monkeypatch):
        """Terra r1-2: a symlink under an approved root pointing outside
        it must be rejected — --add-dir follows it at CLI runtime."""
        import os

        from drivers import workspace
        from drivers.workspace import WorkspaceConfigError, render_run_script

        share = tmp_path / "share"
        share.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = share / "escape"
        os.symlink(outside, link)
        monkeypatch.setattr(
            workspace, "APPROVED_EXTRA_DIR_ROOTS", (str(share),))

        with pytest.raises(WorkspaceConfigError, match="resolves to"):
            render_run_script(
                engagement_id="x" * 16,
                permission_mode="dontAsk",
                extra_dirs=[str(link)],
             uid=200005, gid=200005)
        # A real subdir under the root still passes.
        real = share / "ok"
        real.mkdir()
        out = render_run_script(
            engagement_id="x" * 16,
            permission_mode="dontAsk",
            extra_dirs=[str(real)],
         uid=200005, gid=200005)
        assert f"--add-dir {real}" in out

    def test_plugin_dirs_are_not_containment_checked(self):
        """plugin_dirs are immutable store paths under /data (§3.8) —
        the containment rule applies to extra_dirs only."""
        from drivers.workspace import render_run_script
        out = render_run_script(
            engagement_id="x" * 16,
            permission_mode="dontAsk",
            extra_dirs=[],
            plugin_dirs=["/data/casa/plugin-store/sha256-abc/artifact"],
         uid=200005, gid=200005)
        assert "--plugin-dir /data/casa/plugin-store/sha256-abc/artifact" in out

    def test_render_run_script_exports_private_outbox_dir(self):
        """Fix-loop round 1, finding 1 (wiring-site coverage): a real uid's
        rendered run script must export CASA_PLUGIN_OUTBOX_DIR pointing at
        that uid's PRIVATE outbox dir — the producer's only way to learn it
        no longer has access to the shared, root-only outbox."""
        import plugin_outbox
        from drivers.workspace import render_run_script
        out = render_run_script(
            engagement_id="x" * 16,
            permission_mode="dontAsk",
            extra_dirs=[],
            uid=200005, gid=200005,
        )
        expected_dir = plugin_outbox.engagement_outbox_dir(200005)
        assert f"export {plugin_outbox.OUTBOX_ENV}='{expected_dir}'" in out

    def test_render_run_script_caller_extra_env_wins_over_outbox_dir(self):
        """Same collision precedence as the plugin-dirs env overlay: an
        explicit extra_env entry for the outbox var wins over the derived
        one."""
        import plugin_outbox
        from drivers.workspace import render_run_script
        out = render_run_script(
            engagement_id="x" * 16,
            permission_mode="dontAsk",
            extra_dirs=[],
            extra_env={plugin_outbox.OUTBOX_ENV: "/custom/outbox"},
            uid=200005, gid=200005,
        )
        assert f"export {plugin_outbox.OUTBOX_ENV}='/custom/outbox'" in out
        assert plugin_outbox.engagement_outbox_dir(200005) not in out
