"""Tests for ExecutorRegistry - Tier 3 type loader."""

from __future__ import annotations

import os
import textwrap

import pytest


def _write(base, name, enabled=True):
    d = os.path.join(base, "executors", name)
    os.makedirs(os.path.join(d, "doctrine"), exist_ok=True)
    with open(os.path.join(d, "definition.yaml"), "w") as fh:
        fh.write(textwrap.dedent(f"""\
            schema_version: 1
            type: {name}
            description: A reasonably long description that meets minLength 20.
            model: claude-sonnet-5
            driver: in_casa
            enabled: {str(enabled).lower()}
            tools:
              allowed: [Read]
              permission_mode: acceptEdits
            mcp_server_names: [casa-framework]
        """))
    with open(os.path.join(d, "prompt.md"), "w") as fh:
        fh.write("Hello.")


class TestExecutorRegistry:
    def test_load_empty(self, tmp_path):
        from executor_registry import ExecutorRegistry
        r = ExecutorRegistry(str(tmp_path / "executors"))
        r.load()
        assert r.list_types() == []
        assert r.get("configurator") is None

    def test_load_one_enabled(self, tmp_path):
        from executor_registry import ExecutorRegistry
        _write(str(tmp_path), "configurator")
        r = ExecutorRegistry(str(tmp_path / "executors"))
        r.load()
        assert r.list_types() == ["configurator"]
        d = r.get("configurator")
        assert d is not None
        assert d.enabled is True

    def test_disabled_excluded_from_list(self, tmp_path):
        from executor_registry import ExecutorRegistry
        _write(str(tmp_path), "configurator", enabled=False)
        r = ExecutorRegistry(str(tmp_path / "executors"))
        r.load()
        assert r.list_types() == []
        assert r.get("configurator") is None
        # v0.71.1: a disabled executor stays INSPECTABLE (is_disabled +
        # definition_any) so verify/health can validate a plugin assigned to it
        # without get()==None being read as empty tools.allowed (false alarm).
        assert r.is_disabled("configurator") is True
        d = r.definition_any("configurator")
        assert d is not None and d.enabled is False
        assert d.tools_allowed == ["Read"]

    def test_definition_any_and_is_disabled_for_enabled(self, tmp_path):
        from executor_registry import ExecutorRegistry
        _write(str(tmp_path), "configurator")            # enabled
        r = ExecutorRegistry(str(tmp_path / "executors"))
        r.load()
        assert r.is_disabled("configurator") is False
        assert r.definition_any("configurator") is r.get("configurator")
        # Unknown type: neither disabled nor present.
        assert r.is_disabled("nope") is False
        assert r.definition_any("nope") is None

    def test_load_missing_dir(self, tmp_path):
        from executor_registry import ExecutorRegistry
        r = ExecutorRegistry(str(tmp_path / "nope"))
        r.load()
        assert r.list_types() == []


class TestExecutorRegistryFailedLogging:
    def test_failed_executor_logged_but_others_load(self, tmp_path, caplog):
        """B-1b regression — one broken executor must not wipe the registry."""
        import os
        import textwrap
        from executor_registry import ExecutorRegistry

        # configurator: valid
        _write(str(tmp_path), "configurator")
        # plugin-developer: broken (permission_mode typo)
        broken = textwrap.dedent("""\
            schema_version: 1
            type: plugin-developer
            description: A reasonably long description that meets minLength 20.
            model: sonnet
            driver: claude_code
            enabled: true
            tools:
              allowed: [Read]
              permission_mode: acceptedits
        """)
        d = os.path.join(str(tmp_path), "executors", "plugin-developer")
        os.makedirs(os.path.join(d, "doctrine"), exist_ok=True)
        with open(os.path.join(d, "definition.yaml"), "w") as fh:
            fh.write(broken)
        with open(os.path.join(d, "prompt.md"), "w") as fh:
            fh.write("Hello.")

        r = ExecutorRegistry(str(tmp_path / "executors"))
        with caplog.at_level("INFO", logger="executor_registry"):
            r.load()
        # configurator loaded; plugin-developer failed but logged.
        assert r.list_types() == ["configurator"]
        assert any(
            "plugin-developer" in rec.message and "permission_mode" in rec.message
            for rec in caplog.records if rec.levelname == "ERROR"
        )
        assert any(
            "loaded=" in rec.message
            and "failed=" in rec.message
            and "configurator" in rec.message
            and "plugin-developer" in rec.message
            for rec in caplog.records
            if rec.levelname == "INFO"
        ), "expected final summary log line with loaded=/failed=/disabled= shape"


class TestLoadFailureKeepsRegistry:
    def test_unexpected_scan_error_keeps_previous_registry(
        self, tmp_path, monkeypatch,
    ):
        """#351: load() used to clear all collections BEFORE scanning and
        caught only LoadError — a transient PermissionError while listing the
        executors dir escaped with the registry already emptied, deleting
        every executor definition until a later successful reload. The scan
        must build into fresh structures and swap only on success."""
        import agent_loader
        from executor_registry import ExecutorRegistry

        _write(str(tmp_path), "configurator")
        r = ExecutorRegistry(str(tmp_path / "executors"))
        r.load()
        assert r.list_types() == ["configurator"]

        def boom(base):
            raise PermissionError("transient EACCES listing executors dir")

        monkeypatch.setattr(agent_loader, "load_all_executors", boom)
        with pytest.raises(PermissionError):
            r.load()
        # The live registry survives the failed scan untouched.
        assert r.list_types() == ["configurator"]
        assert r.get("configurator") is not None

    def test_state_publishes_atomically(self, tmp_path, monkeypatch):
        """Sol r2-3: load() runs in a worker thread while readers run on the
        loop — the registry's collections must publish as ONE assignment so
        a reader can never see e.g. new defs beside old disabled. Pinned by
        asserting all views come off a single state tuple."""
        from executor_registry import ExecutorRegistry

        _write(str(tmp_path), "configurator", enabled=True)
        r = ExecutorRegistry(str(tmp_path / "executors"))
        r.load()
        state = r._state
        assert r._defs is state.defs
        assert r._disabled is state.disabled
        assert r._disabled_defs is state.disabled_defs
        assert r._failed_types is state.failed_types
        assert r.get("configurator") is state.defs["configurator"]
        # A reload swaps the whole tuple; the old snapshot stays coherent.
        r.load()
        assert r._state is not state
        assert state.defs["configurator"] is not None
