"""Shared component-construction fixture for specialist-component tests.

Lifted from ``tests/test_specialist_install.py``'s local ``_write_component``
helper (Task 1 of the specialist-bundled-plugins plan) so new test modules
don't need to duplicate the role/persona/manifest scaffolding just to
exercise ``load_specialist_component``. The original helper in
``test_specialist_install.py`` is left in place untouched — DRY convergence
between the two is Task 12 cleanup, not this task.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from canonical_bytes import canonical_json_bytes, canonical_text, checksum_bytes
from specialist_component import compute_component_checksum


def write_minimal_component(tmp_path: Path, *, slug: str = "mtg-test",
                             extra_dependencies: list[dict] | None = None,
                             tools_allowed: list[str] | None = None) -> tuple[Path, Path]:
    """Build a minimal, checksum-valid specialist component tree under
    ``tmp_path / "component"`` and return ``(component_dir, manifest_path)``.

    ``extra_dependencies`` rows are appended to the manifest's
    ``dependencies`` list, after the mandatory persona dependency row.
    """
    root = tmp_path / "component"
    (root / "role").mkdir(parents=True)
    (root / "persona" / "pack").mkdir(parents=True)
    role_yaml = {
        "api_version": "casa.role/v1", "id": f"specialist:{slug}", "kind": "specialist",
        "slot": slug, "mission": "Answer test questions.", "enabled": True,
        "model": {"source": "fixed", "value": "sonnet"},
        "tools": {"allowed": list(tools_allowed or []), "disallowed": ["Bash"],
                   "permission_mode": "dontAsk",
                   "max_turns": 8, "skills": "none", "voice_guard": "none"},
        "mcp_servers": [], "channels": [], "memory": {"token_budget": 0, "read_strategy": "per_turn"},
        "session": {"strategy": "ephemeral", "idle_timeout_seconds": 0},
        "disclosure": {"policy": "delegated", "overrides": {}},
        "delegates": [], "executors": [], "triggers": [], "hooks": {"pre_tool_use": []},
        "tts": {"tag_dialect": "none", "error_phrases": {}},
        "response": {"text": {"register": "precise"}, "voice": {"register": "spoken"},
                      "restricted_webhook": {"register": "plain"}},
        "persona": {"policy": "required", "compatibility": ["casa/judge@>=0.1.0 <1.0.0"]},
        "requires": {"plugins": [], "tools": []}, "doctrine_file": "doctrine.md",
    }
    (root / "role" / "role.yaml").write_text(yaml.safe_dump(role_yaml, sort_keys=False), encoding="utf-8")
    (root / "role" / "doctrine.md").write_text("# Core doctrine\n\nAnswer test questions.\n", encoding="utf-8")
    config_schema = {"required": [], "secret_names": []}
    (root / "config-schema.json").write_text(json.dumps(config_schema), encoding="utf-8")

    persona_yaml = {
        "api_version": "casa.persona/v1", "id": "casa/judge", "version": "0.1.0",
        "trait_schema_version": 1,
        "identity": {"display_name": "Judge", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        "relationship_posture": "established", "archetype": "adjudicator",
        "traits": {"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                    "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        "quirks": [],
    }
    (root / "persona" / "pack" / "persona.yaml").write_text(
        yaml.safe_dump(persona_yaml, sort_keys=False), encoding="utf-8")
    core = "X" * 350
    (root / "persona" / "pack" / "persona.md").write_text(
        f"# Core\n\n{core}\n\n## Negative space\n\nNever guesses.\n", encoding="utf-8")
    manifest_rows = []
    # persona_pack._admit_files sorts admitted files by NAME
    # ("persona.md" < "persona.yaml" alphabetically) — the manifest row
    # order must match that sort, not source-declaration order, or
    # load_persona_pack's own recomputed manifest payload (and hence its
    # checksum) will never equal what's written to disk here.
    for name in sorted(os.listdir(root / "persona" / "pack")):
        text = canonical_text((root / "persona" / "pack" / name).read_text(encoding="utf-8"))
        manifest_rows.append({"path": name, "type": "file", "executable": False,
                               "checksum": checksum_bytes(text.encode("utf-8"))})
    persona_manifest_payload = {"api_version": "casa.persona.manifest/v1", "files": manifest_rows}
    persona_checksum = checksum_bytes(canonical_json_bytes(persona_manifest_payload))
    persona_manifest_payload["checksum"] = persona_checksum
    (root / "persona" / "manifest.json").write_text(json.dumps(persona_manifest_payload), encoding="utf-8")

    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    component_checksum = compute_component_checksum(files)
    dependencies = [
        {"kind": "persona", "identifier": "casa/judge@0.1.0", "digest": persona_checksum},
    ]
    dependencies.extend(extra_dependencies or [])
    manifest = {
        "api_version": "casa.specialist-component/v1", "component_id": f"casa-test/{slug}",
        "version": "0.1.0",
        "default_persona": {"ref": "casa/judge@0.1.0", "checksum": persona_checksum},
        "dependencies": dependencies,
        "checksum": component_checksum,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return root, manifest_path


def write_bundled_plugin(
    component_dir: Path, name: str = "mtg", *,
    triggers: object = None, sysreqs: list[dict] | None = None,
    env_names: list[str] | None = None,
    protected_tools: list | None = None,
    mcp_command_servers: dict[str, dict] | None = None,
) -> str:
    """Task 8: write a minimal, real-shaped plugin tree at
    ``component_dir / "plugins" / name`` (mirroring `plugin_store`'s own
    parsing functions — ``manifest_sysreqs`` reads
    ``casa.systemRequirements``, ``manifest_triggers``/`validate_manifest`
    reads ``casa.triggers``, `plugin_env_extractor.extract_env_vars` reads
    `${VAR}` references in an `.mcp.json` server's ``env`` block) and return
    the digest a manifest dependency row's ``digest`` field must pin
    (``"sha256:" + plugin_store.content_checksum(plugin_dir)``) for the tree
    to resolve ``available=True`` against `resolve_dependency_closure`.

    ``env_names`` (when given) are declared on a ``url``-form MCP server —
    deliberately NOT a ``command`` server — so `plugin_store.
    mcp_command_verdicts` (which only checks `command` servers) never flags
    a "missing" verdict for an executable/PATH entry that may not exist in
    the test sandbox; this fixture is about env-name extraction, not
    command resolvability.

    ``protected_tools`` (fix-round-1, spec §3.2) is written verbatim to
    ``casa.protectedTools`` — pass the legacy string form or the
    ``{"name": ..., "summary": ...}`` object form, per
    ``plugin_store.manifest_protected_tools``.

    ``mcp_command_servers`` (fix-round-1) are merged into ``mcpServers``
    ALONGSIDE any ``env_names``-derived url server — each value is a raw
    server config dict (e.g. ``{"command": "python3", "args": [...]}}``);
    callers are responsible for using a command that resolves cleanly
    against `plugin_store.mcp_command_verdicts` in the test sandbox (e.g.
    ``python3``), same convention as ``tests/plugin_fixtures.py``.
    """
    import plugin_store

    plugin_dir = component_dir / "plugins" / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    manifest: dict = {"name": name, "version": "0.1.0"}
    casa: dict = {}
    if sysreqs is not None:
        casa["systemRequirements"] = sysreqs
    if triggers is not None:
        casa["triggers"] = triggers
    if protected_tools is not None:
        casa["protectedTools"] = protected_tools
    if casa:
        manifest["casa"] = casa
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    servers: dict = {}
    if env_names:
        servers["main"] = {
            "url": "https://example.invalid/mcp",
            "env": {n: f"${{{n}}}" for n in env_names},
        }
    if mcp_command_servers:
        servers.update(mcp_command_servers)
    if servers:
        (plugin_dir / ".mcp.json").write_text(
            json.dumps({"mcpServers": servers}), encoding="utf-8")
    return "sha256:" + plugin_store.content_checksum(plugin_dir)
