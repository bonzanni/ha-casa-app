"""P-5: plugin MCP-tool grants derived from the RESOLVED artifact (unified
plugin architecture, spec §3.4). Grants come from `<ResolvedPlugin.path>/
.mcp.json` — the same resolved object the loader/verify/secrets consume, no
settings scan, no version-dir guessing.

Namespace (documented — code.claude.com/docs/en/mcp.md "Plugin MCP tool
names"): ``mcp__plugin_<plugin>_<server>__<tool>``, segments sanitized so any
char outside ``A-Za-z0-9_-`` becomes ``_``. A SERVER-LEVEL grant is that
string without the ``__<tool>`` suffix — covers every tool the server exposes
(same prefix rule as ``mcp__homeassistant``; proven live for the plugin form
on CC 2.1.150 — the ``mcp__<server>__<tool>`` naming scheme is unchanged
through the 2.1.220 pin). Derivation never raises into a turn: missing/corrupt files
degrade to no grants at DEBUG.

The resident/specialist/executor OPTION-BUILDER integration tests live in
tests/test_agent_plugin_binding.py (Task 7), not here.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path

from claude_agent_sdk import PermissionResultDeny

from plugin_registry import runtime_name
from text_util import sanitize_segment  # noqa: F401 — re-exported (existing
# callers/tests import `sanitize_segment` from this module; the canonical
# implementation now lives in text_util so plugin_store.py — stdlib-only,
# imported by the Dockerfile build helper before any venv — can share it
# without importing this (claude_agent_sdk-dependent) module.

logger = logging.getLogger(__name__)


def _mcp_servers(mcp_json_path: Path) -> dict:
    """The {server-name: config} map from a plugin ``.mcp.json`` — delegates to
    the shared, stdlib-only ``plugin_store.mcp_servers_map`` (also used by the
    build-time verifier) so both understand the wrapper AND top-level shapes."""
    from plugin_store import mcp_servers_map
    return mcp_servers_map(mcp_json_path)


def mcp_json_malformed(rp) -> bool:
    """Sol #16 / CI-review: True iff ``.mcp.json`` is PRESENT but broken — via the
    ONE shared parser (``plugin_store.parse_mcp_servers``) so it agrees with grant
    derivation across BOTH the ``mcpServers`` wrapper and the top-level shape:
    unparseable / not an object / non-mapping ``mcpServers`` / server-like objects
    none of which declare command|url. An ABSENT ``.mcp.json`` (skill-only) and an
    empty/no-server config are NOT malformed. Verify uses this so a broken MCP
    config can't report ready when grants/secrets degrade to ``[]``."""
    from plugin_store import parse_mcp_servers
    return parse_mcp_servers(Path(rp.path) / ".mcp.json")[1]


def grants_for_resolved(rp) -> list[str]:
    """Server-level grant strings for one resolved plugin (sorted). Skill-only
    plugins (no ``.mcp.json``) yield ``[]``. Namespaced on the plugin's
    RUNTIME identity (Task 5, spec §2.1: ``plugin_registry.runtime_name`` —
    an owned artifact's ``manifest_name``, else its registry name), never the
    scoped registry name, so an owned ``mtg.mtg`` entry grants
    ``mcp__plugin_mtg_<server>``, matching the SDK's own plugin-tool
    namespace (which loads ``plugin.json`` from ``rp.path`` and only ever
    knows the manifest name)."""
    mcp_json = Path(rp.path) / ".mcp.json"
    if not mcp_json.is_file():
        return []
    plugin_seg = sanitize_segment(runtime_name(rp))
    return sorted(
        f"mcp__plugin_{plugin_seg}_{sanitize_segment(server)}"
        for server in _mcp_servers(mcp_json)
    )


def grants_for_resolution(res) -> list[str]:
    """Sorted, deduplicated union of server-level grants for every fully-
    resolved plugin in a ResolutionResult."""
    out: set[str] = set()
    for rp in res.plugins:
        out.update(grants_for_resolved(rp))
    return sorted(out)


def declared_tools_for_resolution(res) -> set[str]:
    """Union of manifest-declared tool-level names for every resolved
    plugin (spec A5). ``grants_for_resolution`` is SERVER-level
    (``mcp__plugin_mtg_mtg``) — a ``requires.tools`` entry is TOOL-level
    (``mcp__plugin_mtg_mtg__lookup_rule``), so it must be checked against
    a manifest-declared inventory instead, never against server grants
    directly. Each plugin declares its own tools via
    ``casa.provides_tools: list[str]`` in its manifest — no "observe from
    grants" bootstrap; WS-B's plugin.json ships the names verbatim.

    FAIL CLOSED on malformed metadata (r1-review): a non-dict ``manifest``,
    a non-dict ``casa``, or a ``provides_tools`` that is not a list all
    contribute NOTHING, and only non-empty ``str`` entries survive from a
    valid list. This is deliberate — a dict ``provides_tools`` would
    otherwise leak its KEYS (a malformed manifest could then satisfy a tool
    requirement), and a non-list would raise a ``TypeError`` that escapes
    ``_prelaunch`` instead of denying with ``dependency_unavailable``.
    Malformed metadata degrades to "no declared tools", which makes the
    requirement unmet → ``dependency_unavailable`` (same never-raise
    contract as the other grant helpers in this module)."""
    out: set[str] = set()
    for rp in getattr(res, "plugins", None) or []:
        manifest = getattr(rp, "manifest", None)
        if not isinstance(manifest, dict):
            continue
        casa = manifest.get("casa")
        if not isinstance(casa, dict):
            continue
        provided = casa.get("provides_tools")
        if not isinstance(provided, list):
            if provided is not None:
                logger.debug(
                    "declared_tools_for_resolution: %s casa.provides_tools is "
                    "%s not a list — contributing nothing",
                    getattr(rp, "name", "?"), type(provided).__name__,
                )
            continue
        out.update(t for t in provided if isinstance(t, str) and t)
    return out


def required_env_vars_for_resolved(rp) -> list[str]:
    """Skill-only plugins (no .mcp.json) require nothing; malformed JSON
    degrades to [] at DEBUG — same never-raise contract as grants."""
    mcp_json = Path(rp.path) / ".mcp.json"
    if not mcp_json.is_file():
        return []
    try:
        from plugin_env_extractor import extract_env_vars
        return sorted(extract_env_vars(mcp_json))
    except Exception as exc:  # noqa: BLE001 — never raise into a tool/turn
        logger.debug("env-var extraction failed (%s): %s", mcp_json, exc)
        return []


def unresolved_env_vars_for_resolved(rp, environ=None) -> list[str]:
    """The subset of :func:`required_env_vars_for_resolved` that is NOT
    usable in the effective environment: absent, empty, or still an
    ``op://`` reference (boot/reload resolution failed and fell back to the
    raw value — the same rule verify_plugin_state applies). #423/#424: the
    CLI passes an undefined ``${VAR}`` through as the LITERAL string, so a
    plugin loaded with unresolved vars runs against placeholder credentials
    instead of failing; callers gate on this being empty."""
    env = os.environ if environ is None else environ
    unresolved = []
    for var in required_env_vars_for_resolved(rp):
        # ONE read per var (#424 r4): the check runs in worker threads while
        # reload_plugin_env can pop keys — a get-then-index pair would raise
        # KeyError mid-build instead of withholding the plugin.
        value = env.get(var)
        if not value or value.startswith("op://"):
            unresolved.append(var)
    return unresolved


# #429: re-exported from plugin_store (stdlib-only, so the manifest validator
# and this module cannot drift on which names are Casa-owned). Existing
# callers import it from here.
from plugin_store import CASA_OWNED_ENV_OPTIONS  # noqa: E402


def env_remediation_hint(missing) -> str:
    """The operator-facing repair for an unresolved-env withhold (#429).
    Names the app OPTION for Casa-owned vars and the plugin-env path for the
    rest, so a message can never send an operator to the wrong control."""
    owned = [v for v in missing if v in CASA_OWNED_ENV_OPTIONS]
    other = [v for v in missing if v not in CASA_OWNED_ENV_OPTIONS]
    parts = []
    if owned:
        parts.append(
            "set the app option(s) " + ", ".join(
                sorted({CASA_OWNED_ENV_OPTIONS[v] for v in owned}))
            + " and restart the app")
    if other:
        parts.append(
            "wire " + ", ".join(sorted(other))
            + " via set_plugin_env_reference + casa_reload(plugin_env)")
    return "; ".join(parts) if parts else "no remediation known"


def declared_absent_env_vars_for_resolved(rp) -> set[str]:
    """The env vars this plugin's manifest declares Casa must NOT withhold it
    for (#429): ``casa.setupProvides`` — the ones the plugin's OWN setup tool
    creates.

    0.153.0's gate held a settled setup episode until every ``${VAR}`` in
    ``.mcp.json`` resolved, which deadlocks any plugin whose setup tool exists
    to PROVISION its credentials: setup cannot run until they exist, and they
    only exist after setup runs. The plugin had no way to say so — this
    declaration is that way.

    A genuinely OPTIONAL variable needs no declaration and is not one of
    these (#431): ``${VAR:-}`` in ``.mcp.json`` already expands to empty and
    is invisible to the extractor, so it neither withholds nor leaks a
    placeholder. What a default cannot express is readiness, which is the
    only reason ``setupProvides`` exists as a declaration at all.

    FAIL CLOSED on malformed metadata, matching
    :func:`declared_tools_for_resolution`: a manifest whose declaration does
    not parse contributes NOTHING, so the plugin keeps the strict 0.153.0
    behaviour (withheld) instead of having a broken declaration relax its
    gate. ``artifact_verdict`` already excludes such an artifact from
    resolution; this is the belt for a path that somehow reaches here."""
    from plugin_store import StoreError, manifest_setup_provides
    manifest = getattr(rp, "manifest", None)
    if not isinstance(manifest, dict):
        return set()
    try:
        return set(manifest_setup_provides(manifest))
    except StoreError as exc:
        logger.warning(
            "plugin %s: %s — treating its env declaration as absent "
            "(the plugin stays withheld until the vars resolve)",
            getattr(rp, "name", "?"), exc)
        return set()


def blocking_unresolved_env_vars_for_resolved(rp, environ=None) -> list[str]:
    """:func:`unresolved_env_vars_for_resolved` MINUS the vars the manifest
    declares as setup-provisioned or optional (#429) — the subset that must
    actually WITHHOLD the plugin from a session build and hold its setup
    episode. Empty means the plugin is loadable; the declared-but-absent
    remainder is handled by :func:`sanitized_env_for_resolution` instead of
    by exclusion, and stays visible on the verify surface."""
    exempt = declared_absent_env_vars_for_resolved(rp)
    return [v for v in unresolved_env_vars_for_resolved(rp, environ)
            if v not in exempt]


def setup_secrets_ready(resolution, plugin: str, environ=None) -> bool:
    """Whether *plugin*'s settled setup episode may dispatch (#423, amended
    #429). A plugin absent from *resolution* is NOT ready (the dispatch-time
    registry gate owns that path's messaging and retries).

    Lives here, beside the rule it applies, rather than inside casa_core's
    boot closure — the gate and the session-build withhold must answer the
    same question, and a copy in an untestable closure is how they drift."""
    rp = next((p for p in getattr(resolution, "plugins", None) or []
               if p.name == plugin), None)
    if rp is None:
        return False
    return not blocking_unresolved_env_vars_for_resolved(rp, environ)


def sanitized_env_for_resolution(resolution, environ=None) -> dict[str, str]:
    """``{VAR: ""}`` for every declared-absent var (#429) that is still
    unresolved across *resolution*'s plugins — the env overlay every session
    builder passes as ``ClaudeAgentOptions.env``.

    This is the half of the fix that keeps #423's guarantee intact: the CLI
    expands an UNDEFINED ``${VAR}`` to the LITERAL string, so merely letting
    an exempt plugin load would spawn its MCP server with placeholder
    credentials — exactly the failure (an OAuth URL carrying
    ``client_id=${GMAIL_CLIENT_ID}``) that motivated the gate. An explicit
    empty string makes the variable genuinely absent-shaped to the server,
    which is what a plugin awaiting its own setup run must see.

    Only UNRESOLVED vars are included, so a value that is actually wired —
    including one wired by a setup run that already happened — is never
    overwritten with an empty string. A var left holding a raw ``op://``
    reference (boot/reload resolution failed) counts as unresolved and IS
    emptied: an unresolvable reference is not a credential, and handing the
    literal to the server is the same placeholder failure in another
    spelling. It stays visible — verify grades it off the effective
    environment, not off this overlay.

    Driven by the DECLARATION, not by the ``.mcp.json`` reference set (r2,
    Sol): a plugin whose server reads a provisioned credential from the
    inherited environment rather than naming it in its launch config would
    otherwise get no binding at all, and the server would see the leftover
    ``op://`` reference — which an idempotent setup tool can easily read as
    "already provisioned" and skip the creation over. Every declared name is
    plugin-owned by construction (``PLUGIN_ENV_DECLARATION_PREFIX``), so
    binding one that the launch config never mentions is safe."""
    env = os.environ if environ is None else environ
    out: dict[str, str] = {}
    for rp in getattr(resolution, "plugins", None) or []:
        for var in declared_absent_env_vars_for_resolved(rp):
            value = env.get(var)
            if not value or value.startswith("op://"):
                out[var] = ""
    return out


def sanitized_env_for_paths(plugin_paths, environ=None) -> dict[str, str]:
    """:func:`sanitized_env_for_resolution` for artifact PATHS (#429).

    The engagement run script attaches artifacts by path via ``--plugin-dir``
    with no ``ResolutionResult`` in hand, so there is nothing to read the
    declarations off. Reads each artifact's own manifest — the same file the
    CLI loads from that path. An unreadable manifest contributes nothing (no
    declarations ⇒ nothing bound ⇒ exactly the pre-#429 behaviour). Never
    raises: it runs on an engagement-start path."""
    import json
    from plugin_registry import ResolutionResult, ResolvedPlugin
    plugins = []
    for path in plugin_paths or []:
        manifest = {}
        try:
            manifest = json.loads(
                (Path(path) / ".claude-plugin" / "plugin.json")
                .read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — absent/corrupt ⇒ no declarations
            manifest = {}
        plugins.append(ResolvedPlugin(
            name="", artifact_id="", path=str(path), version="",
            manifest=manifest if isinstance(manifest, dict) else {}))
    return sanitized_env_for_resolution(
        ResolutionResult(registry_valid=True, plugins=plugins, issues=[]),
        environ)


def withhold_env_unresolved(resolution, *, context: str, environ=None):
    """Filter *resolution* down to env-resolved plugins (INV-PLUG-008)
    WITHOUT mutating the input — H7b shares a creation resolve between the
    engagement record and the options builder, so callers must never see
    their object shrink under them. Returns ``(resolution, withheld)`` where
    withheld is ``[(rp, missing_vars)]``; the input object is returned
    as-is when nothing is withheld, a ``dataclasses.replace`` copy (fresh
    plugins list, other fields shared) otherwise. Each withheld plugin is
    logged at WARNING with *context* naming the session build.

    *environ* defaults to ``os.environ``, matching every other helper here.
    The seam is not decoration: without it this was the one gate whose verdict
    a test could not state, so its cases read the AMBIENT environment and
    passed on a developer machine with the variable exported while failing in
    CI without it — main was red for two releases behind a green local suite.
    """
    import dataclasses
    withheld = []
    loadable = []
    for rp in resolution.plugins:
        # #429: the BLOCKING subset — a var the manifest declares as
        # setup-provisioned or optional must not withhold the plugin, or a
        # plugin whose setup tool exists to CREATE its credentials can never
        # be loaded to run that setup tool. Those are emptied instead, by
        # sanitized_env_for_resolution at the session builder.
        missing = blocking_unresolved_env_vars_for_resolved(rp, environ)
        if missing:
            logger.warning(
                "plugin %s withheld from %s: required env unresolved (%s) — %s",
                rp.name, context, ", ".join(missing),
                env_remediation_hint(missing))
            withheld.append((rp, missing))
            continue
        loadable.append(rp)
    if not withheld:
        return resolution, []
    return dataclasses.replace(resolution, plugins=loadable), withheld


def protected_map(resolution) -> dict[str, dict]:
    """Full-tool-name -> ``{"artifact_id": str, "summary": str | None}`` map
    for every ``casa.protectedTools`` entry across a RESOLVED
    ``ResolutionResult`` (A:§3.7, value shape extended v0.78.0 W1). Derived
    from the RESOLVED ARTIFACT's manifest (the content-addressed store copy
    named by the ``ResolutionResult`` — never a duplicated registry field).
    ``summary`` is the plugin-declared advisory copy (``None`` for a legacy
    string entry or an object entry without one) — the authz hook consumes
    ``artifact_id`` exactly as before (NO grant/GrantKey/enforcement
    change); ``summary`` is threaded to the challenge render only (W2).

    Namespacing reuses the grant-derivation sanitization
    (``mcp__plugin_<plugin>_<server>__<tool>``); a BARE tool name expands
    across EVERY MCP server the plugin declares (a plugin with two servers
    protects the tool on both).

    PER-PLUGIN DEGRADATION (r2-B6/r3-4): a malformed ``casa.protectedTools``
    in one resolved plugin's manifest excludes JUST that plugin's protected
    tools from the map (logged at WARNING) — every other resolved plugin
    still contributes normally, matching the existing artifact-failure
    ``PluginIssue`` pattern. A plugin declaring no MCP servers (skill-only,
    or a malformed ``.mcp.json``) contributes nothing either, since there is
    no server to qualify the tool name with (no runtime MCP enumeration —
    B7).
    """
    from plugin_store import StoreError, manifest_protected_tools

    out: dict[str, dict] = {}
    for rp in getattr(resolution, "plugins", None) or []:
        try:
            entries = manifest_protected_tools(rp.manifest)
        except StoreError:
            logger.warning(
                "protected_tools_invalid: excluding %s (artifact_id=%s) "
                "from the protected-tool map", rp.name, rp.artifact_id)
            continue
        if not entries:
            continue
        servers = sorted(_mcp_servers(Path(rp.path) / ".mcp.json"))
        if not servers:
            continue
        # Task 5: same runtime-identity namespacing as grants_for_resolved —
        # the authz hook matches these full tool names against what the SDK
        # actually calls, which is namespaced on the manifest name.
        plugin_seg = sanitize_segment(runtime_name(rp))
        for tool_entry in entries:
            tool_seg = sanitize_segment(tool_entry["name"])
            for server in servers:
                full = (f"mcp__plugin_{plugin_seg}_"
                        f"{sanitize_segment(server)}__{tool_seg}")
                out[full] = {"artifact_id": rp.artifact_id,
                             "summary": tool_entry["summary"]}
    return out


# ---------------------------------------------------------------------------
# #792: the result-contract map — what the result broker keys every plugin
# tool call on. Derived from the RESOLVED artifacts exactly like protected_map.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ToolContract:
    """One non-setup tool's contract entry, keyed by its FULL runtime name."""
    artifact_id: str
    plugin_seg: str
    kind: str                      # "safe" | "capability"
    provides: tuple[str, ...]      # capability slots this tool deposits
    consumes: dict                 # {param: slot} references this tool redeems


@dataclasses.dataclass(frozen=True)
class PluginContract:
    """One resolved MCP-bearing plugin: adopted or not, and its exempt
    setup-tool names (expanded to full names across its servers)."""
    artifact_id: str
    adopted: bool
    setup_tools: frozenset


@dataclasses.dataclass(frozen=True)
class ResultContractMap:
    tools: dict          # full tool name -> ToolContract (adopting plugins only)
    plugins: dict        # plugin_seg -> PluginContract (every MCP-bearing plugin)

    def plugin_seg_of(self, tool_name: str) -> str | None:
        """The ``<plugin>`` segment of ``mcp__plugin_<plugin>_<server>__<tool>``
        — resolved against the KNOWN plugin segments (longest match), because
        ``_`` is legal inside a plugin name and inside a server name alike, so
        the name cannot be split by counting underscores."""
        if not tool_name.startswith("mcp__plugin_"):
            return None
        rest = tool_name[len("mcp__plugin_"):]
        best = None
        for seg in self.plugins:
            if rest == seg or rest.startswith(seg + "_"):
                if best is None or len(seg) > len(best):
                    best = seg
        return best


def result_contract_map(resolution) -> ResultContractMap:
    """Build the :class:`ResultContractMap` for a RESOLVED ``ResolutionResult``
    (#792). Every resolved plugin that declares at least one MCP server is
    represented in ``plugins`` — adopting or not — so the broker can tell
    "non-adopting plugin" from "unknown tool" and refuse both. Tool names are
    expanded across every server the plugin declares, namespaced on the
    runtime identity exactly like ``grants_for_resolved`` / ``protected_map``.

    PER-PLUGIN DEGRADATION: a malformed ``casa.resultContract`` (or a
    malformed ``casa.setupTool``) in one plugin's manifest represents that
    plugin as NOT adopted (logged at WARNING) — under the broker its non-setup
    tools are then refused, never passed. Never raises."""
    from plugin_store import (
        StoreError, manifest_result_contract, manifest_setup_tool,
    )
    tools: dict = {}
    plugins: dict = {}
    for rp in getattr(resolution, "plugins", None) or []:
        servers = sorted(_mcp_servers(Path(rp.path) / ".mcp.json"))
        if not servers:
            continue
        plugin_seg = sanitize_segment(runtime_name(rp))
        manifest = getattr(rp, "manifest", None)
        manifest = manifest if isinstance(manifest, dict) else {}
        artifact_id = str(getattr(rp, "artifact_id", "") or "")
        setup_names: set[str] = set()
        try:
            setup = manifest_setup_tool(manifest)
        except StoreError:
            setup = None
        if setup:
            for server in servers:
                setup_names.add(f"mcp__plugin_{plugin_seg}_"
                                f"{sanitize_segment(server)}__"
                                f"{sanitize_segment(setup)}")
        contract = None
        try:
            contract = manifest_result_contract(manifest)
        except StoreError:
            logger.warning(
                "result_contract_invalid: %s (artifact_id=%s) is treated as "
                "NOT adopting the result contract", rp.name, artifact_id)
            contract = None
        adopted = contract is not None and bool(artifact_id)
        plugins[plugin_seg] = PluginContract(
            artifact_id=artifact_id, adopted=adopted,
            setup_tools=frozenset(setup_names))
        if not adopted:
            continue
        for name, entry in contract["tools"].items():
            tool_seg = sanitize_segment(name)
            for server in servers:
                full = (f"mcp__plugin_{plugin_seg}_"
                        f"{sanitize_segment(server)}__{tool_seg}")
                if full in setup_names:
                    continue  # the setup tool is exempt, never contracted
                tools[full] = ToolContract(
                    artifact_id=artifact_id, plugin_seg=plugin_seg,
                    kind=entry["result"], provides=tuple(entry["provides"]),
                    consumes=dict(entry["consumes"]))
    return ResultContractMap(tools=tools, plugins=plugins)


def make_fail_closed_can_use_tool(role: str):
    """Fail-closed ``can_use_tool`` for in-casa agents (P-5b).

    The SDK consults this ONLY for tool calls not already auto-approved via
    ``allowed_tools``/``permission_mode`` — granted tools never reach it. With
    no callback, an ungranted call falls through to CC's interactive prompt,
    which nothing on the in-casa path can answer (no relay) → headless hang.
    Deny fast and loud instead. No awaits inside, so caller cancellation
    (voice barge-in) has nothing to be swallowed by.
    """
    async def _deny(tool_name: str, tool_input: dict, context) -> PermissionResultDeny:
        logger.warning(
            "fail-closed deny: tool=%s role=%s (not in allowed_tools)",
            tool_name, role,
        )
        return PermissionResultDeny(
            message=(
                f"{tool_name} is not granted to {role!r}; grant it via the "
                "configurator or install the plugin that provides it."
            ),
            interrupt=False,
        )
    return _deny
