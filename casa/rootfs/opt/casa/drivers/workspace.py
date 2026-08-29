"""Per-engagement workspace provisioner for the claude_code driver."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import time
from pathlib import Path

import yaml

from atomic_io import atomic_write_json
from drivers.hook_bridge import translate_hooks_to_settings
from engagement_uids import (
    UID_BASE, UNALLOCATED_UID, ensure_identity, owner_uid_or_none,
    prune_identity,
)
from safe_fs import SymlinkRefused, atomic_write_beneath, read_text_beneath

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "engagement_run_template.sh",
)

# v0.64.0: single owner of the per-engagement log location. The s6-log run
# script (render_log_run_script), the driver's DEBUG relay, the retention
# sweep, and the delete_engagement_workspace tool all derive from this —
# moving the location means changing exactly one place.
ENGAGEMENT_LOG_ROOT = "/var/log"


def engagement_log_dir(engagement_id: str, *, root: str | None = None) -> str:
    """Absolute path of the engagement's s6-log directory."""
    return os.path.join(
        root if root is not None else ENGAGEMENT_LOG_ROOT,
        f"casa-engagement-{engagement_id}",
    )


# ---------------------------------------------------------------------------
# Containment stage 2, Task 4: root-only control dir.
# ---------------------------------------------------------------------------
# The uid-owned workspace (``/data/engagements/<id>/``, below) holds only what
# the engagement's OWN CLI process reads/writes by path: CLAUDE.md, .mcp.json,
# .home/, doctrine/, plugin-provided files. Every file that ONLY root
# (casa-core) reads or writes — session-id capture, the spawn-epoch fence,
# per-epoch stderr rings, the inbound spool, the crash-safe stream cursor, the
# cached executor-memory block, and the inbound stdin FIFO itself — lives here
# instead, in a directory the CLI never gets ``--add-dir``ed into and that
# stays root:root 0700 even after Stage 2's per-engagement uid+cap-drop lands
# (Task 6/8). This is what kills the symlink-primitive class: a workspace
# symlink swapped in by the (unprivileged, post-Stage-2) CLI process can never
# again redirect a root read/write of one of these files, because none of
# them are joined to the workspace path anymore.
CONTROL_ROOT = "/data/engagement-ctl"

# Kept in step with drivers.claude_code_driver._SPOOL_FILENAME (the driver
# owns the constant name; this module only needs the literal to build paths).
_SPOOL_FILENAME = ".inbound_spool.jsonl"


def control_dir(engagement_id: str, *, root: str | None = None) -> str:
    """Absolute path of the engagement's root-only control directory."""
    return os.path.join(root if root is not None else CONTROL_ROOT, engagement_id)


def provision_control_dir(engagement_id: str, *, root: str | None = None) -> str:
    """Create the control dir for ``engagement_id`` — 0700, root-owned.

    ``exist_ok=False``: mirrors ``provision_workspace``'s own contract (the
    caller must not have created the directory already) so a double-
    provision of the same id is a loud error, not a silent merge into
    leftover state from a prior (crashed?) attempt.
    """
    base = root if root is not None else CONTROL_ROOT
    Path(base).mkdir(parents=True, exist_ok=True)
    d = Path(control_dir(engagement_id, root=root))
    d.mkdir(mode=0o700, exist_ok=False)
    return str(d)


def session_id_path(engagement_id: str, *, root: str | None = None) -> str:
    return os.path.join(control_dir(engagement_id, root=root), ".session_id")


def spawn_epoch_path(engagement_id: str, *, root: str | None = None) -> str:
    return os.path.join(control_dir(engagement_id, root=root), ".spawn_epoch")


def stderr_path(
    engagement_id: str, epoch: int, *, root: str | None = None,
) -> str:
    """Base path of the per-epoch stderr ring (``<base>`` is the live file;
    ``<base>.1`` is ringlog's one rotated chunk)."""
    return os.path.join(
        control_dir(engagement_id, root=root), f".stderr.{epoch}.log")


def casa_meta_path(engagement_id: str, *, root: str | None = None) -> str:
    return os.path.join(control_dir(engagement_id, root=root), ".casa-meta.json")


def fifo_path(engagement_id: str, *, root: str | None = None) -> str:
    return os.path.join(control_dir(engagement_id, root=root), "stdin.fifo")


def inbound_spool_path(engagement_id: str, *, root: str | None = None) -> str:
    return os.path.join(
        control_dir(engagement_id, root=root), _SPOOL_FILENAME)


def stream_cursor_path(engagement_id: str, *, root: str | None = None) -> str:
    return os.path.join(
        control_dir(engagement_id, root=root), ".stream_cursor.json")


def executor_memory_path(engagement_id: str, *, root: str | None = None) -> str:
    return os.path.join(
        control_dir(engagement_id, root=root), ".executor_memory")

# Bug 5 (v0.14.6): env-var names must match shell-identifier syntax.
# Pre-fix the key was interpolated unsanitised into `export {}='{}'`,
# so a key containing "\n" or other shell-special chars escaped the
# export line and ran arbitrary commands. Same upper-snake convention
# as ``plugin_env_conf._VAR_NAME_RE``.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# #429 r2 (Terra): the template's OWN exports, which `{EXTRA_EXPORT}` is
# interpolated AFTER — so an extra_env entry naming one silently overrides
# it for the whole engagement. Refused HERE, at the collision point, and not
# only in the manifest validator upstream: this is where the shadowing
# physically happens, so every present and future caller is covered by it.
# Keep in step with scripts/engagement_run_template.sh.
_TEMPLATE_OWNED_ENV: frozenset[str] = frozenset({
    "HOME",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
    "MCP_TOOL_TIMEOUT",
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
})

# L-1 (v0.34.2): valid CC permission patterns we forward into
# engagement-scoped .claude/settings.json::permissions.allow.
# Anything else (e.g. Casa-internal tool names) is dropped with a WARNING.
# v0.46.4: accept BARE ``Bash`` (broad, no parens — for dev executors that must
# run open-ended toolchains; safety stays in the hook stack: block_dangerous_bash
# + path_scope + the engagement_permission_relay) and ``WebFetch``/``WebSearch``.
_VALID_CC_PERMISSION_RE = re.compile(
    r"^(Bash(\(.+\))?|Read|Write|Edit|Glob|Grep|Skill|WebFetch|WebSearch|mcp__.+)$"
)


def _build_cc_permissions(defn) -> dict:
    """Build CC permissions block for engagement settings.json from ExecutorDefinition.

    Filters ``defn.tools_allowed`` to entries matching valid CC permission
    patterns; non-matching entries (e.g. Casa-internal tool names) are
    dropped with a WARNING. ``permission_mode`` falls through to
    ``"acceptEdits"`` when empty (matches ExecutorDefinition default).

    Round-6 P0-1 (Sol): the emitted block ALWAYS carries a ``deny`` list —
    ``defn.tools_disallowed`` plus the code-mandatory Q-1 sub-agent-spawn
    denial (Agent/Task bypass allow-lists; only an explicit deny is
    enforced) plus ``Bash`` whenever the clamped allowlist does not carry
    it (P0-2 belt+suspenders — harmless for the plugin-developer, which
    legitimately allows Bash). This is the claude_code-driver parity of
    the merge tools._build_executor_options performs for in_casa; before
    it, ``tools_disallowed`` landed NOWHERE on this driver path.
    """
    allow: list[str] = []
    for entry in defn.tools_allowed:
        if _VALID_CC_PERMISSION_RE.match(entry):
            allow.append(entry)
        else:
            logger.warning(
                "executor %r: dropping tools_allowed entry %r — "
                "not a valid CC permission pattern",
                defn.type, entry,
            )
    deny = [t for t in getattr(defn, "tools_disallowed", []) or [] if t]
    for t in ("Agent", "Task"):  # Q-1 set (_SUBAGENT_SPAWN_TOOLS)
        if t not in deny:
            deny.append(t)
    if ("Bash" not in deny
            and not any(a == "Bash" or a.startswith("Bash(") for a in allow)):
        deny.append("Bash")
    return {"allow": allow, "deny": deny,
            "defaultMode": defn.permission_mode or "acceptEdits"}


class WorkspaceConfigError(ValueError):
    """Raised when ExecutorDefinition values would shell-inject the run script."""


def _validate_extra_dir(d: str) -> None:
    """Reject extra_dir entries that aren't usable absolute paths.

    Bug 4 (v0.14.6): pre-fix any string was interpolated unquoted into
    `--add-dir <d>`. Strings with spaces, newlines, semicolons, or
    quotes injected shell. We now require an absolute POSIX path with
    no shell-special characters; values still get shlex.quote'd for
    belt-and-braces.
    """
    if not isinstance(d, str) or not d:
        raise WorkspaceConfigError(f"extra_dir must be a non-empty string: {d!r}")
    if not d.startswith("/"):
        raise WorkspaceConfigError(
            f"extra_dir must be an absolute path (start with '/'): {d!r}"
        )
    if any(c in d for c in "\n\r\0;|&`$<>'\""):
        raise WorkspaceConfigError(
            f"extra_dir contains shell-special characters: {d!r}"
        )


# #344: containment roots for extra_dirs. An executor definition is
# operator-editable runtime configuration, not trusted code ("executor
# definition is a mutable trust surface", #312 family) — an entry like "/"
# or "/config" handed the engagement CLI read/write far outside its
# workspace via --add-dir, unlike every other definition field, which is
# ceiling-checked. Only the HA shared mounts are approvable; the
# per-engagement workspace is always added separately by render_run_script,
# and plugin_dirs (immutable store paths) have their own validation.
APPROVED_EXTRA_DIR_ROOTS = ("/share", "/media")


def _validate_extra_dir_containment(d: str) -> None:
    """Reject an extra_dir outside the approved shared roots (#344).

    Both the LEXICAL path and its REALPATH resolution must sit under an
    approved root (Terra r1-2): a symlink planted at ``/share/x`` →
    ``/config`` passes any purely lexical check, and ``--add-dir``
    follows it at CLI runtime. Realpath at render time closes that for
    anything present when the script is rendered; a link swapped in
    afterwards is out of this validator's reach (the roots themselves
    are not casa-writable).
    """
    if any(part in ("..", ".") for part in d.split("/")):
        raise WorkspaceConfigError(
            f"extra_dir must not contain traversal segments: {d!r}"
        )

    def _under_approved(path: str) -> bool:
        return any(
            path == root or path.startswith(root + "/")
            for root in APPROVED_EXTRA_DIR_ROOTS
        )

    if not _under_approved(d):
        raise WorkspaceConfigError(
            f"extra_dir {d!r} is outside every approved root "
            f"{APPROVED_EXTRA_DIR_ROOTS}"
        )
    resolved = os.path.realpath(d)
    if not _under_approved(resolved):
        raise WorkspaceConfigError(
            f"extra_dir {d!r} resolves to {resolved!r}, outside every "
            f"approved root {APPROVED_EXTRA_DIR_ROOTS}"
        )


def render_run_script(
    *, engagement_id: str, permission_mode: str,
    extra_dirs: list[str], extra_unset: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    plugin_dirs: list[str] | None = None,
    uid: int = UNALLOCATED_UID, gid: int = UNALLOCATED_UID,
) -> str:
    """Read the run-script template and substitute per-engagement values.

    The per-engagement workspace is always included in --add-dir; any
    caller-provided extras are appended after it.

    ``extra_dirs`` — each element MUST be an absolute path with no
    shell-special characters. Each value is also shlex.quote'd before
    interpolation so a stricter validator can be added later without
    re-checking quoting (Bug 4, v0.14.6).

    ``extra_env`` — optional mapping of env var name → value to export
    inside the run script. Names must match ``[A-Za-z_][A-Za-z0-9_]*``
    (rejected at render time). Values are single-quote-escaped via the
    standard ``'\\''`` idiom (Bug 5, v0.14.6).

    ``uid``/``gid`` — containment stage 2, Task 6: the allocated OS-level
    identity the rendered script's final ``exec`` drops into via
    ``setpriv --reuid --regid --clear-groups --bounding-set -all
    --inh-caps -all --no-new-privs`` before handing off to the ``claude``
    CLI. Both default to ``UNALLOCATED_UID`` (the sentinel a not-yet-wired
    caller passes, or that a legacy/specialist ``EngagementRecord`` still
    carries) so an out-of-scope caller keeps compiling; render itself
    refuses to substitute that sentinel — or any uid below ``UID_BASE``,
    which would only ever be 0 (root) — into the script, since emitting
    ``--reuid -1`` or ``--reuid 0`` would silently skip the privilege drop
    this task exists to add. Fail-closed: no uid, no launch.
    """
    if uid == UNALLOCATED_UID or uid < UID_BASE:
        raise ValueError(
            f"render_run_script: refusing to render for unallocated/invalid "
            f"uid={uid!r} (must be >= UID_BASE={UID_BASE}) — the setpriv "
            f"privilege drop would otherwise be silently skipped or would "
            f"target root"
        )
    if gid == UNALLOCATED_UID or gid < UID_BASE:
        raise ValueError(
            f"render_run_script: refusing to render for unallocated/invalid "
            f"gid={gid!r} (must be >= UID_BASE={UID_BASE})"
        )

    with open(_TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        template = fh.read()

    for d in extra_dirs:
        _validate_extra_dir(d)
        _validate_extra_dir_containment(d)
    all_dirs = [f"/data/engagements/{engagement_id}/", *extra_dirs]
    add_dir_flags = " ".join(f"--add-dir {shlex.quote(d)}" for d in all_dirs)

    # §3.8: immutable plugin-artifact paths → repeated --plugin-dir flags.
    # Each is an absolute store path (same validation as extra_dirs, minus
    # the engagement-relative prefixing).
    for d in (plugin_dirs or []):
        _validate_extra_dir(d)
    plugin_dir_flags = " ".join(
        f"--plugin-dir {shlex.quote(d)}" for d in (plugin_dirs or []))

    extra_unset_str = " ".join(extra_unset or [])

    # #429 r2 (Sol): the empty-string pinning for declared-but-unresolved
    # plugin env is derived HERE, from the plugin_dirs being attached, rather
    # than assembled by each caller. Round 1 fixed the driver's start path and
    # round 2 found boot reconciliation re-rendering the same service pair
    # without it — two callers, one of them forgotten, which is what a
    # per-caller contract buys. Deriving it where the dirs are consumed means
    # a future third caller cannot forget. A caller-supplied extra_env wins on
    # a key collision (nothing sets one today; declarations are namespaced).
    if plugin_dirs:
        try:
            from plugin_grants import sanitized_env_for_paths
            derived = sanitized_env_for_paths(plugin_dirs)
        except Exception:  # noqa: BLE001 — never fail a render over this
            logger.warning("plugin env sanitization failed for %s — rendering "
                           "without the empty-string overlay", engagement_id,
                           exc_info=True)
            derived = {}
        if derived:
            extra_env = {**derived, **(extra_env or {})}

    # Task 11 (containment stage 2): a real uid means the CLI (and any
    # producer plugin it spawns) runs `--clear-groups`-dropped and can no
    # longer write the SHARED outbox (root:root, never group/world-
    # writable). Point it at its OWN private outbox dir instead — derived
    # here, from the uid render already refuses to render without, so every
    # uid-dropped launch gets it with no extra per-caller wiring. A
    # caller-supplied extra_env still wins on collision, same as the
    # plugin-dirs overlay above.
    real_uid = owner_uid_or_none(uid)
    if real_uid is not None:
        import plugin_outbox
        outbox_env = {plugin_outbox.OUTBOX_ENV:
                     plugin_outbox.engagement_outbox_dir(real_uid)}
        extra_env = {**outbox_env, **(extra_env or {})}

    if extra_env:
        bad = [k for k in extra_env if not _ENV_VAR_NAME_RE.match(str(k))]
        if bad:
            raise WorkspaceConfigError(
                f"extra_env keys must match [A-Za-z_][A-Za-z0-9_]*; "
                f"got invalid: {bad!r}"
            )
        owned = sorted(k for k in extra_env if k in _TEMPLATE_OWNED_ENV)
        if owned:
            raise WorkspaceConfigError(
                f"extra_env may not override the run template's own exports "
                f"(it is interpolated after them): {owned!r}"
            )
        export_lines = "\n".join(
            "export {}='{}'".format(k, str(v).replace("'", "'\\''"))
            for k, v in extra_env.items()
        )
    else:
        export_lines = ""

    return (
        template
        .replace("{ID_SHORT}", engagement_id[:8])
        .replace("{ID}", engagement_id)
        .replace("{UID}", str(uid))
        .replace("{GID}", str(gid))
        .replace("{PERMISSION_MODE}", permission_mode)
        .replace("{ADD_DIR_FLAGS}", add_dir_flags)
        .replace("{PLUGIN_DIR_FLAGS}", plugin_dir_flags)
        .replace("{EXTRA_UNSET}", extra_unset_str)
        .replace("{EXTRA_EXPORT}", export_lines)
    )


def render_log_run_script(*, engagement_id: str) -> str:
    """Render an s6-log run script for an engagement's stdout capture.

    The resulting script routes the engagement service's stdout to
    <ENGAGEMENT_LOG_ROOT>/casa-engagement-<id>/current, rotating at 1MB with
    up to 20 archive files. This is consumed by
    drivers.claude_code_driver._relay_log_lines via readline tailing.
    """
    log_dir = engagement_log_dir(engagement_id)
    return (
        "#!/command/with-contenv sh\n"
        "set -e\n"
        # GHSA-569r-7crq-xr43: the container umask is 0022, so `mkdir -p` landed
        # this dir at 0755 and s6-log's `current` at 0644 — one engagement uid
        # could read up to ~21 MB of a SIBLING engagement's stdout, which is the
        # exact boundary containment Stage 2 exists to create. A scoped umask
        # here fixes the directory AND every file s6-log rotates inside it;
        # private_state repairs dirs that already exist. Deliberately NOT a
        # process-wide umask for casa-core, which also materialises the plugin
        # store artifacts a dropped uid must be able to read.
        "umask 077\n"
        f"mkdir -p {log_dir}\n"
        f"exec s6-log n20 s1000000 {log_dir}\n"
    )


def workspace_mcp_token(ws_dir: str, *, owner_uid: int | None = None) -> str | None:
    """The engagement token currently baked into ``<ws>/.mcp.json``.

    ``None`` when the file is absent, unreadable, malformed, or predates
    #335 (no token header). Boot replay compares this against the record's
    token to decide whether the workspace credential actually CHANGED — a
    running CLI caches ``.mcp.json`` at spawn, so a changed credential means
    that CLI must be respawned or every one of its calls is rejected.

    Containment stage 2, Task 5: read via ``safe_fs.read_text_beneath`` so a
    symlink planted at ``<ws>/.mcp.json`` (pointing at a SIBLING
    engagement's workspace once the workspace is uid-chowned, Task 8) is
    refused rather than followed. ``owner_uid`` — the record's real
    ``allocated_uid``, or ``None`` for a not-yet-allocated/legacy workspace
    — is layered on top when given. A refusal returns ``None``, the same
    as any other unreadable file: the caller (boot replay) treats that as
    "changed" and regenerates — fail-safe, never a silent same-content skip.
    """
    try:
        cfg = json.loads(
            read_text_beneath(ws_dir, ".mcp.json", owner_uid=owner_uid))
        token = (
            cfg["mcpServers"]["casa-framework"]["headers"]
            ["X-Casa-Engagement-Token"]
        )
    except SymlinkRefused as exc:
        logger.warning(
            "workspace_mcp_token: refused symlinked .mcp.json under %s "
            "(%s) — treating as changed", ws_dir, exc,
        )
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return token if isinstance(token, str) and token else None


def workspace_mcp_url(ws_dir: str, *, owner_uid: int | None = None) -> str | None:
    """The casa-framework URL currently baked into ``<ws>/.mcp.json``.

    ``None`` when the file is absent, unreadable, or malformed. Boot replay
    compares this against the current ``casa_framework_mcp_url`` so a
    workspace whose baked URL has drifted from the served endpoint is
    rewritten (and its CLI cycled) exactly like a changed credential —
    the URL is part of the workspace's identity, not an artifact to trust.

    Containment stage 2, Task 5: same ``safe_fs`` routing/owner-uid/
    fail-safe-None-on-refusal contract as :func:`workspace_mcp_token` above.
    """
    try:
        cfg = json.loads(
            read_text_beneath(ws_dir, ".mcp.json", owner_uid=owner_uid))
        url = cfg["mcpServers"]["casa-framework"]["url"]
    except SymlinkRefused as exc:
        logger.warning(
            "workspace_mcp_url: refused symlinked .mcp.json under %s "
            "(%s) — treating as changed", ws_dir, exc,
        )
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return url if isinstance(url, str) and url else None


def write_workspace_mcp_json(
    ws_dir: str,
    *,
    engagement_id: str,
    engagement_auth_token: str,
    casa_framework_mcp_url: str,
    owner_uid: int | None = None,
) -> None:
    """Write ``<ws>/.mcp.json`` from the engagement's identity + credential.

    #335: the id header alone must never confer authority, so every entry
    carries the per-engagement ``auth_token`` — as an HTTP header for the
    casa-framework bridge, and as an env var for the stdio channel server
    (which posts it in each /internal/channel/* body). Called at provisioning
    AND at boot replay (before the CLI is respawned), so a pre-upgrade
    workspace picks up the token the registry backfilled onto its record.

    E-12 (v0.37.0): casa-engagement-channel is the per-engagement stdio
    channel server for operator UX (reply, ask, permission relay).
    """
    mcp_config = {"mcpServers": {
        "casa-framework": {
            "type": "http",
            "url": casa_framework_mcp_url,
            "headers": {
                "X-Casa-Engagement-Id": engagement_id,
                "X-Casa-Engagement-Token": engagement_auth_token,
            },
        },
        "casa-engagement-channel": {
            "command": "/opt/casa/venv/bin/python",
            "args": [
                "/opt/casa/channels/casa_engagement_channel.py",
                "--engagement-id", engagement_id,
            ],
            "env": {
                "CASA_INTERNAL_SOCKET": "/run/casa/internal.sock",
                "CASA_ENGAGEMENT_TOKEN": engagement_auth_token,
            },
        },
    }}
    # 0600 — this file carries the engagement's credential. Written through
    # the SYMLINK-SAFE atomic helper with an explicit mode so the secret is
    # never briefly world-readable AND so a pre-#335 0644 file is migrated on
    # the boot-replay rewrite. Containment Stage 2 (S1 code-gate fix): route
    # through ``safe_fs.atomic_write_beneath`` — a plain ``mkstemp``+
    # ``os.replace`` created its temp in the workspace dir BY PATHNAME, so a
    # symlinked ``.mcp.json`` (planted by the engagement uid, which owns the
    # workspace) would be REFUSED here rather than clobbered, and boot replay
    # turns that refusal into a fail-closed refuse-to-resume. ``owner_uid`` is
    # the record's real allocated uid on the boot-replay rewrite (workspace is
    # already uid-owned) and ``None`` at provision (root-owned, pre-chown).
    atomic_write_beneath(
        ws_dir, ".mcp.json", json.dumps(mcp_config, indent=2),
        owner_uid=owner_uid, mode=0o600)


def chown_workspace(ws: str, uid: int, gid: int) -> None:
    """Recursively chown *ws* to ``(uid, gid)``, symlink-safe.

    Containment stage 2, Task 8: the FINAL filesystem write of provisioning
    — after every root-side write below it, nothing may touch the workspace
    again before the engagement's own uid takes ownership. Walks the tree
    with ``os.scandir`` (never following a symlinked directory into a
    sibling workspace or elsewhere) and calls ``os.chown(...,
    follow_symlinks=False)`` on every entry, including symlinks themselves —
    a symlink planted in the tree gets ITS OWN ownership changed, but its
    target is never touched, so this can never be used to reach outside the
    workspace and reassign ownership of an arbitrary file.
    """
    os.chown(ws, uid, gid, follow_symlinks=False)
    with os.scandir(ws) as it:
        for entry in it:
            if entry.is_dir(follow_symlinks=False):
                chown_workspace(entry.path, uid, gid)
            else:
                os.chown(entry.path, uid, gid, follow_symlinks=False)


async def provision_workspace(
    *,
    engagements_root: str,
    engagement_id: str,
    engagement_auth_token: str,
    defn,                                    # ExecutorDefinition
    task: str,
    context: str,
    casa_framework_mcp_url: str,
    workspace_template_root: Path | None = None,
    world_state_summary: str = "",
    executor_memory: str = "",
    uid: int = UNALLOCATED_UID,
    gid: int = UNALLOCATED_UID,
) -> str:
    """Create /<engagements_root>/<id>/ with the full provisioning tree.

    Returns the absolute workspace path. Caller must NOT create the
    directory first — this function does.

    If ``workspace_template_root`` is provided and the template directory
    exists, ``render_workspace_template`` populates CLAUDE.md and
    .claude/settings.json — independent of plugin assignment (§3.3; plugins
    now load via --plugin-dir, not settings.json). Otherwise the legacy
    prompt-interpolation path is used.

    Note: filesystem I/O (mkdir, write_text, os.symlink, os.mkfifo) is
    currently synchronous despite the ``async def`` surface. The cost
    per engagement-start (one-time provisioning of a few files + one
    FIFO) is well under 10ms on the N150, so the brief event-loop stall
    is acceptable. If profiling later shows otherwise, wrap the filesystem
    calls in ``asyncio.to_thread`` to match the pattern in
    ``drivers/s6_rc.py``.

    ``uid``/``gid`` — containment stage 2, Task 8: the engagement's
    allocated OS identity. When both are real uids (``>= UID_BASE``, via
    :func:`engagement_uids.owner_uid_or_none`), this function appends the
    uid's passwd/group entry (:func:`engagement_uids.ensure_identity`) and
    then, as the LAST filesystem write before returning, recursively chowns
    the whole workspace to ``(uid, gid)`` and sets the top-dir mode to
    ``0700`` (:func:`chown_workspace`). Every other write above is root-side
    and MUST land before this point — nothing may touch the workspace after
    the chown. Default ``UNALLOCATED_UID`` skips both steps entirely (the
    workspace stays root-owned): legacy/specialist callers and every
    existing direct caller of this function that predates Task 8.
    Fail-closed: a failure in either step propagates — a half-provisioned,
    still-root-owned workspace must never be handed to a caller that thinks
    the uid drop already happened (Task 7's preflight is exactly that
    check).
    """
    ws = Path(engagements_root) / engagement_id
    ws.mkdir(parents=True, exist_ok=False)

    # L-1 (v0.34.2): both legacy and template paths need hooks_yaml_data.
    # Task 4 (#360): read the load-time-validated snapshot
    # (ExecutorDefinition.hooks_document, Task 3) rather than re-reading
    # hooks_path here — a post-load edit to the on-disk hooks.yaml must not
    # change what an already-provisioned (or newly provisioning) engagement
    # enforces. hooks_document is populated for every executor ({} only when
    # there is no hooks file), so no os.path.isfile probe is needed anymore.
    hooks_yaml_data: dict = getattr(defn, "hooks_document", None) or {}

    # Plan 4b §16.3: if a workspace-template exists for this executor, render it
    # into the workspace root. This subsumes the old symlink-loop behavior.
    # §3.3: selection is independent of plugin assignment now.
    if (
        workspace_template_root is not None
        and workspace_template_root.is_dir()
    ):
        render_workspace_template(
            template_root=workspace_template_root,
            dest=ws,
            defn=defn,
            hooks_yaml_data=hooks_yaml_data,
            executor_type=defn.type,
            task=task,
            context=context,
            world_state_summary=world_state_summary,
            executor_memory=executor_memory,
        )
    else:
        # 1. CLAUDE.md — the executor prompt, interpolated (legacy path).
        prompt_text = _read_text(defn.prompt_template_path)
        prompt_interpolated = (
            prompt_text
            .replace("{task}", task or "")
            .replace("{context}", context or "(none)")
            .replace("{executor_type}", defn.type)
            .replace("{executor_memory}", executor_memory or "")
        )
        (ws / "CLAUDE.md").write_text(prompt_interpolated, encoding="utf-8")

        # .claude/settings.json with translated hooks (legacy path).
        (ws / ".claude").mkdir(exist_ok=True)
        settings = translate_hooks_to_settings(
            hooks_yaml_data, proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
        )
        # L-1 (v0.34.2): merge permissions block from defn alongside hooks.
        settings["permissions"] = _build_cc_permissions(defn)
        (ws / ".claude" / "settings.json").write_text(
            json.dumps(settings, indent=2), encoding="utf-8",
        )

    # Task 4 (containment stage 2): the control dir is provisioned here,
    # alongside the workspace — every root-only run-state file below lands
    # in it, never under the uid-owned workspace the CLI can symlink through.
    provision_control_dir(engagement_id)

    # W3 (Task 8): cache the fetched executor_memory block at the control-dir
    # ``.executor_memory`` so a later ``refresh_claude_md`` (boot replay) can
    # re-interpolate the SAME {executor_memory} section — the block is a LIVE
    # Hindsight fetch, not re-derivable at boot, so it must be persisted
    # alongside the workspace. Task 4: moved OUT of the workspace — root
    # generates and reads this, the CLI never does.
    Path(executor_memory_path(engagement_id)).write_text(
        executor_memory or "", encoding="utf-8")

    # v0.74.2 (live finding 2026-07-13): provision the executor's doctrine/
    # into the workspace — the rendered CLAUDE.md references doctrine/*.md,
    # which never existed in claude_code workspaces (the plugin-developer
    # read missing files and proceeded without its conventions). Copy, not
    # symlink: the workspace must stay self-contained + immutable-ish even
    # if the live /config doctrine changes mid-engagement. FAIL CLOSED on a
    # declared-but-missing source (Sol design review: silently proceeding
    # recreates the original degradation); a doctrine-less executor opts out
    # with an explicitly empty `doctrine_dir:` in its definition.yaml.
    doctrine_src = getattr(defn, "doctrine_dir", "") or ""
    if doctrine_src:
        if not os.path.isdir(doctrine_src):
            raise FileNotFoundError(
                f"executor {defn.type!r} declares doctrine at "
                f"{doctrine_src!r} but the directory is missing — refusing "
                "to provision a workspace whose CLAUDE.md references absent "
                "doctrine (set doctrine_dir: '' to opt out)")
        shutil.copytree(doctrine_src, ws / "doctrine")

    # Per-engagement HOME dir (plugins symlinks removed in v0.14.x).
    # L-1 (v0.34.2): hoisted outside the if/else so template path also gets it.
    (ws / ".home" / ".claude" / "plugins").mkdir(parents=True)

    # 2. .mcp.json — point at Casa's MCP HTTP bridge with the engagement id
    # + per-engagement auth token (#335). Written via the same renderer boot
    # replay uses to refresh a resumed workspace's credential.
    write_workspace_mcp_json(
        str(ws),
        engagement_id=engagement_id,
        engagement_auth_token=engagement_auth_token,
        casa_framework_mcp_url=casa_framework_mcp_url,
    )

    # 3. Named FIFO for stdin. Task 4: lives in the control dir — the run
    # template's `exec < .../stdin.fifo` reads it by an absolute
    # control-dir path (scripts/engagement_run_template.sh), never through
    # the workspace.
    os.mkfifo(fifo_path(engagement_id), 0o600)

    # 4. Task 8 (containment stage 2): identity + chown — LAST, after every
    # root-side write above. ensure_identity() runs first so the uid has an
    # NSS passwd/group entry the instant it can touch anything; chown_workspace
    # (recursive, symlink-safe) then hands the whole tree to that uid, and the
    # top dir is pinned to 0700 so only that uid (and root) can even list it.
    # A real uid/gid (>= UID_BASE) is required for either step to run — the
    # UNALLOCATED_UID default (legacy/specialist/pre-Task-8 callers) leaves
    # the workspace root-owned exactly as before. Neither call swallows its
    # exception: a failure here must abort provisioning, not hand back a
    # workspace that is still root-owned while the caller believes the drop
    # succeeded (see Task 7's _preflight_uid_drop).
    real_uid = owner_uid_or_none(uid)
    if real_uid is not None:
        real_gid = gid if gid >= UID_BASE else real_uid
        ensure_identity(real_uid, str(ws / ".home"))
        chown_workspace(str(ws), real_uid, real_gid)
        os.chmod(ws, 0o700)
        # Task 11 (containment stage 2): provision the uid's PRIVATE outbox
        # dir eagerly, here, so it already exists (owned by the uid) before
        # the CLI/any producer plugin it spawns ever starts — a lazy
        # first-send_media provision would be too late for a producer that
        # writes before send_media is ever called. Kept entirely separate
        # from the workspace tree (own root, own retention) — never chowned
        # by chown_workspace above.
        import plugin_outbox
        # fresh=True: a newly-allocated uid must never inherit a predecessor's
        # leftover outbox (S1 r7 defense-in-depth behind the never-reuse uid
        # invariant). Boot-replay resume of an EXISTING uid uses the default
        # (fresh=False) so in-flight outbox contents are preserved.
        plugin_outbox.provision_engagement_outbox(real_uid, fresh=True)

    logger.info("Provisioned workspace for engagement %s at %s",
                engagement_id[:8], ws)
    return str(ws)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def refresh_claude_md(ws_dir: str, *, defn, rec) -> None:
    """Re-render an existing workspace's CLAUDE.md from the engagement record
    (W3/Sol r8-B5, Task 8).

    Boot replay calls this for EVERY resumed brief-bearing engagement so the
    workspace CLAUDE.md is re-derived from the VERBATIM ``origin["brief"]``
    (per design §211 the resume path re-renders from the raw brief; a persisted
    derived form could go stale). Runs the SAME whole-file interpolation as
    provisioning — the same template-vs-legacy CHOICE, and every pre-existing
    placeholder section ({context}/{world_state_summary}/{executor_memory}/
    {executor_type}) survives the refresh:

      - ``{task}``               = ``brief_task_for(rec, defn)`` (derives from
        the raw brief; the canonical ``rec.task`` fallback when no brief).
      - ``{context}``            = ``rec.origin.get("context", "")``.
      - ``{world_state_summary}``= ``rec.origin.get("world_state_summary", "")``.
      - ``{executor_type}``      = ``defn.type``.
      - ``{executor_memory}``    = contents of ``<ws>/.executor_memory`` cached
        at provision (absent → "").

    ``rec`` (not just its origin) is required because ``brief_task_for`` needs
    the canonical ``.task`` fallback for a brief-less record. Raises on I/O
    failure — the caller (boot replay) treats a raised refresh as a fail-closed
    refuse-to-resume.
    """
    from drivers.brief import brief_task_for

    ws = Path(ws_dir)
    # Containment Stage 2 (S1 code-gate fix): the CLAUDE.md write must be
    # symlink-safe. On boot replay the workspace is uid-owned, so a plain
    # ``Path.write_text`` would FOLLOW a symlink the engagement uid planted
    # (e.g. CLAUDE.md → a sibling's .mcp.json) and let root overwrite the
    # sibling's file. Route through ``safe_fs.atomic_write_beneath`` with the
    # record's real allocated uid (``None`` for a legacy/specialist record
    # whose workspace was never uid-chowned) so a symlinked target is REFUSED
    # (SymlinkRefused) and the caller fail-closed refuses the resume.
    owner_uid = owner_uid_or_none(rec.allocated_uid)
    task = brief_task_for(rec, defn)
    context = rec.origin.get("context", "")
    world_state_summary = rec.origin.get("world_state_summary", "")

    # Task 4: cached at PROVISION under the control dir (engagement id is
    # the workspace dir's basename by construction), not the workspace.
    mem_path = Path(executor_memory_path(ws.name))
    executor_memory = (
        mem_path.read_text(encoding="utf-8") if mem_path.is_file() else ""
    )

    # Same selection as provision (workspace.py:228-253): a workspace-template/
    # beside the prompt template selects the template render path; else legacy.
    exec_dir = Path(defn.prompt_template_path).parent
    template_root = exec_dir / "workspace-template"

    if template_root.is_dir():
        # Mirror render_workspace_template (workspace.py:489-496) EXACTLY.
        text = (template_root / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
        text = (
            text.replace("{executor_type}", defn.type)
                .replace("{task}", task)
                .replace("{context}", context)
                .replace("{world_state_summary}", world_state_summary)
                .replace("{executor_memory}", executor_memory or "")
        )
        atomic_write_beneath(
            str(ws), "CLAUDE.md", text, owner_uid=owner_uid, mode=0o644)
    else:
        # Mirror the legacy provision branch (workspace.py:245-253) EXACTLY.
        prompt_text = _read_text(defn.prompt_template_path)
        prompt_interpolated = (
            prompt_text
            .replace("{task}", task or "")
            .replace("{context}", context or "(none)")
            .replace("{executor_type}", defn.type)
            .replace("{executor_memory}", executor_memory or "")
        )
        atomic_write_beneath(
            str(ws), "CLAUDE.md", prompt_interpolated,
            owner_uid=owner_uid, mode=0o644)


def write_casa_meta(
    *, workspace_path: str, engagement_id: str, executor_type: str,
    status: str, created_at: str,
    finished_at: str | None, retention_until: str | None,
    plugin_artifacts: list[dict] | None = None,
    allocated_uid: int = UNALLOCATED_UID,
) -> None:
    # This dict is reconstructed from scratch on every rewrite — the immutable
    # plugin_artifacts (§3.8) must be re-passed by every caller (initial write
    # + terminal finalize) or it is silently dropped. Task 8: allocated_uid
    # likewise must be re-passed forward by the terminal-rewrite caller
    # (tools._finalize_engagement) — it's how the workspace sweeper, which has
    # no registry access, learns which uid's passwd/group entry to prune once
    # this workspace is deleted.
    meta = {
        "engagement_id": engagement_id,
        "executor_type": executor_type,
        "status": status,
        "created_at": created_at,
        "finished_at": finished_at,
        "retention_until": retention_until,
        "plugin_artifacts": list(plugin_artifacts or []),
        "allocated_uid": allocated_uid,
    }
    # Task 4: moved to the control dir — ``workspace_path`` is retained as a
    # parameter for caller back-compat (every existing caller already has it
    # in hand) but no longer contributes to the path; ``engagement_id`` alone
    # determines where the meta lives now.
    path = Path(casa_meta_path(engagement_id))
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


WORKSPACE_RETENTION_DAYS = 7
"""#698: the ONE definition of how long a terminal workspace is kept.

``tools._WORKSPACE_RETENTION_DAYS`` aliases this, so the seven days the sweeper
enforces, the finalize funnel writes and the launch-death path writes cannot
drift apart into three numbers."""


def write_terminal_casa_meta(
    *, engagement_id: str, status: str, allocated_uid: int,
    now: float | None = None,
) -> bool:
    """#698: give a RETAINED workspace a terminal status and a deadline.

    Returns True only when it actually rewrote the metadata. Retention that
    does not end is not retention: ``_sweep_one_workspace`` returns on
    ``status == "UNDERGOING"`` BEFORE it reads ``retention_until``, and
    warn-skips a terminal status whose ``retention_until`` is not an ISO
    string, so a workspace kept with untouched metadata leaks forever. With
    both written, one post-deadline sweep reaps the workspace tree, the log
    dir, the control dir, the uid's identity and its private outbox in a single
    decision.

    Refuses — returning False, having written nothing — unless the metadata
    exists and still reads ``UNDERGOING``. That keeps it EXACTLY ONCE for a
    workspace, by construction rather than by a caller's discipline: the second
    call finds a terminal status and declines.

    Synchronous, never raises, no await: its one caller is the launch-death
    reporter, and the rollback's tail must stay await-free (#755).

    ``write_casa_meta`` rebuilds its dict from scratch, so ``executor_type``,
    ``created_at`` and ``plugin_artifacts`` are re-passed from the metadata
    itself and ``allocated_uid`` from the record — the sweeper has no registry
    access and reads that uid back from this field to prune the identity.
    """
    try:
        path = Path(casa_meta_path(engagement_id))
        if not path.exists():
            logger.warning(
                "terminal workspace metadata for %s: no .casa-meta.json to "
                "rewrite; the workspace cannot be reaped on a deadline",
                engagement_id[:8])
            return False
        meta = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or meta.get("status") != "UNDERGOING":
            logger.warning(
                "terminal workspace metadata for %s: status is %r, not "
                "UNDERGOING; leaving it alone",
                engagement_id[:8],
                meta.get("status") if isinstance(meta, dict) else meta)
            return False
        when = time.time() if now is None else now
        finished_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
        retention_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(when + WORKSPACE_RETENTION_DAYS * 24 * 3600))
        write_casa_meta(
            workspace_path="",
            engagement_id=engagement_id,
            executor_type=meta.get("executor_type") or "",
            status=status,
            created_at=meta.get("created_at") or finished_iso,
            finished_at=finished_iso,
            retention_until=retention_iso,
            plugin_artifacts=meta.get("plugin_artifacts"),
            allocated_uid=allocated_uid,
        )
        return True
    except Exception:  # noqa: BLE001 — best effort; the caller believes False
        logger.error(
            "terminal workspace metadata for %s could not be written — the "
            "workspace is retained but will not be reaped on a deadline",
            engagement_id[:8], exc_info=True)
        return False


def _migrate_legacy_casa_meta(engagement_id: str, legacy_text: str) -> None:
    """Best-effort forward-copy of a pre-Task-4 ``.casa-meta.json`` (written
    under the workspace, back when that was the only location) into the
    control dir — so every read/write from here on lands on the one,
    now-canonical location. Idempotent (``target.exists()`` short-circuits a
    concurrent/repeated migration) and never raises: a migration failure
    must not turn a successful legacy READ into an error, and the next
    ``load_casa_meta`` call simply retries it.

    Takes the already-read file CONTENT (not a path) — Containment stage 2,
    Task 5: the caller reads the legacy file exactly once, through
    ``safe_fs``; re-opening it here by path would defeat that (a second,
    unguarded read of an attacker-controlled workspace path)."""
    try:
        Path(CONTROL_ROOT).mkdir(parents=True, exist_ok=True)
        Path(control_dir(engagement_id)).mkdir(mode=0o700, exist_ok=True)
        target = Path(casa_meta_path(engagement_id))
        if not target.exists():
            target.write_text(legacy_text, encoding="utf-8")
    except OSError:
        logger.warning(
            "load_casa_meta: failed to migrate legacy .casa-meta.json into "
            "the control dir for engagement %s", engagement_id, exc_info=True,
        )


def load_casa_meta(
    workspace_path: str, *, owner_uid: int | None = None,
) -> dict | None:
    # Task 4: the workspace dir's basename IS the engagement id by
    # construction (``provision_workspace`` mkdirs exactly
    # ``<engagements_root>/<engagement_id>``) — every existing caller passes
    # a workspace path, so derive the id rather than widen every call site.
    engagement_id = Path(workspace_path).name
    path = Path(casa_meta_path(engagement_id))
    legacy_text: str | None = None
    is_legacy = not path.exists()
    if not is_legacy:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning(
                "load_casa_meta: I/O error reading %s", path, exc_info=True,
            )
            return None
    else:
        # Fix-loop round 1 (Important 2): an engagement whose
        # .casa-meta.json was written before this release deploys still has
        # it at the LEGACY workspace path — without this fallback it is
        # unreachable, which drops plugin_artifacts/created_at on finalize
        # (tools.py) and permanently leaks its (already-terminal) workspace
        # past the retention sweep (never gets deleted again). Read the
        # legacy copy (via safe_fs, Task 5 — this is a root read of a
        # uid-owned WORKSPACE path, so a symlink there must be refused, not
        # followed) and opportunistically migrate it forward.
        try:
            text = read_text_beneath(
                workspace_path, ".casa-meta.json", owner_uid=owner_uid)
        except SymlinkRefused as exc:
            logger.warning(
                "load_casa_meta: refused symlinked legacy .casa-meta.json "
                "under workspace %s for engagement %s (%s) — treating as "
                "absent", workspace_path, engagement_id, exc,
            )
            return None
        except OSError:
            return None
        legacy_text = text
    try:
        data = json.loads(text)
        # #344: valid-but-non-object JSON ([]/string/number) used to
        # escape here and crash the first .get() in a consumer — one bad
        # file aborted the whole retention sweep for later workspaces.
        if not isinstance(data, dict):
            logger.warning(
                "load_casa_meta: %s legacy=%s is not a JSON object — "
                "treating as absent", engagement_id, is_legacy,
            )
            return None
        if legacy_text is not None:
            _migrate_legacy_casa_meta(engagement_id, legacy_text)
        return data
    except json.JSONDecodeError:
        logger.warning(
            "load_casa_meta: %s legacy=%s is not valid JSON — treating as "
            "absent", engagement_id, is_legacy,
        )
        return None


# ---------------------------------------------------------------------------
# Workspace sweeper — §6.5 of Plan 4a (Plan 4a.1 delivery).
# ---------------------------------------------------------------------------


async def _sweep_workspaces(
    *, engagements_root: str, log_root: str | None = None,
) -> None:
    """Periodic sweep: delete terminal engagement workspaces past retention.

    Status semantics from .casa-meta.json:
      - UNDERGOING: skip (engagement still running).
      - COMPLETED / CANCELLED: delete iff retention_until <= now.
      - Terminal but retention_until is null: log warning + skip (bug).
      - No .casa-meta.json at all: skip (caller-managed via
        delete_engagement_workspace MCP tool).

    v0.64.0: the per-engagement s6-log dir (<log_root>/casa-engagement-<id>)
    follows the same retention — removed together with the workspace, so
    post-mortem logs stay available exactly as long as the workspace does
    (bounded ~21 MB/engagement by ``s6-log n20 s1000000``).

    Disk-pressure mode (§6.5 aggressive tier) is out of scope — see spec
    §8.3. The N150 has >30 GB free.
    """
    if not os.path.isdir(engagements_root):
        return

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for entry in os.scandir(engagements_root):
        if not entry.is_dir():
            continue
        try:
            _sweep_one_workspace(entry, now_iso=now_iso, log_root=log_root)
        except Exception:  # noqa: BLE001 — Sol r6-1: per-workspace boundary
            logger.warning(
                "workspace sweep: entry %s failed; continuing with the "
                "remaining workspaces", entry.name, exc_info=True,
            )


def _sweep_one_workspace(
    entry: os.DirEntry, *, now_iso: str, log_root: str | None,
) -> None:
    """One workspace's retention decision + deletion — isolated so a
    surprising per-entry failure cannot abort the whole sweep (Sol r6-1)."""
    meta = load_casa_meta(entry.path)
    if meta is None:
        return
    status = meta.get("status")
    if status == "UNDERGOING":
        return
    retention_until = meta.get("retention_until")
    # Sol r6-1: a non-string retention_until (e.g. a number) would
    # TypeError against the ISO string comparison below — same
    # abort-the-sweep class as non-object metadata (#344). Treat any
    # non-string the way null is treated: warn + skip.
    if not isinstance(retention_until, str):
        logger.warning(
            "workspace sweep: engagement %s has terminal status %r "
            "but retention_until is %r (expected ISO string); skipping",
            entry.name, status, retention_until,
        )
        return
    if retention_until > now_iso:
        return
    try:
        shutil.rmtree(entry.path)
    except OSError as exc:
        logger.warning(
            "workspace sweep: rmtree %s failed: %s",
            entry.path, exc,
        )
    else:
        logger.info(
            "workspace sweep: removed %s (status=%s, past retention)",
            entry.name, status,
        )
        log_dir = engagement_log_dir(entry.name, root=log_root)
        try:
            if os.path.isdir(log_dir):
                shutil.rmtree(log_dir)
        except OSError as exc:
            # The workspace is already gone, so no future sweep can map
            # to this log dir again — warn, it's the only signal left.
            logger.warning(
                "workspace sweep: log dir rmtree %s failed: %s",
                log_dir, exc,
            )
        # Task 4: the control dir follows the workspace on the SAME
        # retention decision — once the workspace is gone, nothing can
        # ever map back to its control dir again (load_casa_meta derives
        # the id from the workspace basename, which no longer exists).
        ctl_dir = control_dir(entry.name)
        try:
            if os.path.isdir(ctl_dir):
                shutil.rmtree(ctl_dir)
        except OSError as exc:
            logger.warning(
                "workspace sweep: control dir rmtree %s failed: %s",
                ctl_dir, exc,
            )
        # Task 8 (containment stage 2): once a workspace is gone for good,
        # its uid's passwd/group entry must go with it — otherwise
        # /etc/passwd accumulates one stale ``casa-eng-<uid>`` line per
        # completed engagement forever. ``meta`` (read above, before the
        # rmtree) is the ONLY source of the uid here — the sweeper has no
        # registry access, unlike the driver's own rollback or
        # delete_engagement_workspace. ``owner_uid_or_none`` guards against
        # both the UNALLOCATED_UID sentinel and legacy meta with no
        # ``allocated_uid`` key at all (``.get`` default).
        _raw_uid = meta.get("allocated_uid", UNALLOCATED_UID)
        real_uid = (
            owner_uid_or_none(_raw_uid) if isinstance(_raw_uid, int) else None
        )
        if real_uid is not None:
            try:
                prune_identity(real_uid)
            except OSError as exc:
                logger.warning(
                    "workspace sweep: prune_identity(%s) failed: %s",
                    real_uid, exc,
                )
            # Task 11 (containment stage 2): the uid's private outbox dir
            # follows the workspace on the SAME retention decision — same
            # reasoning as the control dir and passwd entry above.
            import plugin_outbox
            try:
                plugin_outbox.teardown_engagement_outbox(real_uid)
            except Exception as exc:  # noqa: BLE001 — best-effort, like siblings
                logger.warning(
                    "workspace sweep: engagement outbox teardown(%s) "
                    "failed: %s", real_uid, exc,
                )


# ---------------------------------------------------------------------------
# Workspace-template rendering — §16.3 of Plan 4b.
# ---------------------------------------------------------------------------


def render_workspace_template(
    *,
    template_root: Path,
    dest: Path,
    defn,                                    # ExecutorDefinition (Plan 4b §16.3 + L-1)
    hooks_yaml_data: dict,                   # L-1 (v0.34.2)
    executor_type: str,
    task: str,
    context: str,
    world_state_summary: str,
    executor_memory: str = "",
) -> None:
    """Copy the executor's workspace-template/ subtree into `dest`, interpolate
    CLAUDE.md.tmpl → CLAUDE.md, and generate .claude/settings.json with hooks +
    permissions from the executor definition. Plan 4b §16.3.

    §3.3 (unified plugin arch): settings.json NO LONGER carries enabledPlugins —
    executor plugins load via the pinned --plugin-dir flags on the run script.

    L-1 (v0.34.2): ``defn`` and ``hooks_yaml_data`` are REQUIRED. The generated
    settings.json always includes a ``hooks`` block (from
    ``translate_hooks_to_settings``) and a ``permissions`` block (from
    ``_build_cc_permissions``).
    """
    if not template_root.is_dir():
        raise FileNotFoundError(f"workspace template missing: {template_root}")

    dest.mkdir(parents=True, exist_ok=True)

    # Copy every file under template_root except CLAUDE.md.tmpl (handled below).
    for src in template_root.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(template_root)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel.name == "CLAUDE.md.tmpl":
            continue
        shutil.copy2(src, target)

    # Interpolate CLAUDE.md.
    tmpl = template_root / "CLAUDE.md.tmpl"
    if tmpl.is_file():
        text = tmpl.read_text(encoding="utf-8")
        text = (
            text.replace("{executor_type}", executor_type)
                .replace("{task}", task)
                .replace("{context}", context)
                .replace("{world_state_summary}", world_state_summary)
                .replace("{executor_memory}", executor_memory or "")
        )
        (dest / "CLAUDE.md").write_text(text, encoding="utf-8")

    # Generate .claude/settings.json (hooks + permissions only).
    # §3.3: no enabledPlugins — executor plugins load via --plugin-dir.
    claude_dir = dest / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    hooks_block = translate_hooks_to_settings(
        hooks_yaml_data, proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
    )
    settings = {
        "hooks": hooks_block.get("hooks", {}),
        "permissions": _build_cc_permissions(defn),
    }
    settings_path.write_text(
        json.dumps(settings, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
