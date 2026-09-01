"""H-3 fix carry-forward (v0.34.0 -> v0.35.0 D.3 shim): casa_reload_triggers
must pass a real ``PolicyLibrary`` to ``agent_loader.load_agent_from_dir`` so
residents (which carry ``disclosure.yaml``) can be reloaded.

Pre-fix, ``tools.py:2094`` always passed ``policies=None``. Residents
have ``disclosure.yaml``, and ``agent_loader._compose_prompt`` raises
LoadError on ``disclosure is not None AND policies is None``. Soft
reload of triggers for residents was permanently broken since
commit ``e81f264`` (2026-04-22, Plan 3 Task 8 - 9 days latent).

Live evidence - exploration3 P8.1 (2026-05-01) cid `30f2aeae`
engagement `09e4bfed`: configurator added a `casa-probe-p8` interval
trigger to ``agents/assistant/triggers.yaml``, called
``casa_reload_triggers(assistant)``, got:

    LoadError: agent 'assistant': disclosure.yaml references policy
    'standard' but no PolicyLibrary was passed

In v0.35.0 the H-3 fix moved into ``reload.reload_triggers`` and
``casa_reload_triggers`` is now a thin dispatch shim. These tests
remain to guard the resident path behavior end-to-end.

Tests build a self-contained agent + policies fixture under tmp_path
and bind a CasaRuntime to ``agent.active_runtime`` so the dispatcher's
handler resolves paths from the runtime.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest.fixture
def configurator_origin():
    """Set origin_var so the L26 role guard lets the call through."""
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _seed_resident_with_disclosure(base: Path, role: str = "assistant") -> Path:
    """Write a minimal valid resident dir that includes disclosure.yaml.
    Mirrors the shape that ``tests/test_agent_loader.py::_seed_resident``
    produces - keep them in sync if loader contract changes."""
    d = base / role
    _w(d / "character.yaml", f"""\
        schema_version: 1
        name: Ellen
        role: {role}
        archetype: assistant
        card: |
          Peer summary.
        prompt: |
          You are Ellen.
    """)
    _w(d / "voice.yaml", "schema_version: 1\ntone: [direct]\ncadence: short\n")
    _w(d / "response_shape.yaml", "schema_version: 1\nregister: written\nformat: plain\n")
    _w(d / "disclosure.yaml", "schema_version: 1\npolicy: standard\n")
    _w(d / "runtime.yaml", """\
        schema_version: 1
        kind: resident
        model: {source: ha_option, option: primary_agent_model, default: opus, allowed: [opus, sonnet, haiku]}
        tools:
          allowed: [Read, Write]
        channels: [telegram]
    """)
    return d


def _seed_policies(base: Path) -> Path:
    """Write a valid ``policies/disclosure.yaml`` under *base*."""
    p = base / "policies" / "disclosure.yaml"
    _w(p, """\
        schema_version: 1
        policies:
          standard:
            categories:
              financial:
                required_trust: authenticated
                examples: [balances]
            safe_on_any_channel: [device_state]
            deflection_patterns:
              household_shared: "private."
    """)
    return p


def _add_one_trigger(agent_dir: Path) -> None:
    _w(agent_dir / "triggers.yaml", """\
        schema_version: 1
        triggers:
          - name: probe-p8
            type: interval
            channel: telegram
            minutes: 1
            prompt: "P8 probe scheduled fire"
    """)


def _runtime_with(tmp_path: Path, *, trigger_registry):
    """Build a CasaRuntime whose paths resolve to a tmp fixture."""
    from runtime import CasaRuntime
    return CasaRuntime(
        agents={}, role_configs={}, specialist_registry=MagicMock(),
        executor_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_registry=MagicMock(),
        trigger_registry=trigger_registry,
        mcp_registry=MagicMock(),
        session_registry=MagicMock(), channel_manager=MagicMock(),
        bus=MagicMock(), engagement_driver=MagicMock(),
        claude_code_driver=MagicMock(),
        policy_lib=MagicMock(),
        config_dir=str(tmp_path),
        agents_dir=str(tmp_path / "agents"),
        home_root=str(tmp_path / "home"),
        defaults_root="/opt/casa",
    )


class TestCasaReloadTriggersResident:
    """The H-3 regression: residents have disclosure.yaml; soft reload
    must thread a real PolicyLibrary or fail."""

    async def test_resident_with_disclosure_succeeds(self, tmp_path, configurator_origin):
        """The fix: casa_reload_triggers must successfully reload a
        resident whose directory contains disclosure.yaml."""
        import agent as agent_mod
        from tools import casa_reload_triggers

        recorded: list[tuple[str, list, list]] = []
        fake_registry = MagicMock()
        fake_registry.reregister_for.side_effect = (
            lambda role, triggers, channels, **kw: recorded.append(
                (role, list(triggers), list(channels)),
            )
        )

        # Build the on-disk shape the dispatcher's handler expects:
        #   <base>/agents/<role>/{character,voice,response_shape,
        #                        disclosure,runtime,triggers}.yaml
        #   <base>/policies/disclosure.yaml
        agents_dir = tmp_path / "agents"
        _seed_resident_with_disclosure(agents_dir, role="assistant")
        _add_one_trigger(agents_dir / "assistant")
        _seed_policies(tmp_path)

        agent_mod.active_runtime = _runtime_with(
            tmp_path, trigger_registry=fake_registry,
        )

        result = await casa_reload_triggers.handler({"role": "assistant"})
        payload = json.loads(result["content"][0]["text"])

        assert payload.get("status") == "ok", (
            f"casa_reload_triggers must succeed for residents with "
            f"disclosure.yaml; got: {payload}"
        )
        assert payload.get("role") == "assistant"
        assert recorded, "trigger_registry.reregister_for was not called"
        recorded_role, recorded_triggers, recorded_channels = recorded[0]
        assert recorded_role == "assistant"
        assert [t.name for t in recorded_triggers] == ["probe-p8"]
        assert recorded_channels == ["telegram"]

    async def test_missing_policies_file_returns_load_error(self, tmp_path, configurator_origin):
        """When ``policies/disclosure.yaml`` is missing, surface a
        ``load_error`` with a useful message - don't silently fall
        back to ``policies=None`` (which is exactly the H-3 bug)."""
        import agent as agent_mod
        from tools import casa_reload_triggers

        agents_dir = tmp_path / "agents"
        _seed_resident_with_disclosure(agents_dir, role="assistant")
        _add_one_trigger(agents_dir / "assistant")
        # NOTE: deliberately do NOT seed ``policies/disclosure.yaml``.

        agent_mod.active_runtime = _runtime_with(
            tmp_path, trigger_registry=MagicMock(),
        )

        result = await casa_reload_triggers.handler({"role": "assistant"})
        payload = json.loads(result["content"][0]["text"])

        assert payload.get("status") == "error"
        assert payload.get("kind") == "load_error"
        # The dispatcher prefixes with "policies: " - so just verify the
        # message references the missing-policy path or the underlying
        # error refers to policies/disclosure.yaml.
        assert "polic" in payload.get("message", "").lower()

    async def test_specialist_path_still_works(
        self, tmp_path, configurator_origin, monkeypatch,
    ):
        """Specialists have NO disclosure.yaml. The fix must not
        regress this path (loading policies is harmless - agent_loader
        only consults the library when the agent has a disclosure)."""
        import agent as agent_mod
        from tools import casa_reload_triggers

        # Personality Phase A, Task 5: agent_loader.load_agent_from_dir now
        # requires a canonical role artifact under
        # defaults/roles/specialist/<slot>/ for every specialist it loads.
        # This test's synthetic 'casa-probe-x' specialist has no real
        # shipped artifact and this call path (the casa_reload_triggers MCP
        # tool) has no roles_dir override to inject one — redirect
        # agent_loader's module-level DEFAULT_ROLES_DIR for the duration of
        # the test instead.
        try:
            from tests.test_agent_loader import _seed_role_artifact
        except ImportError:
            from test_agent_loader import _seed_role_artifact
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "specialist", "casa-probe-x")
        monkeypatch.setattr("agent_loader.DEFAULT_ROLES_DIR", str(roles_dir))

        recorded: list[tuple[str, list, list]] = []
        fake_registry = MagicMock()
        fake_registry.reregister_for.side_effect = (
            lambda role, triggers, channels, **kw: recorded.append(
                (role, list(triggers), list(channels)),
            )
        )

        # Specialist shape - under agents/specialists/<role>/, no
        # disclosure.yaml.
        spec_dir = tmp_path / "agents" / "specialists" / "casa-probe-x"
        _w(spec_dir / "character.yaml", """\
            schema_version: 1
            name: Probe
            role: casa-probe-x
            archetype: probe
            card: |
              probe.
            prompt: |
              probe.
        """)
        _w(spec_dir / "voice.yaml", "schema_version: 1\n")
        _w(spec_dir / "response_shape.yaml", "schema_version: 1\n")
        _w(spec_dir / "runtime.yaml", """\
            schema_version: 1
            kind: specialist
            model: {source: fixed, value: sonnet}
            enabled: false
            memory:
              token_budget: 0
            session:
              strategy: ephemeral
        """)
        # specialists CAN have triggers per current schema; keep empty
        # to check the no-trigger path is also fine.
        _seed_policies(tmp_path)

        agent_mod.active_runtime = _runtime_with(
            tmp_path, trigger_registry=fake_registry,
        )

        result = await casa_reload_triggers.handler(
            {"role": "casa-probe-x"},
        )
        payload = json.loads(result["content"][0]["text"])

        assert payload.get("status") == "ok", (
            f"specialist soft-reload regressed; got: {payload}"
        )
        # registered list comes from runtime.role_configs/specialist_registry
        # in the back-compat shim. Specialists aren't seeded into
        # role_configs in this test, and specialist_registry is a MagicMock
        # so all_configs() returns a Mock object that has .get(...) returning
        # another Mock (truthy) - the shim's getattr(cfg, "triggers", None)
        # against a MagicMock returns a MagicMock too. So the shim may emit
        # an unexpected "registered" - just verify the registration call.
        assert recorded, "trigger_registry.reregister_for was not called"
        recorded_role, recorded_triggers, recorded_channels = recorded[0]
        assert recorded_role == "casa-probe-x"
        assert recorded_triggers == []
