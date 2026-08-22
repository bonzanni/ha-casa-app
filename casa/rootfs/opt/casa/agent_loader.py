"""Per-agent-directory loader.

One directory, one file per concern, schema-validated on load. The
composed system prompt is the derived field used by ``Agent._process``
verbatim.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

import yaml
import jsonschema

from canonical_bytes import canonical_json_bytes
from config import (
    AgentConfig,
    CharacterConfig,
    DelegateEntry,
    DisclosureConfig,
    ExecutorEntry,
    ExecutorMemoryConfig,
    HooksConfig,
    MemoryConfig,
    RequiresConfig,
    ResponseShapeConfig,
    SessionConfig,
    ToolsConfig,
    TriggerSpec,
    TTSConfig,
    VoiceConfig,
    load_yaml_with_env,
    resolve_model,
    _substitute_env,
)
from policies import PolicyLibrary, render_disclosure_section
from role_artifact import RoleArtifactSource, load_role_artifact
from role_slot import (
    EMPTY_CONFIG_DIGEST,
    FIXED_RESIDENT_SLOTS,
    _ha_model_options,
    compute_executor_identity,
    materialize_role,
)

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "defaults", "schema")

# Personality Phase A, Task 5: the only image-owned canonical role layout,
# defaults/roles/<kind>/<slot>/{role.yaml,doctrine.md}. ``roles_dir`` kwargs
# threaded through the loader functions below default to this and let tests
# inject a synthetic tree instead of patching the filesystem.
DEFAULT_ROLES_DIR = os.path.join(os.path.dirname(__file__), "defaults", "roles")

# Round-6 P0-2 (Sol): executor permission modes ranked by permissiveness.
# The image-owned role artifact's tools.permission_mode is the CEILING a
# definition.yaml may not exceed (shipped: configurator=acceptEdits,
# plugin-developer=auto). "bypassPermissions" is deliberately ABSENT — it
# would reduce allowed_tools to mere auto-approval and is NEVER honored for
# executors, whatever the role says; see load_all_executors for the clamp.
_EXECUTOR_PERMISSION_MODE_RANK: dict[str, int] = {
    "plan": 0, "default": 1, "acceptEdits": 2, "dontAsk": 3, "auto": 4,
}

# --- Tier file-set rules ---------------------------------------------------

# NOTE: `plugins.yaml` is legacy, ignored since v0.71.0 (unified plugin
# architecture — assignment lives in the registry). It stays whitelisted here
# so an operator-MODIFIED file (which config_sync preserves) does not crash
# agent loading on upgrade; nothing reads it anymore. Prune with the
# inert-state cleanup release.
TIER_FILES: dict[str, dict[str, set[str]]] = {
    "resident": {
        "required":  {"character.yaml", "voice.yaml", "response_shape.yaml",
                      "disclosure.yaml", "runtime.yaml"},
        "optional":  {"delegates.yaml", "executors.yaml", "triggers.yaml",
                      "hooks.yaml", "plugins.yaml"},
        "forbidden": set(),
    },
    "specialist": {
        "required":  {"character.yaml", "voice.yaml", "response_shape.yaml",
                      "runtime.yaml"},
        "optional":  {"hooks.yaml", "plugins.yaml"},
        "forbidden": {"disclosure.yaml", "delegates.yaml", "executors.yaml",
                      "triggers.yaml"},
    },
}

TIER_FILES["executor"] = {
    "required":  {"definition.yaml", "prompt.md"},
    "optional":  {"hooks.yaml", "observer.yaml", "plugins.yaml"},
    "forbidden": {"character.yaml", "runtime.yaml", "delegates.yaml",
                  "executors.yaml", "disclosure.yaml",
                  "response_shape.yaml", "voice.yaml", "triggers.yaml"},
}

# S-1 fix (v0.35.2): editor backup artifacts that strict-load tolerates.
# Covers sed -i.bak (.bak), vim swap files (.swp), atomic rename temp
# files (.tmp), and "*~" originals from various editors. The agent dir's
# "real" config is still in the un-suffixed files; the backups are
# process state from in-progress edits or unclean shutdowns and must
# not break casactl reload.
#
# `.casabak` is config_sync's own recovery copy, written beside the file it
# is about to overwrite. It belongs here for the same reason: it is not
# config, and rejecting it turns the act of PRESERVING an operator's content
# into a load failure for the very agent that content belongs to. Found in
# live verification of #398 — entry-level reconcile writes the sidecar on
# every merge, where byte-level only wrote one when git was unavailable, so
# a latent fault became a certainty. Its own boot-parity backstop then
# reverted the merge to "repair" the tree, undoing the preservation.
_EDITOR_BACKUP_SUFFIXES = (".bak", ".swp", ".tmp", ".orig", ".casabak")


def _is_editor_backup(name: str) -> bool:
    """Return True for editor-process artifacts that strict-load skips."""
    return name.endswith(_EDITOR_BACKUP_SUFFIXES) or name.endswith("~")


_DELEGATE_MCP_TOOL = "mcp__casa-framework__delegate_to_agent"

# Containment Stage 2 config-validate guard (belt-and-suspenders — see
# doctrine "Scope fence" in the stage-2 design). Stage 2's per-uid
# filesystem isolation stops a dropped engagement uid from `open()`-ing a
# sibling's workspace, but it does NOT stop an executor from asking
# casa-core (root) to do that read/act FOR it via a bridge tool that
# accepts a caller-controlled ``engagement_id`` —
# ``peek_engagement_workspace``, ``list_engagement_workspaces``,
# ``delete_engagement_workspace`` — or via the whole-server
# ``mcp__casa-framework`` grant, which the v0.166 bridge grant-gate
# (``engagement_casa_grant_names``, tools.py) treats as "any casa tool".
# No shipped executor grants any of these today; this set exists purely so
# a future definition.yaml edit can't silently reopen the confused-deputy
# path. Bare server-level grant plus the three admin/workspace tools —
# exact tokens, no prefix matching (a real per-tool grant like
# ``mcp__casa-framework__query_engager`` must never trip this).
_FORBIDDEN_EXECUTOR_GRANTS: frozenset[str] = frozenset({
    "mcp__casa-framework",
    "mcp__casa-framework__peek_engagement_workspace",
    "mcp__casa-framework__list_engagement_workspaces",
    "mcp__casa-framework__delete_engagement_workspace",
})


class LoadError(Exception):
    """Raised on any per-agent load failure."""


# --- Schema cache ----------------------------------------------------------


_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def _load_schema(name: str, version: str = "v1") -> dict[str, Any]:
    cache_key = f"{name}.{version}"
    if cache_key not in _SCHEMA_CACHE:
        path = os.path.join(SCHEMA_DIR, f"{cache_key}.json")
        with open(path, "r", encoding="utf-8") as fh:
            _SCHEMA_CACHE[cache_key] = json.load(fh)
    return _SCHEMA_CACHE[cache_key]


def _validate(
    data: dict[str, Any],
    schema_name: str,
    source: str,
    *,
    version: str = "v1",
) -> None:
    schema = _load_schema(schema_name, version)
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        # Surface the file name and field path for debuggability. The
        # validator's `exc.message` is terse; `exc.absolute_path` names the
        # offending key when available.
        where = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        raise LoadError(
            f"{source}: schema violation at {where}: {exc.message}"
        ) from exc


def _without_inert_webhook_prose(data: Any, source: str) -> Any:
    """*data* with `prompt`/`prompt_file` removed from every `type: webhook`
    entry, warning once per entry it strips. Returns *data* unchanged when
    there is nothing to strip.

    #608. A webhook turn is built from the trigger name and the request
    payload; these fields are never delivered (:func:`_build_triggers` sets
    `prompt_text = ""` for a webhook). The schema now REFUSES them, which is
    what makes `config_trigger_upsert` fail loudly instead of persisting a
    field nothing reads — but that refusal must not reach a document already
    on disk. Such documents exist: the typed tool offered the field and the
    old schema accepted it, so an install can be carrying one right now, and
    for it the refusal would be **boot-fatal** (`_validate` raises `LoadError`,
    `load_all_agents` propagates the first one, and a resident load failure
    takes the whole boot down with it). Worse, `config_sync`'s entry salvage
    drops an entry that fails validation unless the image ships it — so the
    operator's webhook trigger would silently DISAPPEAR on upgrade.

    Copies rather than mutates. `config_sync`'s entry merge re-emits the very
    document it validated, so mutating here would rewrite the operator's file
    as a side effect of reading it.

    This is the same shape as `path` on a webhook, which is deprecated,
    ignored, and warned about at load rather than rejected out of an existing
    document (:func:`_normalize_webhook_auth`).
    """
    triggers = data.get("triggers") if isinstance(data, dict) else None
    if not isinstance(triggers, list):
        return data

    def _inert(entry: object) -> bool:
        return (isinstance(entry, dict) and entry.get("type") == "webhook"
                and ("prompt" in entry or "prompt_file" in entry))

    if not any(_inert(t) for t in triggers):
        return data
    for t in triggers:
        if _inert(t):
            logger.warning(
                "%s: webhook trigger %r carries a prompt, which a webhook turn "
                "never receives — its turn is built from the trigger name and "
                "the request payload. The field is ignored; remove it by "
                "re-running config_trigger_upsert for this trigger without it.",
                source, t.get("name", "?"),
            )
    cleaned = dict(data)
    # The predicate is re-applied per entry rather than testing membership of a
    # precomputed hit list: `in` on a list of dicts compares by VALUE, so two
    # structurally identical entries would answer for each other. That happens
    # to be harmless here (equal entries need the same treatment), but it makes
    # the strip's correctness depend on a coincidence rather than on the rule.
    cleaned["triggers"] = [
        {k: v for k, v in t.items() if k not in ("prompt", "prompt_file")}
        if _inert(t) else t
        for t in triggers
    ]
    return cleaned


def validate_persisted(
    data: dict[str, Any], schema_name: str, source: str, *, version: str = "v1",
) -> None:
    """:func:`_validate` for a document being READ from disk.

    The one seam every reader of a TRIGGER document uses — the resident loader,
    the pre-commit gate's agents walk, and both `config_sync` validators — so
    the tolerance cannot be applied in one place and forgotten in another
    (#608; `config_sync` was the pair this fix originally missed, and missing
    it meant silently deleting an operator's trigger on upgrade). The gate's
    `policies/` walk still calls :func:`_validate` directly and is correct to:
    that map never yields the triggers schema.

    Writers deliberately do NOT come through here. `reminders._schema_error`
    calls :func:`_validate` directly, so a NEW entry is judged by the strict
    schema. Tolerance is a property of reading what is already on disk, never
    of judging what is about to be written — unify the two and the refusal
    this exists to enable stops happening at all.
    """
    _validate(_without_inert_webhook_prose(data, source) if schema_name == "triggers"
              else data, schema_name, source, version=version)


# --- Pre-commit schema gate (E-G v0.31.0) ----------------------------------

# Maps a schema-bearing YAML filename in ``agents/<role>/`` to the schema name
# in defaults/schema/. Used by ``validate_config_repo`` as a pre-commit gate.
# Files outside this map (markdown doctrine, plugin sources, READMEs, etc.)
# are skipped.
_SCHEMA_BY_FILENAME: dict[str, str] = {
    "character.yaml":      "character",
    "voice.yaml":          "voice",
    "response_shape.yaml": "response_shape",
    "runtime.yaml":        "runtime",
    "disclosure.yaml":     "disclosure",
    "delegates.yaml":      "delegates",
    "executors.yaml":      "executors",
    "triggers.yaml":       "triggers",
    "hooks.yaml":          "hooks",
    "definition.yaml":     "executor",
}

# Maps a filename in ``policies/`` to (schema_name, version). Separate from
# ``_SCHEMA_BY_FILENAME`` because ``policies/disclosure.yaml`` reuses the
# same basename as ``agents/<role>/disclosure.yaml`` while binding to a
# DIFFERENT schema (``policy-disclosure.v1.json`` vs ``disclosure.v1.json``).
_SCHEMA_BY_POLICY_FILE: dict[str, tuple[str, str]] = {
    "disclosure.yaml": ("policy-disclosure", "v1"),
}

# Non-resident subtrees at the agents/ root: Tier 2 specialists and Tier 3
# executors, each loaded on its own isolated, boot-non-fatal path. Shared by
# ``load_all_agents`` and ``validate_config_repo``'s boot-parity replay so
# the two skip sets cannot drift (#213).
_NON_RESIDENT_AGENT_DIRS: tuple[str, ...] = ("specialists", "executors")


def validate_config_repo(
    config_dir: str, *, roles_dir: str | None = None,
) -> list[str]:
    """E-G v0.31.0 pre-commit gate. Walk schema-bearing YAML in
    *config_dir/agents/* and *config_dir/policies/* and return a list of
    error messages — one per file that fails schema validation. Empty
    list = all clean.

    Used by ``mcp__casa-framework__config_git_commit`` to refuse commits
    that would land schema-invalid YAML and FATAL the addon on next boot.
    The check uses the same ``_validate`` codepath as boot-time loading,
    so a passing pre-commit gate guarantees a green boot validation for
    every file the configurator can edit.

    ``roles_dir`` — see ``load_agent_from_dir``; propagated to the
    boot-parity replay below (both the resident and specialist loads).

    **Agents/ walk.** Recursive; dot-dirs (``.git/``, the specialist
    pipeline's ``.{slug}.material-*`` content dirs) and the top-level
    ``specialists/`` subtree skipped (#213 — see the scope-boundary note
    in the boot-parity pass below). Basename lookup in
    ``_SCHEMA_BY_FILENAME``. The original E-G repro path is the
    configurator inventing fields under ``agents/<role>/character.yaml``.

    **Policies/ walk (v0.37.12).** Top-level only; the configurator's
    doctrine lists ``policies/disclosure.yaml`` as editable. Path-aware
    lookup in ``_SCHEMA_BY_POLICY_FILE`` —
    ``policies/disclosure.yaml`` reuses the basename of the per-agent
    file but binds to ``policy-disclosure.v1.json``; v0.31.0's flat
    basename map mis-applied the agent schema there and falsely refused
    every commit (closed in v0.31.1 by scoping agents/ only, which left
    the policies/ blast radius open until v0.37.12).

    Skips dot-dirs, the pipeline-managed ``specialists/`` subtree, and
    any file whose basename is not in either map (markdown, plain text,
    etc.).
    """
    errors: list[str] = []

    agents_root = os.path.join(config_dir, "agents")
    if os.path.isdir(agents_root):
        for root, dirs, files in os.walk(agents_root):
            # Dot-dirs are pruned at EVERY level: ``.git/``, plus the
            # specialist pipeline's ``.{slug}.material-*`` content dirs
            # (specialist_materialize) under agents/specialists/.
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            # #213: specialists/ is pipeline-managed territory — the
            # digest-aware load_all_specialists replay below is its sole
            # validation authority (per-specialist failures are
            # boot-non-fatal and deliberately not gate errors). Walking it
            # here double-validates the materialized files and false-flags
            # them. executors/ deliberately STAYS in the walk: it schema-
            # validates default-named executor hooks.yaml files by basename
            # (custom-named ``hooks_file:`` targets are covered by the #312
            # pointer pass below; load_all_executors validates both at boot
            # via the same _resolve_executor_hooks).
            if root == agents_root and "specialists" in dirs:
                dirs.remove("specialists")
            for name in files:
                schema_name = _SCHEMA_BY_FILENAME.get(name)
                if schema_name is None:
                    continue
                # #312 (Terra r2-P1): executor hooks documents are owned by
                # the pointer pass below, which runs the FULL boot-parity
                # resolver (schema + factory constructibility) for default
                # and custom names alike. Validating them here too would
                # double-report every schema failure.
                if name == "hooks.yaml" and (
                    root == os.path.join(agents_root, "executors")
                    or root.startswith(
                        os.path.join(agents_root, "executors") + os.sep)
                ):
                    continue
                path = os.path.join(root, name)
                try:
                    # #608: the gate's contract is that passing it guarantees a
                    # green boot validation, so it must read with exactly the
                    # tolerance boot reads with — one helper, not two.
                    validate_persisted(_read_yaml(path), schema_name, path)
                except LoadError as exc:
                    errors.append(str(exc))

    # #312 gate parity (Terra r1-P2 / r2-P1): boot fail-closes on a hooks
    # pointer whose target is absent, schema-invalid, or non-constructible
    # (factory-refused params), so the gate must refuse those commits too
    # (the recurring validate-weaker-than-boot family). This pass runs the
    # SAME resolver boot uses, for default and custom names alike — the
    # basename walk above skips executor hooks.yaml files so nothing
    # double-reports. Broad except (Terra r2-P2): the gate's mandate is
    # report-never-crash.
    executors_root = os.path.join(agents_root, "executors")
    try:
        executor_entries = (
            sorted(os.listdir(executors_root))
            if os.path.isdir(executors_root) else []
        )
    except OSError as exc:  # Sol r2-3: report, never crash
        executor_entries = []
        errors.append(f"agents/executors unreadable: {exc}")
    if executor_entries:
        for entry in executor_entries:
            exec_dir = os.path.join(executors_root, entry)
            defn_path = os.path.join(exec_dir, "definition.yaml")
            if not os.path.isdir(exec_dir):
                continue
            defn: dict = {}
            if os.path.isfile(defn_path):
                try:
                    defn = _read_yaml(defn_path)
                except LoadError:
                    continue  # malformed definition — surfaced by the walk
                if not isinstance(defn, dict):
                    continue
            # Containment Stage 2 drift guard (belt-and-suspenders — see
            # _FORBIDDEN_EXECUTOR_GRANTS above). Checked against the RAW
            # declared tools.allowed, independent of the role-ceiling clamp
            # load_all_executors applies at boot: this is a build-time gate
            # on the committed config, not a runtime capability check, and
            # must fail closed even if a role artifact would have dropped
            # the grant silently. Every executor is in scope, including the
            # configurator — it does not hold any of these tools today (see
            # defaults/agents/executors/configurator/definition.yaml).
            declared_tools = defn.get("tools") or {}
            if isinstance(declared_tools, dict):
                declared_allowed = declared_tools.get("allowed") or []
            else:
                declared_allowed = []
            if isinstance(declared_allowed, list):
                forbidden_hits = sorted(
                    set(declared_allowed) & _FORBIDDEN_EXECUTOR_GRANTS)
                if forbidden_hits:
                    errors.append(
                        f"executor {entry!r}: definition.yaml tools.allowed "
                        f"grants {forbidden_hits} — the server-level "
                        f"mcp__casa-framework grant and the "
                        f"*_engagement_workspace bridge tools accept a "
                        f"caller-controlled engagement_id and let "
                        f"casa-core (root) read/act on ANY engagement's "
                        f"workspace; Stage 2's per-uid filesystem isolation "
                        f"does not close this confused-deputy path "
                        f"(containment stage 2 design, 'Scope fence'), so "
                        f"no executor may hold them"
                    )

            # A dir WITHOUT definition.yaml fails to load as an executor at
            # boot anyway, but a present default-named hooks.yaml still gets
            # schema-gated here (defn = {} resolves the default pointer) —
            # the walk skips executor hooks.yaml, so this pass is their sole
            # gate.
            try:
                present = {
                    f for f in os.listdir(exec_dir)
                    if os.path.isfile(os.path.join(exec_dir, f))
                }
                _resolve_executor_hooks(exec_dir, entry, defn, present)
                # Task 3 floor check is deliberately NOT enforced here — this
                # pass only validates the hooks POINTER (Terra r1-P2 parity);
                # the containment floor is a load_all_executors gate (#360).
            except LoadError as exc:
                errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001 — report, never crash
                errors.append(
                    f"executor {entry!r}: hooks pointer validation failed: "
                    f"{exc}"
                )

    policies_root = os.path.join(config_dir, "policies")
    if os.path.isdir(policies_root):
        for name in os.listdir(policies_root):
            mapping = _SCHEMA_BY_POLICY_FILE.get(name)
            if mapping is None:
                continue
            path = os.path.join(policies_root, name)
            if not os.path.isfile(path):
                continue
            schema_name, version = mapping
            try:
                _validate(_read_yaml(path), schema_name, path, version=version)
            except LoadError as exc:
                errors.append(str(exc))

    # --- Boot-parity pass (M5) --------------------------------------------
    # The per-file schema walks above cannot see the CROSS-FILE invariants
    # boot's load_all_agents enforces and FATALs on: character.yaml role ==
    # directory name, the tier file-set rules (unknown/forbidden files),
    # executors.yaml assistant-only, and delegates.yaml -> the delegate MCP
    # tool in runtime tools.allowed. A commit can pass every per-file schema
    # yet crash-loop the add-on on the next boot (e.g. a copied resident dir
    # still declaring role: assistant). Exercise the real resident loader so
    # the gate refuses those commits too.
    if os.path.isdir(agents_root):
        from policies import load_policies  # local import avoids a cycle

        # Faithfully replay boot's policy load. casa_core.main (line ~1245)
        # runs, UNGUARDED and before load_all_agents:
        #     policy_lib = load_policies(policies/disclosure.yaml)   # RAISES
        #     role_configs = load_all_agents(agents_dir, policies=policy_lib)
        # A MISSING policy file makes load_policies raise PolicyError
        # (policies.py:68-69) and crash-loops the add-on; the policies/
        # schema walk above cannot see that because it guards on isfile.
        # Report the absence here so the gate refuses such a commit. A
        # MALFORMED policy file, by contrast, is already surfaced by the
        # schema walk, so don't double-count it.
        policy_lib = None
        policy_path = os.path.join(config_dir, "policies", "disclosure.yaml")
        if os.path.isfile(policy_path):
            try:
                policy_lib = load_policies(policy_path)
            except Exception:
                # A malformed policy library is already surfaced by the
                # policies/ schema walk above; don't double-count it here.
                policy_lib = None
        else:
            errors.append(
                f"policies/disclosure.yaml not found at {policy_path} — "
                f"boot's load_policies raises PolicyError and crash-loops "
                f"the add-on when a config carrying agents is committed "
                f"without a policy library"
            )
        resident_configs: dict[str, AgentConfig] = {}
        resident_dir_names: set[str] = set()
        for entry in sorted(os.listdir(agents_root)):
            # Mirror load_all_agents' skip set exactly (specialists/executors
            # are loaded on isolated, boot-non-fatal paths; dotdirs skipped).
            if entry.startswith(".") or entry in _NON_RESIDENT_AGENT_DIRS:
                continue
            path = os.path.join(agents_root, entry)
            if not os.path.isdir(path):
                # Boot's load_all_agents RAISES on any non-directory at
                # agents/ root (each agent is a directory); a silent skip
                # here would let such a commit pass the gate yet crash-loop
                # the add-on on next boot. Report it, matching boot's fatal.
                errors.append(
                    f"unexpected non-directory at agents/{entry} — each "
                    f"agent is a directory; flat YAML files are no longer "
                    f"supported"
                )
                continue
            # Directory presence == boot role presence (role == dir name is
            # enforced on load). Tracked before the load attempt so an
            # agents/assistant/ dir that FAILS to load still counts as "an
            # assistant dir exists" — its own load error is authoritative, and
            # the primary-assistant check below must not pile a derivative
            # "no assistant" onto it.
            resident_dir_names.add(entry)
            try:
                # binding_commit=False (#338): this is a VALIDATION replay —
                # the resident binding reconciliation must not commit a
                # staged desired -> active (activating a pending persona
                # swap) as a side effect of validating an unrelated commit.
                cfg = load_agent_from_dir(
                    path, policies=policy_lib, roles_dir=roles_dir,
                    binding_commit=False,
                )
            except Exception as exc:
                # Broad by design (mandate: 'a validation error must be
                # reported, never itself crash the gate'). load_agent_from_dir
                # raises NON-LoadError on 100%-schema-valid input the boot
                # loader ALSO fatals on: resolve_model raises ValueError for an
                # unknown runtime.yaml model shortname (a free string), and
                # _compose_prompt -> policies.resolve raises PolicyError when a
                # resident disclosure.yaml names a policy absent from
                # policies/disclosure.yaml (also a free string). Catching only
                # LoadError here let those escape and crash the gate; mirror the
                # defensive except-Exception block used for the registry build
                # below so every boot-fatal is reported instead.
                msg = str(exc)
                # When no policy library is present, the ROOT cause (a missing
                # or malformed policies/disclosure.yaml) is already reported
                # above — as the "not found" error or by the policies/ schema
                # walk. Each resident with a disclosure.yaml then raises a
                # derivative "no PolicyLibrary" compose cascade; suppress only
                # that derivative so the single authoritative error stands
                # without N duplicates.
                if policy_lib is None and "no PolicyLibrary was passed" in msg:
                    continue
                if msg not in errors:
                    errors.append(msg)
                continue
            resident_configs[cfg.role] = cfg

        # Primary-assistant invariant (M5). casa_core.main (line ~1306) RAISES
        # RuntimeError "No agent with role 'assistant' found ... Casa cannot
        # start without a primary assistant" when role_configs — the resident
        # set load_all_agents returns — holds no 'assistant'. A committed tree
        # whose only resident is non-assistant (e.g. butler), whose agents/ dir
        # is empty, or whose sole assistant-ish entry is a DISABLED specialist
        # (dropped before the registry) passes every per-file schema check yet
        # crash-loops the add-on here. Reuse the resident dir set the parity
        # loop already walked instead of re-deriving with a different skip set.
        # Key on directory presence, not resident_configs: an agents/assistant/
        # dir that failed to load is reported by its own authoritative error
        # above, and keying on the load result would pile a derivative "no
        # assistant" onto it. Suppress when policy_lib is None — a
        # missing/malformed policy library is the single authoritative
        # boot-crash there (reported above) and makes every disclosure-bearing
        # resident fail to load as a derivative of it.
        #
        # Scope boundary (Finding 2): validate_config_repo sees ONLY the
        # committed tree under config_dir. It does NOT — and cannot — simulate
        # config_sync's post-commit re-injection of image-owned defaults. E.g.
        # a committed deletion of the image-owned agents/assistant/delegates.yaml
        # is internally valid here (no delegates -> the delegate MCP tool is not
        # required), and the boot mismatch surfaces only because config_sync
        # restores that image-owned file after the commit. That reconciler
        # backstop lives in config_sync, not in this gate.
        #
        # The same boundary governs the per-file walk above (#213): the
        # specialist subtree (including its dot-named ``.{slug}.material-*``
        # content dirs) is pipeline-managed and validated by the digest-aware
        # load_all_specialists replay below — per-file schema validation there
        # would double-validate the materialized files and false-flag them.
        # executors/ is the deliberate exception and stays in the walk: its
        # hooks.yaml has no other schema gate.
        if policy_lib is not None:
            for slot in FIXED_RESIDENT_SLOTS:
                if slot in resident_dir_names:
                    continue
                if slot == "assistant":
                    errors.append(
                        "no enabled resident with role 'assistant' — Casa "
                        "cannot boot without a primary assistant "
                        "(agents/assistant/ must exist and declare "
                        "role: assistant in runtime.yaml)"
                    )
                else:
                    # #324: boot's load_all_agents enforces the FULL fixed
                    # resident set (Step 9) and raises LoadError when a slot
                    # is missing — a commit deleting agents/butler/ must be
                    # refused here too, or a warm `agents` reload fails until
                    # config-sync/restart restores the tree. Stray EXTRA
                    # resident dirs need no twin check: the replay loop above
                    # already reports them (validate_role_shape rejects any
                    # resident slot outside the fixed three).
                    errors.append(
                        f"missing resident agents/{slot}/ — boot enforces "
                        f"the fixed resident set {FIXED_RESIDENT_SLOTS} and "
                        f"fails to load without it"
                    )

        # #338: boot registers every resident's triggers UNCAUGHT
        # (casa_core step 13b) — TriggerRegistry.register_agent raises
        # TriggerError on a duplicate name / unregistered channel, and
        # APScheduler's add_job raises on out-of-range cron field values;
        # the triggers schema alone catches none of that (no uniqueItems,
        # channel and schedule are free-ish strings). Replay the SAME
        # registration into a throwaway registry backed by an unstarted
        # scheduler: add_job validates trigger construction identically
        # while nothing is started and nothing can fire (bus/app unused
        # until a job actually runs). Defensive wrapper: the gate's mandate
        # is report-never-crash.
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from trigger_registry import TriggerError, TriggerRegistry

            replay_registry = TriggerRegistry(
                scheduler=BackgroundScheduler(), app=None, bus=None,
            )
            for role, cfg in resident_configs.items():
                if not cfg.triggers:
                    continue
                try:
                    replay_registry.register_agent(
                        role=role, triggers=cfg.triggers, channels=cfg.channels,
                    )
                except TriggerError as exc:
                    errors.append(str(exc))
                except Exception as exc:  # noqa: BLE001 — apscheduler ValueError etc.
                    errors.append(
                        f"agent {role!r}: trigger registration failed: {exc}"
                    )
        except Exception:  # noqa: BLE001 — replay harness itself must never crash the gate
            logger.warning(
                "trigger-registration replay unavailable; gate ran without it",
                exc_info=True,
            )

        # Cross-tier role-registry parity. After load_all_agents, casa_core.main
        # (line ~1280) builds the merged role→config registry, UNGUARDED:
        #     _build_role_registry(residents=role_configs,
        #                          specialists=specialist_registry.all_configs())
        # which RAISES ValueError 'duplicate role(s) across residents and
        # specialists' when a role exists in BOTH tiers (e.g. an agents/<r>/
        # resident plus an enabled agents/specialists/<r>/). That build runs
        # after every try/except in main(), so the ValueError is uncaught and
        # crash-loops the add-on; config_sync does NOT heal it (both dirs are
        # kept). The default image ships agents/specialists/finance/, so a
        # configurator creating a finance resident trips it. Replay the real
        # specialist load + registry build so the gate refuses the collision.
        specialist_configs: dict[str, AgentConfig] = {}
        specialists_dir = os.path.join(agents_root, "specialists")
        try:
            spec_found, _spec_failed = load_all_specialists(
                specialists_dir, roles_dir=roles_dir,
            )
        except LoadError:
            # A collection-level specialist error (non-directory under
            # specialists/) is boot-non-fatal: SpecialistRegistry.load catches
            # it and boots with zero specialists. Mirror that — do not report,
            # and treat the enabled specialist set as empty.
            spec_found = {}
        for role, spec_cfg in spec_found.items():
            # Mirror SpecialistRegistry.load's enablement + Tier-2 shape filter:
            # disabled or tier-2-invalid specialists never reach all_configs(),
            # so they cannot collide at boot. Over-reporting them would falsely
            # refuse a bootable repo.
            if not spec_cfg.enabled:
                continue
            if spec_cfg.channels:
                continue
            if spec_cfg.session.strategy != "ephemeral":
                continue
            specialist_configs[role] = spec_cfg

        try:
            from casa_core import _build_role_registry  # local: heavy module
            _build_role_registry(
                residents=resident_configs, specialists=specialist_configs,
            )
        except ValueError as exc:
            # Duplicate role across the two tiers — boot's uncaught crash.
            msg = str(exc)
            if msg not in errors:
                errors.append(msg)
        except Exception:
            # Defensive: the gate must NEVER crash. If casa_core cannot be
            # imported in this context, fall back to the pure overlap invariant
            # so the collision is still caught.
            overlap = sorted(set(resident_configs) & set(specialist_configs))
            if overlap:
                msg = (
                    f"duplicate role(s) across residents and specialists: "
                    f"{overlap} — each role must be unique"
                )
                if msg not in errors:
                    errors.append(msg)

    return errors


# --- File reader -----------------------------------------------------------


def parse_yaml_text(text: str, source: str) -> dict[str, Any]:
    """Parse *text* as the loader does: the parse decides the shape, ``${VAR}``
    is resolved as each scalar is built.

    The order is the point (#409). Substituting into the text and parsing
    afterwards hands the parser whatever the variable contains, so a value with
    a ``#``, a quote or a newline silently changes the document or stops it
    loading — for a resident, stopping boot. See ``config._EnvSafeLoader``,
    which also explains why a quoted placeholder is the form that gets the
    guarantee.

    Exposed rather than private because everything that must agree with the
    loader about what a file MEANS has to run this exact pipeline: the
    entry-level reconciler's text validator and the reminder writer both kept
    their own ``_substitute_env``-then-parse copy, which is the bug.

    Every failure folds into ``LoadError``, not just ``yaml.YAMLError``: PyYAML's
    own constructors raise plain ``ValueError``/``KeyError`` for an explicitly
    tagged scalar they cannot build (``at: !!int nope``, or a ``!!timestamp``
    naming month 99), and a document nested past the parser's own limit raises
    ``RecursionError``. Every caller here catches ``LoadError``; none of them
    catches those, so leaving them unfolded means an unreadable file aborts
    whatever pass is running — at boot, the process.
    """
    try:
        return load_yaml_with_env(text) or {}
    except (Exception, RecursionError) as exc:  # noqa: BLE001 — see above
        raise LoadError(f"{source}: YAML parse error: {exc}") from exc


def _read_yaml(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return parse_yaml_text(text, path)


def _declared_kind(runtime_data: dict[str, Any], source: str) -> str:
    """Personality Phase A, Task 6: replaces the former ``_infer_tier``
    channels-presence guess. ``runtime.yaml``'s ``kind`` field (added by
    Task 6's revised ``runtime.v1.json``) is now the explicit, authoritative
    source for which ``defaults/roles/<kind>/<slot>/`` directory this agent's
    canonical role artifact is loaded from — no more inference from whether
    ``channels`` happens to be non-empty."""
    kind = runtime_data.get("kind")
    if kind not in {"resident", "specialist", "executor"}:
        raise LoadError(f"{source}: kind is required and must be resident, specialist, or executor")
    return kind


# --- Canonical role artifact loading (Personality Phase A, Task 5) --------
#
# The image-owned canonical layout is defaults/roles/<kind>/<slot>/. This is
# a SEPARATE tree from the per-agent-directory operational files above
# (character.yaml, runtime.yaml, prompts/system.md, ...) — those remain
# legacy operational inputs through Phase A and are never read as
# role-artifact inputs. Task 6 removes the transitional tier-inference
# above and consumes RoleArtifactSource for model resolution and the role
# checksum; Task 5 only wires the loader seam and cross-validates that the
# artifact found on disk actually matches the directory it was loaded for.


def _load_role_artifact_for(
    tier: str, role_from_path: str, roles_dir: str | None,
) -> RoleArtifactSource:
    """Load and cross-validate the canonical role artifact for one resident
    or specialist agent directory (``tier`` is ``resident``/``specialist``
    here; executors use ``_load_executor_role_artifact`` below)."""
    base = roles_dir or DEFAULT_ROLES_DIR
    role_dir = Path(base) / tier / role_from_path
    try:
        artifact = load_role_artifact(role_dir)
    except (OSError, ValueError, yaml.YAMLError,
            jsonschema.exceptions.ValidationError) as exc:
        raise LoadError(
            f"agent {role_from_path!r} ({tier}): role artifact load failed "
            f"at {role_dir}: {exc}"
        ) from exc
    found_kind = artifact.role.get("kind")
    if found_kind != tier:
        raise LoadError(
            f"agent {role_from_path!r}: role artifact kind {found_kind!r} "
            f"at {role_dir} does not match directory tier {tier!r}"
        )
    found_slot = artifact.role.get("slot")
    if found_slot != role_from_path:
        raise LoadError(
            f"agent {role_from_path!r}: role artifact slot {found_slot!r} "
            f"at {role_dir} does not match directory name {role_from_path!r}"
        )
    expected_id = f"{tier}:{role_from_path}"
    if artifact.role.get("id") != expected_id:
        raise LoadError(
            f"agent {role_from_path!r}: role artifact id "
            f"{artifact.role.get('id')!r} at {role_dir} does not match "
            f"expected {expected_id!r}"
        )
    return artifact


def _load_executor_role_artifact(
    entry: str, roles_dir: str | None,
) -> RoleArtifactSource:
    """Load and cross-validate the canonical role artifact for one executor
    type directory. Executors are checked more strictly (id, kind, AND
    slot) since ``load_all_executors`` has no separate directory-name
    cross-check elsewhere in its per-entry loop the way residents/
    specialists get from ``character.yaml``'s role field."""
    base = roles_dir or DEFAULT_ROLES_DIR
    role_dir = Path(base) / "executor" / entry
    try:
        artifact = load_role_artifact(role_dir)
    except (OSError, ValueError, yaml.YAMLError,
            jsonschema.exceptions.ValidationError) as exc:
        raise LoadError(
            f"executor {entry!r}: role artifact load failed at {role_dir}: {exc}"
        ) from exc
    role = artifact.role
    if role.get("kind") != "executor":
        raise LoadError(
            f"executor {entry!r}: role artifact kind {role.get('kind')!r} "
            f"at {role_dir} must be 'executor'"
        )
    if role.get("slot") != entry:
        raise LoadError(
            f"executor {entry!r}: role artifact slot {role.get('slot')!r} "
            f"at {role_dir} does not match executor type {entry!r}"
        )
    expected_id = f"executor:{entry}"
    if role.get("id") != expected_id:
        raise LoadError(
            f"executor {entry!r}: role artifact id {role.get('id')!r} at "
            f"{role_dir} does not match expected {expected_id!r}"
        )
    return artifact


def _check_file_set(agent_dir: str, tier: str, role: str) -> None:
    rules = TIER_FILES[tier]
    required = rules["required"]
    optional = rules["optional"]
    forbidden = rules["forbidden"]

    on_disk: set[str] = set()
    for entry in os.listdir(agent_dir):
        if entry.startswith("."):
            continue  # dotfiles skipped per spec
        if _is_editor_backup(entry):
            continue  # S-1: editor backups (.bak/.swp/.tmp/.orig/~)
        if os.path.isdir(os.path.join(agent_dir, entry)):
            continue  # subdirectories are not config files (e.g. prompts/)
        on_disk.add(entry)

    missing = required - on_disk
    if missing:
        raise LoadError(
            f"agent {role!r} ({tier}): missing required file(s): "
            f"{sorted(missing)}",
        )

    present_forbidden = forbidden & on_disk
    if present_forbidden:
        raise LoadError(
            f"agent {role!r} ({tier}): forbidden file(s) present: "
            f"{sorted(present_forbidden)}",
        )

    allowed = required | optional
    unknown = on_disk - allowed
    if unknown:
        raise LoadError(
            f"agent {role!r} ({tier}): unknown file(s) in directory: "
            f"{sorted(unknown)}. Hint: editor backups "
            f"(.bak/.swp/.tmp/.orig/~) are tolerated; remove other "
            f"unknown files or restore the directory from git.",
        )


# --- Section renderers -----------------------------------------------------


def _render_voice_section(voice: VoiceConfig) -> str:
    lines = ["### Voice", ""]
    if voice.tone:
        lines.append("Tone: " + ", ".join(voice.tone))
    lines.append(f"Cadence: {voice.cadence}")
    if voice.forbidden_patterns:
        lines.append("Avoid: " + ", ".join(voice.forbidden_patterns))
    if voice.signature_phrases:
        lines.append("Signature phrases:")
        for k, v in voice.signature_phrases.items():
            lines.append(f"  - {k}: \"{v}\"")
    return "\n".join(lines).rstrip() + "\n"


def _render_response_shape_section(rs: ResponseShapeConfig) -> str:
    lines = ["### Response shape", ""]
    lines.append(f"Register: {rs.register}")
    lines.append(f"Format: {rs.format}")
    lines.append(f"Max sentences (confirmation): {rs.max_sentences_confirmation}")
    lines.append(f"Max sentences (status): {rs.max_sentences_status}")
    if rs.rules:
        lines.append("Rules:")
        for r in rs.rules:
            lines.append(f"  - {r}")
    return "\n".join(lines).rstrip() + "\n"


def _render_delegates_section(delegates: list[DelegateEntry]) -> str:
    if not delegates:
        return ""
    lines = ["### Delegation", ""]
    lines.append("You may delegate to the following specialists:")
    for d in delegates:
        lines.append(f"  - {d.agent}: {d.purpose} — when: {d.when}")
    lines.append("")
    lines.append(
        "Invoke via mcp__casa-framework__delegate_to_agent(agent=..., task=...)."
    )
    return "\n".join(lines).rstrip() + "\n"


# --- Per-file builders -----------------------------------------------------


def _resolve_prose(
    data: dict[str, Any],
    *,
    field: str,
    agent_dir: str,
    source_label: str,
) -> str:
    """Return either the inline ``<field>`` string or the contents of the
    markdown file referenced by ``<field>_file`` (relative to agent_dir).

    JSON Schema enforces exactly-one-of, so at runtime we trust that
    constraint; the defensive check here surfaces a clearer error if the
    schema ever drifts. Applies ``_substitute_env`` so external prompts
    see the same env-var substitutions as inline YAML strings.
    """
    inline = data.get(field)
    file_ref = data.get(f"{field}_file")

    if inline is not None and file_ref is not None:
        raise LoadError(
            f"{source_label}: both {field!r} and {field}_file set — "
            f"exactly one must be provided"
        )
    if inline is not None:
        return inline
    if file_ref is None:
        return ""

    if os.path.isabs(file_ref):
        raise LoadError(
            f"{source_label}: {field}_file must be relative to agent dir, "
            f"got {file_ref!r}"
        )

    resolved = os.path.realpath(os.path.join(agent_dir, file_ref))
    agent_dir_abs = os.path.realpath(agent_dir)
    try:
        common = os.path.commonpath([agent_dir_abs, resolved])
    except ValueError:
        common = ""
    if common != agent_dir_abs:
        raise LoadError(
            f"{source_label}: {field}_file {file_ref!r} escapes agent dir"
        )
    if not resolved.endswith(".md"):
        raise LoadError(
            f"{source_label}: {field}_file must end in .md, got {file_ref!r}"
        )
    if not os.path.exists(resolved):
        raise LoadError(
            f"{source_label}: {field}_file not found at {resolved}"
        )

    with open(resolved, "r", encoding="utf-8") as fh:
        return _substitute_env(fh.read())


def _build_character(data: dict[str, Any], *, agent_dir: str) -> CharacterConfig:
    return CharacterConfig(
        name=data["name"],
        archetype=data["archetype"],
        card=_resolve_prose(
            data, field="card", agent_dir=agent_dir,
            source_label="character.yaml",
        ),
        prompt=_resolve_prose(
            data, field="prompt", agent_dir=agent_dir,
            source_label="character.yaml",
        ),
    )


def _build_voice(data: dict[str, Any]) -> VoiceConfig:
    return VoiceConfig(
        tone=list(data.get("tone") or []),
        cadence=data.get("cadence", "natural"),
        forbidden_patterns=list(data.get("forbidden_patterns") or []),
        signature_phrases=dict(data.get("signature_phrases") or {}),
    )


def _build_response_shape(data: dict[str, Any]) -> ResponseShapeConfig:
    return ResponseShapeConfig(
        max_sentences_confirmation=data.get("max_sentences_confirmation", 2),
        max_sentences_status=data.get("max_sentences_status", 3),
        register=data.get("register", "written"),
        format=data.get("format", "plain"),
        rules=list(data.get("rules") or []),
    )


def _build_disclosure(data: dict[str, Any]) -> DisclosureConfig:
    return DisclosureConfig(
        policy=data["policy"],
        overrides=dict(data.get("overrides") or {}),
    )


def _build_delegates(data: dict[str, Any]) -> list[DelegateEntry]:
    return [
        DelegateEntry(agent=e["agent"], purpose=e["purpose"], when=e["when"])
        for e in (data.get("delegates") or [])
    ]


def _build_executors(data: dict[str, Any]) -> list[ExecutorEntry]:
    return [
        ExecutorEntry(
            executor_type=e["executor_type"],
            purpose=e["purpose"],
            when=e["when"],
        )
        for e in (data.get("executors") or [])
    ]


def _build_executor_memory(block: dict[str, Any]) -> "ExecutorMemoryConfig":
    """Build an ExecutorMemoryConfig from the parsed `memory:` block.

    Empty block -> default-disabled. Schema validation has already
    enforced the shape before this is called.
    """
    if not block:
        return ExecutorMemoryConfig()
    return ExecutorMemoryConfig(
        enabled=bool(block.get("enabled", False)),
        token_budget=int(block.get("token_budget", 2000)),
    )


logger = logging.getLogger(__name__)

# Default signature header per auth mode (spec A1).
_DEFAULT_AUTH_HEADER = {
    "hmac_body": "X-Webhook-Signature",
    "static_header": "X-API-Key",
    "timestamped_hmac": "ElevenLabs-Signature",
}


def _normalize_webhook_auth(trig: dict[str, Any], trig_name: str) -> dict[str, Any]:
    """Return a fully-defaulted auth policy for a webhook trigger.

    An absent ``auth`` block synthesizes ``hmac_body`` — this also covers v1
    webhook triggers (which have no ``auth`` key), so they inherit the global-
    secret HMAC and the fail-closed registration rule uniformly (spec A1).
    """
    auth = dict(trig.get("auth") or {})
    mode = auth.get("mode", "hmac_body")
    auth["mode"] = mode
    auth.setdefault("header", _DEFAULT_AUTH_HEADER.get(mode, "X-Webhook-Signature"))
    auth.setdefault("tolerance_secs", 300)
    auth.setdefault("secret_owner", "casa")
    if trig.get("path"):
        logger.warning(
            "webhook trigger %r: 'path' is deprecated and ignored; served at "
            "/webhook/%s", trig_name, trig.get("name", trig_name),
        )
    return auth


def _build_triggers(
    data: dict[str, Any], *, agent_dir: str,
) -> list[TriggerSpec]:
    specs: list[TriggerSpec] = []
    for t in (data.get("triggers") or []):
        trig_name = t.get("name", "?")
        if t.get("type") in ("interval", "cron", "date"):
            prompt_text = _resolve_prose(
                t, field="prompt", agent_dir=agent_dir,
                source_label=f"triggers.yaml::{trig_name}",
            )
        else:
            prompt_text = ""  # webhook triggers have no prompt
        is_webhook = t.get("type") == "webhook"
        specs.append(TriggerSpec(
            name=t["name"],
            type=t["type"],
            minutes=int(t.get("minutes", 0) or 0),
            schedule=t.get("schedule", "") or "",
            path=t.get("path", "") or "",
            channel=t.get("channel", "") or "",
            prompt=prompt_text,
            auth=_normalize_webhook_auth(t, trig_name) if is_webhook else None,
            clearance=t.get("clearance", "public") if is_webhook else "public",
            at=t.get("at", "") or "",
            one_shot=bool(t.get("one_shot", False)),
            # #398 release 2: read off the entry, never inferred. An operator
            # may legitimately author a `reminder-`-prefixed dated one-shot, so
            # the name, the type and the flag all fail to identify ownership.
            managed_by=t.get("managed_by", "") or "",
        ))
    return specs


def _build_hooks(data: dict[str, Any]) -> HooksConfig:
    return HooksConfig(
        pre_tool_use=list(data.get("pre_tool_use") or []),
    )


def _build_runtime_fields(
    cfg: AgentConfig, runtime: dict[str, Any],
) -> None:
    """Populate the legacy runtime fields on *cfg* from runtime.yaml data."""
    role_slot_value = materialize_role(source=cfg.role_artifact, options=_ha_model_options())
    if runtime["kind"] != role_slot_value.kind:
        raise LoadError(
            f"runtime kind {runtime['kind']!r} does not match canonical role kind "
            f"{role_slot_value.kind!r}"
        )
    # NOTE (foundation-hardening reconciliation): a raw ``!=`` here is WRONG.
    # ``runtime["model"]`` comes from runtime.yaml via the ordinary YAML path
    # (``model.allowed`` is a plain ``list``); ``cfg.role_artifact.role["model"]``
    # is deep-frozen (``allowed`` is a ``tuple``, the mapping a ``MappingProxyType``).
    # ``list != tuple`` and ``dict != MappingProxyType`` ALWAYS in Python, so the
    # raw comparison would raise ``LoadError`` on every ha_option resident even when
    # the two files agree. Compare CANONICAL bytes instead (canonical_json_bytes now
    # normalizes frozen->plain and rfc8785 sorts keys, so this is a pure structural
    # equality of the two model blocks).
    if canonical_json_bytes(runtime["model"]) != canonical_json_bytes(cfg.role_artifact.role["model"]):
        raise LoadError(
            "runtime.yaml model declaration does not match the canonical role.yaml model block"
        )
    cfg.role_slot = role_slot_value
    cfg.kind = role_slot_value.kind
    cfg.role_id = role_slot_value.role_id
    cfg.resolved_model = role_slot_value.resolved_model.effective
    cfg.model = role_slot_value.resolved_model.sdk_model
    cfg.role_checksum = role_slot_value.checksum
    cfg.enabled = bool(runtime.get("enabled", True))

    tools = runtime.get("tools") or {}
    cfg.tools = ToolsConfig(
        allowed=list(tools.get("allowed") or []),
        disallowed=list(tools.get("disallowed") or []),
        permission_mode=tools.get("permission_mode", "acceptEdits"),
        max_turns=int(tools.get("max_turns", 10)),
        skills=tools.get("skills", "all"),
        voice_guard=tools.get("voice_guard", "none"),
    )

    cfg.mcp_server_names = list(runtime.get("mcp_server_names") or [])

    memory = runtime.get("memory") or {}
    cfg.memory = MemoryConfig(
        token_budget=int(memory.get("token_budget", 4000)),
        read_strategy=memory.get("read_strategy", "per_turn"),
        cross_peer_token_budget=int(memory.get("cross_peer_token_budget", 2000)),
    )

    session = runtime.get("session") or {}
    cfg.session = SessionConfig(
        strategy=session.get("strategy", "ephemeral"),
        idle_timeout=int(session.get("idle_timeout", 300)),
    )

    tts = runtime.get("tts") or {}
    cfg.tts = TTSConfig(tag_dialect=tts.get("tag_dialect", "square_brackets"))

    cfg.voice_errors = dict(runtime.get("voice_errors") or {})
    cfg.channels = list(runtime.get("channels") or [])
    cfg.cwd = runtime.get("cwd", "") or ""

    # requires (spec A5): fail-closed launch dependencies. Absent/empty
    # block -> RequiresConfig() default (both fields empty), which skips
    # the _prelaunch requires gate entirely.
    req = runtime.get("requires") or {}
    cfg.requires = RequiresConfig(
        plugins=list(req.get("plugins") or []),
        tools=list(req.get("tools") or []),
    )


# --- Prompt composer -------------------------------------------------------


def _compose_prompt(
    cfg: AgentConfig, policies: PolicyLibrary | None,
) -> str:
    parts: list[str] = [cfg.character.prompt.rstrip() + "\n"]
    parts.append(_render_voice_section(cfg.voice))
    parts.append(_render_response_shape_section(cfg.response_shape))

    deleg_section = _render_delegates_section(cfg.delegates)
    if deleg_section:
        parts.append(deleg_section)

    if cfg.disclosure is not None:
        if policies is None:
            raise LoadError(
                f"agent {cfg.role!r}: disclosure.yaml references policy "
                f"{cfg.disclosure.policy!r} but no PolicyLibrary was passed"
            )
        resolved = policies.resolve(
            cfg.disclosure.policy, cfg.disclosure.overrides,
        )
        parts.append(render_disclosure_section(resolved))

    return "\n".join(parts).rstrip() + "\n"


# --- Public API ------------------------------------------------------------


def _resident_bindings_root(bindings_dir: str | None) -> Path:
    """Personality Phase A, Task 8: the per-resident instance-tuple root
    (``<root>/resident-<slot>/``). Explicit ``bindings_dir`` wins; else the
    ``CASA_BINDINGS_DIR`` env override (mirrors ``CASA_CONFIG_DIR`` in
    config_sync.py); else the shipped ``/config/bindings`` container path.
    Tests point this at a tmp dir so a resident load never writes under the
    real ``/config`` tree."""
    return Path(
        bindings_dir or os.environ.get("CASA_BINDINGS_DIR", "/config/bindings")
    )


def load_agent_from_dir(
    agent_dir: str, *, policies: PolicyLibrary | None,
    roles_dir: str | None = None, bindings_dir: str | None = None,
    binding_commit: bool = True,
) -> AgentConfig:
    """Load one agent directory. Strict: every error raises LoadError.

    ``policies`` may be None for specialist loads (specialists have no
    disclosure.yaml). It must be non-None for residents or the composer
    raises at Disclosure-render time.

    ``roles_dir`` overrides the image-owned ``defaults/roles/`` root used
    to load and cross-validate the canonical role artifact (Personality
    Phase A, Task 5). Defaults to the real shipped tree; tests inject a
    synthetic ``roles_dir`` instead of patching the filesystem.

    ``bindings_dir`` overrides the resident instance-tuple root (Task 8)
    used by boot-time binding reconciliation; see ``_resident_bindings_root``.

    ``binding_commit=False`` (#338) makes the resident binding reconciliation
    validation-only: the full candidate validation runs, but no InstanceDir
    state is written — ``validate_config_repo``'s boot-parity replay must
    never activate a staged persona swap as a side effect of validating.
    """
    if not os.path.isdir(agent_dir):
        raise LoadError(f"not a directory: {agent_dir}")

    role_from_path = os.path.basename(agent_dir.rstrip(os.sep))

    # Peek runtime.yaml first — its explicit `kind` field selects the tier.
    runtime_path = os.path.join(agent_dir, "runtime.yaml")
    if not os.path.exists(runtime_path):
        raise LoadError(
            f"agent {role_from_path!r}: missing required file runtime.yaml",
        )
    runtime_data = _read_yaml(runtime_path)
    _validate(runtime_data, "runtime", runtime_path)
    tier = _declared_kind(runtime_data, runtime_path)

    _check_file_set(agent_dir, tier, role_from_path)

    # Load the canonical role artifact BEFORE constructing cfg — Step 7 makes
    # AgentConfig.role_artifact a required (kw_only) constructor field with no
    # default, so it must be supplied AT construction, never assigned onto an
    # already-built cfg (the former `cfg = AgentConfig(role=role_from_path)`,
    # followed by a POST-construction `cfg.role_artifact = ...` further down,
    # would otherwise raise `TypeError: missing 1 required keyword-only
    # argument: 'role_artifact'` immediately — this reordering IS the fix).
    role_artifact = _load_role_artifact_for(tier, role_from_path, roles_dir)

    # character.yaml — mandatory, validate then parse.
    char_path = os.path.join(agent_dir, "character.yaml")
    char_data = _read_yaml(char_path)
    _validate(char_data, "character", char_path)
    if char_data["role"] != role_from_path:
        raise LoadError(
            f"agent {role_from_path!r}: character.yaml role "
            f"{char_data['role']!r} must match directory name"
        )

    cfg = AgentConfig(role=role_from_path, role_artifact=role_artifact)
    cfg.character = _build_character(char_data, agent_dir=agent_dir)

    # voice.yaml
    voice_path = os.path.join(agent_dir, "voice.yaml")
    voice_data = _read_yaml(voice_path)
    _validate(voice_data, "voice", voice_path)
    cfg.voice = _build_voice(voice_data)

    # response_shape.yaml
    rs_path = os.path.join(agent_dir, "response_shape.yaml")
    rs_data = _read_yaml(rs_path)
    _validate(rs_data, "response_shape", rs_path)
    cfg.response_shape = _build_response_shape(rs_data)

    # runtime.yaml — already read + validated; build fields. cfg.role_artifact
    # is already populated (passed into the constructor above) — the former
    # standalone `cfg.role_artifact = _load_role_artifact_for(...)` line is
    # DELETED, not merely moved; there is exactly one assignment site now.
    _build_runtime_fields(cfg, runtime_data)

    # disclosure.yaml — resident only (file-set check guarantees presence).
    if tier == "resident":
        disc_path = os.path.join(agent_dir, "disclosure.yaml")
        disc_data = _read_yaml(disc_path)
        _validate(disc_data, "disclosure", disc_path)
        cfg.disclosure = _build_disclosure(disc_data)

    # delegates.yaml — optional, resident only.
    deleg_path = os.path.join(agent_dir, "delegates.yaml")
    if os.path.exists(deleg_path):
        deleg_data = _read_yaml(deleg_path)
        _validate(deleg_data, "delegates", deleg_path)
        cfg.delegates = _build_delegates(deleg_data)

    # executors.yaml — optional, ASSISTANT role only.
    exec_path = os.path.join(agent_dir, "executors.yaml")
    if os.path.exists(exec_path):
        if role_from_path != "assistant":
            raise LoadError(
                f"agent {role_from_path!r}: executors.yaml is only allowed "
                f"on the assistant role (found at {exec_path})"
            )
        exec_data = _read_yaml(exec_path)
        _validate(exec_data, "executors", exec_path)
        cfg.executors = _build_executors(exec_data)

    # triggers.yaml — optional, resident only.
    trig_path = os.path.join(agent_dir, "triggers.yaml")
    if os.path.exists(trig_path):
        trig_data = _read_yaml(trig_path)
        validate_persisted(trig_data, "triggers", trig_path)   # #608
        cfg.triggers = _build_triggers(trig_data, agent_dir=agent_dir)

    # hooks.yaml — optional.
    hooks_path = os.path.join(agent_dir, "hooks.yaml")
    if os.path.exists(hooks_path):
        hooks_data = _read_yaml(hooks_path)
        _validate(hooks_data, "hooks", hooks_path)
        cfg.hooks = _build_hooks(hooks_data)

    # Delegation-tool invariant: if delegates is non-empty, the MCP
    # tool must be whitelisted by runtime.tools.allowed.
    if cfg.delegates and _DELEGATE_MCP_TOOL not in cfg.tools.allowed:
        raise LoadError(
            f"agent {role_from_path!r}: delegates.yaml is non-empty but "
            f"runtime.yaml tools.allowed is missing "
            f"{_DELEGATE_MCP_TOOL!r}"
        )

    # Compose the system prompt.
    cfg.system_prompt = _compose_prompt(cfg, policies)

    # Personality Phase A, Task 8: activate the resident's persona binding and
    # compile its immutable per-surface prompt bundle. Runs LAST so every legacy
    # validation/compose error keeps its ordering; residents are the only kind
    # that carries a persona/binding/compiled bundle (specialists join in Plan 2,
    # executors never). A resident fails to load ONLY when reconciliation itself
    # raises (no active tuple exists AND the fresh attempt failed).
    if cfg.kind == "resident":
        _activate_resident_binding(
            cfg, role_from_path, bindings_dir, binding_commit=binding_commit,
        )
    elif cfg.kind == "specialist":
        # Personality Phase A, Task N1b Step 19: the specialist counterpart
        # to the resident block above — activates the INSTALLED component's
        # binding (Plan 2's /config/specialists/<slug>/ InstanceDir tree,
        # not /config/bindings/) so an installed specialist gets the same
        # compiled_prompt_bundle/speaker_provenance seam residents get. A
        # specialist with no active tuple (pending-configuration, or the
        # legacy bundled `finance` before Task N2's cutover) is a no-op —
        # activate_binding_for_config leaves cfg.compiled_prompt_bundle=None
        # and tools.py's system-prompt seam falls back to the legacy
        # cfg.system_prompt path.
        #
        # Wrapped into LoadError (unlike the resident block, which only
        # wraps its two sub-calls): load_all_specialists' per-directory loop
        # (agent_loader.py, load_all_specialists) isolates siblings by
        # catching ONLY LoadError — a raw ValueError/OSError escaping here
        # would poison every OTHER specialist's load in the same scan, not
        # just this one, so every recoverable failure this call can raise
        # (InstanceDir/parse_component_root/persona-pack loading/
        # compile_prompt_bundle validation all raise ValueError or a
        # ValueError subclass; a missing platform-frame/safety-kernel file
        # raises OSError) is folded into LoadError here.
        from specialist_install import activate_binding_for_config

        try:
            activate_binding_for_config(cfg)  # production root: the function's own default
        except (ValueError, OSError) as exc:
            raise LoadError(
                f"agent {role_from_path!r}: specialist binding activation failed: {exc}"
            ) from exc

    return cfg


def make_candidate_compile_validator(role: Any) -> Callable[[Any, Any], None]:
    """The #339 compile proof for *role*, as a callable.

    ONE definition, shared by boot reconciliation
    (:func:`_activate_resident_binding`) and by ``tools.persona_apply``
    (#607). Two copies of "does this candidate compile" drift, and the copy
    that drifts is the one that admits a binding the loader then rejects —
    which is the whole failure this proof exists to prevent.

    Reads the platform frame and safety kernel EAGERLY, so a broken image
    fails here rather than inside the caller's lock. Raises ``ValueError``
    (via ``compile_prompt_bundle``) for a candidate that exceeds an
    admission ceiling or fails the binding-integrity check; ``OSError`` if
    the image-owned frame/kernel files are unreadable.
    """
    from prompt_compiler import compile_prompt_bundle

    personality_dir = Path(SCHEMA_DIR).parent / "personality"
    platform_frame = (personality_dir / "platform-frame.md").read_text(encoding="utf-8")
    safety_kernel = (personality_dir / "safety-kernel.md").read_text(encoding="utf-8")

    def _prove(persona: Any, binding: Any) -> None:
        compile_prompt_bundle(
            role=role, persona=persona, binding=binding,
            platform_frame=platform_frame, safety_kernel=safety_kernel,
        )

    return _prove


def _activate_resident_binding(
    cfg: AgentConfig, role_from_path: str, bindings_dir: str | None,
    *, binding_commit: bool = True,
) -> None:
    """Reconcile + compile the resident binding onto *cfg* (Task 8, Step 8).

    ``binding_commit=False`` — see ``load_agent_from_dir``: full validation,
    no InstanceDir writes (the validate_config_repo replay).
    """
    from persona_pack import load_persona_pack
    from personality_binding import (
        IMAGE_DEFAULT_PERSONA_BY_SLOT,
        InstanceDir,
        reconcile_resident_binding,
    )
    from personality_types import SpeakerProvenance
    from prompt_compiler import compile_prompt_bundle

    # Image-owned personas ship under defaults/personas/ (module-relative, so
    # this resolves identically in the container and under tests). Operator
    # overrides live under <CASA_CONFIG_DIR>/personas/.
    personas_root = Path(SCHEMA_DIR).parent / "personas"
    override_root = Path(os.environ.get("CASA_CONFIG_DIR", "/config")) / "personas"

    def _pack(root: Path, ref: str) -> "PersonaPack | None":
        persona_id, _, version = ref.partition("@")
        pack_dir = root / persona_id / version / "pack"
        manifest_path = root / persona_id / version / "manifest.json"
        if pack_dir.is_dir() and manifest_path.is_file():
            return load_persona_pack(pack_dir, manifest_path)
        return None

    def _load_default(ref: str):
        pack = _pack(personas_root, ref)
        if pack is None:
            raise ValueError(f"image-default persona {ref!r} is not present under {personas_root}")
        return pack

    def _load_override(ref: str):
        for root in (override_root, personas_root):
            pack = _pack(root, ref)
            if pack is not None:
                return pack
        # #670: this used to end "run resident_persona_reset to recover". That
        # tool begins at `tools._resolve_resident_role`, which answers
        # `runtime_unavailable` when `agent.active_runtime` is absent, and on a
        # resident this failure is boot-fatal — so the advice needed the process
        # it was reporting the death of. This site is the ONE place that knows
        # which roots were searched, so it names them instead of naming a tool.
        raise ValueError(
            f"override persona {ref!r} is unavailable: no pack with a manifest "
            f"under {override_root} or {personas_root}"
        )

    instance_dir = InstanceDir(
        _resident_bindings_root(bindings_dir) / f"resident-{cfg.role_slot.slot}"
    )
    # #339: read the compile inputs BEFORE reconcile so the candidate can be
    # proven compilable pre-commit. A missing/unreadable frame or kernel file
    # is a broken image — the same hard failure it always was, just earlier.
    platform_frame = (Path(SCHEMA_DIR).parent / "personality" / "platform-frame.md").read_text(encoding="utf-8")
    safety_kernel = (Path(SCHEMA_DIR).parent / "personality" / "safety-kernel.md").read_text(encoding="utf-8")

    # #339 (the poisoned-active fix): the same admission-ceiling compile the
    # loader runs after reconcile, applied to the CANDIDATE before
    # desired -> active promotion. A candidate that cannot compile is
    # discarded by reconcile and the retained active keeps running, instead of
    # being committed and failing every subsequent boot. #607: the factory is
    # shared with tools.persona_apply so both prove the same thing.
    _prove_candidate_compiles = make_candidate_compile_validator(cfg.role_slot)

    try:
        active_tuple = reconcile_resident_binding(
            role=cfg.role_slot, image_default_persona_loader=_load_default,
            override_persona_loader=_load_override, instance_dir=instance_dir,
            candidate_validator=_prove_candidate_compiles, commit=binding_commit,
        )
    except ValueError as exc:
        raise LoadError(
            f"agent {role_from_path!r}: resident binding reconciliation failed: {exc}"
        ) from exc

    # Fix 2 (Personality Phase A review): reconcile_resident_binding can
    # return a RETAINED last-known-good tuple WITHOUT itself raising (e.g.
    # nothing staged, and the persona blob backing the current active
    # override has since vanished from disk — reconcile's internal load
    # attempt fails, is caught internally, and the retained active is handed
    # back unchanged since an active tuple exists). The (re)load below then
    # loads that SAME persona a second time; it must translate a missing/
    # invalid blob to LoadError exactly like the reconcile call above, not
    # escape as a raw ValueError.
    try:
        if active_tuple.binding.mode == "image-default":
            bound_persona = _load_default(IMAGE_DEFAULT_PERSONA_BY_SLOT[cfg.role_slot.slot])
        else:
            bound_persona = _load_override(
                f"{active_tuple.binding.persona_id}@{active_tuple.binding.persona_version}"
            )
    except ValueError as exc:
        raise LoadError(
            f"agent {role_from_path!r}: bound persona "
            f"{active_tuple.binding.persona_id}@{active_tuple.binding.persona_version} "
            f"failed to reload after reconciliation: {exc}"
        ) from exc
    try:
        bundle = compile_prompt_bundle(
            role=cfg.role_slot, persona=bound_persona, binding=active_tuple.binding,
            platform_frame=platform_frame, safety_kernel=safety_kernel,
        )
    except ValueError as exc:
        # Terra review (#339): reachable — e.g. an ACTIVE override whose
        # on-disk bytes changed under the pinned version. Reconcile refuses
        # to rematerialize (checksum pin) and retains the active tuple; the
        # reload above then returns the CHANGED pack and this compile's
        # integrity check (binding.persona_checksum vs persona.checksum)
        # refuses it. The altered bytes are never served; the failure is the
        # documented boot-fatal resident load (INV-PERS-003) — typed as
        # LoadError like every other load failure, not a raw ValueError.
        raise LoadError(
            f"agent {role_from_path!r}: compiled binding rejected: {exc}"
        ) from exc
    cfg.persona_pack = bound_persona
    cfg.binding = active_tuple.binding
    cfg.compiled_prompt_bundle = bundle
    cfg.binding_digest = active_tuple.binding.binding_digest
    cfg.speaker_provenance = SpeakerProvenance(
        speaker_kind="resident", role_id=cfg.role_slot.role_id,
        persona_id=bound_persona.persona_id, persona_version=bound_persona.version,
        display_name=bound_persona.identity["display_name"],
        binding_digest=active_tuple.binding.binding_digest,
    )


def load_all_agents(
    agents_dir: str, *, policies: PolicyLibrary | None,
    roles_dir: str | None = None, bindings_dir: str | None = None,
) -> dict[str, AgentConfig]:
    """Walk *agents_dir* for resident directories.

    Skips ``specialists/`` (Tier 2 home), ``executors/`` (reserved for
    Plan 2 Tier 3), and any dotdir. Each subdirectory's name becomes
    the agent role. Raises ``LoadError`` on the first malformed agent —
    strict-mode from day one.

    ``roles_dir``/``bindings_dir`` — see ``load_agent_from_dir``; propagated
    unchanged.
    """
    found: dict[str, AgentConfig] = {}
    if not os.path.isdir(agents_dir):
        return found
    for entry in sorted(os.listdir(agents_dir)):
        if entry.startswith(".") or entry in _NON_RESIDENT_AGENT_DIRS:
            continue
        path = os.path.join(agents_dir, entry)
        if not os.path.isdir(path):
            raise LoadError(
                f"unexpected non-directory at agents/{entry} — each agent "
                f"is a directory; flat YAML files are no longer supported"
            )
        cfg = load_agent_from_dir(
            path, policies=policies, roles_dir=roles_dir, bindings_dir=bindings_dir,
        )
        found[cfg.role] = cfg

    # Personality Phase A, Task 6, Step 9: fail closed on drift from the
    # fixed three-slot resident set (spec: assistant/butler/concierge are
    # the only residents that ever exist) — both against what actually
    # LOADED and against what the image-owned defaults/roles/resident/ tree
    # itself declares, so a stray fourth resident directory (either an
    # agents/ instance or an orphaned roles/ directory) fails the load
    # instead of silently boot with an unexpected resident set.
    resident_slots = {
        cfg.role_slot.slot for cfg in found.values()
        if cfg.kind == "resident" and cfg.role_slot is not None
    }
    if resident_slots != set(FIXED_RESIDENT_SLOTS):
        raise LoadError(
            f"resident set {sorted(resident_slots)} does not match the fixed slots "
            f"{FIXED_RESIDENT_SLOTS}"
        )
    role_dirs_base = Path(roles_dir or DEFAULT_ROLES_DIR) / "resident"
    role_dirs = {p.name for p in role_dirs_base.iterdir() if p.is_dir()}
    if role_dirs != set(FIXED_RESIDENT_SLOTS):
        raise LoadError(
            f"defaults/roles/resident contains {sorted(role_dirs)}, expected exactly "
            f"{FIXED_RESIDENT_SLOTS}"
        )
    return found


def load_all_specialists(
    specialists_dir: str, *, roles_dir: str | None = None,
) -> tuple[dict[str, AgentConfig], list[tuple[str, str]]]:
    """Walk *specialists_dir* for specialist directories.

    Specialists never reference the policy library (taxonomy §4.4: the
    delegating resident owns the disclosure layer).

    Returns ``(found, failed)``:
      * ``found`` — ``{role: AgentConfig}`` for valid specialists.
      * ``failed`` — list of ``(name, error_message)`` tuples for
        per-specialist load errors.

    O-2b (v0.37.9): per-specialist isolation. Mirrors the
    ``load_all_executors`` v0.37.1 B-1b pattern — one malformed
    specialist directory does NOT prevent its siblings from loading,
    and the registry can surface failures to ``casactl reload`` callers
    instead of swallowing them in a single ``except LoadError``.

    Collection-level errors (non-directory file under specialists/) still
    raise ``LoadError`` as before — only single-dir load failures are
    isolated.
    """
    found: dict[str, AgentConfig] = {}
    failed: list[tuple[str, str]] = []
    if not os.path.isdir(specialists_dir):
        return found, failed
    for entry in sorted(os.listdir(specialists_dir)):
        if entry.startswith("."):
            continue
        path = os.path.join(specialists_dir, entry)
        if not os.path.isdir(path):
            raise LoadError(
                f"unexpected non-directory at specialists/{entry} — each "
                f"specialist is a directory; flat YAML files are no longer "
                f"supported"
            )
        try:
            cfg = load_agent_from_dir(path, policies=None, roles_dir=roles_dir)
        except (LoadError, ValueError, OSError) as exc:
            # #338: broader than LoadError by necessity — load_agent_from_dir
            # raises raw ValueError subclasses on schema-valid input too
            # (role_slot.materialize_role's RoleValidationError for an
            # ha_option role whose allowed list excludes the operator's
            # current option value). One inconsistent specialist must land in
            # `failed` and keep its siblings (and boot) alive, not kill the
            # whole scan.
            failed.append((entry, str(exc)))
            continue
        # #541 layer 2 — load-time clamp: an install materialized BEFORE the
        # tool ceiling existed can carry forbidden casa-framework grants in
        # its runtime.yaml. Strip them with a WARN (not boot-fatal: the
        # specialist keeps running minus the forbidden grants). The clamped
        # cfg is what record-pinning and _build_specialist_options see, so
        # every fresh launch inherits it; ALREADY-LIVE engagement records
        # are covered by the dispatch-time ceiling in tools.py (layer 3).
        from specialist_component import specialist_casa_tool_violations
        _violations = specialist_casa_tool_violations(cfg.tools.allowed)
        if _violations:
            logger.warning(
                "specialist %s: stripping casa-framework grants outside the "
                "consumer-safe ceiling from tools.allowed: %r (#541)",
                cfg.role, _violations)
            cfg.tools.allowed = [
                t for t in cfg.tools.allowed if t not in set(_violations)]
        found[cfg.role] = cfg
    return found, failed


def read_hooks_document(path: str) -> dict[str, Any]:
    """THE reader for executor hooks documents (#312 / Sol r1-2).

    Env-substituting (``_read_yaml``), shared by load-time validation, the
    HTTP policy-map builder (``casa_core._build_executor_cc_hook_policies``)
    and the workspace settings bridge (``drivers.workspace``). Pre-fix those
    consumers re-read the file with a raw ``yaml.safe_load`` while validation
    substituted ``${VAR}`` references — a document could validate yet break
    (or silently change) at enforcement time. One reader, one document.
    """
    return _read_yaml(path)


def _resolve_executor_hooks(
    exec_dir: str, entry: str, defn: dict, present: set[str],
) -> tuple[str | None, dict[str, Any]]:
    """#312: resolve ``definition.yaml``'s ``hooks_file`` pointer, fail closed.

    Pre-fix, ANY present file satisfied the pointer — ``hooks_file:
    definition.yaml`` silently shed every declared containment policy (only
    the code-mandatory managed_component_guard survived) — and a
    declared-but-absent pointer silently dropped hooks entirely. Now: a
    present target must validate against the hooks schema; an absent target
    is a LoadError unless the resolved NAME is the default ``hooks.yaml``
    (which stays optional — an explicit ``hooks_file: hooks.yaml`` is the
    default spelled out, not a stricter declaration, Terra r1-P1). Shared by
    ``load_all_executors`` (boot/reload) and ``validate_config_repo`` (the
    pre-commit gate) so the two cannot diverge (Terra r1-P2).

    Task 3 (#360): returns ``(hooks_abs, hooks_data)`` — the resolved path
    (or ``None`` if no hooks file applies) AND the validated, constructible
    parsed document (``{}`` only when no hooks file exists at all), so the
    caller can snapshot the REAL declared document onto
    ``ExecutorDefinition.hooks_document`` instead of re-reading it later.
    """
    hooks_name = defn.get("hooks_file", "hooks.yaml")
    if not isinstance(hooks_name, str) or not hooks_name:
        # Terra r2-P2: the executor schema pins hooks_file to a string, but
        # this helper is also reached by the pre-commit gate BEFORE schema
        # validation — an unhashable value (list/dict) would raise TypeError
        # out of the ``in present`` test below. Report, never crash.
        raise LoadError(
            f"executor {entry!r}: hooks_file must be a non-empty string, "
            f"got {hooks_name!r}"
        )
    if hooks_name in present:
        hooks_abs = os.path.join(exec_dir, hooks_name)
        try:
            hooks_data = read_hooks_document(hooks_abs)
            _validate(hooks_data, "hooks", hooks_abs)
            # Sol r1-1/r1-2: schema validity is not constructibility — the
            # hooks schema deliberately allows arbitrary per-policy params
            # (additionalProperties), so `max_files: notanumber` or an
            # unknown parameter validates yet raises in the policy factory.
            # Pre-fix that raise happened inside the policy-map builder,
            # which SKIPS the executor — enforcement silently fell back to
            # the broader defaults (commit_size_guard 20 vs a configured 5).
            # Build the callbacks once here so a non-constructible document
            # fail-closes the whole executor at load instead.
            from hooks import build_policy_callbacks_from_hooks_yaml
            build_policy_callbacks_from_hooks_yaml(hooks_data)
        except LoadError as exc:
            raise LoadError(
                f"executor {entry!r}: hooks_file {hooks_name!r} is "
                f"not a valid hooks document: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — factory param errors
            raise LoadError(
                f"executor {entry!r}: hooks_file {hooks_name!r} declares "
                f"a policy the hook factories refuse: {exc}"
            ) from exc
        return hooks_abs, hooks_data
    if hooks_name != "hooks.yaml":
        raise LoadError(
            f"executor {entry!r}: hooks_file {hooks_name!r} not found"
        )
    return None, {}


def load_all_executors(
    base_dir: str, *, roles_dir: str | None = None,
) -> tuple[dict[str, "ExecutorDefinition"], list[tuple[str, str]]]:
    """Scan ``<base_dir>/executors/*/`` and return ``(loaded, failed)``.

    Each executor dir must contain ``definition.yaml`` and ``prompt.md``.
    ``hooks.yaml`` and ``observer.yaml`` are optional.

    v0.37.1 B-1b: per-file isolation — a single broken executor
    (schema violation, missing required file, forbidden file, prompt
    file missing, directory/type mismatch) does NOT prevent its
    siblings from loading. The broken one is reported in ``failed`` as
    ``(name, error_message)``; the caller (typically
    :meth:`ExecutorRegistry.load`) decides whether to log/raise.

    A collection-level error (e.g. ``executors_root`` missing) still
    short-circuits with an empty ``(out, failed)`` per the existing
    early-return contract.

    Personality Phase A, Task 5: each executor type also gets its
    canonical role artifact loaded from ``<roles_dir or DEFAULT_ROLES_DIR>/
    executor/<type>/`` and cross-validated (id, kind, and slot must match
    the executor type). A missing or mismatched artifact is a per-executor
    failure like any other, isolated the same way. The existing
    ``definition.yaml``, prompt, hooks, observer, and doctrine directory
    remain operational inputs during Phase A — they are not checksum
    inputs. Executors have no persona and no binding.
    """
    from config import ExecutorDefinition, resolve_model

    executors_root = os.path.join(base_dir, "executors")
    out: dict[str, ExecutorDefinition] = {}
    failed: list[tuple[str, str]] = []
    if not os.path.isdir(executors_root):
        return out, failed

    for entry in sorted(os.listdir(executors_root)):
        exec_dir = os.path.join(executors_root, entry)
        if not os.path.isdir(exec_dir):
            continue

        try:
            present = {
                f for f in os.listdir(exec_dir)
                if os.path.isfile(os.path.join(exec_dir, f))
            }
            rules = TIER_FILES["executor"]
            missing = rules["required"] - present
            if missing:
                raise LoadError(
                    f"executor {entry!r}: missing required file(s) {sorted(missing)}"
                )
            forbidden = present & rules["forbidden"]
            if forbidden:
                raise LoadError(
                    f"executor {entry!r}: forbidden file(s) present {sorted(forbidden)}"
                )

            defn_path = os.path.join(exec_dir, "definition.yaml")
            defn = _read_yaml(defn_path)
            _validate(defn, "executor", defn_path)

            tools = defn.get("tools") or {}
            doctrine_name = defn.get("doctrine_dir", "doctrine")
            # v0.74.2: an EXPLICITLY empty doctrine_dir is the opt-out for a
            # doctrine-less executor (provisioning fails closed on a missing
            # dir otherwise — Sol: never recreate the silent degradation).
            doctrine_abs = (os.path.join(exec_dir, doctrine_name)
                            if doctrine_name else "")

            # #312: the hooks pointer must resolve to a schema-valid hooks
            # document — see _resolve_executor_hooks (shared with the
            # pre-commit gate so the two cannot diverge).
            #
            # Task 3 (#360): the resolver also hands back the validated,
            # constructible parsed document — snapshot it onto
            # ExecutorDefinition.hooks_document verbatim (the executor's
            # REAL declared entries; {} only when no hooks file exists at
            # all — REVISION 3b, Sol r3 plan:171/179: an empty snapshot for
            # in_casa would let resolve_hooks({}) resynthesize the WIDER
            # default /config-wide path_scope, reopening the in_casa hole).
            hooks_abs, hooks_doc = _resolve_executor_hooks(
                exec_dir, entry, defn, present)

            # Task 3 (#360): a claude_code executor must DECLARE the
            # containment floor itself (block_dangerous_bash + path_scope,
            # each with its own params) — we never synthesize path_scope
            # with guessed prefixes, so an absent/incomplete declaration is
            # a load-time LoadError, not a silent narrower default. Gated on
            # `driver` read straight from the definition dict (the schema
            # constrains it to in_casa|claude_code; no default invented
            # here) — the floor does not apply to in_casa executors, whose
            # enforcement-time reread is Task 4.
            if defn["driver"] == "claude_code":
                from hooks import missing_required_cc_policies
                declared_policy_names = {
                    p.get("policy")
                    for p in (hooks_doc.get("pre_tool_use") or [])
                    if isinstance(p, dict)
                }
                missing_floor = missing_required_cc_policies(
                    declared_policy_names)
                if missing_floor:
                    raise LoadError(
                        f"executor {entry!r}: driver claude_code requires "
                        f"declaring the containment floor "
                        f"{sorted(missing_floor)} in "
                        f"{hooks_abs or 'a hooks file (none present)'} — "
                        f"each floor policy must be declared with its own "
                        f"params, never synthesized"
                    )

            observer_name = defn.get("observer_policy_file", "observer.yaml")
            observer_abs = (os.path.join(exec_dir, observer_name)
                            if observer_name in present else None)

            prompt_name = defn.get("prompt_template_file", "prompt.md")
            prompt_abs = os.path.join(exec_dir, prompt_name)
            if not os.path.isfile(prompt_abs):
                raise LoadError(
                    f"executor {entry!r}: prompt template file "
                    f"{prompt_name!r} not found"
                )

            role_artifact = _load_executor_role_artifact(entry, roles_dir)

            # Personality Phase A, Task 6: materialize the canonical role
            # (kind/model resolution/checksum) and derive the role-only
            # identity triple (spec §2.3) — executors have no persona and no
            # binding, so effective_config_digest is always EMPTY_CONFIG_DIGEST
            # today (see role_slot.compute_executor_identity's docstring).
            executor_role_slot = materialize_role(
                source=role_artifact, options=_ha_model_options(),
            )
            executor_identity = compute_executor_identity(
                role=executor_role_slot, effective_config_digest=EMPTY_CONFIG_DIGEST,
            )

            # #205: model boot-parity, the third guard on operator edits of
            # definition.yaml (the tool-allowlist clamp and permission-mode
            # downgrade below are the other two). `defn["model"]` is what
            # ACTUALLY runs — tools.py feeds ExecutorDefinition.model straight
            # into ClaudeAgentOptions.model — while `resolved_model` carries the
            # image-owned role artifact's choice for identity/checksum only.
            # Nothing reconciled them, so an edited definition.yaml could
            # silently run a model the canonical artifact does not declare.
            #
            # Residents have had this since Phase A: _build_runtime_fields
            # LoadErrors when runtime.yaml's model block differs from
            # role.yaml's. Executors get the same rule.
            #
            # FAIL, don't clamp: unlike tools (intersect) and permission_mode
            # (rank), a model has no "narrower" direction to degrade toward —
            # there is no safe default between two model IDs, so a disagreement
            # is unresolvable and the executor must not load. Isolated per
            # executor like every other failure here (reported in `failed`,
            # boot-non-fatal), so one bad definition cannot take down its
            # siblings or the add-on.
            #
            # Compare RESOLVED SDK IDs, not raw text: `defn["model"]` is a
            # shortname ("sonnet") or a full ID, and resolve_model() normalizes
            # both, so `model: sonnet` and `model: claude-sonnet-4-5-…` agree
            # rather than false-flagging. `resolved_model.effective` is the
            # SHORTNAME — comparing against it would mismatch every executor.
            declared_sdk_model = resolve_model(defn["model"])
            if declared_sdk_model != executor_role_slot.resolved_model.sdk_model:
                raise LoadError(
                    f"executor {entry!r}: definition.yaml model "
                    f"{defn['model']!r} resolves to {declared_sdk_model!r}, "
                    f"which disagrees with the canonical role artifact's "
                    f"{executor_role_slot.resolved_model.sdk_model!r} "
                    f"(defaults/roles/executor/{entry}/role.yaml is the "
                    f"authority — definition.yaml may not change the model; "
                    f"treat an unexpected mismatch as a tamper signal)"
                )

            # Round-5 (Terra P0): the canonical role artifact is the
            # immutable capability CEILING. definition.yaml is legitimately
            # editable in operation (executor/enable|disable|edit-definition
            # recipes), so a compromised/mistaken session could re-add a tool
            # (e.g. Bash) that the image-owned role artifact
            # (defaults/roles/executor/<type>/role.yaml — not operator-
            # writable, covered by role_checksum) deliberately does not
            # grant: capability self-escalation through the operational
            # file. Clamp the effective allowlist to the INTERSECTION —
            # definition.yaml may only narrow the role's grant, never exceed
            # it. A role artifact without a tools.allowed list yields an
            # EMPTY ceiling (fail-closed). Dropped entries are a tamper
            # signal — logged loudly, never silent. (tools.disallowed is
            # deliberately NOT clamped: it can only remove capability, so
            # definition editing cannot escalate through it.)
            declared_allowed = list(tools.get("allowed", []))
            role_ceiling = set(
                (role_artifact.role.get("tools") or {}).get("allowed") or [])
            effective_allowed = [
                t for t in declared_allowed if t in role_ceiling]
            dropped_beyond_ceiling = [
                t for t in declared_allowed if t not in role_ceiling]
            if dropped_beyond_ceiling:
                logger.error(
                    "executor %r: definition.yaml grants tools beyond its "
                    "role ceiling; dropped: %s (role.yaml under "
                    "defaults/roles/executor/%s/ is the immutable "
                    "capability ceiling — definition.yaml may only narrow "
                    "it; treat an unexpected drop as a tamper signal)",
                    entry, dropped_beyond_ceiling, entry,
                )

            # Round-6 P0-2 (Sol): permission_mode is a definition-editable
            # capability field — a self-edit to "bypassPermissions" would
            # turn the clamped allowlist into mere auto-approval and
            # resurrect everything the allowlist excludes. The role
            # artifact's required tools.permission_mode (role.v1.json — a
            # free string; shipped: configurator=acceptEdits,
            # plugin-developer=auto) is the CEILING: the definition may
            # declare an equally- or less-permissive mode, never a more
            # permissive one, and "bypassPermissions" is NEVER honored for
            # an executor regardless of the role (it has no rank).
            # Downgrades log the same tamper-signal style as the tool clamp
            # above. (defaults/schema/executor.v1.json — what _validate()
            # checks definition.yaml against — deliberately keeps its wider
            # enum: a tampered value must DEGRADE to a safe mode here, not
            # brick the executor at schema validation.)
            declared_mode = tools.get("permission_mode", "acceptEdits")
            role_mode = (
                (role_artifact.role.get("tools") or {}).get("permission_mode"))
            mode_ceiling = _EXECUTOR_PERMISSION_MODE_RANK.get(
                role_mode, _EXECUTOR_PERMISSION_MODE_RANK["acceptEdits"])
            if (declared_mode not in _EXECUTOR_PERMISSION_MODE_RANK
                    or _EXECUTOR_PERMISSION_MODE_RANK[declared_mode]
                    > mode_ceiling):
                effective_mode = (
                    role_mode
                    if role_mode in _EXECUTOR_PERMISSION_MODE_RANK
                    else "acceptEdits")
                logger.error(
                    "executor %r: definition.yaml permission_mode %r exceeds "
                    "what executors may run at (role ceiling %r; "
                    "bypassPermissions is never honored); downgraded to %r "
                    "— treat as a tamper signal",
                    entry, declared_mode, role_mode, effective_mode,
                )
            else:
                effective_mode = declared_mode

            memory_block = defn.get("memory") or {}
            d = ExecutorDefinition(
                type=defn["type"],
                description=defn["description"],
                model=resolve_model(defn["model"]),
                driver=defn["driver"],
                enabled=defn.get("enabled", True),
                tools_allowed=effective_allowed,
                tools_disallowed=list(tools.get("disallowed", [])),
                permission_mode=effective_mode,
                mcp_server_names=list(defn.get("mcp_server_names", [])),
                idle_reminder_days=int(defn.get("idle_reminder_days", 7)),
                prompt_template_path=prompt_abs,
                hooks_path=hooks_abs,
                hooks_document=hooks_doc,
                observer_policy_path=observer_abs,
                doctrine_dir=doctrine_abs,
                role_artifact=role_artifact,
                role_id=executor_role_slot.role_id,
                role_checksum=executor_role_slot.checksum,
                resolved_model=executor_role_slot.resolved_model.effective,
                effective_config_digest=executor_identity.effective_config_digest,
                # Plan 4a additions
                extra_dirs=list(defn.get("extra_dirs", [])),
                mirror_chat_to_topic=bool(defn.get("mirror_chat_to_topic", True)),
                plugins_dir=(os.path.join(exec_dir, "plugins")
                            if os.path.isdir(os.path.join(exec_dir, "plugins"))
                            else ""),
                # M4 addition (engagement memory)
                memory=_build_executor_memory(memory_block),
            )
            if d.type != entry:
                raise LoadError(
                    f"executor directory {entry!r} holds definition with "
                    f"type={d.type!r} - mismatch"
                )
            out[d.type] = d
        except (LoadError, OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            failed.append((entry, str(exc)))
            continue

    return out, failed
