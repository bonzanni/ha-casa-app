"""Tests for agent_loader.py — per-agent-dir loader, schema validation,
composition, tier-specific file-set rules."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


# Personality Phase A, Task 6: runtime.yaml's `model` is now a structured
# block (matching role.v1.json's oneOf fixed/ha_option shape), and
# agent_loader._build_runtime_fields cross-checks it BYTE-FOR-BYTE
# (canonical_json_bytes) against the resolved role artifact's own `model`
# block — a mismatch is a boot-parity LoadError. `_seed_resident`'s default
# model per role therefore MUST mirror the REAL shipped
# defaults/roles/resident/<role>/role.yaml for "assistant"/"butler" (the only
# two real, roles_dir-default-resolvable resident slots these fixtures use),
# and falls back to the same `{source: fixed, value: sonnet}` shape
# `_seed_role_artifact` below writes for every other (synthetic/cross-tier)
# role name, since those always pair with a custom roles_dir built via
# `_seed_role_artifact`.
_REAL_RESIDENT_MODEL_YAML = {
    "assistant": (
        "model: {source: ha_option, option: primary_agent_model, "
        "default: opus, allowed: [opus, sonnet, haiku]}\n"
    ),
    "butler": (
        "model: {source: ha_option, option: voice_agent_model, "
        "default: haiku, allowed: [opus, sonnet, haiku]}\n"
    ),
    # concierge's real shipped role.yaml uses the identical ha_option shape
    # as butler (both voice_agent_model-resolved residents).
    "concierge": (
        "model: {source: ha_option, option: voice_agent_model, "
        "default: haiku, allowed: [opus, sonnet, haiku]}\n"
    ),
}
_FIXED_SONNET_MODEL_YAML = "model: {source: fixed, value: sonnet}\n"


def _seed_resident(base: Path, role: str = "assistant") -> Path:
    """Write a minimal valid resident directory. Returns the agent dir path."""
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
    _w(d / "voice.yaml", """\
        schema_version: 1
        tone: [direct]
        cadence: short
    """)
    _w(d / "response_shape.yaml", """\
        schema_version: 1
        register: written
        format: plain
    """)
    _w(d / "disclosure.yaml", """\
        schema_version: 1
        policy: standard
    """)
    model_yaml = _REAL_RESIDENT_MODEL_YAML.get(role, _FIXED_SONNET_MODEL_YAML)
    _w(d / "runtime.yaml", f"""\
        schema_version: 1
        kind: resident
        {model_yaml.rstrip()}
        tools:
          allowed: [Read, Write]
        channels: [telegram]
    """)
    return d



def _seed_remaining_residents(repo, roles_dir=None):
    """#324: validate_config_repo now enforces the FULL fixed resident set
    (boot parity with load_all_agents Step 9). Fixtures probing unrelated
    gate paths seed butler+concierge so the tree stays bootable. With a
    custom roles_dir (which replaces the default tree entirely), the two
    role artifacts are seeded too and the runtimes matched to the fixture
    artifacts' fixed-sonnet model shape."""
    for slot in ("butler", "concierge"):
        d = _seed_resident(repo / "agents", slot)
        if roles_dir is not None:
            _seed_role_artifact(roles_dir, "resident", slot)
            _w(d / "runtime.yaml", """\
                schema_version: 1
                kind: resident
                model: {source: fixed, value: sonnet}
                tools:
                  allowed: [Read, Write]
                channels: [telegram]
            """)


def _seed_specialist(base: Path, role: str = "finance") -> Path:
    d = base / role
    _w(d / "character.yaml", f"""\
        schema_version: 1
        name: Alex
        role: {role}
        archetype: finance
        card: |
          Peer card.
        prompt: |
          You are Alex.
    """)
    _w(d / "voice.yaml", "schema_version: 1\n")
    _w(d / "response_shape.yaml", "schema_version: 1\n")
    _w(d / "runtime.yaml", """\
        schema_version: 1
        kind: specialist
        model: {source: fixed, value: sonnet}
        enabled: false
        memory:
          token_budget: 0
        session:
          strategy: ephemeral
    """)
    return d


def _seed_role_artifact(roles_dir: Path, kind: str, slot: str) -> Path:
    """Write a minimal schema-valid canonical role artifact for (kind,
    slot) under a test-owned roles_dir (Personality Phase A, Task 5).
    load_agent_from_dir now requires one at defaults/roles/<kind>/<slot>/
    for every resident/specialist it loads, cross-validated on kind+slot.
    The real shipped image only carries assistant/butler/concierge
    (resident) and finance (specialist); tests that deliberately construct
    synthetic or cross-tier role combinations (e.g. a 'finance' RESIDENT,
    used only to probe the duplicate-role-across-tiers invariant) need
    their own fixture roles_dir instead of colliding with the real
    defaults/roles/ tree, hence this test-only stand-in.

    Personality Phase A, Task 6: role_slot.validate_role_shape (now run by
    materialize_role during every load) requires a non-empty `channels` for
    resident kind and a binding-capable persona policy
    (required/optional-but-bound) for every non-executor kind — `channels:
    []` / `persona: {policy: forbidden}` unconditionally (the pre-Task-6
    shape) fails both checks for resident/specialist. Vary both by kind."""
    channels_yaml = "[ha_voice]" if kind == "resident" else "[]"
    # Personality Phase A, Task 8: a resident load now reconciles + compiles its
    # persona binding against the REAL image-default persona for its slot
    # (IMAGE_DEFAULT_PERSONA_BY_SLOT — casa/ellen|tina|gary). A synthetic role
    # fixture therefore declares a NAMESPACE-WILDCARD compatibility so whichever
    # real casa/* image-default persona the slot resolves to satisfies
    # check_persona_requirements; a slug-pinned "casa/test" would fail the load.
    # Specialists (roles_dir-injected, never binding-compiled in Plan 1) keep a
    # pinned test compat — they never reach the resident reconciler.
    persona_yaml = (
        "{policy: forbidden}" if kind == "executor"
        else '{policy: required, compatibility: ["casa/*@>=0.1.0 <1.0.0"]}'
        if kind == "resident"
        else '{policy: required, compatibility: ["casa/test@>=0.1.0 <1.0.0"]}'
    )
    d = roles_dir / kind / slot
    _w(d / "role.yaml", f"""\
        api_version: casa.role/v1
        id: {kind}:{slot}
        kind: {kind}
        slot: {slot}
        mission: Test fixture role.
        enabled: true
        model: {{source: fixed, value: sonnet}}
        tools:
          allowed: []
          disallowed: []
          permission_mode: acceptEdits
          max_turns: 10
          skills: all
          voice_guard: none
        mcp_servers: []
        channels: {channels_yaml}
        memory: {{token_budget: 0, read_strategy: per_turn}}
        session: {{strategy: ephemeral, idle_timeout_seconds: 0}}
        disclosure: {{policy: standard, overrides: {{}}}}
        delegates: []
        executors: []
        triggers: []
        hooks: {{pre_tool_use: []}}
        tts: {{tag_dialect: none, error_phrases: {{}}}}
        response:
          text: {{register: plain}}
          voice: {{register: plain}}
          restricted_webhook: {{register: plain}}
        persona: {persona_yaml}
        requires: {{plugins: [], tools: []}}
        doctrine_file: doctrine.md
    """)
    _w(d / "doctrine.md", "# Core doctrine\n\nTest fixture doctrine body.\n")
    return d


def _policies_file(base: Path) -> Path:
    p = base / "disclosure.yaml"
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


# ---------------------------------------------------------------------------
# TestHappyPath
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_loads_resident_directory(self, tmp_path):
        from agent_loader import load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        cfg = load_agent_from_dir(str(agent_dir), policies=policies)

        assert cfg.role == "assistant"
        assert cfg.character.name == "Ellen"
        # Personality Phase A, Task 6: cfg.model is resolved through the
        # canonical role artifact now, not runtime.yaml's bare string — the
        # REAL shipped resident/assistant/role.yaml uses
        # ha_option/primary_agent_model (default "opus"), and no env var is
        # set in this test, so the resolved model is opus, not the old
        # literal "sonnet".
        assert cfg.model == "claude-opus-4-6"
        assert cfg.resolved_model == "opus"
        assert "telegram" in cfg.channels
        # Composed prompt surfaces each section.
        assert cfg.system_prompt.startswith("You are Ellen.")
        assert "### Voice" in cfg.system_prompt
        assert "### Response shape" in cfg.system_prompt
        assert "### Disclosure" in cfg.system_prompt
        # Personality Phase A, Task 5: the canonical role artifact is
        # loaded against the REAL shipped defaults/roles/resident/assistant/
        # (no roles_dir override — default production tree).
        assert cfg.role_artifact is not None
        assert cfg.role_artifact.role["id"] == "resident:assistant"
        assert cfg.role_artifact.role["kind"] == "resident"
        assert cfg.role_artifact.role["slot"] == "assistant"
        assert cfg.role_artifact.doctrine.startswith("# Core doctrine\n")

    def test_loads_specialist_directory(self, tmp_path):
        from agent_loader import load_agent_from_dir

        agent_dir = _seed_specialist(tmp_path / "specialists", "finance")
        # Task N2's no-gap cutover removed the real
        # defaults/roles/specialist/finance/ from the image (finance was the
        # only bundled specialist) — synthesize the role artifact instead of
        # relying on the default roles_dir.
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "specialist", "finance")

        cfg = load_agent_from_dir(
            str(agent_dir), policies=None, roles_dir=str(roles_dir),
        )

        assert cfg.role == "finance"
        assert cfg.enabled is False
        # Specialists get character + voice + response_shape only — no
        # Disclosure, no Delegation section in the prompt.
        assert cfg.system_prompt.startswith("You are Alex.")
        assert "### Disclosure" not in cfg.system_prompt
        assert cfg.role_artifact is not None
        assert cfg.role_artifact.role["id"] == "specialist:finance"
        assert cfg.role_artifact.role["kind"] == "specialist"
        assert "### Delegation" not in cfg.system_prompt


# ---------------------------------------------------------------------------
# TestRoleArtifactIntegration — Personality Phase A, Task 5
# ---------------------------------------------------------------------------


class TestRoleArtifactIntegration:
    """load_agent_from_dir loads the image-owned canonical role artifact
    from defaults/roles/<tier>/<role_from_path>/ (or a test-injected
    roles_dir) and cross-validates that its declared kind/slot match the
    directory it was loaded for — a mismatch or missing artifact fails
    closed with LoadError, mirroring every other strict-load invariant in
    this module."""

    def test_missing_role_artifact_raises(self, tmp_path):
        from agent_loader import LoadError, load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))
        empty_roles_dir = tmp_path / "empty_roles"
        empty_roles_dir.mkdir()

        with pytest.raises(LoadError, match="role artifact"):
            load_agent_from_dir(
                str(agent_dir), policies=policies, roles_dir=str(empty_roles_dir),
            )

    def test_mismatched_slot_raises(self, tmp_path):
        """A role artifact whose declared slot doesn't match the agent
        directory name is rejected even though it loads cleanly on its
        own terms."""
        from agent_loader import LoadError, load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))
        roles_dir = tmp_path / "roles"
        # Seed a role artifact for slot 'assistant' but move it under a
        # directory named 'assistant' while it internally declares a
        # DIFFERENT slot — reuse _seed_role_artifact for 'other', then
        # relocate it to where load_agent_from_dir will actually look.
        _seed_role_artifact(roles_dir, "resident", "other")
        (roles_dir / "resident" / "other").rename(roles_dir / "resident" / "assistant")

        with pytest.raises(LoadError, match="slot"):
            load_agent_from_dir(
                str(agent_dir), policies=policies, roles_dir=str(roles_dir),
            )

    def test_mismatched_kind_raises(self, tmp_path):
        """A role artifact found at the CORRECT path (specialist/finance,
        matching the directory's inferred tier and slot) but whose OWN
        declared 'kind' field disagrees is rejected — the loader trusts
        the artifact's own declared kind, not just its location."""
        from agent_loader import LoadError, load_agent_from_dir

        agent_dir = _seed_specialist(tmp_path / "specialists", "finance")
        roles_dir = tmp_path / "roles"
        d = roles_dir / "specialist" / "finance"
        _w(d / "role.yaml", """\
            api_version: casa.role/v1
            id: resident:finance
            kind: resident
            slot: finance
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
        """)
        _w(d / "doctrine.md", "# Core doctrine\n\nBody.\n")

        with pytest.raises(LoadError, match="kind"):
            load_agent_from_dir(
                str(agent_dir), policies=None, roles_dir=str(roles_dir),
            )

    def test_mismatched_id_raises(self, tmp_path):
        """FIX 4 (foundation review, P1): a role artifact whose declared
        kind and slot BOTH match the directory (so the earlier kind/slot
        cross-checks pass) but whose 'id' field disagrees with
        f"{kind}:{slot}" must still be rejected — mirroring the id check
        the executor path already does (_load_executor_role_artifact)."""
        from agent_loader import LoadError, load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "resident", "assistant")
        role_yaml = roles_dir / "resident" / "assistant" / "role.yaml"
        text = role_yaml.read_text(encoding="utf-8")
        assert "id: resident:assistant" in text
        role_yaml.write_text(
            text.replace("id: resident:assistant", "id: resident:butler"),
            encoding="utf-8",
        )

        with pytest.raises(LoadError, match="id"):
            load_agent_from_dir(
                str(agent_dir), policies=policies, roles_dir=str(roles_dir),
            )

    def test_schema_invalid_role_artifact_raises(self, tmp_path):
        """A schema-invalid role.yaml under the resolved role_dir fails
        the load too — load_agent_from_dir wraps role_artifact.py's raw
        jsonschema.ValidationError into the module's own LoadError
        contract, consistent with every other schema-bearing file here."""
        from agent_loader import LoadError, load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))
        roles_dir = tmp_path / "roles"
        d = roles_dir / "resident" / "assistant"
        _w(d / "role.yaml", "api_version: casa.role/v1\n")  # missing everything else
        _w(d / "doctrine.md", "# Core doctrine\n\nBody.\n")

        with pytest.raises(LoadError, match="role artifact"):
            load_agent_from_dir(
                str(agent_dir), policies=policies, roles_dir=str(roles_dir),
            )


def test_runtime_loads_context_surface_policy(tmp_path):
    from agent_loader import load_agent_from_dir
    from policies import load_policies

    agent_dir = _seed_resident(tmp_path / "agents", "butler")
    _w(agent_dir / "runtime.yaml", """\
        schema_version: 1
        kind: resident
        model: {source: ha_option, option: voice_agent_model, default: haiku, allowed: [opus, sonnet, haiku]}
        tools:
          allowed: [mcp__casa-framework__get_schedule]
          skills: none
          voice_guard: ha_direct
        channels: [ha_voice]
    """)
    policies = load_policies(str(_policies_file(tmp_path / "policies")))

    cfg = load_agent_from_dir(str(agent_dir), policies=policies)

    assert cfg.tools.skills == "none"
    assert cfg.tools.voice_guard == "ha_direct"


def test_runtime_context_policy_defaults_preserve_existing_agents(tmp_path):
    from agent_loader import load_agent_from_dir
    from policies import load_policies

    agent_dir = _seed_resident(tmp_path / "agents", "assistant")
    policies = load_policies(str(_policies_file(tmp_path / "policies")))

    cfg = load_agent_from_dir(str(agent_dir), policies=policies)

    assert cfg.tools.skills == "all"
    assert cfg.tools.voice_guard == "none"


# ---------------------------------------------------------------------------
# TestStrictMode — unknown field / file / schema_version
# ---------------------------------------------------------------------------


class TestStrictMode:
    def test_unknown_field_in_character_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "character.yaml", """\
            schema_version: 1
            name: Ellen
            role: assistant
            archetype: assistant
            card: x
            prompt: x
            bogus_field: true
        """)
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="character.yaml"):
            load_agent_from_dir(str(agent_dir), policies=policies)

    def test_unknown_file_in_agent_dir_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "stray.yaml", "schema_version: 1\n")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="stray.yaml"):
            load_agent_from_dir(str(agent_dir), policies=policies)

    def test_dotfiles_skipped(self, tmp_path):
        """.git, .DS_Store, .swp should not trip strict-mode."""
        from agent_loader import load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / ".DS_Store", "junk")
        _w(agent_dir / ".swp.tmp", "junk")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        cfg = load_agent_from_dir(str(agent_dir), policies=policies)
        assert cfg.role == "assistant"

    @pytest.mark.parametrize(
        "backup_name",
        [
            "character.yaml.bak",
            "runtime.yaml.swp",
            "voice.yaml.tmp",
            "response_shape.yaml.orig",
            "runtime.yaml~",
        ],
    )
    def test_editor_backups_skipped(self, tmp_path, backup_name):
        """S-1 regression: sed -i.bak / vim .swp / *~ MUST NOT trip
        strict-mode. The agent on disk is still valid; the editor
        artifact is process state, not configuration."""
        from agent_loader import load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / backup_name, "ignored editor artifact")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        cfg = load_agent_from_dir(str(agent_dir), policies=policies)
        assert cfg.role == "assistant"

    def test_genuine_unknown_file_still_raises(self, tmp_path):
        """S-1 negative: real unknown files (no backup suffix) MUST
        still raise. The whitelist must not be a wildcard."""
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "extra.yaml", "schema_version: 1\n")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="extra.yaml"):
            load_agent_from_dir(str(agent_dir), policies=policies)

    def test_unknown_file_diagnostic_mentions_recovery(self, tmp_path):
        """S-1 UX: diagnostic message MUST point operators at the
        recovery path (mention editor-backup whitelist + git restore)."""
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "extra.yaml", "schema_version: 1\n")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError) as exc_info:
            load_agent_from_dir(str(agent_dir), policies=policies)

        msg = str(exc_info.value)
        assert ".bak" in msg, f"diagnostic should mention editor backups: {msg!r}"
        assert "git" in msg.lower(), f"diagnostic should mention git restore: {msg!r}"

    def test_wrong_schema_version_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "voice.yaml", "schema_version: 2\n")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="schema_version"):
            load_agent_from_dir(str(agent_dir), policies=policies)


# ---------------------------------------------------------------------------
# TestTierRules
# ---------------------------------------------------------------------------


class TestTierRules:
    def test_resident_missing_required_file_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        (agent_dir / "disclosure.yaml").unlink()
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="disclosure.yaml"):
            load_agent_from_dir(str(agent_dir), policies=policies)

    def test_specialist_with_disclosure_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError

        agent_dir = _seed_specialist(tmp_path / "specialists", "finance")
        _w(agent_dir / "disclosure.yaml", "schema_version: 1\npolicy: standard\n")

        with pytest.raises(LoadError, match="disclosure.yaml"):
            load_agent_from_dir(str(agent_dir), policies=None)

    def test_specialist_with_triggers_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError

        agent_dir = _seed_specialist(tmp_path / "specialists", "finance")
        _w(agent_dir / "triggers.yaml",
           "schema_version: 1\ntriggers: []\n")

        with pytest.raises(LoadError, match="triggers.yaml"):
            load_agent_from_dir(str(agent_dir), policies=None)

    def test_role_must_match_directory(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        # character.yaml carries `role: butler` but directory is named assistant
        _w(agent_dir / "character.yaml", """\
            schema_version: 1
            name: Ellen
            role: butler
            archetype: assistant
            card: x
            prompt: x
        """)
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="role"):
            load_agent_from_dir(str(agent_dir), policies=policies)


# ---------------------------------------------------------------------------
# TestDelegatesValidation
# ---------------------------------------------------------------------------


class TestDelegatesValidation:
    def test_non_empty_delegates_require_mcp_tool(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "delegates.yaml", """\
            schema_version: 1
            delegates:
              - agent: finance
                purpose: money
                when: money q
        """)
        # runtime.yaml tools.allowed missing delegate_to_agent
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="delegate_to_agent"):
            load_agent_from_dir(str(agent_dir), policies=policies)

    def test_empty_delegates_does_not_require_mcp_tool(self, tmp_path):
        from agent_loader import load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "delegates.yaml",
           "schema_version: 1\ndelegates: []\n")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        cfg = load_agent_from_dir(str(agent_dir), policies=policies)
        assert cfg.delegates == []


# ---------------------------------------------------------------------------
# TestComposition
# ---------------------------------------------------------------------------


class TestComposition:
    def test_system_prompt_composition_order(self, tmp_path):
        """character → voice → response_shape → delegates → disclosure."""
        from agent_loader import load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "delegates.yaml", """\
            schema_version: 1
            delegates: []
        """)
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        cfg = load_agent_from_dir(str(agent_dir), policies=policies)

        prompt = cfg.system_prompt
        pos_char = prompt.index("You are Ellen.")
        pos_voice = prompt.index("### Voice")
        pos_rshape = prompt.index("### Response shape")
        pos_disc = prompt.index("### Disclosure")

        assert pos_char < pos_voice < pos_rshape < pos_disc


# ---------------------------------------------------------------------------
# TestLoadAllAgents
# ---------------------------------------------------------------------------


class TestLoadAllAgents:
    def test_finds_two_residents_skips_specialists_subdir(self, tmp_path):
        """Personality Phase A, Task 6: load_all_agents now fails closed
        unless the loaded resident set is EXACTLY the fixed three slots
        (role_slot.FIXED_RESIDENT_SLOTS) — seed all three (against the REAL
        production defaults/roles/resident/ tree, no roles_dir override) so
        this test can still isolate its actual point: the specialists/
        subdirectory is skipped, not folded into the resident set."""
        from agent_loader import load_all_agents
        from policies import load_policies

        agents_root = tmp_path / "agents"
        _seed_resident(agents_root, "assistant")
        _seed_resident(agents_root, "butler")
        _seed_resident(agents_root, "concierge")
        _seed_specialist(agents_root / "specialists", "finance")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        found = load_all_agents(str(agents_root), policies=policies)
        assert set(found.keys()) == {"assistant", "butler", "concierge"}

    def test_skips_dotdirs(self, tmp_path):
        """Personality Phase A, Task 6: the fixed three-slot resident-set
        enforcement means this test must seed all three to reach
        load_all_agents' return at all — its actual point (a dotdir sitting
        under agents/ must not be walked as an agent) is isolated by
        asserting no fourth/dot-named key sneaks into the result."""
        from agent_loader import load_all_agents
        from policies import load_policies

        agents_root = tmp_path / "agents"
        _seed_resident(agents_root, "assistant")
        _seed_resident(agents_root, "butler")
        _seed_resident(agents_root, "concierge")
        (agents_root / ".git").mkdir()   # git repo root sits here

        policies = load_policies(str(_policies_file(tmp_path / "policies")))
        found = load_all_agents(str(agents_root), policies=policies)
        assert set(found.keys()) == {"assistant", "butler", "concierge"}

    def test_missing_dir_returns_empty(self, tmp_path):
        from agent_loader import load_all_agents
        assert load_all_agents(str(tmp_path / "nope"), policies=None) == {}


class TestLoadAllSpecialists:
    def test_finds_specialist(self, tmp_path):
        from agent_loader import load_all_specialists

        specialists_root = tmp_path / "specialists"
        _seed_specialist(specialists_root, "finance")
        # Task N2's no-gap cutover removed the real image
        # defaults/roles/specialist/finance/ — synthesize it.
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "specialist", "finance")

        found, failed = load_all_specialists(
            str(specialists_root), roles_dir=str(roles_dir),
        )
        assert "finance" in found
        assert failed == []

    def test_per_specialist_isolation(self, tmp_path):
        """O-2b (v0.37.9): one malformed specialist does NOT prevent its
        siblings from loading; the bad one is recorded in ``failed``.
        Mirrors the v0.37.1 B-1b ``load_all_executors`` pattern.

        Pins INV-AGENT-003. Red case demonstrated: narrowing load_all_specialists' except so LoadError escapes fails this test.
        """
        from agent_loader import load_all_specialists

        specialists_root = tmp_path / "specialists"
        _seed_specialist(specialists_root, "finance")
        # Sibling directory with no required files → load fails.
        (specialists_root / "broken").mkdir()
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "specialist", "finance")

        found, failed = load_all_specialists(
            str(specialists_root), roles_dir=str(roles_dir),
        )
        assert "finance" in found
        assert any(name == "broken" for name, _ in failed)
        # Error message must name the missing required file so the
        # operator can see what went wrong.
        broken_msg = next(err for name, err in failed if name == "broken")
        assert "runtime.yaml" in broken_msg

    def test_clamps_forbidden_casa_grants_from_pre_ceiling_installs(
            self, tmp_path):
        """#541 layer 2: a runtime.yaml materialized before the tool ceiling
        existed keeps loading, minus any casa-framework grant outside the
        consumer-safe allowlist (WARN, not boot-fatal)."""
        from agent_loader import load_all_specialists

        specialists_root = tmp_path / "specialists"
        d = _seed_specialist(specialists_root, "finance")
        runtime = (d / "runtime.yaml").read_text()
        (d / "runtime.yaml").write_text(runtime + (
            "tools:\n"
            "  allowed:\n"
            "    - Read\n"
            "    - mcp__casa-framework__recall_memory\n"
            "    - mcp__casa-framework__engage_executor\n"
            "    - mcp__casa-framework\n"
        ))
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "specialist", "finance")

        found, failed = load_all_specialists(
            str(specialists_root), roles_dir=str(roles_dir),
        )
        assert failed == []
        allowed = found["finance"].tools.allowed
        assert "Read" in allowed
        assert "mcp__casa-framework__recall_memory" in allowed
        assert "mcp__casa-framework__engage_executor" not in allowed
        assert "mcp__casa-framework" not in allowed


# ---------------------------------------------------------------------------
# TestBuildRoleRegistry
# ---------------------------------------------------------------------------


def test_duplicate_role_across_residents_and_specialists_fails(tmp_path):
    """A role present in BOTH residents/<role>/ and specialists/<role>/
    must fail boot — both registries cannot share a role.

    Pins INV-SYS-002 and INV-AGENT-001. Red case demonstrated: neutering _build_role_registry's overlap raise fails this test.
    """
    from casa_core import _build_role_registry  # introduced by Task 5
    residents = {"finance": object()}      # placeholder configs
    specialists = {"finance": object()}    # collision on `finance`
    with pytest.raises(ValueError) as exc:
        _build_role_registry(residents=residents, specialists=specialists)
    msg = str(exc.value).lower()
    assert "duplicate" in msg
    assert "finance" in msg


def test_butler_runtime_grants_homeassistant_server_level():
    """Butler must have server-level mcp__homeassistant grant so every HA
    Assist tool the user exposes is callable without per-tool enumeration.
    Spike (2026-04-26) confirmed the SDK forwards `mcp__<server>` verbatim
    to the CC CLI, which honours it as a server-level wildcard."""
    import yaml
    from pathlib import Path

    runtime_path = (
        Path(__file__).resolve().parents[1]
        / "casa/rootfs/opt/casa/defaults/agents/butler/runtime.yaml"
    )
    data = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    allowed = data["tools"]["allowed"]
    assert "mcp__homeassistant" in allowed, (
        f"butler.tools.allowed missing mcp__homeassistant; got {allowed}"
    )


def test_butler_runtime_sets_minimal_context_surface_policy():
    import yaml
    from pathlib import Path

    runtime_path = (
        Path(__file__).resolve().parents[1]
        / "casa/rootfs/opt/casa/defaults/agents/butler/runtime.yaml"
    )
    data = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))

    assert data["tools"]["skills"] == "none"
    assert data["tools"]["voice_guard"] == "ha_direct"
    assert "Skill" not in data["tools"]["allowed"]
    assert data["mcp_server_names"] == ["homeassistant", "casa-framework"]


def test_assistant_runtime_omits_unused_homeassistant_attachment():
    import yaml
    from pathlib import Path

    runtime_path = (
        Path(__file__).resolve().parents[1]
        / "casa/rootfs/opt/casa/defaults/agents/assistant/runtime.yaml"
    )
    data = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))

    assert "mcp__homeassistant" not in data["tools"]["allowed"]
    assert "homeassistant" not in data["mcp_server_names"]


def test_assistant_delegates_include_butler():
    """Ellen's delegates.yaml must list butler so the v0.15.0 <delegates>
    block advertises Tina to her and delegate_to_agent('butler', ...) passes
    the role-map gate."""
    import yaml
    from pathlib import Path

    delegates_path = (
        Path(__file__).resolve().parents[1]
        / "casa/rootfs/opt/casa/defaults/agents/assistant/delegates.yaml"
    )
    data = yaml.safe_load(delegates_path.read_text(encoding="utf-8"))
    delegate_roles = [d["agent"] for d in data["delegates"]]
    assert "butler" in delegate_roles, (
        f"assistant.delegates missing butler; got {delegate_roles}"
    )


def test_runtime_yaml_loads_cross_peer_token_budget(tmp_path):
    """M6 § 6.3: residents may declare memory.cross_peer_token_budget
    in runtime.yaml; agent_loader populates MemoryConfig with it.

    Default value is 2000 when omitted (spec § 6.3)."""
    from agent_loader import load_agent_from_dir
    from policies import load_policies

    policies = load_policies(str(_policies_file(tmp_path / "policies")))

    # Test 1: explicit value round-trips through the loader.
    explicit_dir = _seed_resident(tmp_path / "agents_explicit", "assistant")
    _w(explicit_dir / "runtime.yaml", """\
        schema_version: 1
        kind: resident
        model: {source: ha_option, option: primary_agent_model, default: opus, allowed: [opus, sonnet, haiku]}
        tools:
          allowed: [Read, Write]
        memory:
          cross_peer_token_budget: 4000
        channels: [telegram]
    """)
    cfg_explicit = load_agent_from_dir(str(explicit_dir), policies=policies)
    assert cfg_explicit.memory.cross_peer_token_budget == 4000

    # Test 2: default value (2000) when the key is omitted.
    default_dir = _seed_resident(tmp_path / "agents_default", "assistant")
    cfg_default = load_agent_from_dir(str(default_dir), policies=policies)
    assert cfg_default.memory.cross_peer_token_budget == 2000


# ---------------------------------------------------------------------------
# TestValidateConfigRepo (E-G v0.31.0 pre-commit gate)
# ---------------------------------------------------------------------------


class TestValidateConfigRepo:
    """Pre-commit schema-validation gate for ``config_git_commit``. Repros
    the v0.30.0 P4.2 finding: configurator wrote ``TRAIT:`` as a top-level
    YAML key in character.yaml; ``additionalProperties: False`` rejects
    it on next boot. The gate refuses such commits before they land."""

    def test_clean_repo_returns_empty(self, tmp_path):
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa"
        resident_dir = _seed_resident(repo / "agents", "assistant")
        _seed_specialist(repo / "agents", "finance")
        # A genuinely-bootable repo has a policy library (boot's load_policies
        # is unguarded); without it the add-on crash-loops (M5).
        _policies_file(repo / "policies")

        # Task N2's no-gap cutover removed the real image
        # defaults/roles/specialist/finance/ — synthesize a roles_dir
        # carrying both 'assistant' (resident) and 'finance' (specialist)
        # since a custom roles_dir replaces the default tree entirely
        # (mirrors TestValidateConfigRepoBootParity's established idiom).
        # The synthetic role artifact's model is always the fixed-sonnet
        # shape, so the resident's runtime.yaml must match it too.
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "resident", "assistant")
        _seed_role_artifact(roles_dir, "specialist", "finance")
        _w(resident_dir / "runtime.yaml", """\
            schema_version: 1
            kind: resident
            model: {source: fixed, value: sonnet}
            tools:
              allowed: [Read, Write]
            channels: [telegram]
        """)

        _seed_remaining_residents(repo, roles_dir)

        errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
        assert errors == []

    def test_no_agents_dir_returns_empty(self, tmp_path):
        """Fresh repo without agents/ subtree (e.g., immediately after init
        but before seed-copy) returns an empty error list rather than
        raising. Defensive: validate_config_repo is called from a
        privileged tool path and must not crash on edge filesystems."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa"
        repo.mkdir(parents=True)
        errors = validate_config_repo(str(repo))
        assert errors == []

    def test_valid_policies_disclosure_passes(self, tmp_path):
        """v0.37.12 — policies/disclosure.yaml is now actively validated
        (against policy-disclosure.v1, NOT the agent disclosure schema).
        A valid file must pass cleanly. Historical note: v0.31.0 walked
        the whole repo with a flat basename map and mis-applied the
        agent schema here; v0.31.1 scoped to agents/ only as a stopgap;
        v0.37.12 introduces a path-aware policies/ walk so the
        configurator can't ship schema-invalid policy YAML."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa"
        _seed_resident(repo / "agents", "assistant")
        _w(repo / "policies" / "disclosure.yaml", """\
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

        _seed_remaining_residents(repo)

        errors = validate_config_repo(str(repo))
        assert errors == [], (
            f"valid policies/disclosure.yaml must pass; got {errors}"
        )

    def test_invalid_policies_disclosure_caught(self, tmp_path):
        """policies/disclosure.yaml with a top-level unknown key fails
        ``additionalProperties: False`` on policy-disclosure.v1 and is
        surfaced by the gate. Repro of the E-G class of bug applied to
        policies/."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa"
        _seed_resident(repo / "agents", "assistant")
        _w(repo / "policies" / "disclosure.yaml", """\
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
            BOGUS_POLICY_KEY: surprise
        """)

        errors = validate_config_repo(str(repo))
        assert len(errors) == 1
        assert "disclosure.yaml" in errors[0]
        assert "BOGUS_POLICY_KEY" in errors[0]
        assert "schema violation" in errors[0]

    def test_top_level_unknown_key_in_character_caught(self, tmp_path):
        """Exact repro of the v0.30.0 / v0.29.0 P4.2 'TRAIT:' incident."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa"
        agent_dir = _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")  # isolate the character.yaml error

        # Append the offending top-level key, mirroring the configurator's
        # actual diff (commit 5cd731ac on 2026-04-30).
        _w(agent_dir / "character.yaml", """\
            schema_version: 1
            name: Ellen
            role: assistant
            archetype: assistant
            card: |
              Peer summary.
            prompt: |
              You are Ellen.
            TRAIT: greets warmly but keeps replies efficient.
        """)

        _seed_remaining_residents(repo)

        errors = validate_config_repo(str(repo))
        assert len(errors) == 1
        assert "character.yaml" in errors[0]
        assert "TRAIT" in errors[0]
        assert "schema violation" in errors[0]

    def test_skips_non_schema_files(self, tmp_path):
        """Markdown doctrine, plugin sources, READMEs etc. must NOT trip
        the gate — they're outside the schema-bearing YAML allowlist."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa"
        _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        _w(repo / "agents" / "assistant" / "prompts" / "system.md",
           "Some prompt body — not schema-validated.\n")
        _w(repo / "doctrine.md", "free-form notes\n")
        # README inside policies/ is not a schema-bearing file.
        _w(repo / "policies" / "README.md", "free-form policy notes\n")
        _seed_remaining_residents(repo)

        errors = validate_config_repo(str(repo))
        assert errors == []

    def test_skips_dotgit_dir(self, tmp_path):
        """``.git/`` inside agents/ must NOT be walked — paranoia (a real
        repo's .git would be at the config_dir root, but a buggy nested
        copy could land schema-named blobs inside agents/)."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa"
        _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        # Write a file inside agents/.git that would FAIL validation if visited.
        _w(repo / "agents" / ".git" / "objects" / "character.yaml",
           "TRAIT: garbage\n")
        _seed_remaining_residents(repo)

        errors = validate_config_repo(str(repo))
        assert errors == []

    def test_aggregates_multiple_offenders(self, tmp_path):
        """Multiple bad files all surface — caller sees the full picture."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa"
        agent_dir = _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")  # isolate the two character.yaml errors
        _w(agent_dir / "character.yaml", """\
            schema_version: 1
            name: Ellen
            role: assistant
            archetype: assistant
            card: x
            prompt: x
            BOGUS_A: 1
        """)
        spec_dir = _seed_specialist(repo / "agents", "finance")
        _w(spec_dir / "character.yaml", """\
            schema_version: 1
            name: Alex
            role: finance
            archetype: finance
            card: x
            prompt: x
            BOGUS_B: 2
        """)
        # Task N2's no-gap cutover removed the real image
        # defaults/roles/specialist/finance/ — synthesize a roles_dir
        # carrying both 'assistant' and 'finance' (see
        # test_clean_repo_returns_empty above for the full rationale).
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "resident", "assistant")
        _seed_role_artifact(roles_dir, "specialist", "finance")
        _w(agent_dir / "runtime.yaml", """\
            schema_version: 1
            kind: resident
            model: {source: fixed, value: sonnet}
            tools:
              allowed: [Read, Write]
            channels: [telegram]
        """)

        _seed_remaining_residents(repo, roles_dir)

        errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
        assert len(errors) == 2
        joined = "\n".join(errors)
        assert "BOGUS_A" in joined
        assert "BOGUS_B" in joined


@pytest.mark.unit
class TestValidateConfigRepoBootParity:
    """M5: the gate must refuse anything load_all_agents would FATAL on at
    boot — cross-file invariants that pass per-file schema validation yet
    crash-loop the add-on on the next boot."""

    def _policies(self, repo: Path) -> None:
        _w(repo / "policies" / "disclosure.yaml", """\
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

    def test_copied_resident_with_stale_role_refused(self, tmp_path):
        """Personality Phase A, Task 6: role-artifact loading now happens
        BEFORE the character.yaml role-match check inside
        load_agent_from_dir (Step 8's constructor-order fix), so a copied
        directory with no corresponding role artifact would fail EARLIER on
        a missing role artifact rather than exercising the check this test
        actually targets. Seed a synthetic roles_dir carrying a 'helper'
        resident role artifact too (alongside 'assistant', since a custom
        roles_dir replaces the default tree entirely), so the role-artifact
        load succeeds and the intended character.yaml/directory-name
        mismatch is what raises."""
        import shutil
        from agent_loader import validate_config_repo, load_agent_from_dir, LoadError

        repo = tmp_path / "cfg"
        src = _seed_resident(repo / "agents", "assistant")
        self._policies(repo)
        # Copy the whole dir but leave character.yaml role: assistant.
        shutil.copytree(src, repo / "agents" / "helper")

        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "resident", "assistant")
        _seed_role_artifact(roles_dir, "resident", "helper")
        # _seed_role_artifact's fixture model is always {source: fixed,
        # value: sonnet} — match both copied runtime.yaml files to it (they
        # otherwise default to the REAL production ha_option shape, which
        # would mismatch this synthetic roles_dir's role.yaml).
        for d in (repo / "agents" / "assistant", repo / "agents" / "helper"):
            _w(d / "runtime.yaml", """\
                schema_version: 1
                kind: resident
                model: {source: fixed, value: sonnet}
                tools:
                  allowed: [Read, Write]
                channels: [telegram]
            """)

        with pytest.raises(LoadError):  # boot fatals on this dir
            load_agent_from_dir(
                str(repo / "agents" / "helper"), policies=None, roles_dir=str(roles_dir),
            )

        errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
        assert any("must match directory name" in e for e in errors), errors

    def test_stray_unknown_file_in_resident_refused(self, tmp_path):
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        d = _seed_resident(repo / "agents", "assistant")
        self._policies(repo)
        _w(d / "notes.yaml", "anything: true\n")  # unmapped basename: schema walk skips it

        errors = validate_config_repo(str(repo))
        assert any("unknown file" in e for e in errors), errors

    def test_schema_valid_executors_yaml_on_non_assistant_refused(self, tmp_path):
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        d = _seed_resident(repo / "agents", "butler")
        self._policies(repo)
        _w(d / "executors.yaml", """\
            schema_version: 1
            executors:
              - executor_type: configurator
                purpose: config edits
                when: on request
        """)

        errors = validate_config_repo(str(repo))
        assert any("only allowed on the assistant role" in e for e in errors), errors

    def test_delegates_without_delegate_tool_refused(self, tmp_path):
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        d = _seed_resident(repo / "agents", "assistant")  # tools.allowed = [Read, Write]
        self._policies(repo)
        _w(d / "delegates.yaml", """\
            schema_version: 1
            delegates:
              - {agent: finance, purpose: money, when: asked}
        """)

        errors = validate_config_repo(str(repo))
        assert any("delegate_to_agent" in e for e in errors), errors

    def test_stray_non_directory_at_agents_root_refused(self, tmp_path):
        """M5 gate-bypass: a schema-valid (or non-schema) stray FILE directly
        under agents/ passes the per-file schema walk but crash-loops boot —
        load_all_agents RAISES LoadError on any non-directory at agents/ root.
        The parity loop must report it, not silently skip it."""
        from agent_loader import validate_config_repo, load_all_agents, LoadError

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        self._policies(repo)
        # Stray non-schema file at agents/ root: schema walk ignores it,
        # but boot fatals on it.
        _w(repo / "agents" / "scratch.md", "stray note, not an agent\n")

        with pytest.raises(LoadError):  # boot fatals on this entry
            load_all_agents(str(repo / "agents"), policies=None)

        errors = validate_config_repo(str(repo))
        assert any("non-directory" in e and "scratch.md" in e for e in errors), errors

    def test_missing_policies_file_with_resident_refused(self, tmp_path):
        """M5 gate-bypass: a resident with a disclosure.yaml but NO
        policies/disclosure.yaml passes every per-file schema check, yet
        boot's ``load_policies`` (casa_core.main line 1245, unguarded)
        RAISES ``PolicyError`` and crash-loops the add-on. The gate must
        report the missing policy library, not suppress the resulting
        'no PolicyLibrary' compose cascade."""
        from agent_loader import validate_config_repo
        from policies import PolicyError, load_policies

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")  # has disclosure.yaml
        # Deliberately NO policies/disclosure.yaml — the bypass under test.

        # Prove boot fatals exactly as casa_core.main would at line 1245.
        with pytest.raises(PolicyError):
            load_policies(str(repo / "policies" / "disclosure.yaml"))

        errors = validate_config_repo(str(repo))
        assert any("disclosure.yaml" in e and "not found" in e for e in errors), errors
        # The single missing-policies error is authoritative; the per-resident
        # 'no PolicyLibrary' cascade rooted in that same absence must NOT
        # double-report.
        assert not any("no PolicyLibrary" in e for e in errors), errors

    def test_missing_policies_file_no_agents_dir_passes(self, tmp_path):
        """A fresh repo with no agents/ subtree (pre-seed) does not trip the
        missing-policies check — boot never reaches load_all_agents there,
        matching the existing no-agents-dir contract."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        repo.mkdir(parents=True)
        errors = validate_config_repo(str(repo))
        assert errors == [], errors

    def test_clean_repo_with_policies_passes(self, tmp_path):
        """A schema- and boot-valid repo still returns no errors."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        _seed_remaining_residents(repo)
        self._policies(repo)

        errors = validate_config_repo(str(repo))
        assert errors == [], errors

    def _enable_specialist(self, spec_dir: Path, role: str) -> None:
        """Rewrite a seeded specialist's runtime.yaml to enabled: true so it
        reaches SpecialistRegistry.all_configs() at boot (the seed default is
        enabled: false, which would be dropped and never collide)."""
        _w(spec_dir / "runtime.yaml", f"""\
            schema_version: 1
            kind: specialist
            model: {{source: fixed, value: sonnet}}
            enabled: true
            memory:
              token_budget: 0
            session:
              strategy: ephemeral
        """)

    def test_resident_specialist_role_collision_refused(self, tmp_path):
        """M5 gate-bypass: a repo with agents/<r>/ (valid resident) AND an
        ENABLED agents/specialists/<r>/ (valid specialist) passes every
        per-file schema check and the per-agent load, yet crash-loops boot.

        casa_core.main (line ~1280) calls, UNGUARDED,
            _build_role_registry(residents=role_configs,
                                  specialists=specialist_registry.all_configs())
        which RAISES ValueError 'duplicate role(s) across residents and
        specialists' when a role exists in both tiers. config_sync does not
        heal it. The gate must refuse the collision by replaying that
        registry build.

        Personality Phase A, Task 6: load_all_agents now fails closed unless
        the loaded resident set is EXACTLY the fixed three slots
        (assistant/butler/concierge) — a resident named 'finance' (the
        original repro's collision slot, prior to Task 6) can no longer
        exist at all, so this collision can only still manifest on ONE OF
        THE FIXED SLOTS. Collide on 'butler' instead: residents
        assistant+butler+concierge (satisfying the fixed-slot invariant)
        plus an ENABLED specialist ALSO named 'butler'."""
        from agent_loader import (
            validate_config_repo,
            load_all_agents,
            load_all_specialists,
        )
        from policies import load_policies
        from casa_core import _build_role_registry

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        _seed_resident(repo / "agents", "butler")             # resident butler
        _seed_resident(repo / "agents", "concierge")
        spec = _seed_specialist(repo / "agents" / "specialists", "butler")
        self._enable_specialist(spec, "butler")               # enabled specialist butler
        self._policies(repo)
        # The real shipped image carries no SPECIALIST 'butler' role
        # artifact — this test deliberately probes the cross-tier
        # collision, so it needs its own fixture roles_dir carrying every
        # artifact this repo's agents load against: the three REAL
        # residents plus a synthetic specialist 'butler'.
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "resident", "assistant")
        _seed_role_artifact(roles_dir, "resident", "butler")
        _seed_role_artifact(roles_dir, "resident", "concierge")
        _seed_role_artifact(roles_dir, "specialist", "butler")
        # _seed_role_artifact's fixture model is always {source: fixed,
        # value: sonnet} — this synthetic roles_dir replaces the default
        # tree, so the three resident runtime.yaml files (which
        # _seed_resident defaulted to the REAL production ha_option shape)
        # must be rewritten to match it.
        for slot in ("assistant", "butler", "concierge"):
            _w(repo / "agents" / slot / "runtime.yaml", """\
                schema_version: 1
                kind: resident
                model: {source: fixed, value: sonnet}
                tools:
                  allowed: [Read, Write]
                channels: [telegram]
            """)

        # Prove boot fatals exactly as casa_core.main would at line ~1280:
        # residents + enabled specialists both carry role 'butler'.
        policy_lib = load_policies(str(repo / "policies" / "disclosure.yaml"))
        residents = load_all_agents(
            str(repo / "agents"), policies=policy_lib, roles_dir=str(roles_dir),
        )
        spec_found, _ = load_all_specialists(
            str(repo / "agents" / "specialists"), roles_dir=str(roles_dir),
        )
        with pytest.raises(ValueError):
            _build_role_registry(residents=residents, specialists=spec_found)

        errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
        assert any(
            "duplicate role" in e and "butler" in e for e in errors
        ), errors

    def test_disabled_specialist_role_collision_passes(self, tmp_path):
        """Boot parity, negative direction: a DISABLED specialist sharing a
        resident's role does NOT collide at boot — SpecialistRegistry.load
        drops disabled specialists before all_configs(), so
        _build_role_registry never sees the overlap. The gate must mirror
        that enablement filter and NOT over-refuse the (bootable) repo.

        Personality Phase A, Task 6: role_slot.validate_role_shape itself
        (not just load_all_agents' Step 9 check) now rejects ANY resident
        slot outside the fixed three (assistant/butler/concierge) — a
        resident named 'finance' can no longer exist at all. Collide on
        'butler' instead, mirroring the sibling
        test_resident_specialist_role_collision_refused above. The repo
        carries all three fixed residents, so the no-primary-assistant
        invariant (below) does not fire — this test isolates the collision
        path, not the missing-assistant path."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")          # primary assistant
        _seed_resident(repo / "agents", "butler")             # resident butler
        _seed_resident(repo / "agents", "concierge")
        # Seeded specialist defaults to enabled: false — dropped at boot.
        _seed_specialist(repo / "agents" / "specialists", "butler")
        self._policies(repo)
        # roles_dir override replaces the default tree entirely, so it
        # must carry every artifact this repo's agents load against —
        # the three REAL residents plus the synthetic specialist 'butler'
        # this test is actually probing.
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "resident", "assistant")
        _seed_role_artifact(roles_dir, "resident", "butler")
        _seed_role_artifact(roles_dir, "resident", "concierge")
        _seed_role_artifact(roles_dir, "specialist", "butler")
        # _seed_role_artifact's fixture model is always {source: fixed,
        # value: sonnet} — rewrite the three resident runtime.yaml files
        # (which _seed_resident defaulted to the REAL production ha_option
        # shape) to match this synthetic roles_dir.
        for slot in ("assistant", "butler", "concierge"):
            _w(repo / "agents" / slot / "runtime.yaml", """\
                schema_version: 1
                kind: resident
                model: {source: fixed, value: sonnet}
                tools:
                  allowed: [Read, Write]
                channels: [telegram]
            """)

        errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
        assert errors == [], errors

    def test_no_primary_assistant_butler_only_refused(self, tmp_path):
        """M5 gate-bypass: a committed tree whose only resident is a valid
        NON-assistant (e.g. butler) passes every per-file schema check and the
        per-agent load, yet crash-loops boot. casa_core.main (line ~1306)
        RAISES RuntimeError 'No agent with role \\'assistant\\' found ... Casa
        cannot start without a primary assistant' because role_configs holds no
        'assistant'. The gate must refuse it.

        Personality Phase A, Task 6: load_all_agents' own Step 9 fixed-slot
        invariant now ALSO independently refuses this tree — even earlier,
        and for a different (LoadError, not the downstream RuntimeError)
        reason, since the loaded resident set ({'butler'}) isn't the
        complete fixed three. validate_config_repo's boot-parity replay is a
        SEPARATE loop (not load_all_agents) and still isolates + reports the
        primary-assistant gap on its own terms, so that assertion is
        unchanged; only the direct load_all_agents call's expectation
        changes from 'succeeds with no assistant key' to 'raises LoadError'."""
        from agent_loader import validate_config_repo, load_all_agents, LoadError
        from policies import load_policies

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "butler")   # enabled, non-assistant
        self._policies(repo)

        # Prove boot fatals on this incomplete resident set: load_all_agents
        # now raises LoadError before ever returning role_configs (Step 9),
        # independent of casa_core.main's downstream primary-assistant check.
        policy_lib = load_policies(str(repo / "policies" / "disclosure.yaml"))
        with pytest.raises(LoadError, match="fixed slots"):
            load_all_agents(str(repo / "agents"), policies=policy_lib)

        errors = validate_config_repo(str(repo))
        assert any("primary assistant" in e for e in errors), errors

    def test_missing_non_assistant_resident_slot_refused(self, tmp_path):
        """#324: the gate must enforce the FULL fixed resident set, not just
        the assistant — a committed tree missing agents/butler/ passed the
        gate while boot's load_all_agents raises LoadError on it (a warm
        `agents` reload then fails until restore/config-sync)."""
        from agent_loader import validate_config_repo, load_all_agents, LoadError
        from policies import load_policies

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        _seed_resident(repo / "agents", "concierge")   # butler missing
        self._policies(repo)

        # Boot parity: load_all_agents refuses this tree.
        policy_lib = load_policies(str(repo / "policies" / "disclosure.yaml"))
        with pytest.raises(LoadError, match="fixed slots"):
            load_all_agents(str(repo / "agents"), policies=policy_lib)

        errors = validate_config_repo(str(repo))
        assert any("agents/butler/" in e for e in errors), errors
        # No derivative noise about the slots that ARE present.
        assert not any("agents/concierge/" in e for e in errors), errors

    def test_no_primary_assistant_empty_agents_dir_refused(self, tmp_path):
        """M5 gate-bypass: an existing but EMPTY agents/ dir (no resident
        subdirs) passes the per-file walk trivially, yet boot's load_all_agents
        returns {} and casa_core.main FATALs on the missing primary assistant.
        The gate must refuse it."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        (repo / "agents").mkdir(parents=True)   # exists, but empty
        self._policies(repo)

        errors = validate_config_repo(str(repo))
        assert any("primary assistant" in e for e in errors), errors

    def test_no_primary_assistant_only_disabled_specialist_refused(self, tmp_path):
        """M5 gate-bypass: the only 'assistant-ish' entry is a DISABLED
        specialist (agents/specialists/assistant/, enabled: false). It is
        dropped before the role registry, so boot has no resident 'assistant'
        and FATALs. The gate must refuse it."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        # No residents at all; only a disabled specialist carrying role assistant.
        _seed_specialist(repo / "agents" / "specialists", "assistant")
        self._policies(repo)

        errors = validate_config_repo(str(repo))
        assert any("primary assistant" in e for e in errors), errors

    def test_resident_unknown_model_reported_not_crashed(self, tmp_path):
        """M5 defensive contract: a resident whose model resolution is
        internally inconsistent is 100% schema-valid at both the
        runtime.yaml AND role.yaml layers, but boot's
        ``role_slot.resolve_role_model`` (inside ``materialize_role``, called
        from ``_build_runtime_fields``) RAISES ``RoleValidationError`` (a
        ``ValueError`` subclass) inside ``load_agent_from_dir`` — a
        NON-``LoadError``. The per-resident replay must REPORT it as a
        validation error, never let it escape and crash the gate itself (the
        mandate: 'a validation error must be reported, never itself crash the
        gate').

        Personality Phase A, Task 6: runtime.yaml's ``model`` is no longer a
        free string (a bare 'bogusmodel' now fails runtime.v1.json SCHEMA
        validation instead — a ``LoadError``, not the ``ValueError`` this
        test's contract targets). The equivalent defensive scenario under the
        new structured shape is a role.yaml whose ha_option ``allowed`` list
        excludes the model that actually resolves (env unset -> role_slot's
        own default 'opus' for primary_agent_model) — schema-valid on its
        own terms (every enum value is individually legal), but
        ``resolve_role_model`` rejects it at the resolution step."""
        from agent_loader import validate_config_repo, load_all_agents
        from policies import load_policies

        repo = tmp_path / "cfg"
        d = _seed_resident(repo / "agents", "assistant")
        self._policies(repo)
        broken_model_yaml = (
            "{source: ha_option, option: primary_agent_model, "
            "default: opus, allowed: [sonnet, haiku]}"  # "opus" excluded
        )
        _w(d / "runtime.yaml", f"""\
            schema_version: 1
            kind: resident
            model: {broken_model_yaml}
            tools:
              allowed: [Read, Write]
            channels: [telegram]
        """)
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "resident", "assistant")
        _w(roles_dir / "resident" / "assistant" / "role.yaml", f"""\
            api_version: casa.role/v1
            id: resident:assistant
            kind: resident
            slot: assistant
            mission: Test fixture role with an inconsistent model policy.
            enabled: true
            model: {broken_model_yaml}
            tools:
              allowed: []
              disallowed: []
              permission_mode: acceptEdits
              max_turns: 10
              skills: all
              voice_guard: none
            mcp_servers: []
            channels: [telegram]
            memory: {{token_budget: 0, read_strategy: per_turn}}
            session: {{strategy: ephemeral, idle_timeout_seconds: 0}}
            disclosure: {{policy: standard, overrides: {{}}}}
            delegates: []
            executors: []
            triggers: []
            hooks: {{pre_tool_use: []}}
            tts: {{tag_dialect: none, error_phrases: {{}}}}
            response:
              text: {{register: plain}}
              voice: {{register: plain}}
              restricted_webhook: {{register: plain}}
            persona: {{policy: required, compatibility: ["casa/test@>=0.1.0 <1.0.0"]}}
            requires: {{plugins: [], tools: []}}
            doctrine_file: doctrine.md
        """)

        # Prove boot fatals with a NON-LoadError exactly as casa_core.main
        # would: load_all_agents raises RoleValidationError (a ValueError),
        # not LoadError.
        policy_lib = load_policies(str(repo / "policies" / "disclosure.yaml"))
        with pytest.raises(ValueError):
            load_all_agents(
                str(repo / "agents"), policies=policy_lib, roles_dir=str(roles_dir),
            )

        # The gate must RETURN an error, not raise.
        errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
        assert any(
            "not in the role's allowed list" in e for e in errors
        ), errors

    def test_resident_unknown_policy_reported_not_crashed(self, tmp_path):
        """M5 defensive contract: a resident disclosure.yaml naming a policy
        absent from policies/disclosure.yaml is 100% schema-valid (``policy``
        is a free string), but boot's _compose_prompt -> policies.resolve
        RAISES PolicyError inside load_agent_from_dir — a NON-LoadError. The
        per-resident replay must REPORT it, never crash the gate."""
        from agent_loader import validate_config_repo, load_all_agents
        from policies import PolicyError, load_policies

        repo = tmp_path / "cfg"
        d = _seed_resident(repo / "agents", "assistant")
        self._policies(repo)  # defines only policy 'standard'
        _w(d / "disclosure.yaml", """\
            schema_version: 1
            policy: nonexistent
        """)

        # Prove boot fatals with a NON-LoadError: load_all_agents (which
        # composes the prompt) raises PolicyError, not LoadError.
        policy_lib = load_policies(str(repo / "policies" / "disclosure.yaml"))
        with pytest.raises(PolicyError):
            load_all_agents(str(repo / "agents"), policies=policy_lib)

        errors = validate_config_repo(str(repo))
        assert any(
            "nonexistent" in e or "unknown policy" in e for e in errors
        ), errors


class TestValidateConfigRepoWalkScope:
    """#213: the per-file schema walk must not descend into pipeline
    territory. agents/specialists/** (both the slug dir and the dot-named
    ``.{slug}.material-*`` content dirs specialist_materialize writes) is
    validated ONLY by the digest-aware load_all_specialists replay, whose
    per-specialist failures are boot-non-fatal and deliberately not gate
    errors. Live repro: config-sync-report.json post_sync_errors flagged
    agents/specialists/{mtg,finance}/runtime.yaml \"'kind' is a required
    property\" on a bootable prod tree."""

    def test_specialist_dir_missing_kind_not_flagged(self, tmp_path):
        """A REAL agents/specialists/<slug>/ dir whose runtime.yaml lacks
        ``kind`` (the pre-symlink legacy layout of the live repro) must not
        produce a per-file schema error."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        _w(repo / "agents" / "specialists" / "demo" / "runtime.yaml", """\
            schema_version: 1
            model: {source: fixed, value: sonnet}
        """)
        _seed_remaining_residents(repo)

        errors = validate_config_repo(str(repo))
        assert errors == [], errors

    def test_materialized_specialist_layout_not_flagged(self, tmp_path):
        """The current materialized layout: a dot-named content dir plus the
        agents/specialists/<slug> symlink pointing at it
        (specialist_materialize). Neither side may trip the walk."""
        import os
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        spec_root = repo / "agents" / "specialists"
        content = f".demo.material-{'0' * 32}"
        _w(spec_root / content / "runtime.yaml", """\
            schema_version: 1
            model: {source: fixed, value: sonnet}
        """)
        os.symlink(content, spec_root / "demo")
        _seed_remaining_residents(repo)

        errors = validate_config_repo(str(repo))
        assert errors == [], errors

    def test_resident_missing_kind_still_flagged(self, tmp_path):
        """Regression guard on the prune: a RESIDENT runtime.yaml lacking
        ``kind`` is configurator territory and must still be refused."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        d = _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        _w(d / "runtime.yaml", """\
            schema_version: 1
            model: {source: ha_option, option: primary_agent_model,
                    default: opus, allowed: [opus, sonnet, haiku]}
        """)

        errors = validate_config_repo(str(repo))
        assert any(
            "runtime.yaml" in e and "kind" in e for e in errors
        ), errors

    def test_executor_hooks_yaml_still_schema_gated(self, tmp_path):
        """#213 executor-coverage decision, ownership moved by #312: the
        gate's POINTER PASS (not the basename walk, which now skips executor
        hooks.yaml) schema-gates executor hooks documents — including a
        default-named hooks.yaml in a dir with no definition.yaml. Dropping
        the pass would silently un-gate them."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        _w(repo / "agents" / "executors" / "task" / "hooks.yaml", """\
            schema_version: 1
            BOGUS_HOOK_KEY: true
        """)
        _seed_remaining_residents(repo)

        errors = validate_config_repo(str(repo))
        assert len(errors) == 1, errors
        assert "hooks.yaml" in errors[0]
        assert "BOGUS_HOOK_KEY" in errors[0]


class TestValidateConfigRepoExecutorGrantGuard:
    """Task 12 — containment stage 2 config-validate guard (belt-and-
    suspenders). Stage 2's per-uid filesystem isolation does not close the
    confused-deputy bridge-read hole: an executor holding the whole-server
    ``mcp__casa-framework`` grant or one of the ``*_engagement_workspace``
    admin tools could ask casa-core (root) to read/act on ANY engagement's
    workspace via a caller-controlled ``engagement_id``. No shipped
    executor grants these today; this guard fails the build if config
    drift ever adds one, so the gate catches it before it ships."""

    def _executor_defn(self, allowed: list[str]) -> str:
        return textwrap.dedent(f"""\
            schema_version: 1
            type: task
            description: A reasonably long description meeting minLength 20.
            model: sonnet
            driver: in_casa
            enabled: true
            tools:
              allowed: {allowed}
              permission_mode: acceptEdits
            mcp_server_names: [casa-framework]
        """)

    def test_rejects_server_level_casa_grant_on_executor(self, tmp_path):
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        _w(
            repo / "agents" / "executors" / "task" / "definition.yaml",
            self._executor_defn(["Read", "mcp__casa-framework"]),
        )
        _w(repo / "agents" / "executors" / "task" / "prompt.md", "Hi.")

        errors = validate_config_repo(str(repo))
        assert any(
            "task" in e and "mcp__casa-framework" in e for e in errors
        ), errors

    def test_rejects_peek_engagement_workspace_grant(self, tmp_path):
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        _w(
            repo / "agents" / "executors" / "task" / "definition.yaml",
            self._executor_defn(
                ["Read", "mcp__casa-framework__peek_engagement_workspace"]
            ),
        )
        _w(repo / "agents" / "executors" / "task" / "prompt.md", "Hi.")

        errors = validate_config_repo(str(repo))
        assert any(
            "peek_engagement_workspace" in e for e in errors
        ), errors

    @pytest.mark.parametrize(
        "tool",
        [
            "mcp__casa-framework__list_engagement_workspaces",
            "mcp__casa-framework__delete_engagement_workspace",
        ],
    )
    def test_rejects_list_and_delete_engagement_workspace_grant(
        self, tmp_path, tool,
    ):
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        _w(
            repo / "agents" / "executors" / "task" / "definition.yaml",
            self._executor_defn(["Read", tool]),
        )
        _w(repo / "agents" / "executors" / "task" / "prompt.md", "Hi.")

        errors = validate_config_repo(str(repo))
        assert any(tool in e for e in errors), errors

    def test_normal_executor_grant_passes(self, tmp_path):
        """A real, per-tool grant list — no bare server-level grant, no
        engagement-workspace admin tools — must not trip the guard."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        _w(
            repo / "agents" / "executors" / "task" / "definition.yaml",
            self._executor_defn([
                "Read",
                "mcp__casa-framework__query_engager",
                "mcp__casa-framework__emit_completion",
            ]),
        )
        _w(repo / "agents" / "executors" / "task" / "prompt.md", "Hi.")
        _seed_remaining_residents(repo)

        errors = validate_config_repo(str(repo))
        assert errors == [], errors

    def test_shipped_configurator_grants_pass(self, tmp_path):
        """The real, shipped configurator definition.yaml grants neither
        the bare server-level tool nor any *_engagement_workspace admin
        tool — copy it verbatim into a synthetic repo and confirm the
        guard does not fire (the configurator is NOT exempted; it simply
        does not hold these grants)."""
        from agent_loader import validate_config_repo

        real_defn = (
            Path(__file__).resolve().parents[1]
            / "casa/rootfs/opt/casa/defaults/agents/executors/configurator"
            / "definition.yaml"
        ).read_text(encoding="utf-8")

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        _w(
            repo / "agents" / "executors" / "configurator" / "definition.yaml",
            real_defn,
        )
        _w(
            repo / "agents" / "executors" / "configurator" / "prompt.md",
            "Hi.",
        )

        errors = validate_config_repo(str(repo))
        assert not any(
            "mcp__casa-framework" in e and "grants" in e for e in errors
        ), errors


class TestSpecialistBindingActivation:
    """Task N1b, Steps 19-21: agent_loader's specialist counterpart to the
    Task 8 resident binding-activation block."""

    def test_specialist_with_an_active_installed_binding_gets_a_compiled_bundle(
        self, tmp_path,
    ) -> None:
        from test_specialist_install import _write_component
        from specialist_component import load_specialist_component
        from specialist_install import (
            InspectionResult, activate_binding_for_config, commit_specialist_install,
            resolve_dependency_closure,
        )
        from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity
        from role_slot import materialize_role
        from role_artifact import load_role_artifact

        staged = _write_component(tmp_path / "staged-component", slug="mtg")
        component = load_specialist_component(staged, staged / "manifest.json")
        deps = resolve_dependency_closure(component, staged)
        from specialist_install import compute_install_root_digest
        root_digest = compute_install_root_digest(
            component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
        inspection = InspectionResult(
            component_id=component.component_id, version=component.version, slug=component.slug,
            component_checksum=component.checksum, root_digest=root_digest,
            mission=str(component.role.role["mission"]),
            default_persona_ref=component.default_persona_ref,
            default_persona_checksum=component.default_persona_checksum,
            required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=staged,
        )
        acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
        identity = install_consent_identity(
            component_id=inspection.component_id, version=inspection.version,
            root_digest=inspection.root_digest, slug=inspection.slug)
        acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                    component_checksum=inspection.root_digest, slug=inspection.slug)

        specialists_dir = tmp_path / "specialists"
        agents_specialists_dir = tmp_path / "agents-specialists"
        commit_specialist_install(
            inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
            specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
        )

        role = materialize_role(source=load_role_artifact(staged / "role"), options={})

        class _Cfg:
            pass

        cfg = _Cfg()
        cfg.role_slot = role
        cfg.persona_pack = None
        cfg.binding = None
        cfg.compiled_prompt_bundle = None
        cfg.binding_digest = None
        cfg.speaker_provenance = None

        activate_binding_for_config(cfg, specialists_root=specialists_dir)

        assert cfg.compiled_prompt_bundle is not None
        assert cfg.speaker_provenance is not None
        assert cfg.speaker_provenance.speaker_kind == "specialist"
        assert cfg.binding is not None and cfg.binding.mode == "component-default"

    def test_specialist_override_binding_loads_persona_from_env_config_root(
        self, tmp_path, monkeypatch,
    ) -> None:
        """#323 (Sol r3-3): a persona override applied to a specialist from a
        pack under $CASA_CONFIG_DIR/personas must ACTIVATE on the next load —
        pre-fix activate_binding_for_config reloaded overrides exclusively
        from /config/personas, so the tool reported success for a binding the
        loader could never compile."""
        from test_specialist_install import _write_component
        from test_persona_install import _write_persona_repo
        from specialist_component import load_specialist_component
        from specialist_install import (
            InspectionResult, activate_binding_for_config, commit_specialist_install,
            compute_install_root_digest, resolve_dependency_closure,
        )
        from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity
        from role_slot import materialize_role
        from role_artifact import load_role_artifact
        from persona_pack import load_persona_pack
        from persona_install import apply_persona_override

        monkeypatch.setenv("CASA_CONFIG_DIR", str(tmp_path / "cfgroot"))

        staged = _write_component(tmp_path / "staged-component", slug="mtg")
        component = load_specialist_component(staged, staged / "manifest.json")
        deps = resolve_dependency_closure(component, staged)
        root_digest = compute_install_root_digest(
            component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
        inspection = InspectionResult(
            component_id=component.component_id, version=component.version, slug=component.slug,
            component_checksum=component.checksum, root_digest=root_digest,
            mission=str(component.role.role["mission"]),
            default_persona_ref=component.default_persona_ref,
            default_persona_checksum=component.default_persona_checksum,
            required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=staged,
        )
        acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
        identity = install_consent_identity(
            component_id=inspection.component_id, version=inspection.version,
            root_digest=inspection.root_digest, slug=inspection.slug)
        acks.record(identity=identity, component_id=inspection.component_id,
                    version=inspection.version,
                    component_checksum=inspection.root_digest, slug=inspection.slug)

        specialists_dir = tmp_path / "specialists"
        commit_specialist_install(
            inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
            specialists_dir=specialists_dir,
            agents_specialists_dir=tmp_path / "agents-specialists",
        )

        # Install the override pack ONLY under the custom config root —
        # casa/judge satisfies the component role's persona_requirements.
        override_dir = tmp_path / "cfgroot" / "personas" / "casa/judge" / "0.1.0"
        override_dir.parent.mkdir(parents=True, exist_ok=True)
        _write_persona_repo(override_dir, persona_id="casa/judge")
        persona = load_persona_pack(
            override_dir / "pack", override_dir / "manifest.json")

        role = materialize_role(source=load_role_artifact(staged / "role"), options={})
        apply_persona_override(
            target_role_id="specialist:mtg", persona=persona, role=role,
            instance_dir_root=specialists_dir / "mtg")

        # Sol r4-1: the PRODUCTION pre-load path must survive too — every
        # reload/boot resolves the roles overlay first, and its operational-
        # file self-heal reloads the override persona; pre-fix that branch
        # alone read a fixed /config/personas and could drop the
        # specialist's operational files.
        import specialist_materialize
        from specialist_registry import InstalledSpecialistIndex

        index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
        index.load()
        agents_specialists_dir = tmp_path / "agents-specialists"
        roles_dir = specialist_materialize.current_specialist_roles_dir(
            installed_index=index, specialists_dir=specialists_dir,
            agents_specialists_dir=agents_specialists_dir,
        )
        assert roles_dir
        op_dir = agents_specialists_dir / "mtg"
        for name in ("character.yaml", "voice.yaml", "response_shape.yaml",
                     "runtime.yaml"):
            assert (op_dir / name).is_file(), name

        class _Cfg:
            pass

        cfg = _Cfg()
        cfg.role_slot = role
        cfg.persona_pack = None
        cfg.binding = None
        cfg.compiled_prompt_bundle = None
        cfg.binding_digest = None
        cfg.speaker_provenance = None
        activate_binding_for_config(cfg, specialists_root=specialists_dir)

        assert cfg.binding is not None and cfg.binding.mode == "override"
        assert cfg.persona_pack.persona_id == "casa/judge"
        assert cfg.compiled_prompt_bundle is not None

    def test_specialist_with_no_active_binding_leaves_cfg_bundle_none(self, tmp_path) -> None:
        from specialist_install import activate_binding_for_config
        from role_slot import RoleSlot, ResolvedModel

        class _Cfg:
            pass

        cfg = _Cfg()
        cfg.role_slot = RoleSlot(
            role_id="specialist:finance", kind="specialist", slot="finance", mission="x",
            resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                          sdk_model="claude-sonnet-4-6", option=None),
            normalized={}, doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
        )
        cfg.compiled_prompt_bundle = None
        activate_binding_for_config(cfg, specialists_root=tmp_path / "specialists")  # no InstanceDir written
        assert cfg.compiled_prompt_bundle is None  # legacy fallback path stays intact


# ---------------------------------------------------------------------------
# #338 — validate_config_repo boot-parity: trigger registration, specialist
# isolation, and no binding side effects from a validation-only pass.
# ---------------------------------------------------------------------------


class TestValidateConfigRepoIssue338:
    """The gate must replay what boot actually runs — and ONLY validate."""

    def _repo(self, tmp_path):
        """A bootable single-resident repo on a synthetic roles_dir."""
        repo = tmp_path / "addon_configs" / "casa"
        resident_dir = _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "resident", "assistant")
        _w(resident_dir / "runtime.yaml", """\
            schema_version: 1
            kind: resident
            model: {source: fixed, value: sonnet}
            tools:
              allowed: [Read, Write]
            channels: [telegram]
        """)
        _seed_remaining_residents(repo, roles_dir)
        return repo, resident_dir, roles_dir

    def test_duplicate_trigger_names_are_refused_by_the_gate(self, tmp_path):
        """Boot registers resident triggers UNCAUGHT (casa_core step 13b);
        TriggerRegistry.register_agent raises TriggerError on a duplicate
        name, and the triggers schema has no uniqueItems — so this commit
        used to pass the gate and crash-loop the add-on."""
        from agent_loader import validate_config_repo

        repo, resident_dir, roles_dir = self._repo(tmp_path)
        _w(resident_dir / "triggers.yaml", """\
            schema_version: 1
            triggers:
              - {name: dup, type: interval, minutes: 5, channel: telegram, prompt: tick}
              - {name: dup, type: interval, minutes: 10, channel: telegram, prompt: tock}
        """)
        errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
        assert any("duplicate trigger name" in e for e in errors), errors

    def test_scheduled_trigger_on_an_unregistered_channel_is_refused(self, tmp_path):
        from agent_loader import validate_config_repo

        repo, resident_dir, roles_dir = self._repo(tmp_path)
        _w(resident_dir / "triggers.yaml", """\
            schema_version: 1
            triggers:
              - {name: tick, type: interval, minutes: 5, channel: voice, prompt: hi}
        """)
        errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
        assert any("not registered on this agent" in e for e in errors), errors

    def test_cron_field_out_of_range_is_refused(self, tmp_path):
        """A 5-field cron string with an out-of-range value passes the
        schema (any non-empty string) and the field-count check, but
        APScheduler's add_job raises at boot registration."""
        from agent_loader import validate_config_repo

        repo, resident_dir, roles_dir = self._repo(tmp_path)
        _w(resident_dir / "triggers.yaml", """\
            schema_version: 1
            triggers:
              - {name: nightly, type: cron, schedule: "99 3 * * *", channel: telegram, prompt: go}
        """)
        errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
        # APScheduler's message names the offending expression, not the
        # trigger; the replay wrapper adds the agent.
        assert any("trigger registration failed" in e and "99" in e
                   for e in errors), errors

    def test_a_clean_repo_with_valid_triggers_still_passes(self, tmp_path):
        """The replay must not false-flag a registrable trigger set."""
        from agent_loader import validate_config_repo

        repo, resident_dir, roles_dir = self._repo(tmp_path)
        _w(resident_dir / "triggers.yaml", """\
            schema_version: 1
            triggers:
              - {name: morning, type: cron, schedule: "0 8 * * *", channel: telegram, prompt: brief}
              - {name: tick, type: interval, minutes: 30, channel: telegram, prompt: check}
        """)
        errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
        assert errors == []

    def test_one_inconsistent_specialist_is_isolated_not_scan_fatal(
            self, tmp_path, monkeypatch):
        """#338: materialize_role raises raw RoleValidationError (a
        ValueError) for a schema-valid but inconsistent role artifact — e.g.
        an ha_option role whose allowed list excludes the operator's current
        option value. load_all_specialists caught only LoadError, so ONE such
        specialist killed the whole scan (and with it boot). It must land in
        `failed` like every other per-specialist error."""
        from agent_loader import load_all_specialists

        base = tmp_path / "agents" / "specialists"
        _seed_specialist(base, "finance")
        _seed_specialist(base, "broken")
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "specialist", "finance")
        _seed_role_artifact(roles_dir, "specialist", "broken")

        ha_model = ("model: {source: ha_option, option: voice_agent_model, "
                    "default: haiku, allowed: [haiku]}")
        role_yaml = roles_dir / "specialist" / "broken" / "role.yaml"
        role_yaml.write_text(role_yaml.read_text(encoding="utf-8").replace(
            "model: {source: fixed, value: sonnet}", ha_model), encoding="utf-8")
        _w(base / "broken" / "runtime.yaml", f"""\
            schema_version: 1
            kind: specialist
            {ha_model}
            enabled: true
            memory:
              token_budget: 0
            session:
              strategy: ephemeral
        """)
        monkeypatch.setenv("VOICE_AGENT_MODEL", "opus")  # permitted by config.yaml

        found, failed = load_all_specialists(str(base), roles_dir=str(roles_dir))
        assert "finance" in found
        assert any(name == "broken" for name, _ in failed), failed

    def test_gate_survives_an_inconsistent_specialist(self, tmp_path, monkeypatch):
        """Same repo through validate_config_repo: per-specialist failures
        are boot-non-fatal (isolated) and deliberately not gate errors — but
        before the fix the raw RoleValidationError CRASHED the gate itself."""
        from agent_loader import validate_config_repo

        repo, _resident_dir, roles_dir = self._repo(tmp_path)
        base = repo / "agents" / "specialists"
        _seed_specialist(base, "broken")
        _seed_role_artifact(roles_dir, "specialist", "broken")
        ha_model = ("model: {source: ha_option, option: voice_agent_model, "
                    "default: haiku, allowed: [haiku]}")
        role_yaml = roles_dir / "specialist" / "broken" / "role.yaml"
        role_yaml.write_text(role_yaml.read_text(encoding="utf-8").replace(
            "model: {source: fixed, value: sonnet}", ha_model), encoding="utf-8")
        _w(base / "broken" / "runtime.yaml", f"""\
            schema_version: 1
            kind: specialist
            {ha_model}
            enabled: true
            memory:
              token_budget: 0
            session:
              strategy: ephemeral
        """)
        monkeypatch.setenv("VOICE_AGENT_MODEL", "opus")

        errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
        assert errors == []

    def test_validation_never_activates_a_staged_persona_swap(self, tmp_path):
        """#338 (medium): validate_config_repo replays the resident loader,
        whose binding reconciliation used to COMMIT a staged desired ->
        active as a side effect — a validation-only config_git_commit could
        flip the live persona binding before the required restart. After the
        fix the staged swap must survive validation untouched."""
        import os as _os
        from pathlib import Path as _P

        from agent_loader import load_agent_from_dir, validate_config_repo
        from personality_binding import (
            InstanceDir, InstanceTuple, materialize_override_binding,
        )
        from persona_pack import load_persona_pack
        from policies import load_policies

        repo, resident_dir, roles_dir = self._repo(tmp_path)
        policies = load_policies(str(repo / "policies" / "disclosure.yaml"))
        # Boot once: establishes the image-default active tuple under the
        # test-scoped CASA_BINDINGS_DIR (conftest).
        cfg = load_agent_from_dir(
            str(resident_dir), policies=policies, roles_dir=str(roles_dir),
        )

        tina_dir = _P("casa/rootfs/opt/casa/defaults/personas/casa/tina/0.1.0")
        tina = load_persona_pack(tina_dir / "pack", tina_dir / "manifest.json")
        binding = materialize_override_binding(
            role=cfg.role_slot, persona=tina, override_source="operator:casa/tina@0.1.0",
        )
        instance_dir = InstanceDir(
            _P(_os.environ["CASA_BINDINGS_DIR"]) / "resident-assistant"
        )
        staged = InstanceTuple(
            root="operator:casa/tina@0.1.0", binding=binding,
            config_snapshot={}, config_digest=binding.effective_config_digest,
        )
        instance_dir.stage_desired(staged)

        errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
        assert errors == []
        assert instance_dir.desired() == staged          # swap NOT consumed
        assert instance_dir.active().binding.mode == "image-default"
