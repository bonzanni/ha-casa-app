from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

import jsonschema

from canonical_bytes import canonical_json_bytes, checksum_bytes
from role_artifact import RoleArtifactSource, load_role_artifact

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
PLUGIN_IDENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_SCOPED_NAME_BYTES = 72

# #541: the casa-framework tools a THIRD-PARTY specialist role may declare —
# an ALLOWLIST (deny-lists cannot fence an open namespace: every tool added
# to CASA_TOOLS later would be granted-by-default under a deny-set). Bare
# names; the role.yaml entries carry the ``mcp__casa-framework__`` prefix.
# Converged over design rounds 1-4 (2026-08-13, Sol+Terra):
#   - spawn (engage_executor, delegate_to_agent), plugin management, config
#     mutation, secrets (list_vault_items/get_item_fields), install/persona
#     lifecycle, workspace admin, reload/restart: NEVER grantable by bundle.
#   - send_message OUT: an unbounded outbound Telegram send primitive.
#   - set_reminder/cancel_reminder OUT: they write the RESIDENT's trigger
#     space (the delegated actor is inherited).
#   - get_schedule kept: read-only; disclosure-not-authority (defended by
#     both reviewers); surfaced on the install consent DM.
#   - send_media kept: the shipped finance specialist's reporting flow needs
#     it; bounded by a code-owned per-context quota at dispatch (tools.py).
# Enforced at three layers: HERE (install/upgrade/rollback restage, before
# consent), agent_loader's load-time clamp (pre-existing installs), and the
# dispatch-time ceiling in tools.py (already-live engagement records).
SPECIALIST_CASA_TOOL_ALLOWLIST: frozenset[str] = frozenset({
    "query_engager", "emit_completion", "react", "ask_user",
    "recall_memory", "ack_event", "get_schedule", "send_media",
})

_CASA_SERVER_GRANT = "mcp__casa-framework"
_CASA_TOOL_PREFIX = "mcp__casa-framework__"


def specialist_casa_tool_violations(allowed: object) -> list[str]:
    """Return the casa-framework entries of *allowed* outside the ceiling.

    The single authority all three #541 enforcement layers call. Entries
    that are not casa-framework grants (CC built-ins, ``mcp__plugin_*``
    servers) are out of scope and never named. Non-list/-string shapes are
    ignored (schema validation owns shape errors).
    """
    out: list[str] = []
    if not isinstance(allowed, (list, tuple)):
        return out
    for entry in allowed:
        if not isinstance(entry, str):
            continue
        if entry == _CASA_SERVER_GRANT:
            out.append(entry)
        elif (entry.startswith(_CASA_TOOL_PREFIX)
                and entry[len(_CASA_TOOL_PREFIX):]
                not in SPECIALIST_CASA_TOOL_ALLOWLIST):
            out.append(entry)
    return out


def is_valid_slug(slug: object) -> bool:
    """Canonical specialist-slug predicate — the SAME regex the loader
    enforces below (``_SLUG_RE``) and role.v1.json's ``slot`` pattern
    (``^[a-z0-9][a-z0-9-]{0,31}$`` — bound to 32 bytes). Exposed so every
    lifecycle entry point
    (uninstall/upgrade/rollback/override/inspect) can validate a
    caller-supplied slug against ONE authority before it ever reaches a
    ``Path`` join — a slug like ``../../..`` or ``/data`` must never index a
    filesystem operation (whole-branch review F1)."""
    return isinstance(slug, str) and _SLUG_RE.fullmatch(slug) is not None


@dataclass(frozen=True, slots=True)
class PluginDepSource:
    type: Literal["bundled", "github"]
    path: str = ""
    repo: str = ""
    ref: str = ""
    revision: str = ""


_BUNDLED_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REVISION_RE = re.compile(r"^git:[0-9a-f]{40}$")


def _load_dep_source(row: dict) -> "PluginDepSource | None":
    """Parse+validate a dependency row's `source` object.

    The JSON schema (specialist-component.v1.json) only checks that `source`
    is an object with a string `type` — every other shape/format/containment
    rule (required fields per type, path/repo/revision format, traversal
    containment) is enforced HERE, in plain Python, as the single authority.
    This is a deliberate delta from the Task 1 brief's sketch (which had the
    schema do full oneOf-based structural validation): a pre-existing test
    (test_specialist_component.py::test_load_specialist_component_rejects_
    manifest_schema_violation) pins jsonschema.exceptions.ValidationError to
    propagate UNWRAPPED for generic manifest violations, so `source` shape
    violations must raise ValueError from our own code rather than from
    jsonschema, to avoid two different exception types for "malformed
    manifest" depending on which field misbehaves.
    """
    src = row.get("source")
    if src is None:
        return None
    src_type = src.get("type")
    if src_type == "bundled":
        path = src.get("path")
        if not isinstance(path, str) or not (1 <= len(path) <= 200) or not _BUNDLED_PATH_RE.fullmatch(path):
            raise ValueError(f"malformed bundled source.path {path!r}")
        # relative, no '..' segment, no leading/duplicate slash. Symlink
        # escape is checked at inspect time against the staged tree (Task 5)
        # — a manifest string can't be symlink-checked without the bytes.
        parts = path.split("/")
        if any(seg in ("", ".", "..") for seg in parts):
            raise ValueError(f"non-canonical bundled source.path {path!r}")
        extra = set(src) - {"type", "path"}
        if extra:
            raise ValueError(f"bundled source has unexpected keys {sorted(extra)!r}")
        return PluginDepSource(type="bundled", path=path)
    if src_type == "github":
        repo, ref, revision = src.get("repo"), src.get("ref"), src.get("revision")
        if not isinstance(repo, str) or not _GITHUB_REPO_RE.fullmatch(repo):
            raise ValueError(f"malformed github source.repo {repo!r}")
        if not isinstance(ref, str) or not ref:
            raise ValueError("github source.ref is required and must be non-empty")
        if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
            raise ValueError(f"malformed github source.revision {revision!r}")
        extra = set(src) - {"type", "repo", "ref", "revision"}
        if extra:
            raise ValueError(f"github source has unexpected keys {sorted(extra)!r}")
        return PluginDepSource(type="github", repo=repo, ref=ref, revision=revision)
    raise ValueError(f"unknown plugin dependency source type {src_type!r}")


@dataclass(frozen=True, slots=True)
class ComponentDependency:
    kind: Literal["persona", "corpus/data", "plugin/implementation"]
    identifier: str
    digest: str
    source: "PluginDepSource | None" = None


@dataclass(frozen=True, slots=True)
class SpecialistComponent:
    component_id: str
    version: str
    slug: str
    role: RoleArtifactSource
    default_persona_ref: str
    default_persona_checksum: str
    config_schema: Mapping[str, object]
    dependencies: tuple[ComponentDependency, ...]
    checksum: str


def compute_component_checksum(files: Mapping[str, bytes]) -> str:
    rows = [{"path": name, "checksum": checksum_bytes(files[name])} for name in sorted(files)]
    payload = {"api_version": "casa.specialist-component.manifest/v1", "files": rows}
    return checksum_bytes(canonical_json_bytes(payload))


def load_specialist_component(component_dir: Path, manifest_path: Path) -> SpecialistComponent:
    """Load a validated, checksum-verified specialist component from a LOCAL directory
    (already fetched/staged by Plan 2's N1 install pipeline, or a test fixture). This
    function performs no network I/O and trusts nothing about the caller's fetch —
    every byte here is re-validated exactly as image content is (spec §2.5)."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema_path = Path(__file__).parent / "defaults/schema/specialist-component.v1.json"
    # NOTE (delta from the Task 1 brief's sketch): the pre-existing contract
    # (test_specialist_component.py::test_load_specialist_component_rejects_
    # manifest_schema_violation) requires jsonschema.exceptions.ValidationError
    # to propagate UNWRAPPED for generic manifest-shape violations — so this
    # call is deliberately left un-caught. The new `source` sub-schema below
    # is therefore kept structural-only (object with a string `type`); all
    # `source`-specific shape/format/containment rules are enforced in
    # `_load_dep_source` in plain Python, which raises ValueError — the single
    # authority for those checks, and the type the Task-1 tests expect.
    jsonschema.validate(manifest, json.loads(schema_path.read_text(encoding="utf-8")))

    role_dir = component_dir / "role"
    role_source = load_role_artifact(role_dir)
    if role_source.role["kind"] != "specialist":
        raise ValueError("a specialist component's role artifact must declare kind: specialist")
    slug = str(role_source.role["slot"])
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(f"invalid specialist slug {slug!r}")

    # #541 tool ceiling — enforced HERE so a violating bundle fails at
    # inspect, BEFORE any consent prompt exists to approve it. The same
    # loader runs on upgrade/rollback restage, so no lifecycle path admits
    # the grant later.
    violations = specialist_casa_tool_violations(
        (role_source.role.get("tools") or {}).get("allowed"))
    if violations:
        raise ValueError(
            "specialist role declares casa-framework tools outside the "
            f"consumer-safe ceiling: {violations!r} (allowed: "
            f"{sorted(SPECIALIST_CASA_TOOL_ALLOWLIST)!r})")

    config_schema_path = component_dir / "config-schema.json"
    config_schema = json.loads(config_schema_path.read_text(encoding="utf-8"))
    dependencies_list: list[ComponentDependency] = []
    seen_rows: set[tuple[str, str]] = set()
    for row in manifest.get("dependencies", []):
        source = _load_dep_source(row)
        key = (row["kind"], row["identifier"])
        if key in seen_rows:
            # Sol plan-r1 P1: duplicate (kind, identifier) is rejected for ALL
            # rows — sourced, sourceless, and non-plugin kinds alike.
            raise ValueError(f"duplicate dependency {key!r}")
        seen_rows.add(key)
        if row["kind"] == "plugin/implementation" and source is not None:
            ident = row["identifier"]
            if not PLUGIN_IDENT_RE.fullmatch(ident):
                raise ValueError(f"sourced plugin identifier {ident!r} violates the "
                                  "plugin-name grammar (lowercase [a-z0-9-], <=40 chars)")
            scoped = f"{slug}.{ident}"
            if len(scoped.encode()) > MAX_SCOPED_NAME_BYTES:
                raise ValueError(f"scoped plugin name {scoped!r} exceeds "
                                  f"{MAX_SCOPED_NAME_BYTES} bytes")
        elif row["kind"] != "plugin/implementation" and source is not None:
            raise ValueError(f"source is only valid on plugin/implementation deps "
                              f"(got kind={row['kind']!r})")
        dependencies_list.append(ComponentDependency(
            kind=row["kind"], identifier=row["identifier"], digest=row["digest"],
            source=source))
    dependencies = tuple(dependencies_list)
    files = {
        "role/role.yaml": role_dir.joinpath("role.yaml").read_bytes(),
        "role/doctrine.md": role_dir.joinpath("doctrine.md").read_bytes(),
        "config-schema.json": config_schema_path.read_bytes(),
    }
    computed = compute_component_checksum(files)
    if computed != manifest["checksum"]:
        raise ValueError("specialist component checksum does not match its manifest")

    return SpecialistComponent(
        component_id=manifest["component_id"], version=manifest["version"], slug=slug, role=role_source,
        default_persona_ref=manifest["default_persona"]["ref"],
        default_persona_checksum=manifest["default_persona"]["checksum"],
        config_schema=MappingProxyType(dict(config_schema)), dependencies=dependencies, checksum=computed,
    )
