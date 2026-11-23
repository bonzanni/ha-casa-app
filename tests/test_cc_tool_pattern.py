"""Tests for cc_tool_pattern — CC CLI tools.allowed pattern matcher."""

from __future__ import annotations

import pytest


class TestBareToolName:
    def test_bare_matches_same_tool(self):
        from cc_tool_pattern import matches
        assert matches("Read", "Read", {}) is True

    def test_bare_ignores_input(self):
        from cc_tool_pattern import matches
        assert matches("Read", "Read", {"file_path": "anything"}) is True

    def test_bare_does_not_match_other_tool(self):
        from cc_tool_pattern import matches
        assert matches("Read", "Write", {}) is False

    def test_mcp_bare_match(self):
        from cc_tool_pattern import matches
        full = "mcp__casa-framework__memory_read"
        assert matches(full, full, {"key": "v"}) is True


class TestBashSpec:
    def test_npm_prefix_matches(self):
        from cc_tool_pattern import matches
        assert matches(
            "Bash(npm*)", "Bash", {"command": "npm install"},
        ) is True

    def test_npm_mismatch(self):
        from cc_tool_pattern import matches
        assert matches(
            "Bash(npm*)", "Bash", {"command": "git push"},
        ) is False

    def test_exact_match(self):
        from cc_tool_pattern import matches
        assert matches(
            "Bash(git status)", "Bash", {"command": "git status"},
        ) is True

    def test_empty_command(self):
        from cc_tool_pattern import matches
        assert matches("Bash(npm*)", "Bash", {}) is False

    def test_glob_question_mark(self):
        from cc_tool_pattern import matches
        assert matches(
            "Bash(curl http?://*)", "Bash",
            {"command": "curl https://example.com"},
        ) is True

    def test_pipeline_string_match(self):
        """Documented limitation: matches the raw command string the same
        way CC does (no argv split)."""
        from cc_tool_pattern import matches
        assert matches(
            "Bash(npm*)", "Bash",
            {"command": "npm install && echo ok"},
        ) is True


class TestFilePathSpec:
    def test_edit_under_prefix(self):
        from cc_tool_pattern import matches
        assert matches(
            "Edit(/data/engagements/*)", "Edit",
            {"file_path": "/data/engagements/abc/foo.py"},
        ) is True

    def test_edit_outside_prefix(self):
        from cc_tool_pattern import matches
        assert matches(
            "Edit(/data/engagements/*)", "Edit",
            {"file_path": "/etc/passwd"},
        ) is False

    def test_write_glob(self):
        from cc_tool_pattern import matches
        # fnmatch.fnmatchcase ``*`` is NOT path-segment-aware: it matches
        # across ``/`` just like any other char. So ``*.md`` matches the
        # full string ``/tmp/foo.md``. Documented under §4.1 best-effort
        # semantics — operators who want path-segment safety must spell
        # the prefix explicitly (see test_write_with_anchored_glob).
        assert matches(
            "Write(*.md)", "Write",
            {"file_path": "/tmp/foo.md"},
        ) is True

    def test_write_with_anchored_glob(self):
        from cc_tool_pattern import matches
        assert matches(
            "Write(/tmp/*)", "Write",
            {"file_path": "/tmp/foo.md"},
        ) is True

    def test_glob_pattern_field(self):
        from cc_tool_pattern import matches
        assert matches(
            "Glob(*.py)", "Glob",
            {"pattern": "*.py"},
        ) is True


class TestMatchesAny:
    def test_unsupported_tool_with_spec(self):
        """Bare match still works for unsupported-spec tools; spec returns False."""
        from cc_tool_pattern import matches
        assert matches("WebFetch", "WebFetch", {}) is True
        assert matches("WebFetch(x)", "WebFetch", {"url": "x"}) is False
