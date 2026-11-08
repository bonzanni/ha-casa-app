"""Tests for the config_git_commit MCP tool."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


@pytest.fixture
def configurator_origin(monkeypatch):
    """Bug 7 fix: tool refuses unless caller role == 'configurator'. A bound
    runtime with no residents, and every commit a plugins-only one, so a
    commit's reload plan is computed and is empty: these cases pin the commit
    itself (its reload is tests/test_config_git_commit_reload.py's)."""
    import types
    import agent as agent_mod
    import config_git
    monkeypatch.setattr(agent_mod, "active_runtime", types.SimpleNamespace(
        role_configs={}, agents_dir="/nonexistent/agents"), raising=False)
    monkeypatch.setattr(config_git, "changed_paths",
                        lambda config_dir, sha: ["plugins/registry.json"])
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


class TestConfigGitCommitTool:
    async def test_happy_path(self, configurator_origin):
        from tools import config_git_commit
        with patch("config_git.commit_config_checked", return_value=("abc123def", [])), \
                patch("config_git.changed_paths", return_value=["plugins/registry.json"]):
            result = await config_git_commit.handler({"message": "test"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["sha"] == "abc123def"
        assert payload["message"] == "test"
        assert payload["reloads"] == []        # a plugins-only commit reloads nothing

    async def test_noop_returns_empty_sha(self, configurator_origin):
        from tools import config_git_commit
        with patch("config_git.commit_config_checked", return_value=("", [])):
            result = await config_git_commit.handler({"message": "noop"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["sha"] == ""

    async def test_noop_carries_loud_gitignore_warning(self, configurator_origin):
        """P-3 (v0.69.1): a silent {"sha": ""} left agents looping to
        reconcile "committed ok" vs "untracked" when their writes landed on
        gitignored paths. The no-op result must SAY that only whitelisted
        paths are tracked."""
        from tools import config_git_commit
        with patch("config_git.commit_config_checked", return_value=("", [])):
            result = await config_git_commit.handler({"message": "noop"})
        payload = json.loads(result["content"][0]["text"])
        warning = payload.get("warning", "")
        assert "gitignored" in warning
        assert "plugin-env.conf" in warning   # the intentionally-secret case
        assert "registry.json" in warning     # the tracked exception

    async def test_real_commit_has_no_warning(self, configurator_origin):
        from tools import config_git_commit
        with patch("config_git.commit_config_checked", return_value=("abc123def", [])), \
                patch("config_git.changed_paths", return_value=["plugins/registry.json"]):
            result = await config_git_commit.handler({"message": "test"})
        payload = json.loads(result["content"][0]["text"])
        assert "warning" not in payload

    async def test_raises_bubbles_as_error_kind(self, configurator_origin):
        from tools import config_git_commit
        with patch("config_git.commit_config_checked", side_effect=RuntimeError("git broke")):
            result = await config_git_commit.handler({"message": "x"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert "git broke" in payload["message"]


class TestConfigGitCommitRoleGuard:
    """Bug 7 (v0.14.6): config_git_commit must refuse non-configurator
    callers even if their runtime.yaml::tools.allowed lists the tool."""

    async def test_no_origin_refused(self):
        from tools import config_git_commit
        result = await config_git_commit.handler({"message": "x"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"

    async def test_assistant_role_refused(self):
        import agent as agent_mod
        from tools import config_git_commit
        tok = agent_mod.origin_var.set({"role": "assistant"})
        try:
            result = await config_git_commit.handler({"message": "x"})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"

    async def test_plugin_developer_role_refused(self):
        """Plugin-developer is an executor but not authorised for these tools."""
        import agent as agent_mod
        from tools import config_git_commit
        tok = agent_mod.origin_var.set({"role": "plugin-developer"})
        try:
            result = await config_git_commit.handler({"message": "x"})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"


def _checked_that_runs_validator(config_dir, message, validate):
    """Stand-in for commit_config_checked honoring its contract: run the
    supplied validator; errors refuse (no commit), clean commits."""
    errors = validate(config_dir)
    return ("", errors) if errors else ("abcd1234", [])


class TestConfigGitCommitSchemaGate:
    """E-G v0.31.0 pre-commit schema-validation gate. The tool must
    refuse a commit that would land schema-invalid YAML and FATAL the
    addon on next boot. See `bug-review-2026-05-01-exploration.md` and
    `project_eg_configurator_schema_invalid_yaml`. #351 moved the gate
    INSIDE config_git.commit_config_checked (staged-tree validation); the
    refusal semantics under a real repo are pinned in test_config_git.py —
    here the stand-in honors the checked contract while the validator's
    errors are what's under test."""

    async def test_refuses_when_validation_errors_found(
        self, configurator_origin,
    ):
        from tools import config_git_commit
        with patch(
            "agent_loader.validate_config_repo",
            return_value=[
                "/addon_configs/casa/agents/assistant/character.yaml: "
                "schema violation at (root): Additional properties are not "
                "allowed ('TRAIT' was unexpected)",
            ],
        ), patch("config_git.commit_config_checked",
                 side_effect=_checked_that_runs_validator):
            result = await config_git_commit.handler(
                {"message": "add trait"},
            )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "schema_invalid"
        assert payload["errors"][0].endswith(
            "Additional properties are not allowed ('TRAIT' was unexpected)"
        )

    async def test_proceeds_when_validation_clean(self, configurator_origin):
        from tools import config_git_commit
        with patch(
            "agent_loader.validate_config_repo", return_value=[],
        ), patch("config_git.commit_config_checked",
                 side_effect=_checked_that_runs_validator):
            result = await config_git_commit.handler({"message": "ok"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["sha"] == "abcd1234"
        assert payload["message"] == "ok"

    async def test_aggregates_multiple_errors(self, configurator_origin):
        """The error list surfaces every offending file at once so the
        configurator can fix them all in one round-trip."""
        from tools import config_git_commit
        with patch(
            "agent_loader.validate_config_repo",
            return_value=[
                "/p/character.yaml: schema violation at (root): bad1",
                "/p/runtime.yaml: schema violation at (root): bad2",
            ],
        ), patch("config_git.commit_config_checked",
                 side_effect=_checked_that_runs_validator):
            result = await config_git_commit.handler({"message": "bulk"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "schema_invalid"
        assert len(payload["errors"]) == 2
        assert "2 schema validation failure" in payload["message"]


class TestConfigGitCommitCheckedGate:
    """#351: the tool routes through commit_config_checked so validation
    covers exactly the staged tree; #278: the tracked-path strings come from
    config_git.TRACKED_PATHS_SUMMARY (single source of truth)."""

    async def test_validator_refusal_returns_schema_invalid(
            self, configurator_origin):
        from tools import config_git_commit
        with patch("config_git.commit_config_checked",
                   return_value=("", ["agents/x/character.yaml: bad"])):
            result = await config_git_commit.handler({"message": "x"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "schema_invalid"
        assert payload["errors"] == ["agents/x/character.yaml: bad"]

    async def test_checked_commit_receives_the_boot_validator(
            self, configurator_origin):
        """The gate must run agent_loader.validate_config_repo — the same
        codepath boot uses — over the exported staged tree."""
        import agent_loader
        from tools import config_git_commit
        with patch("config_git.commit_config_checked",
                   return_value=("abc", [])) as checked:
            await config_git_commit.handler({"message": "x"})
        _dir, _msg, validator = checked.call_args.args
        assert validator is agent_loader.validate_config_repo


class TestTrackedPathsSummaryPinned:
    """#278: the summary string must stay in step with the gitignore
    whitelist — a drifting explanatory string told agents that bindings/
    (tracked since v0.100.0) was 'gitignored by design'."""

    def _tracked_top_level(self):
        import config_git
        names = set()
        for line in config_git._GITIGNORE_CONTENT.splitlines():
            if line.startswith("!") and line != "!.gitignore":
                names.add(line[1:].split("/")[0])
        return names

    def test_every_tracked_top_level_is_named_in_the_summary(self):
        import config_git
        for name in self._tracked_top_level():
            assert name in config_git.TRACKED_PATHS_SUMMARY, (
                f"tracked path {name!r} missing from TRACKED_PATHS_SUMMARY "
                f"— the whitelist and the operator-facing strings drifted "
                f"again (#278)")

    def test_tool_description_and_warning_carry_the_summary(self):
        import config_git
        from tools import config_git_commit
        assert config_git.TRACKED_PATHS_SUMMARY in config_git_commit.description
