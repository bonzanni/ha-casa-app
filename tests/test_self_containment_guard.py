"""self_containment_guard — pre-push grep for §2.0 anti-patterns."""
from __future__ import annotations

from pathlib import Path

import pytest

from hooks import HOOK_POLICIES

pytestmark = pytest.mark.asyncio


def _build_git_push_input(cwd: Path) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin main", "description": "push"},
        "cwd": str(cwd),
    }


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


@pytest.fixture
def plugin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "casa-plugin-x"
    (repo / ".claude-plugin").mkdir(parents=True)
    _write(repo / ".claude-plugin" / "plugin.json",
           '{"name":"x","description":"d","version":"0.1.0","author":"a"}')
    _write(repo / "README.md", "# x\nNormal readme.\n")
    return repo


async def _run_policy(input_data: dict) -> dict:
    factory = HOOK_POLICIES["self_containment_guard"]["factory"]
    hook = factory()
    return await hook(input_data, None, {})


class TestSelfContainmentGuard:
    async def test_clean_repo_allows(self, plugin_repo: Path) -> None:
        result = await _run_policy(_build_git_push_input(plugin_repo))
        assert result == {}, f"unexpected deny: {result}"

    async def test_readme_please_install_blocks(self, plugin_repo: Path) -> None:
        _write(plugin_repo / "README.md", "# x\nplease install ffmpeg manually.\n")
        result = await _run_policy(_build_git_push_input(plugin_repo))
        assert result is not None
        assert "self_containment_guard" in result["hookSpecificOutput"]["permissionDecisionReason"].lower()

    async def test_apt_install_in_script_blocks(self, plugin_repo: Path) -> None:
        _write(plugin_repo / "scripts" / "setup.sh",
               "#!/bin/sh\napt install -y ffmpeg\n")
        result = await _run_policy(_build_git_push_input(plugin_repo))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_hardcoded_nonbaseline_path_blocks(self, plugin_repo: Path) -> None:
        _write(plugin_repo / "server.py",
               "import subprocess\nsubprocess.run(['/usr/bin/terraform', 'plan'])\n")
        result = await _run_policy(_build_git_push_input(plugin_repo))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_non_push_bash_allowed(self, plugin_repo: Path) -> None:
        input_data = {
            "tool_name": "Bash",
            "tool_input": {"command": "git status", "description": ""},
            "cwd": str(plugin_repo),
        }
        result = await _run_policy(input_data)
        assert result == {}

    async def test_registered_in_hook_policies(self) -> None:
        assert "self_containment_guard" in HOOK_POLICIES
        assert HOOK_POLICIES["self_containment_guard"]["matcher"] == "Bash"


# ---------------------------------------------------------------------------
# P2 (2026-07-18 self-containment plan): untracked/ignored MCP launch refs
# + the real, auditable override.
# ---------------------------------------------------------------------------

import json
import subprocess


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(repo), "PATH": "/usr/bin:/bin"})


@pytest.fixture
def git_plugin_repo(plugin_repo: Path) -> Path:
    _git(plugin_repo, "init", "-q")
    _git(plugin_repo, "add", "-A")
    _git(plugin_repo, "commit", "-qm", "init")
    return plugin_repo


def _mcp(repo: Path, servers: dict, commit: bool = True) -> None:
    _write(repo / ".mcp.json", json.dumps({"mcpServers": servers}))
    if commit:
        _git(repo, "add", ".mcp.json")
        _git(repo, "commit", "-qm", "mcp")


def _deny_reason(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


class TestMcpLaunchRefTracking:
    async def test_gitignored_venv_ref_blocks(self, git_plugin_repo: Path):
        """The gmail-v0.2.0 repro: the interpreter EXISTS in the working tree
        but is gitignored — the installed artifact will not contain it."""
        repo = git_plugin_repo
        _write(repo / ".gitignore", "server/.venv/\n")
        _write(repo / "server" / ".venv" / "bin" / "python", "fake")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore")
        _mcp(repo, {"gmail": {
            "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}})
        result = await _run_policy(_build_git_push_input(repo))
        assert result and result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert ".venv" in _deny_reason(result)
        assert "not in the pushed commit" in _deny_reason(result)

    async def test_tracked_refs_allow(self, git_plugin_repo: Path):
        repo = git_plugin_repo
        _write(repo / "server" / "server.py", "print('serve')\n")
        _git(repo, "add", "server/server.py")
        _git(repo, "commit", "-qm", "server")
        _mcp(repo, {"s": {"command": "python3",
                          "args": ["${CLAUDE_PLUGIN_ROOT}/server/server.py"]}})
        result = await _run_policy(_build_git_push_input(repo))
        assert result == {}, f"unexpected deny: {result}"

    async def test_parent_escape_ref_blocks(self, git_plugin_repo: Path):
        repo = git_plugin_repo
        _mcp(repo, {"s": {"command": "python3",
                          "args": ["${CLAUDE_PLUGIN_ROOT}/../outside.py"]}})
        result = await _run_policy(_build_git_push_input(repo))
        assert result and result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "escapes" in _deny_reason(result)

    async def test_absolute_after_interpolation_blocks(self, git_plugin_repo: Path):
        repo = git_plugin_repo
        _mcp(repo, {"s": {"command": "${CLAUDE_PLUGIN_ROOT}//etc/ssl/x"}})
        result = await _run_policy(_build_git_push_input(repo))
        assert result and result["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_resolves_repo_root_from_subdir_cwd(self, git_plugin_repo: Path):
        """Never assume tool cwd == repo root: push may run from a subdir."""
        repo = git_plugin_repo
        _write(repo / ".gitignore", "server/.venv/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore")
        _mcp(repo, {"s": {
            "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}})
        sub = repo / "server"
        sub.mkdir(exist_ok=True)
        result = await _run_policy(_build_git_push_input(sub))
        assert result and result["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_non_git_cwd_skips_tracking_check(self, plugin_repo: Path):
        """No repo → tracked-ness is unjudgeable; other checks still apply."""
        _write(plugin_repo / ".mcp.json", json.dumps({"mcpServers": {
            "s": {"command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}}}))
        result = await _run_policy(_build_git_push_input(plugin_repo))
        assert result == {}

    async def test_override_env_prefix_allows_and_logs(
            self, git_plugin_repo: Path, caplog):
        repo = git_plugin_repo
        _write(repo / ".gitignore", "server/.venv/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore")
        _mcp(repo, {"s": {
            "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}})
        input_data = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "CASA_ALLOW_ANTI_PATTERN=1 git push origin main",
                "description": "push"},
            "cwd": str(repo),
        }
        import logging
        with caplog.at_level(logging.WARNING):
            result = await _run_policy(input_data)
        assert result == {}
        assert any("override" in r.message and ".venv" in r.message
                   for r in caplog.records)

    async def test_denial_text_advertises_real_override(self, git_plugin_repo: Path):
        repo = git_plugin_repo
        _mcp(repo, {"s": {"command": "${CLAUDE_PLUGIN_ROOT}/gone/bin/x"}})
        result = await _run_policy(_build_git_push_input(repo))
        reason = _deny_reason(result)
        assert "CASA_ALLOW_ANTI_PATTERN=1" in reason
        assert "--allow-anti-pattern" not in reason


class TestGuardArmingAndOracle:
    """Sol r4-3/4/6/7: recognizer variants, HEAD-tree oracle, env refs,
    subdir cwd for the original tree scan."""

    def _push(self, repo: Path, cmd: str) -> dict:
        return {"tool_name": "Bash",
                "tool_input": {"command": cmd, "description": ""},
                "cwd": str(repo)}

    async def _denied(self, repo: Path, cmd: str) -> bool:
        r = await _run_policy(self._push(repo, cmd))
        return bool(r) and r["hookSpecificOutput"]["permissionDecision"] == "deny"

    @pytest.fixture
    def bad_repo(self, git_plugin_repo: Path) -> Path:
        repo = git_plugin_repo
        _write(repo / ".gitignore", "server/.venv/\n")
        # Mirror the real incident: the dev-only venv EXISTS in the worktree.
        _write(repo / "server" / ".venv" / "bin" / "python", "fake")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore")
        _mcp(repo, {"s": {
            "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}})
        return repo

    async def test_foreign_env_prefix_still_scans(self, bad_repo: Path):
        assert await self._denied(bad_repo, "FOO=1 git push origin main")

    async def test_git_dash_c_push_scans(self, bad_repo: Path):
        assert await self._denied(bad_repo, "git -C . push origin main")

    async def test_compound_command_push_scans(self, bad_repo: Path):
        assert await self._denied(
            bad_repo, "cd server && git push origin main")

    async def test_git_stash_push_does_not_arm(self, git_plugin_repo: Path):
        r = await _run_policy(self._push(git_plugin_repo,
                                         "git stash push -m wip"))
        assert r == {}

    async def test_quoted_override_allows_and_logs(self, bad_repo: Path, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            r = await _run_policy(self._push(
                bad_repo, "CASA_ALLOW_ANTI_PATTERN='1' git push origin main"))
        assert r == {}
        assert any("override" in rec.message for rec in caplog.records)

    async def test_staged_but_uncommitted_ref_blocks(self, git_plugin_repo: Path):
        """Sol r4-4: ls-files sees the index; the PUSHED commit (HEAD) does
        not contain a merely-staged file — must deny."""
        repo = git_plugin_repo
        _mcp(repo, {"s": {"command": "python3",
                          "args": ["${CLAUDE_PLUGIN_ROOT}/server/server.py"]}})
        _write(repo / "server" / "server.py", "print('x')\n")
        _git(repo, "add", "server/server.py")   # staged, NOT committed
        assert await self._denied(repo, "git push origin main")

    async def test_untracked_env_vendor_dir_blocks(self, git_plugin_repo: Path):
        """Sol r4-6: PYTHONPATH vendor dir gitignored → deny."""
        repo = git_plugin_repo
        _write(repo / ".gitignore", "server/vendor/\n")
        _write(repo / "server" / "vendor" / "pkg" / "__init__.py", "")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore")
        _mcp(repo, {"s": {"command": "python3",
                          "env": {"PYTHONPATH":
                                  "${CLAUDE_PLUGIN_ROOT}/server/vendor"}}})
        assert await self._denied(repo, "git push origin main")

    async def test_subdir_cwd_scans_whole_tree_for_anti_patterns(
            self, git_plugin_repo: Path):
        """Sol r4-7: from repo/server, a root README anti-pattern must still
        be found (tree scan runs from the repo root, not cwd)."""
        repo = git_plugin_repo
        _write(repo / "README.md", "# x\nplease install ffmpeg manually.\n")
        sub = repo / "server"
        sub.mkdir(exist_ok=True)
        assert await self._denied(sub, "git push origin main")

    async def test_mcp_json_read_from_head_not_worktree(self, bad_repo: Path):
        """Sol r5-2: the PUSHED commit's .mcp.json is what ships — deleting
        or fixing it only in the worktree must not hide the broken commit."""
        (bad_repo / ".mcp.json").unlink()
        assert await self._denied(bad_repo, "git push origin main")

    async def test_worktree_only_breakage_does_not_block(
            self, git_plugin_repo: Path):
        """Converse of r5-2: HEAD is clean; an uncommitted broken .mcp.json
        is not in the pushed commit and must not deny."""
        repo = git_plugin_repo
        _write(repo / "server" / "server.py", "print('x')\n")
        _git(repo, "add", "server/server.py")
        _git(repo, "commit", "-qm", "server")
        _mcp(repo, {"s": {"command": "python3",
                          "args": ["${CLAUDE_PLUGIN_ROOT}/server/server.py"]}})
        _write(repo / ".mcp.json", json.dumps({"mcpServers": {"s": {
            "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}}}))
        r = await _run_policy(self._push(repo, "git push origin main"))
        assert r == {}, f"unexpected deny: {r}"

    async def test_git_dash_c_other_repo_scans_target(
            self, tmp_path: Path, bad_repo: Path):
        """Sol r5-3: `git -C <other> push` must scan the TARGET repo."""
        clean = tmp_path / "clean-cwd"
        clean.mkdir()
        assert await self._denied(
            clean, f"git -C {bad_repo} push origin main")

    async def test_cd_other_repo_scans_target(
            self, tmp_path: Path, bad_repo: Path):
        clean = tmp_path / "clean-cwd2"
        clean.mkdir()
        assert await self._denied(
            clean, f"cd {bad_repo} && git push origin main")

    async def test_cd_newline_separated_scans_target(
            self, tmp_path: Path, bad_repo: Path):
        """#348: bash treats a newline as a command separator exactly like
        `;` — a two-line `cd <repo>\\ngit push` must scan <repo>, not the
        hook cwd."""
        clean = tmp_path / "clean-cwd3"
        clean.mkdir()
        assert await self._denied(
            clean, f"true\ncd {bad_repo}\ngit push origin main")

    async def test_cd_after_or_separator_scans_target(
            self, tmp_path: Path, bad_repo: Path):
        """#348 companion: `||` and `|`-adjacent separators are command
        separators too — `true || cd <repo>; git push` may run the cd."""
        clean = tmp_path / "clean-cwd4"
        clean.mkdir()
        assert await self._denied(
            clean, f"false || cd {bad_repo}; git push origin main")

    async def test_unexecuted_conditional_cd_cannot_redirect_scan(
            self, tmp_path: Path, bad_repo: Path):
        """Terra r1 (#348): the guard cannot know which cds actually execute —
        `true || cd <clean>` never runs, the push happens FROM bad_repo. The
        scan is a UNION over the hook cwd and every textual cd target, so a
        conditional cd can never redirect it away from the pushed repo."""
        clean = tmp_path / "clean-decoy"
        clean.mkdir()
        assert await self._denied(
            bad_repo, f"true || cd {clean}\ngit push origin main")

    async def test_nonexistent_cd_target_still_scans_hook_cwd(
            self, bad_repo: Path):
        """Terra r1 (#348): a nonexistent cd target must not fail OPEN — the
        hook cwd is always in the scanned union."""
        assert await self._denied(
            bad_repo, "cd /nonexistent-dir-xyz; git push origin main")

    async def test_branching_relative_cd_scans_diverged_base(
            self, tmp_path: Path, bad_repo: Path):
        """Terra r2 (#348): each cd may or may not have executed — a relative
        cd must rebase from EVERY feasible prior base. `false && cd decoy;
        cd ../<bad>` really lands in <bad> (decoy never ran), which one
        linear chain would compute as decoy/../<bad> and fail to resolve."""
        clean = bad_repo.parent / "clean-branch-cwd"
        clean.mkdir()
        assert await self._denied(
            clean,
            f"false && cd decoy; cd ../{bad_repo.name}; git push origin main")

    async def test_cd_dashdash_and_flag_target_is_parsed(
            self, tmp_path: Path, bad_repo: Path):
        """Sol r2 (#348): `cd -P -- <dir>` — flags and the `--` terminator
        must not be mistaken for the target."""
        clean = tmp_path / "clean-flags-cwd"
        clean.mkdir()
        assert await self._denied(
            clean, f"cd -P -- {bad_repo} && git push origin main")

    async def test_multiple_git_dash_c_options_apply_sequentially(
            self, tmp_path: Path, bad_repo: Path):
        """Sol r2 (#348): git applies every -C in order, a relative -C
        resolving against the previous one."""
        clean = tmp_path / "clean-multic-cwd"
        clean.mkdir()
        assert await self._denied(
            clean,
            f"git -C {bad_repo.parent} -C {bad_repo.name} push origin main")

    async def test_cd_after_then_keyword_is_recognized(
            self, tmp_path: Path, bad_repo: Path):
        """Sol r3 (#348): a cd introduced by a shell keyword body
        (`if …; then cd <bad>`) must still rebase the scan."""
        clean = tmp_path / "clean-then-cwd"
        clean.mkdir()
        assert await self._denied(
            clean,
            f"if true; then cd {bad_repo}; git push origin main; fi")

    async def test_cd_inside_subshell_is_recognized(
            self, tmp_path: Path, bad_repo: Path):
        """Sol r3 (#348): `(cd <bad>; git push)` — the subshell paren is a
        cd position too."""
        clean = tmp_path / "clean-subsh-cwd"
        clean.mkdir()
        assert await self._denied(
            clean, f"(cd {bad_repo}; git push origin main)")

    async def test_cd_in_case_branch_is_recognized(
            self, tmp_path: Path, bad_repo: Path):
        """Terra/Sol r4 (#348): a case-pattern `)` precedes the cd —
        recognized because EVERY standalone cd word contributes a
        candidate."""
        clean = tmp_path / "clean-case-cwd"
        clean.mkdir()
        assert await self._denied(
            clean,
            f"case x in x) cd {bad_repo};; esac; git push origin main")

    async def test_negated_and_command_prefixed_cd_are_recognized(
            self, tmp_path: Path, bad_repo: Path):
        """Terra/Sol r4 (#348): `! cd <bad>` still changes directory, and
        `command cd <bad>` is the builtin through a wrapper word."""
        clean = tmp_path / "clean-neg-cwd"
        clean.mkdir()
        assert await self._denied(
            clean, f"if ! cd {bad_repo}; then :; fi; git push origin main")
        clean2 = tmp_path / "clean-cmdword-cwd"
        clean2.mkdir()
        assert await self._denied(
            clean2, f"command cd {bad_repo}; git push origin main")

    async def test_cd_with_redirection_before_target_is_recognized(
            self, tmp_path: Path, bad_repo: Path):
        """Terra r5 (#348): bash resolves `cd 2>/dev/null /bad` (and the
        stdin form) to /bad — redirection tokens between cd and its target
        must be skipped, never captured as the target."""
        for i, redir in enumerate(("2>/dev/null", "</dev/null",
                                   "-P 2>/dev/null", "2> /dev/null",
                                   "&>/dev/null", "2>& 1", "2>| /dev/null",
                                   "<<< x", "<& 0", "&> /dev/null",
                                   "1>&2", ">> log",
                                   # Terra/Sol r7: {fd} descriptors, quoted
                                   # and escaped operands, `<<-` heredoc.
                                   "{fd}>/dev/null", "{fd}>&-",
                                   '2>"log file"', "2>'log file'",
                                   "2>log\\ file", '>"literal log"',
                                   "<<- EOF")):
            clean = tmp_path / f"clean-redir-cwd{i}"
            clean.mkdir()
            assert await self._denied(
                clean, f"cd {redir} {bad_repo} && git push origin main"), redir

    async def test_cd_target_with_brace_char_is_kept_whole(
            self, tmp_path: Path, git_plugin_repo: Path, bad_repo: Path):
        """Sol r5 (#348): `}` is a reserved word, not a metachar — a glued
        `}` belongs to the target word. A bad repo literally named `bad}`
        must be scanned as such."""
        import shutil
        # A parent where the BRACE-TRUNCATED name resolves to nothing — a
        # truncating parser sees a nonexistent candidate and allows.
        brace_repo = tmp_path / "braceland" / "bad}"
        shutil.copytree(bad_repo, brace_repo, symlinks=True)
        clean = tmp_path / "clean-brace-cwd"
        clean.mkdir()
        assert await self._denied(
            clean, f"cd {brace_repo}; git push origin main")

    async def test_quoted_separator_in_target_is_not_truncated(
            self, tmp_path: Path, bad_repo: Path):
        """Terra/Sol r8 (#348): a separator INSIDE quotes (or behind a
        backslash) is part of the path word — `cd '/tmp/bad;repo'` really
        enters that directory and must be scanned."""
        import shutil
        for i, (name, spell) in enumerate((
                ("bad;repo", "'{p}'"), ("bad&repo", '"{p}"'),
                ("bad|repo", "'{p}'"), ("bad;esc", "{esc}"))):
            target = tmp_path / f"qsep{i}" / name
            shutil.copytree(bad_repo, target, symlinks=True)
            clean = tmp_path / f"clean-qsep-cwd{i}"
            clean.mkdir()
            arg = spell.format(p=target,
                               esc=str(target).replace(";", "\\;"))
            assert await self._denied(
                clean, f"cd {arg} && git push origin main"), arg

    async def test_quoted_or_glued_cd_command_word_is_recognized(
            self, tmp_path: Path, bad_repo: Path):
        """Terra/Sol r8-r10 (#348): the command word itself may be quoted or
        escaped (`"cd" /bad`, `c''d /bad`, `c\\d /bad`) and needs no space
        before an operator (`cd</dev/null /bad`).

        Deliberately NOT included: `cd'/bad'` — verified against bash, that
        concatenates into the single word `cd/bad` (command-not-found), so
        it is not a cd invocation and nothing is owed."""
        forms = [
            f'"cd" {bad_repo}',
            f"c''d {bad_repo}",
            f"cd</dev/null {bad_repo}",
            # Terra r9/r10: backslash — including a line continuation — is
            # bash's third literal-quoting mechanism; `c\d` and `c\<nl>d`
            # are the cd builtin too.
            f"c\\d {bad_repo}",
            f'c"d" {bad_repo}',
            f"c\\\nd {bad_repo}",
            # Terra r10: quoted command word AND a quoted separator inside
            # the target, together.
            f"c''d '{bad_repo}'",
        ]
        for i, form in enumerate(forms):
            clean = tmp_path / f"clean-cmdword{i}"
            clean.mkdir()
            assert await self._denied(
                clean, f"{form} && git push origin main"), form

    async def test_cd_behind_wrapper_options_and_leading_redirects(
            self, tmp_path: Path, bad_repo: Path):
        """Terra/Sol r11 (#348): a wrapper's OWN options (`command -p cd`)
        and leading redirections (`2>/dev/null cd …`) both sit before the
        command word — every `cd` token counts, wherever it sits."""
        forms = [
            f"command -p cd {bad_repo}",
            f"2>/dev/null cd {bad_repo}",
            f"env -i cd {bad_repo}",
            f"FOO=1 2>/dev/null cd {bad_repo}",
        ]
        for i, form in enumerate(forms):
            clean = tmp_path / f"clean-prefix{i}"
            clean.mkdir()
            assert await self._denied(
                clean, f"{form}; git push origin main"), form

    async def test_dash_prefixed_dir_after_ddash_propagates_to_chain(
            self, tmp_path: Path, bad_repo: Path):
        """Terra r12 (#348): after `--` every word is an OPERAND — a
        directory literally named `-weird` must become the base for the
        following relative cd, or the chain's real target is never scanned."""
        import shutil
        holder = tmp_path / "holder"
        (holder / "-weird").mkdir(parents=True)
        shutil.copytree(bad_repo, holder / "-weird" / "inner", symlinks=True)
        assert await self._denied(
            holder, "cd -- -weird; cd inner; git push origin main")

    async def test_earlier_quoted_git_push_does_not_truncate_scan(
            self, tmp_path: Path, bad_repo: Path):
        """Sol r12 (#348): the arming match may be another command's
        ARGUMENT (`echo git push; cd <bad>; git push`) — collecting cds only
        from the text before it would skip the real one."""
        clean = tmp_path / "clean-echo-cwd"
        clean.mkdir()
        assert await self._denied(
            clean, f"echo git push; cd {bad_repo}; git push origin main")

    async def test_directory_literally_named_cd_is_scanned(
            self, tmp_path: Path, bad_repo: Path):
        """Sol r13 (#348): `cd cd` — a directory NAMED `cd` is a legitimate
        operand; the token must count as both operand and (harmlessly) the
        start of another cd."""
        holder = tmp_path / "cd-holder"
        holder.mkdir()
        (holder / "cd").symlink_to(bad_repo, target_is_directory=True)
        assert await self._denied(holder, "cd cd; git push origin main")

    async def test_redirect_operand_is_not_the_chain_base(
            self, tmp_path: Path, bad_repo: Path):
        """Terra r13 (#348): `cd 2> /dev/null <dir>` — the redirection TARGET
        must not seed the base for a following relative cd, or the chain's
        real destination is never scanned."""
        import shutil
        # The bad repo is reachable ONLY as a sibling of the first cd's
        # target — never from the hook cwd — so the scan can only find it
        # when that target (not the redirection operand) seeded the base.
        chain = tmp_path / "chain"
        (chain / "x").mkdir(parents=True)
        shutil.copytree(bad_repo, chain / "badsib", symlinks=True)
        clean = tmp_path / "clean-redirbase"
        clean.mkdir()
        assert await self._denied(
            clean,
            f"cd 2> /dev/null {chain}/x; cd ../badsib; git push origin main")

    async def test_hash_inside_target_word_is_literal(
            self, tmp_path: Path, bad_repo: Path):
        """Terra r14 (#348): bash treats `#` as a comment only at the START
        of a word — `cd /tmp/dir#name` is a literal path, so the lexer must
        not truncate it."""
        import shutil
        target = tmp_path / "hashdir" / "repo#bad"
        shutil.copytree(bad_repo, target, symlinks=True)
        clean = tmp_path / "clean-hash"
        clean.mkdir()
        assert await self._denied(
            clean, f"cd {target}; git push origin main")

    async def test_single_quoted_line_continuation_is_literal(
            self, tmp_path: Path, bad_repo: Path):
        """Sol r14 (#348): bash honors `\\<newline>` outside quotes and in
        double quotes, but inside SINGLE quotes it is literal path data — so
        the continuation strip must be quote-aware."""
        import shutil
        target = tmp_path / "contdir" / "part\\\nrest"
        shutil.copytree(bad_repo, target, symlinks=True)
        clean = tmp_path / "clean-cont"
        clean.mkdir()
        assert await self._denied(
            clean, f"cd '{target}'; git push origin main")

    async def test_line_continuation_inside_git_push_still_arms(
            self, tmp_path: Path, bad_repo: Path):
        """Sol r15 (#348): a continuation between `git` and `push` is
        removed by bash before the words are formed — the guard must arm."""
        clean = tmp_path / "clean-contarm"
        clean.mkdir()
        assert await self._denied(
            clean, f"cd {bad_repo} && git \\\npush origin main")

    async def test_logical_dotdot_across_symlink_is_scanned(
            self, tmp_path: Path, bad_repo: Path):
        """Terra r15 (#348): `cd` is LOGICAL by default — `cd /a/link;
        cd ../b` lands in `/a/b`, not in the symlink target's parent."""
        import shutil
        physical = tmp_path / "physical" / "elsewhere"
        physical.mkdir(parents=True)
        lexical = tmp_path / "lexical"
        lexical.mkdir()
        (lexical / "link").symlink_to(physical, target_is_directory=True)
        shutil.copytree(bad_repo, lexical / "bad", symlinks=True)
        clean = tmp_path / "clean-logical"
        clean.mkdir()
        assert await self._denied(
            clean, f"cd {lexical}/link; cd ../bad; git push origin main")

    async def test_cd_chain_overflow_fails_closed(
            self, tmp_path: Path, git_plugin_repo: Path):
        """Terra/Sol r3 (#348): once the feasible-base set overflows, later
        cds are unexamined — the guard must DENY (with the logged override as
        the escape hatch), never silently scan a partial candidate set."""
        clean = tmp_path / "clean-overflow-cwd"
        clean.mkdir()
        # Seven conditional relative cds double the base set each time
        # (2^7 = 128 > 64); the push itself targets a CLEAN repo — the denial
        # must come from the fail-closed complexity finding alone.
        chain = "; ".join(f"false && cd d{i}" for i in range(7))
        r = await _run_policy(self._push(
            clean, f"{chain}; cd {git_plugin_repo}\ngit push origin main"))
        assert bool(r) and (
            r["hookSpecificOutput"]["permissionDecision"] == "deny")
        assert "too complex" in _deny_reason(r)

    async def test_reserved_env_self_declaration_blocks(
            self, git_plugin_repo: Path):
        """G6 corrected: a committed .mcp.json self-declaring a CLI-reserved
        env var must block at push time (it shadows the CLI's native value
        with a literal at runtime)."""
        repo = git_plugin_repo
        _write(repo / "server.py", "print('x')\n")
        _git(repo, "add", "server.py")
        _git(repo, "commit", "-qm", "server")
        _mcp(repo, {"s": {
            "command": "python3",
            "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"],
            "env": {"CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}"}}})
        r = await _run_policy(self._push(repo, "git push origin main"))
        assert r and r["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "CLI-reserved" in _deny_reason(r)

    async def test_reserved_env_worktree_only_does_not_block(
            self, git_plugin_repo: Path):
        """HEAD-tree semantics: an uncommitted reserved-key declaration is
        not in the pushed commit — must not deny."""
        repo = git_plugin_repo
        _write(repo / "server.py", "print('x')\n")
        _git(repo, "add", "server.py")
        _git(repo, "commit", "-qm", "server")
        _mcp(repo, {"s": {"command": "python3",
                          "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"]}})
        _write(repo / ".mcp.json", json.dumps({"mcpServers": {"s": {
            "command": "python3",
            "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"],
            "env": {"CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}"}}}}))
        r = await _run_policy(self._push(repo, "git push origin main"))
        assert r == {}, f"unexpected deny: {r}"

    async def test_git_dash_c_quoted_spaced_path_scans_target(
            self, tmp_path: Path, git_plugin_repo: Path):
        """Sol r6-1: a quoted -C path containing spaces must still arm."""
        import shutil
        spaced = tmp_path / "bad repo"
        shutil.copytree(git_plugin_repo, spaced)
        _mcp(spaced, {"s": {
            "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}})
        clean = tmp_path / "clean-cwd3"
        clean.mkdir()
        assert await self._denied(
            clean, f'git -C "{spaced}" push origin main')


# ---------------------------------------------------------------------------
# #714: the fixture's own `git commit` must leave no background writer running
# against a repository this file then byte-copies.  The eight bare
# `shutil.copytree` sites above walk a live `.git` with `os.scandir` and copy
# afterwards, so an entry that git's detached auto-maintenance child creates and
# unlinks in that window is collected into a `shutil.Error` -- the tier2 flake on
# protected main at 1e17f7d1.  A green suite is NOT evidence here (0/300 and
# 0/250 local loops never reproduced the race), so the pin counts the WRITER,
# not the copy.
# ---------------------------------------------------------------------------

def _git_supports_auto_maintenance(tmp_path: Path) -> bool:
    """True if this git has the `maintenance` command at all.

    Deliberately matcher-free: it reads no trace2 output, so a broken argv
    matcher cannot route the pin into a skip (it must fail the control arm
    instead).
    """
    probe = tmp_path / "maintenance-capability-probe"
    probe.mkdir()
    _git(probe, "init", "-q")
    done = subprocess.run(
        ["git", "-C", str(probe), "maintenance", "run", "--auto", "--quiet"],
        capture_output=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "HOME": str(probe), "PATH": "/usr/bin:/bin"})
    return done.returncode == 0


def _maintenance_children(repo: Path, trace: Path, probe: str) -> list[list[str]]:
    """Commit `probe` in `repo` with trace2 bound FRESH to `trace`; return the
    argv of every maintenance/gc child that commit spawned.

    trace2 is honoured only from the environment and from *global* config -- not
    from `-c` and not from repo-local config (measured: `git -c
    trace2.eventTarget=... commit` writes no file at all).  `_git` replaces the
    environment and sets `HOME` to the repo, so `<repo>/.gitconfig` IS this
    repo's global config and is the working carrier.  A byte-copied `.gitconfig`
    keeps the SOURCE's absolute target, which is why every caller rebinds.

    The observation contract lives HERE rather than in each arm: a zero count
    must mean "observed the commit, and it spawned no writer", never "did not
    observe".  So the target is rebound immediately before the commit (nothing
    earlier can be in the file) and exactly one `commit` command event must be
    present before any match is returned.
    """
    _write(repo / ".gitconfig", f"[trace2]\n\teventTarget = {trace}\n")
    _write(repo / probe, "probe\n")
    _git(repo, "add", "--", probe)
    _git(repo, "commit", "-qm", f"probe {probe}")

    assert trace.exists(), (
        f"trace2 wrote nothing to {trace}: the probe commit was not observed, "
        f"so a zero maintenance-child count would be meaningless")
    events = []
    for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # tolerate a torn trailing line from the detached child
    commits = [e for e in events
               if e.get("event") == "cmd_name" and "commit" in str(e.get("name", ""))]
    assert len(commits) == 1, (
        f"expected exactly 1 traced `commit` in {trace}, got {len(commits)}: "
        f"the arm did not observe its own probe commit "
        f"({len(events)} events total)")
    return [e["argv"] for e in events
            if e.get("event") == "child_start"
            and len(e.get("argv") or []) > 1
            and e["argv"][1] in ("maintenance", "gc")]


def _local_maintenance_auto(repo: Path) -> list[str]:
    """Every repo-local value of maintenance.auto, as text (stdout is bytes)."""
    done = subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "--get-all",
         "maintenance.auto"],
        capture_output=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "HOME": str(repo), "PATH": "/usr/bin:/bin"})
    return done.stdout.decode().split()


async def test_fixture_commit_spawns_no_background_writer(
        tmp_path: Path, git_plugin_repo: Path) -> None:
    """#714: no maintenance/gc child from a fixture commit, in the repo OR a copy.

    Three arms.  The CONTROL proves the predicate can fire on this git build
    (a zero there fails, it does not skip).  The TREATMENT and COPY arms are the
    property the eight copytree sites depend on.
    """
    import shutil

    if not _git_supports_auto_maintenance(tmp_path):
        pytest.skip("this git has no `maintenance` command; the race cannot occur")

    # --- control: a repo built the pre-fix way, auto-maintenance explicitly on.
    control = tmp_path / "control-repo"
    control.mkdir()
    _git(control, "init", "-q")
    _git(control, "config", "--local", "maintenance.auto", "true")
    spawned = _maintenance_children(control, tmp_path / "control.trace", "c.txt")
    assert len(spawned) >= 1, (
        "control arm observed 0 maintenance children with maintenance.auto=true: "
        "the oracle cannot see the writer, so the assertions below would be "
        f"vacuous (git: {subprocess.run(['git', '--version'], capture_output=True).stdout!r})")

    # --- treatment: the real fixture.  Count FIRST, so the pre-fix tree fails on
    # the outcome (a writer was spawned) rather than on the arrangement.
    treated = _maintenance_children(
        git_plugin_repo, tmp_path / "treatment.trace", "t.txt")
    assert treated == [], f"fixture commit spawned background writers: {treated}"
    assert _local_maintenance_auto(git_plugin_repo) == ["false"], (
        "git_plugin_repo must disable auto-maintenance repo-locally, so that "
        "every shutil.copytree of it inherits the setting through .git/config")

    # --- copy: what line 692 actually does, and what line 693 commits into.
    copy = tmp_path / "copied repo"
    shutil.copytree(git_plugin_repo, copy)
    copied = _maintenance_children(copy, tmp_path / "copy.trace", "p.txt")
    assert copied == [], f"commit inside the copy spawned background writers: {copied}"
    assert _local_maintenance_auto(copy) == ["false"], (
        "the byte-copy did not inherit maintenance.auto=false from .git/config")
