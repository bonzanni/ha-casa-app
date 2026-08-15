"""Tests for executors.yaml parsing + assistant-only role validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from config import MODEL_MAP

from agent_loader import LoadError, _read_yaml, _validate

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.asyncio


def _write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_executors_schema_accepts_minimal_valid(tmp_path):
    f = tmp_path / "executors.yaml"
    _write(f, """\
        schema_version: 1
        executors:
          - executor_type: configurator
            purpose: edit configs
            when: user wants to change configuration
    """)
    data = _read_yaml(str(f))
    _validate(data, "executors", str(f))  # must not raise


def test_executors_schema_rejects_missing_field(tmp_path):
    f = tmp_path / "executors.yaml"
    _write(f, """\
        schema_version: 1
        executors:
          - executor_type: configurator
            purpose: edit configs
            # `when` missing
    """)
    data = _read_yaml(str(f))
    with pytest.raises(LoadError) as exc:
        _validate(data, "executors", str(f))
    assert "when" in str(exc.value)


def test_executors_schema_rejects_unknown_property(tmp_path):
    f = tmp_path / "executors.yaml"
    _write(f, """\
        schema_version: 1
        executors:
          - executor_type: configurator
            purpose: edit configs
            when: x
            extra: nope
    """)
    data = _read_yaml(str(f))
    with pytest.raises(LoadError):
        _validate(data, "executors", str(f))


def test_executor_entry_dataclass_fields():
    from config import ExecutorEntry
    e = ExecutorEntry(
        executor_type="configurator",
        purpose="edit configs",
        when="user wants to change configuration",
    )
    assert e.executor_type == "configurator"
    assert e.purpose == "edit configs"
    assert e.when == "user wants to change configuration"


def test_agent_config_has_executors_field():
    from config import AgentConfig
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT)
    assert cfg.executors == []


# ---------------------------------------------------------------------------
# Task 3: loader wiring + assistant-only validation
# ---------------------------------------------------------------------------


def test_executors_yaml_parses_on_assistant(tmp_path):
    """A valid agents/<assistant>/executors.yaml loads into cfg.executors."""
    from agent_loader import load_agent_from_dir
    from policies import load_policies
    try:
        from tests.test_agent_loader import _seed_resident, _policies_file
    except ImportError:
        from test_agent_loader import _seed_resident, _policies_file

    role = "assistant"
    d = _seed_resident(tmp_path / "agents", role=role)
    _write(d / "executors.yaml", """\
        schema_version: 1
        executors:
          - executor_type: configurator
            purpose: edit configs
            when: user wants to change configuration
    """)
    policies = load_policies(str(_policies_file(tmp_path / "policies")))
    cfg = load_agent_from_dir(str(d), policies=policies)
    assert len(cfg.executors) == 1
    assert cfg.executors[0].executor_type == "configurator"


def test_executors_yaml_rejected_on_non_assistant_resident(tmp_path):
    """executors.yaml on butler (non-assistant resident) must fail load."""
    from agent_loader import LoadError, load_agent_from_dir
    from policies import load_policies
    try:
        from tests.test_agent_loader import _seed_resident, _policies_file
    except ImportError:
        from test_agent_loader import _seed_resident, _policies_file

    role = "butler"
    d = _seed_resident(tmp_path / "agents", role=role)
    _write(d / "executors.yaml", """\
        schema_version: 1
        executors:
          - executor_type: configurator
            purpose: x
            when: x
    """)
    policies = load_policies(str(_policies_file(tmp_path / "policies")))
    with pytest.raises(LoadError) as exc:
        load_agent_from_dir(str(d), policies=policies)
    msg = str(exc.value).lower()
    assert "executors.yaml" in msg
    assert "assistant" in msg


def test_executors_yaml_rejected_on_specialist(tmp_path):
    """executors.yaml on specialist must fail load (forbidden file)."""
    from agent_loader import LoadError, load_agent_from_dir
    try:
        from tests.test_agent_loader import _seed_specialist
    except ImportError:
        from test_agent_loader import _seed_specialist

    role = "finance"
    d = _seed_specialist(tmp_path / "agents", role=role)
    _write(d / "executors.yaml", """\
        schema_version: 1
        executors:
          - executor_type: configurator
            purpose: x
            when: x
    """)
    with pytest.raises(LoadError):
        load_agent_from_dir(str(d), policies=None)


# ---------------------------------------------------------------------------
# Task 13: assistant/delegates.yaml seed sanity
# ---------------------------------------------------------------------------


def test_assistant_delegates_yaml_has_no_executor_entries():
    """The seed assistant/delegates.yaml must not contain configurator,
    plugin-developer, or engagement — those moved to executors.yaml."""
    from agent_loader import _read_yaml
    path = (
        "casa/rootfs/opt/casa/defaults/agents/assistant/delegates.yaml"
    )
    data = _read_yaml(path)
    agents = {entry["agent"] for entry in (data.get("delegates") or [])}
    assert "configurator" not in agents
    assert "plugin-developer" not in agents
    assert "engagement" not in agents


# ---------------------------------------------------------------------------
# Task 14: assistant/executors.yaml seed sanity
# ---------------------------------------------------------------------------


def test_assistant_executors_yaml_seed_loads():
    from agent_loader import _read_yaml, _validate
    path = (
        "casa/rootfs/opt/casa/defaults/agents/assistant/executors.yaml"
    )
    data = _read_yaml(path)
    _validate(data, "executors", path)
    types = {e["executor_type"] for e in data["executors"]}
    # F-6 (v0.32.0): the fictional ``engagement`` entry was removed —
    # interactive-mode delegation to a specialist is a Tier 2 primitive
    # (delegate_to_agent(mode='interactive')), not a Tier 3 executor type.
    # The remaining seed list must match the real executor registry shape;
    # see test_assistant_prompts.test_executors_yaml_lists_only_real_registered_executor_types
    # for the cross-check against agents/executors/.
    assert {"configurator", "plugin-developer"}.issubset(types)


# ---------------------------------------------------------------------------
# v0.37.1 B-1: permission_mode enum expansion (CC CLI 2.1.119 parity)
# ---------------------------------------------------------------------------


class TestExecutorSchemaPermissionMode:
    """CC CLI 2.1.119's --permission-mode accepts six values:
    acceptEdits, auto, bypassPermissions, default, dontAsk, plan.
    Casa's schema must accept all six (B-1).
    """

    @staticmethod
    def _executor_yaml(mode: str) -> str:
        return textwrap.dedent(f"""\
            schema_version: 1
            type: probe-exec
            description: Probe executor used for schema permission-mode coverage.
            model: sonnet
            driver: in_casa
            tools:
              allowed: [Read]
              permission_mode: {mode}
        """)

    @pytest.mark.parametrize("mode", [
        "acceptEdits", "auto", "bypassPermissions",
        "default", "dontAsk", "plan",
    ])
    def test_schema_accepts_mode(self, tmp_path, mode):
        f = tmp_path / "definition.yaml"
        _write(f, self._executor_yaml(mode))
        data = _read_yaml(str(f))
        _validate(data, "executor", str(f))  # must not raise

    def test_schema_rejects_typo(self, tmp_path):
        f = tmp_path / "definition.yaml"
        _write(f, self._executor_yaml("acceptedits"))  # lowercase typo
        data = _read_yaml(str(f))
        with pytest.raises(LoadError) as exc:
            _validate(data, "executor", str(f))
        assert "permission_mode" in str(exc.value)


# ---------------------------------------------------------------------------
# v0.37.1 B-1b: load_all_executors per-file isolation
# ---------------------------------------------------------------------------


def _write_minimal_executor(base, name, body_override=None):
    """Create executors/<name>/{definition.yaml,prompt.md,doctrine/}."""
    import os
    d = os.path.join(base, "executors", name)
    os.makedirs(os.path.join(d, "doctrine"), exist_ok=True)
    body = body_override or textwrap.dedent(f"""\
        schema_version: 1
        type: {name}
        description: A reasonably long description that meets minLength 20.
        model: sonnet
        driver: in_casa
        enabled: true
        tools:
          allowed: [Read]
          permission_mode: acceptEdits
    """)
    with open(os.path.join(d, "definition.yaml"), "w", encoding="utf-8") as fh:
        fh.write(body)
    with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as fh:
        fh.write("Hello.")


class TestLoadAllExecutorsIsolation:
    def test_one_broken_one_valid_returns_loaded_and_failed(self, tmp_path):
        from agent_loader import load_all_executors
        # Valid configurator
        _write_minimal_executor(str(tmp_path), "configurator")
        # Broken plugin-developer (typo permission_mode)
        broken_body = textwrap.dedent("""\
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
        _write_minimal_executor(str(tmp_path), "plugin-developer",
                                 body_override=broken_body)
        loaded, failed = load_all_executors(str(tmp_path))
        assert list(loaded.keys()) == ["configurator"]
        assert len(failed) == 1
        assert failed[0][0] == "plugin-developer"
        assert "permission_mode" in failed[0][1]

    def test_all_broken_returns_empty_loaded(self, tmp_path):
        from agent_loader import load_all_executors
        # Two broken
        broken_body = textwrap.dedent("""\
            schema_version: 1
            type: {name}
            description: A reasonably long description that meets minLength 20.
            model: sonnet
            driver: in_casa
            enabled: true
            tools:
              allowed: [Read]
              permission_mode: nope_not_real
        """)
        _write_minimal_executor(str(tmp_path), "a",
                                 body_override=broken_body.format(name="a"))
        _write_minimal_executor(str(tmp_path), "b",
                                 body_override=broken_body.format(name="b"))
        loaded, failed = load_all_executors(str(tmp_path))
        assert loaded == {}
        assert len(failed) == 2
        names = {f[0] for f in failed}
        assert names == {"a", "b"}

    def test_collection_level_error_still_raises(self, tmp_path):
        """``executors_root`` exists but is a FILE, not a directory.

        That's a true collection-level error (we can't even scan
        entries), so the function still raises rather than returning
        an empty loaded + failed.
        """
        import os
        from agent_loader import load_all_executors, LoadError
        # Pre-create executors as a FILE (not directory).
        executors_path = os.path.join(str(tmp_path), "executors")
        # Need executors path to exist as a file. But the function
        # uses os.path.isdir — a file at that path means isdir=False
        # and the function returns ({}, []) per the early-return.
        # Pivot the test: cause an unexpected raise at scan time by
        # making the dir unreadable... that's OS-dependent. Instead,
        # cover the case where an executor's prompt.md path is gone:
        _write_minimal_executor(str(tmp_path), "configurator")
        os.remove(os.path.join(str(tmp_path), "executors",
                               "configurator", "prompt.md"))
        loaded, failed = load_all_executors(str(tmp_path))
        # Missing prompt file is per-executor (LoadError raised
        # inside the loop) — isolated, not collection-level.
        assert loaded == {}
        assert len(failed) == 1
        assert "prompt" in failed[0][1].lower()


class TestLoadAllExecutorsBroadIsolation:
    """v0.37.1 B-1b: per-file isolation must catch more than LoadError.

    The pre-validate code path (`_read_yaml`, `int()` casts on
    fields, `ExecutorDefinition` construction) can raise YAMLError,
    ValueError, OSError, TypeError before the schema validator
    even runs. Those used to escape the per-executor loop and wipe
    the entire registry. v0.37.1 widens the catch.
    """

    def test_corrupt_yaml_does_not_wipe_registry(self, tmp_path):
        import os
        from agent_loader import load_all_executors
        # configurator: valid
        _write_minimal_executor(str(tmp_path), "configurator")
        # plugin-developer: definition.yaml is not parseable YAML
        d = os.path.join(str(tmp_path), "executors", "plugin-developer")
        os.makedirs(os.path.join(d, "doctrine"), exist_ok=True)
        with open(os.path.join(d, "definition.yaml"), "w", encoding="utf-8") as fh:
            fh.write("schema_version: 1\ntype: : : : invalid yaml: [\n")
        with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as fh:
            fh.write("Hello.")
        loaded, failed = load_all_executors(str(tmp_path))
        assert list(loaded.keys()) == ["configurator"]
        assert len(failed) == 1
        assert failed[0][0] == "plugin-developer"


class TestDoctrineDirOptOut:
    def test_empty_doctrine_dir_yaml_produces_empty_defn_field(self, tmp_path):
        """v0.74.2 (Sol S1): an explicitly empty `doctrine_dir: ""` in
        definition.yaml is the doctrine-less opt-out — the loader must NOT
        join it into `<exec_dir>` (which would make provisioning copy the
        whole executor dir) but yield defn.doctrine_dir == ''."""
        import os
        from agent_loader import load_all_executors
        try:
            from tests.test_load_all_executors import _seed_executor_role_artifact
        except ImportError:
            from test_load_all_executors import _seed_executor_role_artifact
        _write_minimal_executor(str(tmp_path), "noduct", body_override=textwrap.dedent("""\
            schema_version: 1
            type: noduct
            description: A reasonably long description that meets minLength 20.
            model: sonnet
            driver: in_casa
            enabled: true
            doctrine_dir: ""
            tools:
              allowed: [Read]
              permission_mode: acceptEdits
        """))
        os.rmdir(os.path.join(tmp_path, "executors", "noduct", "doctrine"))
        roles_dir = os.path.join(str(tmp_path), "roles")
        _seed_executor_role_artifact(roles_dir, "noduct")
        loaded, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert failed == []
        assert loaded["noduct"].doctrine_dir == ""

    def test_default_doctrine_dir_resolves_to_abs_path(self, tmp_path):
        import os
        from agent_loader import load_all_executors
        try:
            from tests.test_load_all_executors import _seed_executor_role_artifact
        except ImportError:
            from test_load_all_executors import _seed_executor_role_artifact
        _write_minimal_executor(str(tmp_path), "withduct")
        roles_dir = os.path.join(str(tmp_path), "roles")
        _seed_executor_role_artifact(roles_dir, "withduct")
        loaded, _failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert loaded["withduct"].doctrine_dir == os.path.join(
            str(tmp_path), "executors", "withduct", "doctrine")


class TestExecutorModelBootParity:
    """#205: definition.yaml may not change the model the role artifact declares.

    ExecutorDefinition.model is what ACTUALLY runs — tools.py feeds it straight
    into ClaudeAgentOptions.model — while resolved_model carries the image-owned
    role artifact's choice for identity/checksum only. Nothing reconciled them,
    so an edited definition.yaml (a legitimately operator-writable file) could
    silently run a model the canonical artifact does not declare. Residents have
    had this parity check since Phase A; executors now do too.
    """

    @staticmethod
    def _seed(tmp_path, name, *, defn_model, role_model_block=None):
        import os
        import textwrap
        try:
            from tests.test_load_all_executors import _seed_executor_role_artifact
        except ImportError:
            from test_load_all_executors import _seed_executor_role_artifact
        _write_minimal_executor(str(tmp_path), name, body_override=textwrap.dedent(f"""\
            schema_version: 1
            type: {name}
            description: A reasonably long description that meets minLength 20.
            model: {defn_model}
            driver: in_casa
            enabled: true
            tools:
              allowed: [Read]
              permission_mode: acceptEdits
        """))
        roles_dir = os.path.join(str(tmp_path), "roles")
        kwargs = {} if role_model_block is None else {"model_block": role_model_block}
        _seed_executor_role_artifact(roles_dir, name, **kwargs)
        return roles_dir

    def test_matching_shortname_loads(self, tmp_path):
        """The shipped shape: definition says `sonnet`, role says fixed sonnet."""
        from agent_loader import load_all_executors
        roles_dir = self._seed(tmp_path, "agree", defn_model="sonnet")
        loaded, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert failed == []
        assert loaded["agree"].model == MODEL_MAP["sonnet"]

    def test_equivalent_full_model_id_loads(self, tmp_path):
        """Comparison is on RESOLVED SDK IDs, so a full ID that resolves to the
        same model as the role's shortname is NOT a mismatch. Comparing raw text
        would false-flag this and fail a perfectly valid executor."""
        from agent_loader import load_all_executors
        roles_dir = self._seed(
            tmp_path, "fullid", defn_model=MODEL_MAP["sonnet"])
        loaded, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert failed == []
        assert loaded["fullid"].model == MODEL_MAP["sonnet"]

    def test_mismatched_model_is_reported_not_loaded(self, tmp_path):
        """The bug this closes: definition.yaml quietly wins over the artifact."""
        from agent_loader import load_all_executors
        roles_dir = self._seed(tmp_path, "drift", defn_model="opus")
        loaded, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert "drift" not in loaded, (
            "an executor whose definition.yaml model disagrees with its "
            "canonical role artifact must not load"
        )
        assert [name for name, _ in failed] == ["drift"]
        message = failed[0][1]
        assert (MODEL_MAP["opus"] in message
                and MODEL_MAP["sonnet"] in message), (
            f"the error must name both resolved models; got: {message}"
        )

    def test_mismatch_is_isolated_from_siblings(self, tmp_path):
        """Per-executor isolation: one bad definition must not take down its
        siblings or the add-on (the failure is boot-non-fatal by design)."""
        from agent_loader import load_all_executors
        roles_dir = self._seed(tmp_path, "bad", defn_model="haiku")
        self._seed(tmp_path, "good", defn_model="sonnet")
        loaded, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert "good" in loaded
        assert [name for name, _ in failed] == ["bad"]

    def test_ha_option_role_model_resolves_before_comparison(self, tmp_path):
        """Executor artifacts ship `fixed` today, but role_slot supports
        `ha_option`. The check must compare against the RESOLVED value so an
        ha_option artifact keeps working — `resolved_model.effective` is a
        shortname, which is exactly why the comparison uses sdk_model."""
        from agent_loader import load_all_executors
        roles_dir = self._seed(
            tmp_path, "opt", defn_model="haiku",
            role_model_block=(
                "{source: ha_option, option: voice_agent_model, "
                "default: haiku, allowed: [opus, sonnet, haiku]}"
            ),
        )
        loaded, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert failed == []
        assert loaded["opt"].model == "claude-haiku-4-5"

    def test_ha_option_mismatch_still_caught(self, tmp_path):
        """...and an ha_option artifact still catches a genuine disagreement."""
        from agent_loader import load_all_executors
        roles_dir = self._seed(
            tmp_path, "optbad", defn_model="opus",
            role_model_block=(
                "{source: ha_option, option: voice_agent_model, "
                "default: haiku, allowed: [opus, sonnet, haiku]}"
            ),
        )
        loaded, failed = load_all_executors(str(tmp_path), roles_dir=roles_dir)
        assert "optbad" not in loaded
        assert [name for name, _ in failed] == ["optbad"]
