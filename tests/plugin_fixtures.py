"""Shared fixtures for unified-plugin-architecture tests: build store
artifacts (with identity-matching metadata) + registry files + entries.

Metadata identity MUST match the registry entry's (name/repo/revision/
subdir/artifact_id) or plugin_store.artifact_verdict → artifact_invalid.
"""
from __future__ import annotations

import json
from pathlib import Path

from plugin_registry import compute_artifact_id
from plugin_store import content_checksum, write_metadata


def mk_artifact(store: Path, name: str, artifact_id: str,
                version: str = "1.0.0", manifest_name: str | None = None,
                revision: str = "git:" + "a" * 40, subdir: str = "",
                mcp_servers: dict | None = None,
                extra_manifest: dict | None = None,
                extra_files: dict | None = None) -> Path:
    root = Path(store) / name / artifact_id
    (root / ".claude-plugin").mkdir(parents=True)
    manifest = {"name": manifest_name or name, "version": version}
    if extra_manifest:
        manifest.update(extra_manifest)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    for rel, text in (extra_files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if mcp_servers is not None:
        # Fixtures declare servers by NAME (for grant/secret derivation); ensure
        # each has a runnable command so the strict malformed check (Sol) treats
        # them as valid servers. The injected command must ALSO resolve on PATH
        # (python3) so mcp_command_verdicts stays neutral. Grants come from keys
        # + secrets from env, so this injection changes no assertion.
        servers = {
            k: (v if isinstance(v, dict) and (v.get("command") or v.get("url"))
                else {"command": "python3", **(v if isinstance(v, dict) else {})})
            for k, v in mcp_servers.items()}
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": servers}), encoding="utf-8")
    write_metadata(root, name=name, repo="o/r", ref="v1",
                   revision=revision, subdir=subdir,
                   artifact_id=artifact_id, version=version,
                   checksum=content_checksum(root))
    return root


def entry(name: str, targets: list[str], revision: str = "git:" + "a" * 40,
          subdir: str = "", version: str = "1.0.0",
          source_type: str = "github") -> dict:
    """A registry entry. `source_type` DEFAULTS to "github" — an
    operator-installed plugin — and stays that way on purpose (#923): an
    operator-installed plugin does not reach a worker, so a test that needs a
    plugin ATTACHED to an `executor:*` target must say `source_type="bundled"`
    explicitly. Flipping this default would make every such test pass for a
    reason it never states, and would hide the very regression the
    github-sourced red case exists to catch.
    """
    return {
        "name": name,
        "source": {"type": source_type, "repo": "o/r", "ref": "v1",
                   "revision": revision, "subdir": subdir},
        "artifact_id": compute_artifact_id(repo="o/r", revision=revision,
                                           subdir=subdir, name=name),
        "version": version,
        "targets": targets,
    }


def mk_registry(tmp_path: Path, entries: list[dict]) -> Path:
    p = Path(tmp_path) / "registry.json"
    p.write_text(json.dumps({"schema_version": 1, "plugins": entries}),
                 encoding="utf-8")
    return p


def owned_entry(name="mtg.mtg", owner="specialist:mtg", manifest_name="mtg",
                targets=None, revision: str = "git:" + "a" * 40,
                repo: str = "bonzanni/casa-specialist-mtg",
                subdir: str = "plugins/mtg", **over) -> dict:
    """A valid specialist-owned registry entry (spec §2 invariant).

    Mirrors ``tests/test_plugin_registry_owned.py``'s local ``_entry()``
    (Task 3) — extracted here so later test files can build an owned entry
    without duplicating the ``compute_artifact_id`` wiring. That file keeps
    its own local ``_entry``; this is imported by newer test files only.
    """
    targets = targets if targets is not None else [owner]
    e = {
        "name": name,
        "source": {"type": "github", "repo": repo, "ref": "v0.2.0",
                   "revision": revision, "subdir": subdir},
        "artifact_id": compute_artifact_id(
            repo=repo, revision=revision, subdir=subdir, name=name),
        "version": "1.0.0", "targets": targets,
    }
    if owner is not None:
        e["owner"] = owner
    if manifest_name is not None:
        e["manifest_name"] = manifest_name
    e.update(over)
    return e
