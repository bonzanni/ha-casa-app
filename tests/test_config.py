"""Tests for config.py -- model mapping and dataclasses."""

import dataclasses

import pytest

from config import resolve_model

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


# ------------------------------------------------------------------
# resolve_model
# ------------------------------------------------------------------


class TestResolveModel:
    def test_shortname_opus(self):
        assert resolve_model("opus") == "claude-opus-5"

    def test_shortname_sonnet(self):
        assert resolve_model("sonnet") == "claude-sonnet-5"

    def test_shortname_haiku(self):
        assert resolve_model("haiku") == "claude-haiku-4-5"

    def test_passthrough_full_id(self):
        assert resolve_model("claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_passthrough_custom_full_id(self):
        assert resolve_model("my-custom-model-3") == "my-custom-model-3"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown model shortname"):
            resolve_model("gpt4")

    def test_unresolved_env_placeholder_is_deferred_not_error(self):
        # 2026-07-10: a ${VAR} placeholder is a DEFERRED value (resolved at boot
        # when the env var is set), so validation without the env must not raise
        # — makes validate_config_repo env-independent for all callers.
        assert resolve_model("${PRIMARY_AGENT_MODEL}") == "${PRIMARY_AGENT_MODEL}"
        assert resolve_model("${VOICE_AGENT_MODEL}") == "${VOICE_AGENT_MODEL}"
        # A genuine non-placeholder typo still raises (boot strictness intact).
        with pytest.raises(ValueError):
            resolve_model("opuss")


# ------------------------------------------------------------------
# Dataclasses (Phase 4.x agent-definition refactor)
# ------------------------------------------------------------------


class TestCharacterConfig:
    def test_defaults(self):
        from config import CharacterConfig
        cfg = CharacterConfig(
            name="X", archetype="y", card="c", prompt="p",
        )
        assert cfg.name == "X"
        assert cfg.archetype == "y"
        assert cfg.card == "c"
        assert cfg.prompt == "p"


class TestVoiceConfig:
    def test_defaults(self):
        from config import VoiceConfig
        cfg = VoiceConfig()
        assert cfg.tone == []
        assert cfg.cadence == "natural"
        assert cfg.forbidden_patterns == []
        assert cfg.signature_phrases == {}


class TestResponseShapeConfig:
    def test_defaults(self):
        from config import ResponseShapeConfig
        cfg = ResponseShapeConfig()
        assert cfg.max_sentences_confirmation == 2
        assert cfg.max_sentences_status == 3
        assert cfg.register == "written"
        assert cfg.format == "plain"
        assert cfg.rules == []


class TestDisclosureConfig:
    def test_defaults(self):
        from config import DisclosureConfig
        cfg = DisclosureConfig(policy="standard")
        assert cfg.policy == "standard"
        assert cfg.overrides == {}


class TestDelegateEntry:
    def test_fields(self):
        from config import DelegateEntry
        e = DelegateEntry(agent="finance", purpose="p", when="w")
        assert e.agent == "finance"


class TestTriggerSpec:
    def test_interval(self):
        from config import TriggerSpec
        t = TriggerSpec(name="hb", type="interval",
                        minutes=60, channel="telegram", prompt="x")
        assert t.type == "interval"
        assert t.minutes == 60

    def test_cron(self):
        from config import TriggerSpec
        t = TriggerSpec(name="morning", type="cron",
                        schedule="0 7 * * *", channel="telegram", prompt="x")
        assert t.type == "cron"

    def test_webhook(self):
        from config import TriggerSpec
        t = TriggerSpec(name="gh", type="webhook", path="/webhook/gh")
        assert t.type == "webhook"
        assert t.path == "/webhook/gh"


class TestHooksConfig:
    def test_defaults(self):
        from config import HooksConfig
        h = HooksConfig()
        assert h.pre_tool_use == []


class TestAgentConfigNewFields:
    def test_new_fields_default_to_empty(self):
        from config import (
            AgentConfig, CharacterConfig, VoiceConfig,
            ResponseShapeConfig, HooksConfig,
        )
        cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT)
        assert isinstance(cfg.character, CharacterConfig)
        assert isinstance(cfg.voice, VoiceConfig)
        assert isinstance(cfg.response_shape, ResponseShapeConfig)
        assert cfg.disclosure is None
        assert cfg.delegates == []
        assert cfg.triggers == []
        assert isinstance(cfg.hooks, HooksConfig)
        assert cfg.system_prompt == ""


class TestAgentConfigRoleArtifactRequired:
    """Personality Phase A, Task 6, Step 7: role_artifact is now a required
    kw_only constructor field with no default on both AgentConfig and
    ExecutorDefinition — fixes defect #5 (a role-artifact-less agent could
    silently boot with stale legacy runtime.yaml model resolution)."""

    def test_agent_config_role_artifact_is_required(self) -> None:
        from config import AgentConfig
        field_ = next(f for f in dataclasses.fields(AgentConfig) if f.name == "role_artifact")
        assert field_.default is dataclasses.MISSING
        assert field_.default_factory is dataclasses.MISSING  # type: ignore[comparison-overlap]
        assert field_.kw_only is True

    def test_executor_definition_role_artifact_is_required(self) -> None:
        from config import ExecutorDefinition
        field_ = next(f for f in dataclasses.fields(ExecutorDefinition) if f.name == "role_artifact")
        assert field_.default is dataclasses.MISSING
        assert field_.kw_only is True


class TestExecutorDefinition:
    def test_minimal_fields(self):
        from config import ExecutorDefinition
        d = ExecutorDefinition(
            role_artifact=STUB_ROLE_ARTIFACT,
            type="configurator",
            description="Configure Casa.",
            model="claude-sonnet-4-6",
            driver="in_casa",
        )
        assert d.type == "configurator"
        assert d.enabled is True
        assert d.idle_reminder_days == 7
        assert d.tools_allowed == []
        assert d.permission_mode == "acceptEdits"

    def test_full_fields(self):
        from config import ExecutorDefinition
        d = ExecutorDefinition(
            role_artifact=STUB_ROLE_ARTIFACT,
            type="configurator",
            description="Configure Casa.",
            model="claude-sonnet-4-6",
            driver="in_casa",
            enabled=True,
            tools_allowed=["Read", "Write"],
            tools_disallowed=[],
            permission_mode="acceptEdits",
            mcp_server_names=["casa-framework"],
            idle_reminder_days=14,
            prompt_template_path="/x/prompt.md",
            hooks_path="/x/hooks.yaml",
            observer_policy_path="/x/observer.yaml",
            doctrine_dir="/x/doctrine",
        )
        assert d.idle_reminder_days == 14
        assert d.mcp_server_names == ["casa-framework"]
