"""Capability parity — every capability an agent is *granted* or *told about*
must resolve to something that actually exists (Layer 1 seam guard).

Motivation: post-campaign, the surviving bugs are seam bugs, not logic bugs.
The `recall_memory` regression (v0.59.2) was a prompt promising a tool the
config never granted; the delegates-without-delegate-tool boot crash was a
grant without its backing. These are invisible to the unit gate because no
single module owns the cross-file contract. This suite asserts that contract
statically over the whole agent-config corpus.

Three invariants:
  A. Every `tools.allowed` entry RESOLVES — a real framework tool
     (`mcp__casa-framework__X`, X ∈ CASA_TOOLS), a known built-in, or an
     `mcp__<server>` whose server is declared (or plugin-provided). Catches
     typos, stale grants for removed tools, and MCP grants without a server.
  C. Curated required-tools manifest — agents that MUST self-use a framework
     tool (verified intent) actually allow it. Catches the recall_memory class
     (a MISSING grant a prompt depends on), which invariant A cannot see.
  D. Every trigger's `prompt_file` resolves and its `channel` is set; every
     add-on option has a translation. Reference integrity for the other
     "declared thing must exist" surfaces.

NOT asserted: a bare "prompt mentions tool ⇒ must allow it" rule. Empirically
that false-positives — a specialist's prompt says "you are invoked via
`delegate_to_agent`" (it is the delegation TARGET, not a caller), and shared
doctrine lists tools as generic retry examples. Intent lives in the curated
manifest, not an English parse.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
CASA = REPO / "casa" / "rootfs" / "opt" / "casa"
AGENTS = CASA / "defaults" / "agents"
sys.path.insert(0, str(CASA))

import tools  # noqa: E402  (after sys.path insert)

FRAMEWORK_TOOLS = frozenset(
    (getattr(t, "name", None) or getattr(t, "__name__", "")) for t in tools.CASA_TOOLS
)

# SDK built-in tool tokens Casa actually grants. A bare (non-mcp) allowed entry
# outside this set is almost certainly a typo (e.g. "Bashh"). Extend
# deliberately if a new built-in is genuinely adopted.
KNOWN_BUILTINS = frozenset({
    "Read", "Write", "Edit", "Bash", "Glob", "Grep", "Skill",
    "WebFetch", "WebSearch",
})

# Plugin NAMES assigned via the registry (unified plugin architecture), NOT
# the static `mcp_server_names` list. Registry-assigned plugins load via
# --plugin-dir and grant `mcp__plugin_<name>_<server>` — validated at runtime
# by verify_plugin_state's authorization check, so the static parity check
# only sanity-checks the plugin name prefix here.
KNOWN_PLUGIN_SERVERS = frozenset({"context7"})

# Invariant C — verified self-use requirements. Key = role dir name; value =
# framework tools the agent's prompts/behaviour depend on it CALLING itself.
# Seeded from confirmed intent only (do not add unverified rows — a false
# requirement is as bad as a missing tool). The memory tools: the v0.59.2
# incident (a prompt naming a memory tool the role was not granted).
REQUIRED_FRAMEWORK_TOOLS: dict[str, set[str]] = {
    "assistant": {
        "search_memory", "browse_memory_day", "open_memory", "correct_memory",
        "delegate_to_agent",
        "send_message", "get_schedule", "send_media",
    },
    "butler": {"search_memory", "browse_memory_day"},  # the voice channel
    # Since v0.351.0 the configurator is a delegated run — it no
    # longer completes an engagement or cleans up engagement topics.
    "configurator": {"config_git_commit"},
}

CONFIGURATOR_ROLE_YAML = CASA / "defaults" / "roles" / "executor" / "configurator" / "role.yaml"


def _agent_configs() -> list[tuple[str, Path, dict]]:
    """(role, config_path, parsed) for every resident/specialist runtime.yaml
    and the configurator. Since v0.351.0 the configurator ships as
    an image-owned role artifact; its entry is the RUNTIME config Casa builds
    from it (``agent_loader.load_configurator``), keyed to that role.yaml."""
    import agent_loader

    out = []
    for rt in sorted(AGENTS.rglob("runtime.yaml")):
        out.append((rt.parent.name, rt, yaml.safe_load(rt.read_text(encoding="utf-8")) or {}))
    cfg = agent_loader.load_configurator()
    out.append(("configurator", CONFIGURATOR_ROLE_YAML, {
        "tools": {"allowed": list(cfg.tools.allowed)},
        "mcp_server_names": list(cfg.mcp_server_names),
    }))
    return out


def test_configs_discovered() -> None:
    roles = {r for r, _, _ in _agent_configs()}
    assert {"assistant", "butler", "configurator"} <= roles


def test_voice_job_broker_tools_exist_on_framework_surface() -> None:
    """Gary's eventual private grant must resolve before live reconfiguration."""
    assert {
        "delegate_to_agent", "voice_job_status",
        "cancel_voice_job", "continue_voice_job",
    } <= FRAMEWORK_TOOLS


def test_every_allowed_tool_resolves() -> None:
    """Invariant A: no dangling grants."""
    violations: list[str] = []
    for role, path, data in _agent_configs():
        allowed = (data.get("tools") or {}).get("allowed") or []
        servers = set(data.get("mcp_server_names") or []) | KNOWN_PLUGIN_SERVERS
        for entry in allowed:
            if entry.startswith("mcp__casa-framework__"):
                name = entry.removeprefix("mcp__casa-framework__")
                if name not in FRAMEWORK_TOOLS:
                    violations.append(f"{role}: '{entry}' is not a CASA_TOOLS tool")
            elif entry.startswith("mcp__plugin_"):
                # Registry-assigned plugin grant mcp__plugin_<name>_<server>.
                # Names + servers can both contain hyphens (single-underscore
                # separated), so match by known-plugin-name prefix.
                rest = entry.removeprefix("mcp__plugin_")
                if not any(rest == p or rest.startswith(p + "_")
                           for p in KNOWN_PLUGIN_SERVERS):
                    violations.append(
                        f"{role}: '{entry}' plugin not in known plugins "
                        f"{sorted(KNOWN_PLUGIN_SERVERS)}")
            elif entry.startswith("mcp__"):
                server = entry.removeprefix("mcp__").split("__", 1)[0]
                if server not in servers:
                    violations.append(
                        f"{role}: '{entry}' server '{server}' not in mcp_server_names "
                        f"{sorted(set(data.get('mcp_server_names') or []))} nor a known plugin server"
                    )
            elif entry not in KNOWN_BUILTINS:
                violations.append(f"{role}: '{entry}' is not a known built-in tool")
    assert not violations, "dangling tool grants:\n  " + "\n  ".join(violations)


def test_configurator_has_no_bash() -> None:
    """Round-2 W1 (Sol+Terra ratified, v0.101.0): the configurator's Bash was
    removed at the capability layer — argv inspection (managed_component_guard)
    can never close the script-writer/unmodeled-binary bypass class, and a
    doctrine audit found no legitimate configurator Bash dependency. Pinned in
    BOTH the runtime config (``agent_loader.load_configurator`` — feeds
    ClaudeAgentOptions.allowed_tools via tools._build_specialist_options since
    v0.351.0) and the canonical role artifact (role.yaml)."""
    by_role = {r: d for r, _, d in _agent_configs()}
    cfg_allowed = (by_role["configurator"].get("tools") or {}).get("allowed") or []
    assert "Bash" not in cfg_allowed, (
        "the configurator's runtime config must NOT allow Bash (round-2 W1)")
    role_yaml = yaml.safe_load(CONFIGURATOR_ROLE_YAML.read_text(encoding="utf-8"))
    assert "Bash" not in ((role_yaml.get("tools") or {}).get("allowed") or []), (
        "configurator role.yaml must NOT allow Bash (round-2 W1)")


def test_required_self_use_tools_present() -> None:
    """Invariant C: verified must-call tools are granted."""
    by_role = {r: d for r, _, d in _agent_configs()}
    missing: list[str] = []
    for role, required in REQUIRED_FRAMEWORK_TOOLS.items():
        data = by_role.get(role)
        assert data is not None, f"required-manifest role {role!r} has no config"
        allowed = (data.get("tools") or {}).get("allowed") or []
        fw = {a.removeprefix("mcp__casa-framework__") for a in allowed
              if a.startswith("mcp__casa-framework__")}
        for tool in sorted(required):
            if tool not in fw:
                missing.append(f"{role}: MUST allow '{tool}' (required self-use) but does not")
    assert not missing, "missing required tools:\n  " + "\n  ".join(missing)


def test_trigger_prompt_files_and_channels_resolve() -> None:
    """Invariant D (triggers): every trigger's prompt_file exists and channel is set."""
    violations: list[str] = []
    for tf in sorted(AGENTS.rglob("triggers.yaml")):
        data = yaml.safe_load(tf.read_text(encoding="utf-8")) or {}
        for trig in data.get("triggers") or []:
            name = trig.get("name", "?")
            if not trig.get("channel"):
                violations.append(f"{tf.parent.name}/{name}: empty channel")
            pf = trig.get("prompt_file")
            if pf and not (tf.parent / pf).is_file():
                violations.append(f"{tf.parent.name}/{name}: prompt_file '{pf}' missing")
            if not pf and not trig.get("prompt"):
                violations.append(f"{tf.parent.name}/{name}: neither prompt nor prompt_file")
    assert not violations, "trigger reference errors:\n  " + "\n  ".join(violations)


def test_validate_config_repo_is_env_independent(monkeypatch) -> None:
    """D3 (2026-07-10): validating the shipped defaults tree must NOT require the
    model env vars — a ${VAR} model placeholder is deferred, not an error. Pre-fix
    this returned two 'Unknown model shortname ${…}' errors when the env was
    unset (the D1 fragility for non-boot callers)."""
    monkeypatch.delenv("PRIMARY_AGENT_MODEL", raising=False)
    monkeypatch.delenv("VOICE_AGENT_MODEL", raising=False)
    from agent_loader import validate_config_repo
    errs = validate_config_repo(str(CASA / "defaults"))
    model_errs = [e for e in errs if "Unknown model shortname" in e]
    assert not model_errs, f"env-unset validation still trips on model placeholders: {model_errs}"


def test_every_option_has_a_translation() -> None:
    """Invariant D (options): each add-on schema key has an en.yaml translation,
    so the HA UI never renders a raw key."""
    cfg = yaml.safe_load((REPO / "casa" / "config.yaml").read_text(encoding="utf-8"))
    trans = yaml.safe_load((REPO / "casa" / "translations" / "en.yaml").read_text(encoding="utf-8"))
    schema_keys = set((cfg.get("schema") or {}).keys())
    translated = set((trans.get("configuration") or {}).keys())
    missing = schema_keys - translated
    assert not missing, f"schema options with no en.yaml translation: {sorted(missing)}"


def test_configurator_definition_and_role_allowlists_are_identical() -> None:
    """#210 true root cause (round-3, both reviewers): Plan 2 added the
    specialist/persona pipeline tools to role.yaml only, but the RUNTIME
    allowlist flowed from a separate definition.yaml — so the live
    configurator had no typed install path and hand-authored instead. Since
    v0.351.0 the runtime config is built from role.yaml itself
    (``agent_loader.load_configurator``); the two must never drift again."""
    import agent_loader
    runtime_allowed = set(agent_loader.load_configurator().tools.allowed)
    role = yaml.safe_load(CONFIGURATOR_ROLE_YAML.read_text(encoding="utf-8"))
    role_allowed = set((role.get("tools") or {}).get("allowed") or [])
    assert runtime_allowed == role_allowed, (
        "configurator tool allowlists drifted:\n"
        f"  only in the runtime config: {sorted(runtime_allowed - role_allowed)}\n"
        f"  only in role.yaml:          {sorted(role_allowed - runtime_allowed)}")


def test_assistant_denies_bash():
    """Stage-1 containment: the assistant is a router; broad Bash is unused
    authority and must be fenced via the CLI-enforced disallowed knob, not
    merely absent from the auto-approve allowlist. Same applies to Agent/Task:
    they bypass allowed_tools + fail-closed callback and spawn children with
    broad default toolsets, so they must be disallowed in both config sources."""
    role = yaml.safe_load(
        (CASA / "defaults" / "roles" / "resident" / "assistant"
         / "role.yaml").read_text())
    runtime = yaml.safe_load(
        (AGENTS / "assistant" / "runtime.yaml").read_text())
    for cfg, src in ((role, "role.yaml"), (runtime, "runtime.yaml")):
        allowed = cfg["tools"].get("allowed") or []
        disallowed = cfg["tools"].get("disallowed") or []
        # Bash
        assert "Bash" not in allowed, f"{src}: Bash still auto-approved"
        assert "Bash" in disallowed, f"{src}: Bash not CLI-denied"
        # Agent/Task sub-agent spawn bypass
        assert "Agent" not in allowed, f"{src}: Agent still auto-approved"
        assert "Agent" in disallowed, f"{src}: Agent not CLI-denied"
        assert "Task" not in allowed, f"{src}: Task still auto-approved"
        assert "Task" in disallowed, f"{src}: Task not CLI-denied"


# ---------------------------------------------------------------------------
# #633 — the wipe door is granted to NOBODY, and the corpus says so
#
# `casa/DOCS.md` and `docs/architecture/memory-wipe.md` publish the claim
# "no shipped agent is granted `wipe_memory`". Nothing bound it in either
# direction before this test, which is how the opposite claim survived four
# releases. Asserted on the EXPOSURE outcome (`select_casa_tools`), never on a
# string grep of a YAML file: exposure is what decides whether the tool reaches
# an agent's MCP server, and the wildcard grant `mcp__casa-framework` would
# expose it without the name ever appearing.
#
# This is a GREEN mutation pin, not a red case: the fact already holds at the
# base. Its job is to keep holding.
# ---------------------------------------------------------------------------

# Since v0.351.0 no executor definition.yaml ships any more — the
# configurator's grants are its role.yaml's.
GRANT_CARRIERS = ("runtime.yaml", "role.yaml")


def _grant_carrier_paths() -> dict[str, list[Path]]:
    defaults = CASA / "defaults"
    return {
        "runtime": sorted((defaults / "agents").glob("*/runtime.yaml")),
        "role": sorted((defaults / "roles").glob("*/*/role.yaml")),
    }


def test_no_shipped_agent_artifact_selects_wipe_memory():
    by_kind = _grant_carrier_paths()
    assert len(by_kind["runtime"]) == 3
    assert len(by_kind["role"]) == 4

    carriers: list[tuple[Path, frozenset[str]]] = []
    for paths in by_kind.values():
        for path in paths:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            allowed = ((doc.get("tools") or {}).get("allowed")) or []
            if isinstance(allowed, str):
                allowed = allowed.split()
            carriers.append((path, frozenset(str(x) for x in allowed)))
    assert len(carriers) == 7

    selected_wipes = [
        (path, tool.name)
        for path, allowed in carriers
        for tool in tools.select_casa_tools(allowed)
        if tool.name == "wipe_memory"
    ]
    assert len(selected_wipes) == 0, selected_wipes

    # The predicate itself is exercised, so a selector that stopped selecting
    # anything cannot make the assertion above pass for the wrong reason.
    assert sum(len(tools.select_casa_tools(allowed)) > 0
               for _path, allowed in carriers) == 7
    assert len(tools.select_casa_tools(
        frozenset({"mcp__casa-framework__wipe_memory"}))) == 1
