"""Tests for agent_loader.load_all_executors."""

from __future__ import annotations

import os
import textwrap

import pytest

pytestmark = pytest.mark.asyncio


def _write_exec(base, name, defn_yaml=None, prompt="Hi."):
    d = os.path.join(base, name)
    os.makedirs(os.path.join(d, "doctrine"), exist_ok=True)
    defn = defn_yaml or textwrap.dedent(f"""\
        schema_version: 1
        type: {name}
        description: A reasonably long description that meets minLength 20.
        model: sonnet
        driver: in_casa
        enabled: true
        tools:
          allowed: [Read]
          permission_mode: acceptEdits
        mcp_server_names: [casa-framework]
    """)
    with open(os.path.join(d, "definition.yaml"), "w") as fh:
        fh.write(defn)
    with open(os.path.join(d, "prompt.md"), "w") as fh:
        fh.write(prompt)
    return d


def _seed_executor_role_artifact(roles_dir, type_name, allowed=(),
                                 permission_mode="acceptEdits",
                                 model_block="{source: fixed, value: sonnet}"):
    """Write a minimal schema-valid canonical role artifact for an
    executor type under a test-owned roles_dir (Personality Phase A,
    Task 5 — load_all_executors now requires one, cross-validated on
    id/kind/slot, per defaults/roles/executor/<type>/). Real shipped
    types ("configurator", "plugin-developer") already have real
    artifacts under the production defaults/roles/ tree and don't need
    this; synthetic test-only types (e.g. "myx") do. ``allowed`` seeds
    the role's tools.allowed — the capability CEILING (round-5).
    ``model_block`` seeds the role's model declaration — the model AUTHORITY
    (#205); pass an ``ha_option`` block to exercise that resolution path."""
    d = os.path.join(roles_dir, "executor", type_name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "role.yaml"), "w") as fh:
        fh.write(textwrap.dedent(f"""\
            api_version: casa.role/v1
            id: executor:{type_name}
            kind: executor
            slot: {type_name}
            mission: Test fixture executor role.
            enabled: true
            model: {model_block}
            tools:
              allowed: {list(allowed)}
              disallowed: []
              permission_mode: {permission_mode}
              max_turns: 10
              skills: all
              voice_guard: none
            mcp_servers: []
            channels: []
            memory: {{token_budget: 0, read_strategy: per_turn}}
            session: {{strategy: ephemeral, idle_timeout_seconds: 0}}
            disclosure: {{policy: executor-internal, overrides: {{}}}}
            delegates: []
            executors: []
            triggers: []
            hooks: {{pre_tool_use: []}}
            tts: {{tag_dialect: none, error_phrases: {{}}}}
            response:
              text: {{register: plain}}
              voice: {{register: unavailable}}
              restricted_webhook: {{register: unavailable}}
            persona: {{policy: forbidden}}
            requires: {{plugins: [], tools: []}}
            doctrine_file: doctrine.md
        """))
    with open(os.path.join(d, "doctrine.md"), "w") as fh:
        fh.write("# Core doctrine\n\nTest fixture doctrine body.\n")
    return d


class TestLoadAllExecutors:
    def test_empty_dir_returns_empty(self, tmp_path):
        from agent_loader import load_all_executors
        os.makedirs(tmp_path / "executors", exist_ok=True)
        out, failed = load_all_executors(str(tmp_path))
        assert out == {}
        assert failed == []

    def test_happy_path_one_type(self, tmp_path):
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        _write_exec(str(base), "configurator")
        out, failed = load_all_executors(str(tmp_path))
        assert failed == []
        assert "configurator" in out
        d = out["configurator"]
        assert d.type == "configurator"
        assert d.model == "claude-sonnet-4-6"
        assert d.driver == "in_casa"
        assert d.enabled is True
        assert d.prompt_template_path.endswith("prompt.md")

    def test_schema_violation_reports_failed(self, tmp_path):
        """v0.37.1 B-1b: per-file isolation — schema violation lands
        on ``failed`` instead of raising LoadError out of the loop."""
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        bad = textwrap.dedent("""\
            schema_version: 1
            type: bad
            description: too short
            model: sonnet
            driver: in_casa
        """)
        _write_exec(str(base), "bad", defn_yaml=bad)
        out, failed = load_all_executors(str(tmp_path))
        assert out == {}
        assert len(failed) == 1
        assert failed[0][0] == "bad"

    def test_missing_prompt_reports_failed(self, tmp_path):
        """v0.37.1 B-1b: missing prompt.md is per-file failure, not raise."""
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        d = str(base / "x")
        os.makedirs(d)
        with open(os.path.join(d, "definition.yaml"), "w") as fh:
            fh.write(textwrap.dedent("""\
                schema_version: 1
                type: x
                description: Adequate description for loading tests.
                model: sonnet
                driver: in_casa
            """))
        out, failed = load_all_executors(str(tmp_path))
        assert out == {}
        assert len(failed) == 1
        assert failed[0][0] == "x"


class TestHooksFileValidation:
    """#312: ``hooks_file`` must resolve to a schema-valid hooks document.

    Pre-fix, any present file satisfied the pointer (``hooks_name in
    present``) — ``hooks_file: definition.yaml`` silently shed every declared
    safety policy — and a declared-but-absent pointer silently dropped hooks
    entirely."""

    def _seed(self, tmp_path, extra_defn="", hooks=None, driver="claude_code"):
        ex_dir = tmp_path / "executors" / "myx"
        ex_dir.mkdir(parents=True)
        (ex_dir / "definition.yaml").write_text(
            "schema_version: 1\n"
            "type: myx\n"
            "description: A test executor with a minimum of twenty characters.\n"
            "model: sonnet\n"
            f"driver: {driver}\n" + extra_defn
        )
        (ex_dir / "prompt.md").write_text("hi")
        if hooks is not None:
            (ex_dir / "hooks.yaml").write_text(hooks)
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx")
        return str(roles_dir)

    def test_hooks_file_pointing_at_non_hooks_document_fails_closed(
        self, tmp_path,
    ):
        from agent_loader import load_all_executors
        roles_dir = self._seed(
            tmp_path, extra_defn="hooks_file: definition.yaml\n",
        )
        out, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert out == {}
        assert len(failed) == 1 and failed[0][0] == "myx"
        assert "hooks" in failed[0][1]

    def test_declared_hooks_file_absent_fails_closed(self, tmp_path):
        from agent_loader import load_all_executors
        roles_dir = self._seed(
            tmp_path, extra_defn="hooks_file: custom-hooks.yaml\n",
        )
        out, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert out == {}
        assert len(failed) == 1 and failed[0][0] == "myx"
        assert "custom-hooks.yaml" in failed[0][1]

    def test_invalid_default_hooks_yaml_fails_closed(self, tmp_path):
        from agent_loader import load_all_executors
        roles_dir = self._seed(tmp_path, hooks="just: nonsense\n")
        out, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert out == {}
        assert len(failed) == 1 and failed[0][0] == "myx"

    def test_valid_hooks_yaml_still_loads(self, tmp_path):
        from agent_loader import load_all_executors
        roles_dir = self._seed(
            tmp_path,
            hooks=(
                "schema_version: 1\n"
                "pre_tool_use:\n"
                "  - policy: block_dangerous_bash\n"
                "  - policy: path_scope\n"
                "    writable: [/data/engagements/]\n"
                "    readable: [/data/engagements/]\n"
            ),
        )
        out, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert failed == []
        assert out["myx"].hooks_path.endswith("hooks.yaml")

    def test_absent_default_hooks_yaml_stays_optional(self, tmp_path):
        """Terra r1-P1's optionality is about the POINTER's own default, not
        the containment floor — Task 3 (#360) makes an absent hooks file a
        LoadError for ``driver: claude_code`` specifically (an executor with
        no declared floor policies), so this stays in_casa to keep testing
        the pointer behaviour it names."""
        from agent_loader import load_all_executors
        roles_dir = self._seed(tmp_path, driver="in_casa")
        out, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert failed == []
        assert out["myx"].hooks_path is None

    def test_explicit_default_hooks_file_absent_stays_optional(self, tmp_path):
        """Terra r1-P1: ``hooks_file: hooks.yaml`` spelled out is the default,
        not a stricter declaration — absence stays optional. Task 3 (#360):
        in_casa here for the same reason as the sibling test above — the
        containment floor is a claude_code-only requirement."""
        from agent_loader import load_all_executors
        roles_dir = self._seed(
            tmp_path, extra_defn="hooks_file: hooks.yaml\n", driver="in_casa",
        )
        out, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert failed == []
        assert out["myx"].hooks_path is None

    def test_non_constructible_policy_params_fail_closed(self, tmp_path):
        """Sol r1-1/r1-2: the hooks schema allows arbitrary per-policy params,
        so a schema-valid document can still raise in the policy factory —
        pre-fix that raise happened in the policy-map builder, which skipped
        the executor and silently fell back to broader defaults. Load must
        refuse it instead."""
        from agent_loader import load_all_executors
        roles_dir = self._seed(
            tmp_path,
            hooks=(
                "schema_version: 1\n"
                "pre_tool_use:\n"
                "  - policy: commit_size_guard\n"
                "    max_files: notanumber\n"
            ),
        )
        out, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert out == {}
        assert len(failed) == 1 and failed[0][0] == "myx"

    def test_env_substituted_params_reach_sdk_options(self, tmp_path, monkeypatch):
        """Terra r3-1: the in_casa/SDK options builder must parse the SAME
        substituted document validation saw — pre-fix its raw safe_load made
        ``int("${VAR}")`` raise when the executor actually started, after the
        document had validated cleanly at load."""
        import tools as tools_mod
        from agent_loader import load_all_executors
        monkeypatch.setenv("CASA_TEST_MAX_FILES", "5")
        # Task 3 (#360): in_casa — the containment floor is claude_code-only,
        # and this test's own docstring names the in_casa options builder.
        roles_dir = self._seed(
            tmp_path,
            hooks=(
                "schema_version: 1\n"
                "pre_tool_use:\n"
                "  - policy: commit_size_guard\n"
                "    max_files: ${CASA_TEST_MAX_FILES}\n"
            ),
            driver="in_casa",
        )
        out, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert failed == []
        # Pre-fix this raised inside resolve_hooks (int on the raw literal).
        opts = tools_mod._build_executor_options(
            out["myx"], executor_type="myx", plugin_paths=[])
        assert opts.hooks and "PreToolUse" in opts.hooks

    def test_env_substituted_params_load_and_build(self, tmp_path, monkeypatch):
        """Sol r1-2: validation reads through the env-substituting reader, so
        the enforcement builders must parse the SAME substituted document —
        pre-fix they re-read raw and ``int("${VAR}")`` blew up the policy-map
        entry after validation had passed."""
        from agent_loader import load_all_executors, read_hooks_document
        monkeypatch.setenv("CASA_TEST_MAX_FILES", "5")
        # Task 3 (#360): in_casa — the containment floor is claude_code-only
        # and does not bear on this test's env-substitution concern.
        roles_dir = self._seed(
            tmp_path,
            hooks=(
                "schema_version: 1\n"
                "pre_tool_use:\n"
                "  - policy: commit_size_guard\n"
                "    max_files: ${CASA_TEST_MAX_FILES}\n"
            ),
            driver="in_casa",
        )
        out, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert failed == []
        data = read_hooks_document(out["myx"].hooks_path)
        assert data["pre_tool_use"][0]["max_files"] == 5


class TestContainmentFloorSnapshot:
    """Task 3 (#360): ExecutorDefinition.hooks_document snapshots the
    load-time-validated parsed hooks doc for EVERY executor, and a
    ``driver: claude_code`` executor must DECLARE the containment floor
    (block_dangerous_bash + path_scope) itself or fail to load."""

    def _seed_claude_code(self, tmp_path, hooks=None):
        ex_dir = tmp_path / "executors" / "myx"
        ex_dir.mkdir(parents=True)
        (ex_dir / "definition.yaml").write_text(
            "schema_version: 1\n"
            "type: myx\n"
            "description: A test executor with a minimum of twenty characters.\n"
            "model: sonnet\n"
            "driver: claude_code\n"
        )
        (ex_dir / "prompt.md").write_text("hi")
        if hooks is not None:
            (ex_dir / "hooks.yaml").write_text(hooks)
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx")
        return str(roles_dir)

    def test_claude_code_empty_pre_tool_use_fails_closed_naming_missing_policy(
        self, tmp_path,
    ):
        from agent_loader import load_all_executors
        roles_dir = self._seed_claude_code(
            tmp_path, hooks="schema_version: 1\npre_tool_use: []\n",
        )
        out, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert out == {}
        assert len(failed) == 1 and failed[0][0] == "myx"
        message = failed[0][1]
        assert "block_dangerous_bash" in message
        assert "path_scope" in message

    def test_claude_code_no_hooks_file_fails_closed(self, tmp_path):
        from agent_loader import load_all_executors
        roles_dir = self._seed_claude_code(tmp_path, hooks=None)
        out, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert out == {}
        assert len(failed) == 1 and failed[0][0] == "myx"

    def test_claude_code_full_floor_loads_with_snapshot(self, tmp_path):
        from agent_loader import load_all_executors
        hooks_yaml = (
            "schema_version: 1\n"
            "pre_tool_use:\n"
            "  - policy: block_dangerous_bash\n"
            "  - policy: path_scope\n"
            "    writable: [/data/engagements/]\n"
            "    readable: [/data/engagements/]\n"
        )
        roles_dir = self._seed_claude_code(tmp_path, hooks=hooks_yaml)
        out, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert failed == []
        assert "myx" in out
        import yaml
        expected = yaml.safe_load(hooks_yaml)
        assert out["myx"].hooks_document["pre_tool_use"] == expected["pre_tool_use"]

    def test_in_casa_configurator_snapshot_is_declared_not_empty(self, tmp_path):
        """REVISION 3b (Sol r3 plan:171/179): an in_casa executor's snapshot
        must be its REAL declared document, never {} — {} would let
        resolve_hooks({}) resynthesize the wider default /config-wide
        path_scope downstream, reopening the in_casa hole. Floor rejection
        must NOT apply to in_casa."""
        from pathlib import Path
        import yaml
        from agent_loader import load_all_executors

        agents_base = (
            Path(__file__).resolve().parents[1]
            / "casa" / "rootfs" / "opt" / "casa" / "defaults" / "agents"
        )
        out, failed = load_all_executors(str(agents_base))
        assert failed == []
        defn = out["configurator"]
        assert defn.driver == "in_casa"
        real_hooks = yaml.safe_load(
            (agents_base / "executors" / "configurator" / "hooks.yaml")
            .read_text(encoding="utf-8"))
        assert defn.hooks_document != {}
        assert (
            defn.hooks_document["pre_tool_use"] == real_hooks["pre_tool_use"]
        )

    def test_in_casa_no_hooks_file_snapshots_empty_dict(self, tmp_path):
        """{} is reserved for an executor with NO hooks file at all."""
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        _write_exec(str(base), "configurator")  # in_casa, no hooks.yaml
        out, failed = load_all_executors(str(tmp_path))
        assert failed == []
        assert out["configurator"].hooks_document == {}


class TestGateHooksPointerParity:
    """#312 gate parity (Terra r1-P2 / Sol r1-3): validate_config_repo must
    refuse the same hooks-pointer commits boot refuses."""

    def _seed_config(self, tmp_path, extra_defn="", custom=None):
        exec_dir = tmp_path / "agents" / "executors" / "myx"
        exec_dir.mkdir(parents=True)
        (exec_dir / "definition.yaml").write_text(
            "schema_version: 1\n"
            "type: myx\n"
            "description: A test executor with a minimum of twenty characters.\n"
            "model: sonnet\n"
            "driver: claude_code\n" + extra_defn
        )
        (exec_dir / "prompt.md").write_text("hi")
        if custom is not None:
            name, body = custom
            (exec_dir / name).write_text(body)
        return str(tmp_path)

    def test_gate_flags_pointer_at_non_hooks_document(self, tmp_path):
        from agent_loader import validate_config_repo
        cfg = self._seed_config(
            tmp_path, extra_defn="hooks_file: definition.yaml\n",
        )
        errors = validate_config_repo(cfg)
        assert any("hooks_file" in e and "myx" in e for e in errors)

    def test_gate_flags_absent_custom_pointer(self, tmp_path):
        from agent_loader import validate_config_repo
        cfg = self._seed_config(
            tmp_path, extra_defn="hooks_file: custom-hooks.yaml\n",
        )
        errors = validate_config_repo(cfg)
        assert any("custom-hooks.yaml" in e for e in errors)

    def test_gate_accepts_valid_custom_pointer(self, tmp_path):
        from agent_loader import validate_config_repo
        cfg = self._seed_config(
            tmp_path,
            extra_defn="hooks_file: custom-hooks.yaml\n",
            custom=("custom-hooks.yaml",
                    "schema_version: 1\n"
                    "pre_tool_use:\n"
                    "  - policy: block_dangerous_bash\n"),
        )
        errors = validate_config_repo(cfg)
        assert not any("hooks" in e for e in errors)

    def test_gate_does_not_double_report_invalid_default_hooks(self, tmp_path):
        """An invalid default-named executor hooks.yaml is reported EXACTLY
        once — the pointer pass owns executor hooks validation and the
        basename walk skips those files (Terra r2-P1)."""
        from agent_loader import validate_config_repo
        cfg = self._seed_config(
            tmp_path, custom=("hooks.yaml", "just: nonsense\n"),
        )
        errors = validate_config_repo(cfg)
        assert len([e for e in errors if "hooks.yaml" in e]) == 1

    def test_gate_flags_factory_invalid_default_hooks(self, tmp_path):
        """Terra r2-P1: a schema-valid but factory-refused DEFAULT hooks.yaml
        (``max_files: notanumber``) must fail the gate exactly as it fails
        boot — the earlier default-name skip re-opened gate-weaker-than-boot
        for the default pointer."""
        from agent_loader import validate_config_repo
        cfg = self._seed_config(
            tmp_path,
            custom=("hooks.yaml",
                    "schema_version: 1\n"
                    "pre_tool_use:\n"
                    "  - policy: commit_size_guard\n"
                    "    max_files: notanumber\n"),
        )
        errors = validate_config_repo(cfg)
        assert any("myx" in e and "hooks" in e for e in errors)

    def test_gate_reports_unhashable_hooks_file_without_crashing(self, tmp_path):
        """Terra r2-P2: ``hooks_file: []`` must be REPORTED (the gate's
        mandate is report-never-crash), not raise TypeError out of the
        pointer pass."""
        from agent_loader import validate_config_repo
        cfg = self._seed_config(tmp_path, extra_defn="hooks_file: []\n")
        errors = validate_config_repo(cfg)  # must not raise
        assert any("hooks_file" in e and "myx" in e for e in errors)

    def test_gate_accepts_shipped_default_hooks(self, tmp_path):
        """A valid default-named hooks.yaml stays clean through the gate."""
        from agent_loader import validate_config_repo
        cfg = self._seed_config(
            tmp_path,
            custom=("hooks.yaml",
                    "schema_version: 1\n"
                    "pre_tool_use:\n"
                    "  - policy: block_dangerous_bash\n"),
        )
        errors = validate_config_repo(cfg)
        assert not any("hooks" in e for e in errors)


class TestExecutorDefinitionPlan4aFields:
    def test_populates_plan4a_fields_with_defaults(self, tmp_path):
        from agent_loader import load_all_executors

        ex_dir = tmp_path / "executors" / "myx"
        ex_dir.mkdir(parents=True)
        (ex_dir / "definition.yaml").write_text(
            "schema_version: 1\n"
            "type: myx\n"
            "description: A test executor with a minimum of twenty characters.\n"
            "model: sonnet\n"
            # Task 3 (#360): in_casa — these fields are driver-independent
            # and no hooks.yaml is seeded here, which the claude_code
            # containment floor would now reject.
            "driver: in_casa\n"
        )
        (ex_dir / "prompt.md").write_text("hi")
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx")

        result, _failed = load_all_executors(str(tmp_path), roles_dir=str(roles_dir))
        defn = result["myx"]

        assert defn.extra_dirs == []
        assert defn.mirror_chat_to_topic is True
        assert defn.plugins_dir == ""
        assert defn.role_artifact is not None
        assert defn.role_artifact.role["id"] == "executor:myx"

    def test_reads_plan4a_fields_from_yaml(self, tmp_path):
        from agent_loader import load_all_executors

        ex_dir = tmp_path / "executors" / "myx"
        ex_dir.mkdir(parents=True)
        (ex_dir / "definition.yaml").write_text(
            "schema_version: 1\n"
            "type: myx\n"
            "description: A test executor with a minimum of twenty characters.\n"
            "model: sonnet\n"
            "driver: in_casa\n"  # Task 3 (#360): no hooks.yaml seeded here
            "extra_dirs:\n"
            "  - /data/casa-plugins-repo\n"
            "mirror_chat_to_topic: false\n"
        )
        (ex_dir / "prompt.md").write_text("hi")
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx")

        loaded, _failed = load_all_executors(str(tmp_path), roles_dir=str(roles_dir))
        defn = loaded["myx"]

        assert defn.extra_dirs == ["/data/casa-plugins-repo"]
        assert defn.mirror_chat_to_topic is False

    def test_plugins_dir_resolves_when_plugins_subdir_exists(self, tmp_path):
        from agent_loader import load_all_executors

        ex_dir = tmp_path / "executors" / "myx"
        ex_dir.mkdir(parents=True)
        (ex_dir / "definition.yaml").write_text(
            "schema_version: 1\n"
            "type: myx\n"
            "description: A test executor with a minimum of twenty characters.\n"
            "model: sonnet\n"
            "driver: in_casa\n"  # Task 3 (#360): no hooks.yaml seeded here
        )
        (ex_dir / "prompt.md").write_text("hi")
        (ex_dir / "plugins").mkdir()        # the positive-path trigger
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx")

        loaded, _failed = load_all_executors(str(tmp_path), roles_dir=str(roles_dir))
        defn = loaded["myx"]

        # plugins_dir resolves to the absolute plugins/ path inside the executor dir.
        expected = str(ex_dir / "plugins")
        assert defn.plugins_dir == expected


class TestExecutorRoleArtifact:
    """Personality Phase A, Task 5: load_all_executors also loads and
    cross-validates the canonical role artifact at
    defaults/roles/executor/<type>/, isolated per-executor like every
    other failure mode."""

    def test_real_shipped_configurator_gets_role_artifact(self, tmp_path):
        """The real shipped 'configurator' type resolves against the
        production defaults/roles/ tree with no roles_dir override."""
        from agent_loader import load_all_executors

        base = tmp_path / "executors"
        _write_exec(str(base), "configurator")
        out, failed = load_all_executors(str(tmp_path))

        assert failed == []
        defn = out["configurator"]
        assert defn.role_artifact is not None
        assert defn.role_artifact.role["id"] == "executor:configurator"
        assert defn.role_artifact.role["kind"] == "executor"
        assert defn.role_artifact.role["slot"] == "configurator"
        assert defn.role_artifact.role["persona"] == {"policy": "forbidden"}

    def test_missing_role_artifact_is_isolated_failure(self, tmp_path):
        """A synthetic executor type with no matching role artifact under
        roles_dir fails closed as a per-entry failure, not a raise."""
        from agent_loader import load_all_executors

        base = tmp_path / "executors"
        _write_exec(str(base), "nope")
        empty_roles_dir = tmp_path / "empty_roles"
        empty_roles_dir.mkdir()

        out, failed = load_all_executors(str(tmp_path), roles_dir=str(empty_roles_dir))

        assert out == {}
        assert len(failed) == 1
        assert failed[0][0] == "nope"
        assert "role artifact" in failed[0][1]

    def test_mismatched_slot_is_isolated_failure(self, tmp_path):
        """A role artifact whose declared slot does not match the executor
        type directory name is rejected, not silently accepted."""
        from agent_loader import load_all_executors

        base = tmp_path / "executors"
        _write_exec(str(base), "aaa")
        roles_dir = tmp_path / "roles"
        # Seed the artifact under the WRONG slot directory name relative
        # to its own declared id/slot ("bbb" inside), so aaa/ ends up
        # holding a role.yaml that declares slot: bbb.
        _seed_executor_role_artifact(str(roles_dir), "bbb")
        import shutil
        shutil.move(
            str(roles_dir / "executor" / "bbb"),
            str(roles_dir / "executor" / "aaa"),
        )

        out, failed = load_all_executors(str(tmp_path), roles_dir=str(roles_dir))

        assert out == {}
        assert len(failed) == 1
        assert failed[0][0] == "aaa"
        assert "role artifact" in failed[0][1]

    def test_mismatched_kind_is_isolated_failure(self, tmp_path):
        """A role artifact whose declared kind is not 'executor' is
        rejected even if slot/id otherwise line up structurally."""
        from agent_loader import load_all_executors

        base = tmp_path / "executors"
        _write_exec(str(base), "ccc")
        roles_dir = tmp_path / "roles"
        d = roles_dir / "executor" / "ccc"
        d.mkdir(parents=True)
        (d / "role.yaml").write_text(textwrap.dedent("""\
            api_version: casa.role/v1
            id: resident:ccc
            kind: resident
            slot: ccc
            mission: Wrong-kind fixture.
            enabled: true
            model: {source: fixed, value: sonnet}
            tools:
              allowed: []
              disallowed: []
              permission_mode: acceptEdits
              max_turns: 10
              skills: all
              voice_guard: none
            mcp_servers: []
            channels: [telegram]
            memory: {token_budget: 0, read_strategy: per_turn}
            session: {strategy: ephemeral, idle_timeout_seconds: 0}
            disclosure: {policy: standard, overrides: {}}
            delegates: []
            executors: []
            triggers: []
            hooks: {pre_tool_use: []}
            tts: {tag_dialect: none, error_phrases: {}}
            response:
              text: {register: plain}
              voice: {register: plain}
              restricted_webhook: {register: plain}
            persona: {policy: required, compatibility: ["x@>=1.0.0 <2.0.0"]}
            requires: {plugins: [], tools: []}
            doctrine_file: doctrine.md
        """), encoding="utf-8")
        (d / "doctrine.md").write_text("# Core doctrine\n\nBody.\n", encoding="utf-8")

        out, failed = load_all_executors(str(tmp_path), roles_dir=str(roles_dir))

        assert out == {}
        assert len(failed) == 1
        assert failed[0][0] == "ccc"
        assert "role artifact" in failed[0][1]


class TestRoleCeilingClamp:
    """Round-5 (Terra P0): defaults/roles/executor/<type>/role.yaml is the
    IMMUTABLE capability ceiling (image-owned, covered by role_checksum);
    definition.yaml is operationally editable (executor recipes), so its
    tools.allowed is clamped to the intersection with the role's — it may
    only narrow, never exceed. Regression: a configurator session re-adding
    Bash to its own definition.yaml must not get Bash back next session."""

    def _write_myx(self, base, allowed_line):
        _write_exec(str(base), "myx", defn_yaml=textwrap.dedent(f"""\
            schema_version: 1
            type: myx
            description: A reasonably long description that meets minLength 20.
            model: sonnet
            driver: in_casa
            enabled: true
            tools:
              allowed: {allowed_line}
              permission_mode: acceptEdits
            mcp_server_names: [casa-framework]
        """))

    def test_self_escalation_clamped_and_logged(self, tmp_path, caplog):
        """(a) definition re-adds Bash; role ceiling lacks it -> dropped,
        loudly logged (tamper signal), executor still loads."""
        import logging
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        self._write_myx(base, "[Read, Bash]")
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx", allowed=["Read"])
        with caplog.at_level(logging.ERROR):
            out, failed = load_all_executors(
                str(tmp_path), roles_dir=str(roles_dir))
        assert failed == []
        assert out["myx"].tools_allowed == ["Read"]
        assert any(
            "beyond its role ceiling" in r.getMessage()
            and "Bash" in r.getMessage()
            for r in caplog.records
        ), "the drop must be logged loudly — it is a tamper signal"

    def test_built_options_do_not_contain_escalated_tool(self, tmp_path):
        """(a, end-to-end) the clamped list is what reaches
        ClaudeAgentOptions.allowed_tools via tools._build_executor_options
        (the in_casa builder; the claude_code path consumes the same
        defn.tools_allowed via drivers.workspace._build_cc_permissions)."""
        import tools as tools_mod
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        self._write_myx(base, "[Read, Bash]")
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx", allowed=["Read"])
        out, failed = load_all_executors(
            str(tmp_path), roles_dir=str(roles_dir))
        assert failed == []
        opts = tools_mod._build_executor_options(
            out["myx"], executor_type="myx", plugin_paths=[])
        assert "Bash" not in opts.allowed_tools
        assert "Read" in opts.allowed_tools

    def test_definition_narrower_than_role_is_respected(self, tmp_path, caplog):
        """(b) narrowing is the definition's prerogative — no clamp, no log."""
        import logging
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        self._write_myx(base, "[Read]")
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(
            str(roles_dir), "myx", allowed=["Read", "Write", "Bash"])
        with caplog.at_level(logging.ERROR):
            out, failed = load_all_executors(
                str(tmp_path), roles_dir=str(roles_dir))
        assert failed == []
        assert out["myx"].tools_allowed == ["Read"]
        assert not any(
            "beyond its role ceiling" in r.getMessage()
            for r in caplog.records)

    def test_shipped_files_clamp_is_noop(self, caplog):
        """(d) shipped parity: the real definition.yaml and role.yaml lists
        are equal for both executors, so the clamp changes nothing and logs
        no drop."""
        import logging
        from pathlib import Path
        import yaml
        from agent_loader import load_all_executors

        agents_base = (
            Path(__file__).resolve().parents[1]
            / "casa" / "rootfs" / "opt" / "casa" / "defaults" / "agents"
        )
        with caplog.at_level(logging.ERROR):
            out, failed = load_all_executors(str(agents_base))
        assert failed == []
        for t in ("configurator", "plugin-developer"):
            declared = yaml.safe_load(
                (agents_base / "executors" / t / "definition.yaml")
                .read_text(encoding="utf-8"))["tools"]["allowed"]
            assert out[t].tools_allowed == list(declared), t
        # Round-6 P0-2: shipped modes are within their role ceilings
        # (plugin-developer legitimately ships "auto" — declared in BOTH
        # its definition.yaml and its image-owned role.yaml).
        assert out["configurator"].permission_mode == "acceptEdits"
        assert out["plugin-developer"].permission_mode == "auto"
        assert not any(
            "beyond its role ceiling" in r.getMessage()
            or "permission_mode" in r.getMessage()
            for r in caplog.records)


class TestPermissionModeClamp:
    """Round-6 P0-2 (Sol): permission_mode is a definition-editable
    capability field — self-editing it to bypassPermissions would turn the
    clamped allowlist into mere auto-approval. Executors only ever run at
    default/acceptEdits; the role artifact's permission_mode is the ceiling
    when it names one of those. Downgrades log the tamper signal."""

    def _write_myx_mode(self, base, mode):
        _write_exec(str(base), "myx", defn_yaml=textwrap.dedent(f"""\
            schema_version: 1
            type: myx
            description: A reasonably long description that meets minLength 20.
            model: sonnet
            driver: in_casa
            enabled: true
            tools:
              allowed: [Read]
              permission_mode: {mode}
            mcp_server_names: [casa-framework]
        """))

    def test_bypasspermissions_is_downgraded_and_logged(self, tmp_path, caplog):
        import logging
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        self._write_myx_mode(base, "bypassPermissions")
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx", allowed=["Read"])
        with caplog.at_level(logging.ERROR):
            out, failed = load_all_executors(
                str(tmp_path), roles_dir=str(roles_dir))
        assert failed == []
        assert out["myx"].permission_mode == "acceptEdits"
        assert any(
            "permission_mode" in r.getMessage() and "tamper" in r.getMessage()
            for r in caplog.records)

    def test_role_default_ceiling_clamps_acceptedits(self, tmp_path, caplog):
        """Role declares 'default' -> definition's acceptEdits exceeds the
        ceiling and clamps down to 'default'."""
        import logging
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        self._write_myx_mode(base, "acceptEdits")
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(
            str(roles_dir), "myx", allowed=["Read"], permission_mode="default")
        with caplog.at_level(logging.ERROR):
            out, failed = load_all_executors(
                str(tmp_path), roles_dir=str(roles_dir))
        assert failed == []
        assert out["myx"].permission_mode == "default"
        assert any("permission_mode" in r.getMessage() for r in caplog.records)

    def test_mode_exceeding_role_ceiling_is_clamped(self, tmp_path, caplog):
        """Configurator-shaped self-escalation: role ceiling acceptEdits,
        definition self-edited to the more permissive 'auto' -> clamped
        back to the role's mode."""
        import logging
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        self._write_myx_mode(base, "auto")
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx", allowed=["Read"])
        with caplog.at_level(logging.ERROR):
            out, failed = load_all_executors(
                str(tmp_path), roles_dir=str(roles_dir))
        assert failed == []
        assert out["myx"].permission_mode == "acceptEdits"
        assert any("permission_mode" in r.getMessage() for r in caplog.records)

    def test_acceptedits_within_ceiling_is_untouched(self, tmp_path, caplog):
        import logging
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        self._write_myx_mode(base, "acceptEdits")
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx", allowed=["Read"])
        with caplog.at_level(logging.ERROR):
            out, failed = load_all_executors(
                str(tmp_path), roles_dir=str(roles_dir))
        assert failed == []
        assert out["myx"].permission_mode == "acceptEdits"
        assert not any(
            "permission_mode" in r.getMessage() for r in caplog.records)


class TestExecutorDisallowedMerge:
    """Round-6 P0-1 (Sol): Q-1 — Agent/Task bypass allowed_tools and are
    only stopped by explicit disallowed_tools; executors shipped
    disallowed: []. The in_casa builder now merges {Agent, Task} (the Q-1
    _SUBAGENT_SPAWN_TOOLS set) code-side, plus Bash whenever the clamped
    allowlist does not carry it (P0-2 belt+suspenders)."""

    def _load_myx(self, tmp_path, allowed_line, role_allowed):
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        _write_exec(str(base), "myx", defn_yaml=textwrap.dedent(f"""\
            schema_version: 1
            type: myx
            description: A reasonably long description that meets minLength 20.
            model: sonnet
            driver: in_casa
            enabled: true
            tools:
              allowed: {allowed_line}
              disallowed: []
              permission_mode: acceptEdits
            mcp_server_names: [casa-framework]
        """))
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(
            str(roles_dir), "myx", allowed=role_allowed)
        out, failed = load_all_executors(
            str(tmp_path), roles_dir=str(roles_dir))
        assert failed == []
        return out["myx"]

    def test_built_options_disallow_agent_task_and_bash(self, tmp_path):
        """Configurator-shaped executor (no Bash in the allowlist), explicit
        disallowed: [] -> built options still hard-deny Agent/Task/Bash."""
        import tools as tools_mod
        defn = self._load_myx(tmp_path, "[Read]", ["Read"])
        opts = tools_mod._build_executor_options(
            defn, executor_type="myx", plugin_paths=[])
        assert {"Agent", "Task", "Bash"} <= set(opts.disallowed_tools)

    def test_bash_not_denied_when_legitimately_allowed(self, tmp_path):
        """plugin-developer-shaped executor (Bash within the role ceiling):
        Agent/Task still denied, Bash NOT (it is a legitimate grant)."""
        import tools as tools_mod
        defn = self._load_myx(tmp_path, "[Read, Bash]", ["Read", "Bash"])
        opts = tools_mod._build_executor_options(
            defn, executor_type="myx", plugin_paths=[])
        assert {"Agent", "Task"} <= set(opts.disallowed_tools)
        assert "Bash" not in opts.disallowed_tools
        assert "Bash" in opts.allowed_tools

    def test_cc_permissions_deny_parity(self, tmp_path):
        """claude_code-driver parity: _build_cc_permissions emits the same
        code-mandatory deny set into settings.json permissions."""
        from drivers.workspace import _build_cc_permissions
        defn = self._load_myx(tmp_path, "[Read]", ["Read"])
        perms = _build_cc_permissions(defn)
        assert {"Agent", "Task", "Bash"} <= set(perms["deny"])
        defn_bash = self._load_myx(tmp_path / "b", "[Read, Bash]", ["Read", "Bash"])
        perms_bash = _build_cc_permissions(defn_bash)
        assert {"Agent", "Task"} <= set(perms_bash["deny"])
        assert "Bash" not in perms_bash["deny"]
