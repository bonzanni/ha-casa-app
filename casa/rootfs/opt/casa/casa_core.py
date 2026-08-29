"""Casa core entry point -- wires everything together."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import shutil
import signal
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

# Ensure the Casa package root is on sys.path regardless of cwd
_CASA_ROOT = str(Path(__file__).resolve().parent)
if _CASA_ROOT not in sys.path:
    sys.path.insert(0, _CASA_ROOT)

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agent_loader import load_all_agents
from authz_grants import CHALLENGES, GRANTS
import callback_http
from bus import BusMessage, BusShutdownError, MessageBus, MessageType
from channel_authz import agent_allowed_on
from channels import ChannelManager, DeliveryOutcome
from claude_runtime import (
    CLAUDE_CLI_PATH,
    CLAUDE_CLI_VERSION,
    verify_effective_cli,
)
from config import AgentConfig
from config_git import init_repo, snapshot_manual_edits
import engagement_quiesce
from engagement_uids import (
    UID_BASE, UNALLOCATED_UID, UidAllocator, UidStateError, owner_uid_or_none,
)
from freshness_reaper import FreshnessReaper
from ingress_identity import (
    IngressIdentityError,
    ingress_identity,
    validate_ingress_identity_table,
)
from log_cid import install_logging, new_cid
from casa_core_middleware import cid_middleware, CasaAccessLogger
from ha_mcp_facade import HomeAssistantFacade
from mcp_registry import McpServerRegistry
from semantic_memory import SemanticMemory
from policies import load_policies
import private_state
from provenance import sanitize_external_context
from session_registry import SessionRegistry
from session_sweeper import SessionSweeper
from rate_limit import RateLimiter, rate_limit_response
from safe_fs import SymlinkRefused, atomic_write_beneath, read_text_beneath
from timekeeping import resolve_tz
from trigger_registry import TriggerRegistry
from voice_delivery_config import load_voice_delivery_config

logger = logging.getLogger(__name__)

CONFIG_DIR = "/config"
DATA_DIR = "/data"


# ---------------------------------------------------------------------------
# Plan 4b/3.6: internal Unix-socket AppRunner for svc-casa-mcp consumption
# ---------------------------------------------------------------------------


async def start_internal_unix_runner(
    *,
    socket_path: str,
    tool_dispatch: dict,
    engagement_registry,
    hook_policies: dict,
    executor_hook_policies: dict | None = None,
    runtime=None,
    telegram_channel=None,
) -> "web.AppRunner":
    """Build and start a second aiohttp AppRunner bound to a Unix socket.

    Routes:
      POST /internal/tools/call    -> _make_internal_tools_call_handler(...)
      POST /internal/hooks/resolve -> _make_internal_hooks_resolve_handler(...)
      POST /admin/reload           -> build_admin_reload_handler(...)
        (Task E.1 -- casactl operator CLI dispatch)
      POST /internal/channel/*     -> channels.channel_handlers._make_channel_handlers(...)
        (E-12 v0.37.0 -- only registered when ``telegram_channel`` is not None;
         Phase 1 exposes just /internal/channel/send_to_topic. Tests and any
         fallback boot path without telegram skip this family entirely, so a
         POST to /internal/channel/* on those runners returns 404.)

    Returns the AppRunner so the caller can `await runner.cleanup()` on
    shutdown. We register an `on_cleanup` hook on the internal app that
    unlinks the socket file when cleanup runs — `web.UnixSite` does not
    do this on its own.

    Parent directory permissions: 0700 if we have to create it. Socket
    permissions: 0600 (root-only). Both processes in the addon container
    run as root, so 0600 is sufficient (no group access needed).
    """
    parent = os.path.dirname(socket_path) or "/"
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    # If a prior instance left a stale socket file, remove it.
    if os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError as exc:
            logger.warning(
                "start_internal_unix_runner: stale socket %s could not be "
                "unlinked: %s", socket_path, exc,
            )

    from internal_handlers import (
        _make_internal_tools_call_handler,
        _make_internal_hooks_resolve_handler,
        build_admin_memory_wipe_handler,
        build_admin_reload_handler,
        admin_peercred_middleware,
    )

    # #467: gate the operator-only /admin/* family to a root peer via
    # SO_PEERCRED. The forwarded /internal/* family passes through (its
    # forwarder is non-root and is authorized per-engagement elsewhere).
    internal_app = web.Application(middlewares=[admin_peercred_middleware])
    internal_app.router.add_post(
        "/internal/tools/call",
        _make_internal_tools_call_handler(
            tool_dispatch=tool_dispatch,
            engagement_registry=engagement_registry,
        ),
    )
    internal_app.router.add_post(
        "/internal/hooks/resolve",
        _make_internal_hooks_resolve_handler(
            hook_policies=hook_policies,
            executor_hook_policies=executor_hook_policies,
            engagement_registry=engagement_registry,
        ),
    )
    # Task E.1 (granular-reload plan): casactl operator CLI POSTs here
    # over the unix socket. Same dispatch path as the casa_reload MCP tool.
    internal_app.router.add_post(
        "/admin/reload",
        build_admin_reload_handler(runtime=runtime),
    )
    # #411: operator memory wipe (casactl memory-wipe --yes). Root-gated by
    # the same peercred middleware; shares memory_wipe's single-flight slot
    # with the consented agent-tool door.
    internal_app.router.add_post(
        "/admin/memory/wipe",
        build_admin_memory_wipe_handler(),
    )

    # Task 14 (personality Phase A): lean inspection/explain admin routes —
    # POST /admin/personality/{inspect,render,diff}, /admin/specialist/status,
    # /admin/explain. Unix-socket-only: registered on internal_app alone,
    # NEVER on the public 8099 app (see casa_core.py's `app` router further
    # down). Skipped when runtime is None (some test/fallback boot paths).
    if runtime is not None:
        from personality_admin_handlers import register_personality_admin_routes

        register_personality_admin_routes(internal_app, runtime=runtime)

    # E-12 (v0.37.0): /internal/channel/* family — POSTed by per-engagement
    # casa-engagement-channel MCP servers proxying outbound traffic to Telegram.
    # Only registered if a TelegramChannel instance is available (production
    # boots always have one; some test paths pass None).
    if telegram_channel is not None:
        from channels.channel_handlers import (
            _make_channel_handlers,
            _make_channel_get_handlers,
        )
        # W1: record each reply() text for the claude_code driver's live
        # topic-stream relay reply de-dup. Resolved lazily — the driver is
        # constructed later (in main), so this closure looks it up at request
        # time via the agent module.
        def _record_engagement_reply(engagement_id: str, text: str) -> None:
            try:
                import agent as _agent_mod
                drv = getattr(_agent_mod, "active_claude_code_driver", None)
                if drv is not None:
                    drv.record_reply_text(engagement_id, text)
            except Exception:  # noqa: BLE001 — de-dup hint is best-effort
                pass

        channel_handlers = _make_channel_handlers(
            telegram_channel=telegram_channel,
            engagement_registry=engagement_registry,
            record_reply=_record_engagement_reply,
        )
        for path, handler_fn in channel_handlers.items():
            internal_app.router.add_post(path, handler_fn)
        channel_get_handlers = _make_channel_get_handlers(
            engagement_registry=engagement_registry,
        )
        for path, handler_fn in channel_get_handlers.items():
            internal_app.router.add_get(path, handler_fn)
        logger.info(
            "E-12: registered %d POST + %d GET /internal/channel/* routes "
            "(POST: %s; GET: %s)",
            len(channel_handlers), len(channel_get_handlers),
            sorted(channel_handlers.keys()), sorted(channel_get_handlers.keys()),
        )

    async def _unlink_socket_on_cleanup(_app: web.Application) -> None:
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "start_internal_unix_runner: unlink %s on cleanup failed: %s",
                socket_path, exc,
            )

    internal_app.on_cleanup.append(_unlink_socket_on_cleanup)

    runner = web.AppRunner(internal_app)
    await runner.setup()
    site = web.UnixSite(runner, socket_path)
    await site.start()
    # web.UnixSite doesn't accept a mode= kwarg; chmod after bind.
    try:
        os.chmod(socket_path, 0o600)
    except OSError as exc:
        logger.warning(
            "start_internal_unix_runner: chmod 0600 on %s failed: %s",
            socket_path, exc,
        )
    logger.info("Internal Unix-socket runner listening on %s", socket_path)
    return runner


# ------------------------------------------------------------------
# Health endpoint
# ------------------------------------------------------------------


async def healthz(_request: web.Request) -> web.Response:
    """Return a simple health-check response."""
    return web.json_response({"status": "ok"})


# ------------------------------------------------------------------
# Plan 4a Phase E: boot replay for claude_code engagements
# ------------------------------------------------------------------


def _regenerate_cc_settings(defn) -> dict:
    """Task 6 (#360): rebuild the CC ``.claude/settings.json`` shape from
    ``defn.hooks_document`` (the Task 3 load-time-validated snapshot) —
    the exact assembly ``drivers.workspace.render_workspace_template`` uses
    for provisioning (``translate_hooks_to_settings`` + ``_build_cc_permissions``),
    reused verbatim here so boot replay and provisioning can never drift.
    Raises whatever the two emitters raise; the caller is responsible for
    fail-closed handling (a snapshot that cannot yield a settings document
    at all is refused, not silently skipped)."""
    from drivers.hook_bridge import translate_hooks_to_settings
    from drivers.workspace import _build_cc_permissions

    hooks_block = translate_hooks_to_settings(
        getattr(defn, "hooks_document", None) or {},
        proxy_script_path="/opt/casa/scripts/hook_proxy.sh",
    )
    return {
        "hooks": hooks_block.get("hooks", {}),
        "permissions": _build_cc_permissions(defn),
    }


def _cc_settings_missing_floor(settings: dict) -> bool:
    """Task 6 (#360) defense-in-depth: verify the regenerated settings
    document actually carries every ``REQUIRED_CLAUDE_CODE_POLICIES`` entry
    in its ``PreToolUse`` block, by policy name (the last whitespace-
    separated token of the proxy command). ``translate_hooks_to_settings``
    already appends the floor unconditionally (Task 4), so this should
    never trip in practice — it exists so a future change to that emitter
    (or a caller that bypasses it) fails boot replay closed rather than
    silently shipping a hollow floor."""
    from hooks import REQUIRED_CLAUDE_CODE_POLICIES

    pre = (settings or {}).get("hooks", {}).get("PreToolUse", []) or []
    declared: set = set()
    for entry in pre:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) or []:
            command = hook.get("command", "") if isinstance(hook, dict) else ""
            if command:
                declared.add(command.rsplit(" ", 1)[-1])
    return bool(REQUIRED_CLAUDE_CODE_POLICIES - declared)


def _workspace_owner_ids(engagements_root: str) -> dict[int, set[str]]:
    """Map each on-disk workspace-owner uid → the set of engagement-id basenames
    it owns under ``engagements_root`` (containment Stage 2, Task 10, Sol r2).

    Boot replay's uid-uniqueness gate consults this so a resumed record whose uid
    already owns a workspace belonging to a DIFFERENT engagement — even one whose
    registry record was pruned/lost, which the record-only check cannot see — is
    refused rather than chowned/started onto a uid that could read that sibling
    workspace. Best-effort: a missing/unreadable root or entry contributes
    nothing. Root-owned (uid 0) dirs are irrelevant here (a dropped uid is always
    ``>= UID_BASE``), but are recorded harmlessly."""
    owners: dict[int, set[str]] = {}
    try:
        with os.scandir(engagements_root) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        owners.setdefault(
                            entry.stat().st_uid, set()).add(entry.name)
                except OSError:
                    continue
    except (FileNotFoundError, NotADirectoryError):
        pass
    return owners


_UNSET = object()   # sentinel: distinguishes "not injected" from an injected None


def _best_effort_kill_uid(uid: int, *, proc_root: str = "/proc",
                          _pidfd_open=_UNSET, _pidfd_send=_UNSET,
                          _close=os.close) -> None:
    """Best-effort SIGKILL of any lingering process whose real/effective/saved/
    fsuid is ``uid`` (an engagement's allocated uid, always ``>= UID_BASE``).

    Containment Stage 2 (S1 r5, secondary defense-in-depth; the durable,
    monotonic uid high-water is the actual non-reissue guarantee): after the
    boot down-first sweep confirms an engagement's SERVICE down, a
    ``setsid``/double-forked NON-root descendant could linger. Kill it so it
    cannot keep reading its own soon-retained workspace. NEVER raises, NEVER
    blocks; only ``uid >= UID_BASE`` is targeted, so casa-core (root) and
    container services are never touched.

    S1 r6 (Sol S2 — PID-reuse race): a numeric ``os.kill`` after a status read
    can hit a REUSED pid (the inspected process exited and its pid was recycled
    for an unrelated, possibly critical process) → self-DoS. Instead: open a
    ``pidfd`` FIRST (pinning that exact process instance), verify the uid via
    the now-current ``/proc/<pid>/status``, then signal via
    ``signal.pidfd_send_signal`` — which only ever reaches the pinned process
    (or ``ESRCH`` if it already exited), never a pid reuser. If the pidfd
    primitives are unavailable, SKIP entirely (this is non-load-bearing DiD)
    rather than signal a bare numeric pid.

    (A CAP_DAC_OVERRIDE / root-equivalent survivor reads any workspace
    regardless of uid; only a legacy pre-Stage-2 ROOT engagement could grant
    such caps to a descendant across the single upgrade boot — post-migration
    engagements are non-root with an empty bounding set + no_new_privs and
    CANNOT. That residual is the excluded root-survivor class, Stage 3
    mount/AppArmor/pid-namespace, not closed here.)"""
    if uid < UID_BASE:
        return
    # v0.170.2: resolve via a sentinel so an injected ``None`` EXPLICITLY means
    # "primitive unavailable" (reaches the skip guard), while the production
    # default (``_UNSET``) uses the real primitives. The prior ``_pidfd_open or
    # getattr(...)`` made an injected ``None`` fall back to the REAL
    # ``os.pidfd_open`` — so the "unavailable" test path never skipped and, in
    # CI (where the fake-proc pid numbers map to real kernel threads),
    # ``os.pidfd_open`` succeeded and a signal was recorded.
    pidfd_open = (getattr(os, "pidfd_open", None)
                  if _pidfd_open is _UNSET else _pidfd_open)
    pidfd_send = (getattr(signal, "pidfd_send_signal", None)
                  if _pidfd_send is _UNSET else _pidfd_send)
    if pidfd_open is None or pidfd_send is None:
        return   # no race-free primitive → skip (never signal a bare pid)
    try:
        entries = list(os.scandir(proc_root))
    except OSError:
        return
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pidfd = None
        try:
            # Pin the process instance BEFORE inspecting it, so the later signal
            # can never reach a reused pid.
            pidfd = pidfd_open(int(entry.name))
            hit = False
            with open(os.path.join(proc_root, entry.name, "status"),
                      "r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.startswith("Uid:"):
                        continue
                    for tok in line.split()[1:]:
                        try:
                            if int(tok) == uid:
                                hit = True
                                break
                        except ValueError:
                            continue
                    break
            if hit:
                pidfd_send(pidfd, signal.SIGKILL)
        except (OSError, ValueError):
            continue   # process vanished / unreadable / already gone — skip
        finally:
            if pidfd is not None:
                try:
                    _close(pidfd)
                except OSError:
                    pass


def _gather_reconstruct_evidence(registry, *, data_dir: str):
    """Gather every real-uid evidence source for ``UidAllocator.reconstruct``
    (Containment Stage 2; S1 r7 completed the artifact set, v0.170.1 corrected
    the fresh-vs-loss key to REAL-uid evidence).

    Returns ``(known_uids, dir_owner_uids)``:
      - ``known_uids``: ``allocated_uid`` of EVERY record incl. terminal/retained
        (a pruned-but-lingering process still holds its uid);
      - ``dir_owner_uids``: the owner uid of every on-disk per-engagement dir
        under ALL three artifact roots — ``<data_dir>/engagements/*``,
        ``/data/engagement-ctl/*``, AND ``/data/plugin-outbox-eng/*`` (a leftover
        dir chowned to a uid the counter never recorded is real-uid evidence).

    Only OWNER uids are returned, NOT a "root exists" boolean: mere existence of
    a pre-Stage-2 (root-owned, uid 0) engagement dir is NOT evidence a uid was
    allocated — treating it as such refused allocation on every existing
    install's first Stage-2 upgrade boot (v0.170.0 regression). ``reconstruct``
    keys the fresh-vs-loss decision on whether any evidenced value is
    ``>= UID_BASE``. Every source only ever RAISES the high-water; scanning is
    best-effort (a missing/unreadable root or entry contributes nothing)."""
    from plugin_outbox import ENGAGEMENT_OUTBOX_ROOT
    known_uids = [
        r.allocated_uid
        for r in (list(registry.active_and_idle())
                  + list(registry.terminal_records()))
        if r.allocated_uid != UNALLOCATED_UID
    ]
    artifact_roots = (
        os.path.join(data_dir, "engagements"),
        "/data/engagement-ctl",
        ENGAGEMENT_OUTBOX_ROOT,
    )
    dir_owner_uids: list[int] = []
    for base in artifact_roots:
        try:
            with os.scandir(base) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            dir_owner_uids.append(entry.stat().st_uid)
                    except OSError:
                        continue
        except (FileNotFoundError, NotADirectoryError):
            continue
    return known_uids, dir_owner_uids



def read_followup_incomplete(engagement_driver, rec, token) -> str:
    """#692: the ``_driver_turn_incomplete`` seam's whole body, at module
    level so it can be tested as production code rather than reimplemented in
    a test double.

    SYNCHRONOUS. Returns ``""`` for a ``claude_code`` record (that driver has
    its own failure ownership) and for any driver that does not expose the
    accessor.

    ``getattr``-guarded on purpose: ``followup_turn_incomplete`` is
    deliberately NOT on ``DriverProtocol``, so a driver without it must read
    as "nothing to report" rather than raise ``AttributeError`` into a
    delivery task that would then surface a failure for a healthy turn.

    There is deliberately NO ``token is None`` guard. The accessor is already
    total over a ticket it does not hold — the observation map is keyed by the
    admission ticket and the writing branch requires a non-None one, so
    ``None`` is never a key and a lookup with no ticket returns "" on its own.
    A guard whose removal changes no observable behaviour cannot be pinned by
    any test, and an unpinnable guard is the thing this change was told to cut
    rather than sharpen: the mutation sweep surfaced it exactly that way, as an
    arm that survived because it was redundant and not because its test was
    weak.
    """
    if rec.driver == "claude_code":
        return ""
    fn = getattr(engagement_driver, "followup_turn_incomplete", None)
    return fn(rec.id, token) if callable(fn) else ""


async def replay_undergoing_engagements(
    *, registry, driver, executor_registry=None,
    engagements_root: str = "/data/engagements",
    telegram_ready=None,
) -> None:
    """On Casa boot: reconstruct s6 services for UNDERGOING claude_code engagements.

    Heal path (Plan 4a.1): when a UNDERGOING engagement's service dir is
    missing but the workspace dir at /data/engagements/<id>/ still exists
    and the executor is registered, re-render the run script and re-plant
    the s6 service dir. Missing workspace dir remains a warn-and-skip case
    (§7.3 of the 4a.1 spec).
    """
    from drivers import s6_rc
    from drivers.claude_code_driver import (
        UidDropRefused, _check_plugin_dirs_readable,
    )
    from drivers.workspace import (
        chown_workspace, fifo_path, refresh_claude_md, render_log_run_script,
        render_run_script, workspace_mcp_token, workspace_mcp_url,
        write_workspace_mcp_json,
    )

    # Containment Stage 2 (Task 10): the allocator injected into the registry at
    # boot (casa_core wiring). Used to regenerate each resumed uid's NSS identity
    # (container /etc is ephemeral — design §7). Backfill of a legacy record's
    # uid goes through ``registry.backfill_allocated_uid`` (strict-persisted).
    _uid_allocator = getattr(registry, "_uid_allocator", None)

    undergoing = [
        r for r in registry.active_and_idle()
        if r.driver == "claude_code"
    ]
    keep_ids = {r.id for r in undergoing}
    # §3.8/Sol F5: engagements whose recorded plugin artifacts are gone are
    # refused resume — and MUST be excluded from the start_service and
    # background-task loops below, not merely skipped during rendering.
    refused_ids: set[str] = set()
    # #369 (Sol diff-gate r2): records whose clearance-downgrade context
    # reset ran this boot — their durable rebuild flag clears only AFTER
    # their service start succeeds; every refusal path leaves it set.
    pending_rebuild_started_ids: set[str] = set()

    # v0.83.0 (§A3(b), Sol r6-3/r7-3/4): the BOOT open-question reconciliation
    # owner. Take a PRE-SERVICE snapshot of every claude_code record that has
    # outstanding raw open_questions AND a topic — REGARDLESS of terminal status
    # (the ownership predicate; the summary-adoption-failure path mark_error's the
    # record TERMINAL before refused_ids is even consulted, so a non-terminal
    # filter would miss exactly the case that must still settle). Snapshotting
    # HERE, before any service start / background-task spawn, preserves the
    # invariant that a fresh same-process ask registered by a just-resumed CLI is
    # never captured + expired. A shared claimed-set guarantees exactly one
    # reconciler per record per boot (the attached driver pass OR the casa_core
    # pass below — never both).
    reconcile_snapshots: dict[str, list[dict]] = {}
    reconcile_claimed: set[str] = set()
    _seen_snapshot_ids: set[str] = set()
    for _rec in (list(registry.active_and_idle())
                 + list(registry.terminal_records())):
        if _rec.id in _seen_snapshot_ids:
            continue
        _seen_snapshot_ids.add(_rec.id)
        if getattr(_rec, "driver", None) != "claude_code":
            continue
        if getattr(_rec, "topic_id", None) is None:
            continue
        # B3: record the per-record snapshot ALWAYS — an EMPTY list stays [] (a
        # replay context that reconciles NOTHING), distinct from a missing entry.
        # A record whose open_questions is empty at snapshot time must reconcile
        # nothing so a fresh same-process ask created BETWEEN this snapshot and
        # attach is never fresh-read + expired as prior-process.
        _oq = list(getattr(_rec, "open_questions", ()) or ())
        reconcile_snapshots[_rec.id] = [dict(q) for q in _oq]

    async def _refuse_brief_resume(
        rec, reason: str, *, kind: str = "refuse_teardown_failed",
    ) -> None:
        """Fail-closed teardown of an engagement we refuse to resume
        (r11-B1/r13-B1/r14-B1; B2 Sol r2 reuses it for the migration path via
        ``kind``). Source removal + recompile alone is NOT reliable —
        ``remove_service_dir`` swallows OSError and the later compile can
        fail-and-continue — so run the CHECKED teardown ladder, and land a
        TERMINAL ``kind`` mark when physical containment can't be confirmed
        (the marking ACCOMPANIES the removal, it does not replace it).
        ``registry`` is the real parameter name here; ``_engagement_registry``
        does not exist in this function and would NameError straight into the
        per-record warn-and-continue."""
        logger.warning(
            "boot replay: engagement %s refuses resume — %s; "
            "tearing down", rec.id[:8], reason,
        )
        refused_ids.add(rec.id)
        down = await s6_rc.ensure_service_down(engagement_id=rec.id)
        if down is False:
            try:
                await registry.mark_error(
                    rec.id, kind=kind,
                    message=(
                        f"resume refused ({reason}) but the engagement "
                        "service could not be confirmed down"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — best-effort terminal mark
                logger.warning(
                    "boot replay: mark_error(%s) failed for %s: %s",
                    kind, rec.id[:8], exc,
                )
        s6_rc.remove_service_dir(
            svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT, engagement_id=rec.id,
        )

    async def _ensure_stdin_fifo(rec) -> bool:
        """Sol r3-2 (#342): ensure the workspace stdin.fifo exists before
        ANY path lets this record reach start_service — the run template
        ``set -e``-reads it, so a missing FIFO is a guaranteed crash-loop.
        Runs on BOTH the complete-pair fast path and the heal path: an
        intact pair does not imply an intact FIFO (a prior refusal's pair
        removal is best-effort and its error mark can fail, leaving an
        intact pair + undergoing record for this boot's fast path).
        True = present/created; False = resume refused via the
        checked-teardown ladder (retryable next boot when containment is
        confirmed, terminal-marked when it is not).

        Task 4 (containment stage 2): the FIFO itself lives in the root-only
        control dir now, not the workspace — moved so the workspace-symlink
        primitive this whole stage removes could never again retarget IT.
        The workspace-symlink guard below is kept regardless: other boot-
        replay touches (CLAUDE.md refresh, .mcp.json rewrite) still reach
        INTO the workspace and are not yet made symlink-safe (Task 5), so a
        symlinked workspace is still refused here as general corruption —
        this is the one boot-replay step that already runs for every record
        early enough to catch it. The control dir gets its OWN symlink guard
        (a real one is never created there, but a repair must never trust
        that blindly)."""
        ws_dir = os.path.join(engagements_root, rec.id)
        if os.path.islink(ws_dir):
            await _refuse_brief_resume(
                rec,
                "workspace directory is a symlink; refusing FIFO "
                "verification/repair through it",
                kind="fifo_recreate_failed",
            )
            return False
        fifo = fifo_path(rec.id)
        ctl_dir = os.path.dirname(fifo)
        # Sol r5-1: never run the destructive repair below THROUGH a
        # symlinked control dir — isdir/lstat would examine (and rmtree
        # could delete) entries under the symlink's target. Control dirs
        # are always created as real directories, so a symlink here is
        # corruption: refuse the resume.
        if os.path.islink(ctl_dir):
            await _refuse_brief_resume(
                rec,
                "control directory is a symlink; refusing FIFO "
                "verification/repair through it",
                kind="fifo_recreate_failed",
            )
            return False
        try:
            # Task 4: the control dir may not exist yet for an engagement
            # provisioned before this release (or lost to a prior partial
            # failure) — repair it here too, idempotently, rather than
            # silently skipping FIFO creation the way the pre-Task-4 code
            # skipped it for a missing WORKSPACE (M7 already refused those
            # earlier in this same boot pass).
            if not os.path.isdir(ctl_dir):
                os.makedirs(ctl_dir, mode=0o700, exist_ok=True)
            st = os.lstat(fifo) if os.path.lexists(fifo) else None
            if st is not None and not stat.S_ISFIFO(st.st_mode):
                # Terra r4-1: a NON-fifo at the FIFO path (regular
                # file, dir, or symlink left by corruption/partial
                # recovery) reads as instant EOF or fails outright —
                # the same crash-loop as a missing FIFO. Repair by
                # replacing it; a repair failure takes the refusal
                # ladder below.
                logger.warning(
                    "boot replay: %s exists but is not a FIFO — "
                    "replacing it", fifo,
                )
                if stat.S_ISDIR(st.st_mode):
                    shutil.rmtree(fifo)
                else:
                    os.remove(fifo)
                st = None
            if st is None:
                os.mkfifo(fifo, 0o600)
            return True
        except OSError as exc:
            await _refuse_brief_resume(
                rec,
                f"stdin.fifo recreation failed ({exc}); the run template "
                "reads this FIFO, so starting would crash-loop",
                kind="fifo_recreate_failed",
            )
            return False

    # §A3(b) boot reconciliation owner — TERMINAL records (Sol r7-3): terminal
    # records are DISJOINT from ``undergoing`` (they never attach), so schedule
    # their readiness-gated reconcile HERE, BEFORE the compile lock — the lock's
    # fast-path return (no undergoing + no orphans) would otherwise skip the tail
    # of this function and a terminal summary-adoption-failure record with a live
    # question would never settle. Claimed so the refused-undergoing pass below
    # never double-settles.
    _schedule_reconcile = getattr(driver, "schedule_boot_reconcile", None)
    if _schedule_reconcile is not None:
        for _trec in registry.terminal_records():
            _tsnap = reconcile_snapshots.get(_trec.id)
            if not _tsnap or _trec.id in reconcile_claimed:
                continue
            try:
                _schedule_reconcile(
                    _trec, _tsnap, telegram_ready, claimed=reconcile_claimed)
            except Exception as exc:  # noqa: BLE001 — best-effort per record
                logger.warning(
                    "boot replay: terminal open-question reconcile for %s "
                    "failed to schedule: %s", _trec.id[:8], exc,
                )

    async with s6_rc._compile_lock:
        # 0. DOWN FIRST — Containment Stage 2 migration (design §6). Before ANY
        # registry-status filtering, workspace access, or migration, drive EVERY
        # existing engagement service to a CONFIRMED down. This is scandir-driven
        # (source dirs + live scandir), NOT record-driven: a record that went
        # terminal *before* driver.cancel() ran (crash between mark_* and cancel)
        # leaves a supervised ROOT service that no UNDERGOING loop would touch —
        # so it is enumerated from the filesystem directly and killed here. Every
        # PRE-Stage-2 run script exec'd ``claude`` as root; downing them all now,
        # before the re-render below plants the uid-dropped form, is what
        # migrates in-flight engagements off root without ever leaving two
        # generations (old root + new dropped) supervised at once. A service that
        # cannot be confirmed down blocks its own engagement (kept durably down +
        # mark_error), never left running as root and never re-started below.
        # S1 r5 (secondary): map each engagement id → its allocated uid so the
        # down-first sweep can best-effort kill escaped non-root descendants
        # (hygiene; the durable high-water is the real guarantee).
        _svc_uid_by_id: dict[str, int] = {}
        for _r in (list(registry.active_and_idle())
                   + list(registry.terminal_records())):
            _u = getattr(_r, "allocated_uid", UNALLOCATED_UID)
            if _u >= UID_BASE:
                _svc_uid_by_id[_r.id] = _u

        for _svc_eid in s6_rc.iter_engagement_service_ids(
            svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
            scandir_root=s6_rc.SERVICE_SCANDIR_ROOT,
        ):
            try:
                _pre_down = await s6_rc.ensure_service_down(
                    engagement_id=_svc_eid)
            except Exception as exc:  # noqa: BLE001 — teardown is best-effort
                _pre_down = False
                logger.warning(
                    "boot replay: pre-migration ensure_service_down raised for "
                    "%s: %s", _svc_eid[:8], exc,
                )
            # Best-effort: after the service is downed, SIGKILL any escaped
            # non-root descendant still holding this engagement's uid (S1 r5
            # secondary). Never blocks, never raises; only runs when the uid is
            # known and real. Not the guarantee — the durable high-water is.
            _esc_uid = _svc_uid_by_id.get(_svc_eid)
            if _esc_uid is not None:
                _best_effort_kill_uid(_esc_uid)
            if _pre_down is False:
                # Cannot confirm this service down → refuse to migrate/start it.
                # It is NOT added to any start loop (refused_ids) and is marked
                # error best-effort (a pure scandir orphan with no record just
                # no-ops the mark). Its still-supervised state is a durable-down
                # attempt already made by ensure_service_down's ladder.
                logger.error(
                    "boot replay: engagement service %s could not be confirmed "
                    "down before migration — refusing resume (never re-started "
                    "as root)", _svc_eid[:8],
                )
                refused_ids.add(_svc_eid)
                try:
                    await registry.mark_error(
                        _svc_eid,
                        kind="refuse_pre_migration_down_failed",
                        message=(
                            "engagement service could not be confirmed down "
                            "before the Stage 2 uid migration"
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort mark
                    logger.warning(
                        "boot replay: mark_error(refuse_pre_migration_down_"
                        "failed) for %s failed: %s", _svc_eid[:8], exc,
                    )

        # 0a. Private-state mode repair — GHSA-569r-7crq-xr43. Placed HERE, and
        # not earlier in boot, because ordering is the whole point: step 0 above
        # has just driven every existing engagement service to a CONFIRMED down,
        # so at this instant no uid-dropped process exists and the repair cannot
        # race a reader. (Running it before replay would leave a real interval —
        # a casa-main-only respawn leaves the previous generation's engagement
        # longrun wanted-up and running until step 0 downs it.) It is also before
        # the compile lock's fast-path return below, so it runs on EVERY boot,
        # including one with zero records.
        #
        # This pass, not the write sites, is what repairs an already-deployed
        # install: every affected file already exists at 0644 and atomic_io
        # preserves an existing file's mode, so a file whose next write is days
        # away would otherwise stay exposed.
        _ps_report = private_state.enforce()
        if _ps_report.changed:
            logger.info(
                "private-state: repaired %d path mode(s) on this boot",
                len(_ps_report.changed),
            )

        # 0c. Credential-exposure gate — the same shape as 0b below. A
        # credential-class repair that FAILED means Casa cannot honour the
        # guarantee the uid drop exists for, so no uid-dropped engagement runs
        # this boot; the services are already down from step 0, so this keeps
        # them down rather than starting something that can read a Supervisor
        # bearer token. Deliberately NOT boot-fatal (both design reviewers):
        # Telegram, the resident agents and specialist engagements stay up, so a
        # read-only /run or a restored backup degrades rather than bricking.
        # Re-stated from the filesystem rather than from _ps_report, so this
        # decides on what is true now.
        _cred_offenders = private_state.credential_modes_ok()
        if undergoing and _cred_offenders:
            logger.critical(
                "boot replay: %d credential path(s) are still readable beyond "
                "root (%s) — refusing ALL %d claude_code engagement resume(s) "
                "this boot (kept down by the pre-migration sweep). Fix the file "
                "modes and restart to resume.",
                len(_cred_offenders), ", ".join(_cred_offenders),
                len(undergoing),
            )
            for rec in undergoing:
                if rec.id in refused_ids:
                    continue
                refused_ids.add(rec.id)
                try:
                    await registry.mark_error(
                        rec.id,
                        kind="refuse_private_state_exposed",
                        message=(
                            "private runtime state is readable beyond root "
                            f"({', '.join(_cred_offenders)}); refusing to start "
                            "a uid-dropped engagement"
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort mark
                    logger.warning(
                        "boot replay: mark_error(refuse_private_state_exposed) "
                        "for %s failed: %s", rec.id[:8], exc,
                    )

        # 0b. setpriv presence gate — Containment Stage 2 (design §5, §6(g)
        # acceptance criterion: "setpriv removed from image ⇒ engagement
        # REFUSED, not crash-looping, not root"). A single BOOT-level check (not
        # per render): every re-rendered run script ends in ``exec setpriv …``
        # under ``set -e``, so if setpriv is absent from the image the started
        # service would fail that exec and s6 would crash-loop it. Refuse EVERY
        # claude_code resume this boot instead — the services are already downed
        # by step 0, so this keeps them down (never started, never root, never
        # crash-looping) and lands an operator-visible terminal mark. Placed
        # after the down-first sweep so the refusal builds on already-down
        # services.
        if undergoing and shutil.which("setpriv") is None:
            logger.critical(
                "boot replay: setpriv not found on PATH — refusing ALL %d "
                "claude_code engagement resume(s) this boot (kept down by the "
                "pre-migration sweep, never started as a crash-looping or root "
                "service). Restore setpriv in the image to resume.",
                len(undergoing),
            )
            for rec in undergoing:
                if rec.id in refused_ids:
                    continue
                refused_ids.add(rec.id)
                try:
                    await registry.mark_error(
                        rec.id, kind="refuse_uid_drop_failed",
                        message=(
                            "setpriv missing from image — the run script's "
                            "privilege drop cannot execute; resume refused "
                            "rather than started as a crash-looping service"
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort mark
                    logger.warning(
                        "boot replay: mark_error(refuse_uid_drop_failed) for "
                        "%s failed: %s", rec.id[:8], exc,
                    )

        # 0c. Re-fold live /proc uids into the uid high-water NOW that the
        # down-first sweep has confirmed every engagement service down (S1
        # code-gate r3 — TOCTOU). The boot-time uid reconstruct() ran BEFORE
        # this sweep, so a legacy engagement could have setsid/double-forked a
        # non-root survivor under a not-yet-issued uid AFTER that scan but
        # BEFORE its service was killed here — the boot scan would have missed
        # it. With every legacy service now confirmed down (step 0), no service
        # can spawn a NEW survivor past this point, and this re-scan captures
        # any that escaped earlier, so the high-water folds it in and no
        # backfill below can reissue a uid a live process still holds.
        # Fail-CLOSED: an unscannable /proc (or a never-reconstructed allocator)
        # sets ``_refold_failed``, which refuses every resume NEEDING a fresh
        # uid allocation (a legacy record's backfill) rather than allocating
        # against a stale/unknown high-water. (A legacy ROOT survivor that
        # escapes the sweep and STAYS root is a separate pre-existing class — a
        # root process reads siblings regardless of uid — closed by Stage 3
        # mount/AppArmor, not this uid-isolation fix.) A missing allocator (unit
        # fakes) skips the refold; production always wires one.
        # #599: discharge every OUTSTANDING uid-quiesce obligation before any
        # resume. A casa death between a terminal commit and its ladder leaves
        # the obligation durable on the record (``quiesce_pending``, exempt from
        # terminal expiry precisely so it survives to be found here); this is
        # where it is honoured. Runs after the down-first sweep, so each service
        # is already confirmed down and the ladder kills leftovers rather than
        # racing a live supervisor. Bounded and reporting: unlike the
        # best-effort uid kill above, an extinction that cannot be observed is
        # logged as an ERROR and the obligation STAYS on the record for the next
        # boot rather than being silently dropped.
        for _owing in list(registry.records_owing_quiesce()):
            try:
                _q = await engagement_quiesce.quiesce_engagement(
                    engagement_id=_owing.id,
                    uid=getattr(_owing, "allocated_uid", UNALLOCATED_UID),
                    latch_down=s6_rc.latch_down,
                    wanted_down=s6_rc.wanted_down,
                )
            except Exception as exc:  # noqa: BLE001 — never abort boot replay
                logger.warning(
                    "boot replay: uid quiesce for %s raised: %s",
                    _owing.id[:8], exc)
                continue
            if _q.extinct:
                try:
                    await registry.clear_quiesce_pending(_owing.id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "boot replay: clearing the quiesce obligation for %s "
                        "failed: %s", _owing.id[:8], exc)

        _refold_failed = False
        if _uid_allocator is not None:
            try:
                _uid_allocator.refold_live_uids()
            except UidStateError as exc:
                _refold_failed = True
                logger.critical(
                    "boot replay: live /proc uid refold failed (%s) — every "
                    "resume needing a fresh uid allocation will be REFUSED this "
                    "boot (fail-closed) rather than risk reissuing a uid held "
                    "by a live survivor process", exc,
                )

        # 1. Orphan sweep — dirs for non-UNDERGOING engagements, remove them.
        removed_orphans = s6_rc.sweep_orphan_service_dirs(
            svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
            keep_engagement_ids=keep_ids,
        )
        # Also reap stale /tmp/s6-casa-db-* dirs left by the previous
        # container run (L12 leak guard) — /run is fresh tmpfs after a
        # restart, so any prior compiled db still in /tmp is orphaned.
        s6_rc.sweep_orphan_compiled_dbs()

        # Fast path: no UNDERGOING engagements and no orphans were swept →
        # the engagement sources dir is empty and unchanged. Running
        # s6-rc-compile against an empty source dir prints
        # "source /data/casa-s6-services is empty" to stderr at every boot,
        # plus burns one compile + one s6-rc-update for nothing. Skip it.
        if not undergoing and not removed_orphans:
            return

        # 2. Heal missing/incomplete service pairs for UNDERGOING
        # engagements. v0.64.0: 'main dir present' no longer means 'unit
        # present' — the predicate is pair-completeness, so torn halves AND
        # legacy (≤v0.63.x, nested-log/) dirs are re-planted, migrating
        # in-flight engagements to the working log pipeline. Each record
        # heals independently: one failure must not abort the others'
        # compile/start below.
        # Containment Stage 2 (Task 10): uid-uniqueness gate across the WHOLE
        # registry (Sol+Terra review r1). ``_migrated_uids`` alone only catches
        # a collision between two records BOTH resumed this boot; a uid shared
        # with a TERMINAL/retained (or otherwise non-undergoing) record would
        # slip through — and that record's retained workspace is chowned to the
        # very uid we would hand this resume, so starting here would let this
        # CLI read a sibling's files. The monotonic allocator prevents NEW
        # collisions, but a pre-existing corrupt duplicate on disk
        # (hand-edited / torn tombstone) must still be refused. Snapshot every
        # record's real uid → owning id(s) so a resumed record whose uid is
        # owned by a DIFFERENT engagement is refused, not migrated.
        _all_records = (list(registry.active_and_idle())
                        + list(registry.terminal_records()))
        # Sol r2: also snapshot uid → owning workspace-id basenames on disk, so a
        # uid owning a sibling workspace whose RECORD was pruned/lost is caught
        # too (the record-only map cannot see it). Together these close the whole
        # uid-uniqueness class: records ∪ on-disk workspace owners.
        _ws_owner_ids = _workspace_owner_ids(engagements_root)
        _migrated_uids: set[int] = set()
        for rec in undergoing:
            # Step 0 refused this record (its pre-migration service could not be
            # confirmed down) — never migrate/start it. Skip before any workspace
            # write or uid allocation.
            if rec.id in refused_ids:
                continue
            try:
                # #314: the M7 (missing workspace) and §3.8 (missing recorded
                # artifact) validations must gate EVERY resume — they used to
                # sit below the service_pair_complete fast path, so an intact
                # pair skipped both: replay started a service whose run script
                # does `set -e; cd <workspace>` (instant exit, s6 respawn
                # loop) or resumed a CLI with a missing --plugin-dir target.
                # Placed FIRST in the loop (Terra r1): the brief CLAUDE.md
                # refresh below would otherwise fail on the missing workspace
                # and take the checked-teardown refusal — possibly terminal-
                # marking a record M7 deliberately retains — and the #335
                # credential refresh must not cycle a record we refuse.
                #
                # M7: missing workspace ⇒ REFUSED (was warn-and-skip, which
                # the start loop ignored — it filters on refused_ids alone,
                # so the service started regardless, #314(3)). The record
                # stays UNDERGOING (no terminal mark): /data is the workspace
                # home, so this state is unrecoverable-but-diagnosable and
                # re-warns each boot rather than silently retiring.
                _ws_dir = os.path.join(engagements_root, rec.id)
                if not os.path.isdir(_ws_dir):
                    logger.warning(
                        "boot replay: workspace dir %s missing for "
                        "engagement %s — refusing resume (M7, #314)",
                        _ws_dir, rec.id[:8],
                    )
                    refused_ids.add(rec.id)
                    continue

                # §3.8: replay renders --plugin-dir flags from the RECORDED
                # artifacts, never a re-resolution of current assignments. A
                # missing recorded artifact refuses resume (fail-closed) —
                # start/background loops skip it via refused_ids.
                missing = [pa for pa in rec.plugin_artifacts
                           if not os.path.isdir(pa.get("path", ""))]
                if missing:
                    names = ", ".join(pa.get("name", "?") for pa in missing)
                    logger.warning(
                        "boot replay: engagement %s refuses resume — plugin "
                        "artifact(s) missing: %s", rec.id[:8], names)
                    if rec.topic_id is not None:
                        try:
                            await driver._send_to_topic(
                                rec.topic_id,
                                "⚠️ This engagement can't resume: its pinned "
                                f"plugin artifact(s) are missing ({names}). "
                                "Start a new engagement.")
                        except Exception:  # noqa: BLE001 — best-effort notice
                            pass
                    refused_ids.add(rec.id)
                    continue

                # Task 6 (#360): resolve the executor definition for EVERY
                # resumed claude_code record via definition_any — NOT gated
                # on origin["brief"] (Sol/Terra r2, casa_core.py:507): the
                # settings.json floor-regeneration below is a containment
                # requirement, not a brief feature, so a task-only record
                # (no brief) must be covered exactly like a brief-bearing
                # one — it used to skip this whole block and never got its
                # floor re-verified after a post-load hooks.yaml edit.
                # Resolves DISABLED specialists too (r10-B5: a specialist
                # disabled after launch still resumes); registry absent OR
                # unresolved -> fail-closed refuse (checked teardown).
                defn_any = (
                    executor_registry.definition_any(rec.role_or_type)
                    if executor_registry is not None else None
                )
                if defn_any is None:
                    await _refuse_brief_resume(
                        rec,
                        "no executor_registry passed"
                        if executor_registry is None
                        else f"executor type {rec.role_or_type!r} "
                             "not resolvable (definition_any → None)",
                    )
                    continue

                # #369: a crash between a clearance clamp and its context
                # rebuild leaves context_rebuild_pending set — the recorded
                # session and the cached executor-memory archive were built
                # ABOVE the record's clamped floor. Blank the archive cache
                # and drop the control-dir session pointer BEFORE any refresh
                # or service start: the run template then spawns a FRESH
                # session, the CLAUDE.md refresh below renders from the
                # (clamp-withheld) record, and the replayed engagement comes
                # up at the floor — which IS the rebuild, so clear the flag.
                if getattr(rec, "context_rebuild_pending", False):
                    from drivers.workspace import (
                        executor_memory_path, session_id_path,
                    )
                    try:
                        _mem = Path(executor_memory_path(rec.id))
                        if _mem.is_file():
                            _mem.write_text("", encoding="utf-8")
                        Path(session_id_path(rec.id)).unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "boot replay: engagement %s context reset failed "
                            "(%s) — refusing resume (flag stays set)",
                            rec.id[:8], exc)
                        refused_ids.add(rec.id)
                        continue
                    # Force the CLAUDE.md re-render here: the clamp popped
                    # origin["brief"], so the brief-gated refresh below will
                    # NOT run for this record, yet the workspace still holds
                    # the pre-clamp task text.
                    try:
                        refresh_claude_md(
                            os.path.join(engagements_root, rec.id),
                            defn=defn_any, rec=rec)
                    except Exception as exc:  # noqa: BLE001 — fail-closed
                        logger.warning(
                            "boot replay: engagement %s floor CLAUDE.md "
                            "re-render failed (%s) — refusing resume",
                            rec.id[:8], exc)
                        refused_ids.add(rec.id)
                        continue
                    # Sol diff-gate r2: the flag clears only once THIS
                    # record's service has actually started (below) — a
                    # durable clear here, followed by any later refusal,
                    # would leave a live unflagged record whose next turn
                    # skips the rebuild it still needs.
                    pending_rebuild_started_ids.add(rec.id)
                    logger.info(
                        "boot replay: engagement %s context reset at the "
                        "clamped floor (fresh session, archive cleared); "
                        "flag clears after service start", rec.id[:8])

                # W3 (r8-B5/r9-B5): re-render the workspace CLAUDE.md from the
                # VERBATIM origin["brief"] for EVERY resumed brief-bearing
                # engagement — placed BEFORE the service_pair_complete fast
                # path (but after the #314 refusal guards, so its failure
                # path never fires for a record M7 already refused).
                # /data/casa-s6-services persists across restarts, so an
                # ordinary restart takes the early `continue`; a refresh after
                # the pair-rewrite would never run.
                brief_defn = None
                has_brief = bool(rec.origin.get("brief"))
                if has_brief:
                    brief_defn = defn_any
                    ws_dir = os.path.join(engagements_root, rec.id)
                    try:
                        refresh_claude_md(ws_dir, defn=brief_defn, rec=rec)
                    except Exception as exc:  # noqa: BLE001 — fail-closed
                        await _refuse_brief_resume(
                            rec, f"CLAUDE.md refresh failed: {exc}",
                        )
                        continue

                # #335 + Task 6 (#360): refresh the workspace credential from
                # the RECORD's auth token AND regenerate .claude/settings.json
                # from defn_any.hooks_document — for EVERY resumed claude_code
                # record. Placed, like the CLAUDE.md refresh above, BEFORE the
                # service_pair_complete fast-path continue (Sol/Terra r2,
                # casa_core.py:649: the fast path used to resume WITHOUT
                # regeneration) — an ordinary restart takes that continue, so
                # a refresh after it would never run — but AFTER the refusal
                # checks, so a record we are about to tear down is not cycled
                # first. This is what keeps a pre-upgrade workspace resumable:
                # load() backfilled a token onto its record, and the CLI reads
                # the refreshed credential from disk when it respawns; a
                # hollowed or stale settings.json is likewise repaired from
                # the load-time-validated snapshot.
                #
                # Only rewrite when something actually CHANGED, and then force
                # the engagement's CLI to respawn (Terra + Sol, review r1;
                # extended to settings by Task 6): engagement services are
                # supervised independently of casa-main, so one can already be
                # running — with the OLD ``.mcp.json``/``settings.json``
                # cached at its spawn — while this boot mints a fresh
                # credential or a regenerated floor for it. A rewrite alone is
                # inert against a CLI that already cached settings at spawn.
                # ``start_service`` below is idempotent and would NOT respawn
                # it. Bring it down here, once every changed file is durable,
                # so that start brings it back up reading the current state.
                # Both triggers share ONE cycle decision (level-triggered,
                # like the token+URL merge below) rather than cycling the
                # service twice in the same boot. The fully-unchanged case
                # (every ordinary restart) writes nothing and cycles nothing.
                #
                # v0.164.0 (Terra, plan r1): the baked casa-framework URL is
                # part of the same identity — a workspace whose .mcp.json
                # points anywhere but the served endpoint (e.g. a pre-v0.14.0
                # 8099 URL, whose public fallback routes are gone) would keep
                # a matching token and never be repaired by the token check
                # alone. Level-triggered: compare both, rewrite on either.
                _ws_dir = os.path.join(engagements_root, rec.id)
                _current_mcp_url = getattr(
                    driver, "_casa_framework_mcp_url",
                    "http://127.0.0.1:8100/mcp/casa-framework",
                )
                # Task 5: owner_uid is the record's REAL allocated uid (once
                # Task 8 chowns the workspace), else None for a
                # not-yet-allocated/legacy workspace — never 0.
                _owner_uid = owner_uid_or_none(rec.allocated_uid)
                _credential_changed = bool(
                    os.path.isdir(_ws_dir)
                    and (workspace_mcp_token(_ws_dir, owner_uid=_owner_uid)
                         != rec.auth_token
                         or workspace_mcp_url(_ws_dir, owner_uid=_owner_uid)
                         != _current_mcp_url)
                )

                # Task 6: regenerate settings.json from the load-time
                # snapshot. Fail closed — a snapshot that cannot yield a
                # settings document, or that yields one missing the
                # containment floor, refuses the resume rather than shipping
                # a partial/hollow rewrite.
                try:
                    _new_settings = _regenerate_cc_settings(defn_any)
                except Exception as exc:  # noqa: BLE001 — fail-closed
                    await _refuse_brief_resume(
                        rec,
                        f"settings.json regeneration failed: {exc}",
                        kind="refuse_settings_regenerate_failed",
                    )
                    continue
                if _cc_settings_missing_floor(_new_settings):
                    await _refuse_brief_resume(
                        rec,
                        "regenerated settings.json snapshot does not carry "
                        "the containment floor",
                        kind="refuse_settings_floor_missing",
                    )
                    continue
                _settings_path = os.path.join(
                    _ws_dir, ".claude", "settings.json")
                _on_disk_settings = None
                if os.path.isdir(_ws_dir):
                    # Task 5: compare-read via safe_fs — a symlinked
                    # .claude/settings.json (e.g. planted pointing at a
                    # sibling engagement's workspace) is refused rather than
                    # followed. Fail-safe: refusal (like any other read
                    # failure) leaves _on_disk_settings None, which never
                    # equals _new_settings, so the floor is (re)written.
                    try:
                        _on_disk_settings = json.loads(read_text_beneath(
                            _ws_dir, os.path.join(".claude", "settings.json"),
                            owner_uid=_owner_uid,
                        ))
                    except SymlinkRefused as exc:
                        logger.warning(
                            "boot replay: engagement %s settings.json "
                            "compare-read refused (symlink) — treating as "
                            "changed: %s", rec.id[:8], exc,
                        )
                        _on_disk_settings = None
                    except (OSError, ValueError):
                        _on_disk_settings = None
                _settings_changed = _on_disk_settings != _new_settings

                if _credential_changed:
                    try:
                        write_workspace_mcp_json(
                            _ws_dir,
                            engagement_id=rec.id,
                            engagement_auth_token=rec.auth_token,
                            casa_framework_mcp_url=_current_mcp_url,
                            owner_uid=_owner_uid,
                        )
                    except Exception as exc:  # noqa: BLE001 — fail-closed
                        await _refuse_brief_resume(
                            rec, f".mcp.json refresh failed: {exc}",
                            kind="refuse_credential_refresh_failed",
                        )
                        continue

                if _settings_changed and os.path.isdir(_ws_dir):
                    # Containment Stage 2 (S1 code-gate fix): the settings
                    # regeneration is a root WRITE into the (on replay,
                    # uid-owned) workspace, so it must be symlink-safe. The
                    # prior ``makedirs`` + open-``.tmp`` + ``os.replace`` chain
                    # created the temp BY PATHNAME under ``.claude``, so an
                    # A-planted ``.claude`` symlink into a sibling's dir let
                    # root clobber the sibling's settings.json. Route the write
                    # through ``safe_fs.atomic_write_beneath`` (no-follow,
                    # workspace-confined, renameat): a symlinked ``.claude`` OR
                    # a symlinked settings.json is REFUSED (SymlinkRefused, an
                    # OSError subclass) and the resume is refused, never
                    # written through. The ``makedirs`` is retained only to
                    # create a genuinely-absent ``.claude`` (exist_ok=True
                    # short-circuits on a symlink, so the safe write still
                    # catches it).
                    try:
                        os.makedirs(
                            os.path.join(_ws_dir, ".claude"), exist_ok=True)
                        atomic_write_beneath(
                            _ws_dir,
                            os.path.join(".claude", "settings.json"),
                            json.dumps(
                                _new_settings, indent=2, sort_keys=True) + "\n",
                            owner_uid=_owner_uid, mode=0o644,
                        )
                    except OSError as exc:
                        await _refuse_brief_resume(
                            rec, f"settings.json rewrite failed: {exc}",
                            kind="refuse_settings_rewrite_failed",
                        )
                        continue

                if _credential_changed or _settings_changed:
                    logger.info(
                        "boot replay: engagement %s workspace state "
                        "refreshed (%s) — cycling its service so the CLI "
                        "reloads", rec.id[:8],
                        "+".join(n for n, c in (
                            ("credential", _credential_changed),
                            ("settings", _settings_changed),
                        ) if c),
                    )
                    # An unconfirmed stop is FATAL to the resume, not a
                    # warning (Sol, review r2): the new state is already on
                    # disk, so a surviving CLI holds a credential that no
                    # longer authenticates or hooks it never re-read — and
                    # the NEXT boot would see the on-disk state matching the
                    # record and never retry the cycle, leaving a permanently
                    # mute (or under-contained) engagement that still looks
                    # live. Refuse it instead: the shared helper runs the
                    # checked teardown ladder, lands a terminal mark, and
                    # adds it to ``refused_ids`` so the start and
                    # background-task loops skip it.
                    try:
                        _down = await s6_rc.ensure_service_down(
                            engagement_id=rec.id)
                    except Exception as exc:  # noqa: BLE001
                        _down = False
                        logger.warning(
                            "boot replay: service cycle after workspace "
                            "state refresh raised for %s: %s",
                            rec.id[:8], exc,
                        )
                    if _down is False:
                        # Mark TERMINAL unconditionally, not via the helper's
                        # own teardown outcome (Terra, review r3): the helper
                        # only marks when ITS stop also fails, so a
                        # fail-then-succeed pair would leave the record
                        # active/idle with its service dir removed — dormant
                        # this boot, silently resurrected the next one. The
                        # decision was already made here, so record it here;
                        # the helper's own mark then no-ops against the #326
                        # terminal guard while still running the checked
                        # teardown and adding the id to ``refused_ids``.
                        # STRICT: the mark must reach DISK before we move on
                        # (Terra, review r4). ``mark_error`` persists
                        # best-effort, so a swallowed tombstone-write failure
                        # would leave the next boot reloading the record as
                        # active/idle — and it would then see the on-disk
                        # state matching, skip the cycle, and resume the very
                        # engagement this refusal exists to retire.
                        try:
                            await registry.try_transition_terminal(
                                rec.id, "error", strict=True,
                                error_kind="refuse_workspace_cycle_failed",
                                error_message=(
                                    "engagement could not be confirmed down "
                                    "after a workspace state refresh"
                                ),
                            )
                        except Exception:  # noqa: BLE001 — teardown still runs
                            logger.warning(
                                "boot replay: durable terminal mark after a "
                                "workspace-cycle failure did not persist for "
                                "%s — the engagement may be reloaded as live "
                                "on the next boot", rec.id[:8], exc_info=True,
                            )
                        await _refuse_brief_resume(
                            rec,
                            "engagement could not be confirmed down after a "
                            "workspace state refresh, so a surviving CLI "
                            "would run with a stale credential or hooks",
                            kind="refuse_workspace_cycle_failed",
                        )
                        continue

                # Containment Stage 2 (Task 10): establish the record's OS uid.
                # Placed AFTER the credential/settings compare above and BEFORE
                # the chown/render below (design §6): the compare-reads use
                # ``owner_uid_or_none(rec.allocated_uid)``, so a LEGACY record
                # (still UNALLOCATED_UID here) compares with owner_uid=None —
                # correct, because its workspace is still ROOT-owned until the
                # chown lands just below; only after this backfill + chown does a
                # subsequent boot compare against the now-uid-owned files. A
                # legacy uid is backfilled via the allocator (allocate +
                # strict-persist under the registry lock); a record that already
                # carries a real uid (Stage-2 steady state) is returned
                # unchanged. Fail-CLOSED: an allocation failure (no allocator, or
                # the boot reconstruct failed so allocate() raises) refuses the
                # resume rather than launch a root/unallocated CLI.
                if rec.allocated_uid == UNALLOCATED_UID:
                    # S1 r3: if the post-sweep live-uid refold could not run,
                    # the high-water is not confirmed against live survivors —
                    # refuse THIS resume (it needs a fresh allocation) rather
                    # than backfill a uid that a setsid survivor may still hold.
                    if _refold_failed:
                        await _refuse_brief_resume(
                            rec,
                            "live /proc uid refold failed earlier this boot — "
                            "refusing to allocate a uid against an unconfirmed "
                            "high-water",
                            kind="refuse_uid_alloc_failed",
                        )
                        continue
                    try:
                        await registry.backfill_allocated_uid(rec.id)
                    except Exception as exc:  # noqa: BLE001 — fail-closed
                        await _refuse_brief_resume(
                            rec, f"uid backfill failed: {exc}",
                            kind="refuse_uid_alloc_failed",
                        )
                        continue
                _rec_uid = rec.allocated_uid
                if _rec_uid < UID_BASE:
                    # Defensive: backfill returned but the uid is still invalid
                    # (should be unreachable — allocate() returns >= UID_BASE).
                    await _refuse_brief_resume(
                        rec,
                        f"allocated_uid {_rec_uid} below UID_BASE after backfill",
                        kind="refuse_uid_alloc_failed",
                    )
                    continue
                # Uniqueness across the WHOLE registry AND on-disk workspaces,
                # not just this boot's resumes (Sol+Terra r1/r2): refuse if this
                # uid is already owned by ANY other record (terminal/retained/
                # refused included), by another engagement resumed earlier in
                # this loop, OR by a workspace directory whose id is not this
                # record's (a pruned/lost-record sibling the record map misses).
                _uid_conflict = (
                    _rec_uid in _migrated_uids
                    or any(
                        other.id != rec.id and other.allocated_uid == _rec_uid
                        for other in _all_records)
                    or any(
                        _bn != rec.id
                        for _bn in _ws_owner_ids.get(_rec_uid, ()))
                )
                if _uid_conflict:
                    await _refuse_brief_resume(
                        rec,
                        f"allocated_uid {_rec_uid} is already owned by another "
                        "engagement (duplicate uid) — refusing to chown/start a "
                        "second engagement onto one uid",
                        kind="refuse_uid_duplicate",
                    )
                    continue
                _migrated_uids.add(_rec_uid)

                # §7: regenerate the uid's NSS identity — container /etc is
                # ephemeral, so an UNDERGOING record resumed after a container
                # restart has no passwd/group entry and ``getpwuid`` (git, node,
                # the CLI) would fail. Idempotent. Routed through the injected
                # allocator so tests bind it to a tmp passwd/group; a missing
                # allocator (unit fakes) skips it — production always wires one.
                if _uid_allocator is not None:
                    try:
                        _uid_allocator.ensure_identity(
                            _rec_uid, os.path.join(_ws_dir, ".home"))
                    except OSError as exc:
                        await _refuse_brief_resume(
                            rec,
                            f"NSS identity regeneration failed: {exc}",
                            kind="refuse_uid_identity_failed",
                        )
                        continue

                # Containment Stage 2 (Task 8/§3): re-chown the workspace to the
                # record's uid as the LAST filesystem write before the service is
                # (re-)started — after every root-side write above (CLAUDE.md
                # refresh, .mcp.json/settings rewrite). The service is already
                # down (step 0 confirmed it), so nothing runs against a
                # mid-chown tree. Idempotent: an already-uid-owned workspace
                # (ordinary restart of a Stage-2 engagement) is re-chowned
                # cheaply; a legacy root-owned workspace is handed to its uid
                # here for the first time. A chown failure refuses the resume —
                # a still-root-owned workspace would make the dropped CLI EACCES
                # its own files (or the preflight/render invariant break).
                try:
                    chown_workspace(_ws_dir, _rec_uid, _rec_uid)
                    os.chmod(_ws_dir, 0o700)
                    # Task 11 (containment stage 2): a legacy record migrating
                    # to the uid drop for the first time has never had a
                    # private outbox dir — provision it here, alongside the
                    # chown, before the (re-)rendered run script below can
                    # start a producer plugin that expects it to exist.
                    # Idempotent for an ordinary restart of an
                    # already-migrated engagement.
                    import plugin_outbox
                    plugin_outbox.provision_engagement_outbox(_rec_uid)
                except OSError as exc:
                    await _refuse_brief_resume(
                        rec,
                        f"workspace chown to uid {_rec_uid} failed: {exc}",
                        kind="refuse_uid_chown_failed",
                    )
                    continue

                if s6_rc.service_pair_complete(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=rec.id,
                ):
                    # B1 (Sol r1): a COMPLETE pre-v0.75 pair still carries an
                    # old run script that emits neither the stream-json output
                    # nor the ``casa_control`` spawn NDJSON frame, so the new
                    # _InboundSpool never arms and every resumed operator turn
                    # queues forever. Detect the stale script and DROP the pair
                    # so the heal path below re-renders it from the current
                    # template (reusing the existing incomplete-pair heal — no
                    # duplication). A current pair keeps the fast-path continue.
                    if not s6_rc.run_script_is_stale(
                        svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                        engagement_id=rec.id,
                    ):
                        # Sol r3-2 (#342): the fast path must not trust
                        # pair-completeness as FIFO-existence — recreate
                        # (or refuse) before the start loop can run this
                        # record. Refusal populates refused_ids itself.
                        await _ensure_stdin_fifo(rec)
                        continue
                    logger.info(
                        "boot replay: migrating pre-v0.75 run script for "
                        "engagement %s (%s) — re-rendering pair",
                        rec.id[:8], rec.role_or_type,
                    )
                    s6_rc.remove_service_dir(
                        svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                        engagement_id=rec.id,
                    )
                    # B2 (Sol r2): remove_service_dir SWALLOWS rmtree failures,
                    # so a surviving old main (full or partial removal) would
                    # collide with write_service_dir's exist_ok=False re-plant
                    # and leave a stale, unlogged main whose spawn frames never
                    # reach the relay. VERIFY the pair is actually gone; if not,
                    # fail CLOSED (checked teardown + terminal mark) rather than
                    # compiling/starting a stale pair.
                    if not s6_rc.service_dirs_absent(
                        svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                        engagement_id=rec.id,
                    ):
                        await _refuse_brief_resume(
                            rec,
                            "stale pre-v0.75 pair removal did not complete "
                            "(service dir survivor)",
                            kind="refuse_migration_failed",
                        )
                        continue

                # M7 already validated above (#314) — the workspace exists
                # from here on.
                ws_dir = os.path.join(engagements_root, rec.id)

                # Task 6 (#360): ``executor_registry is None`` now refuses
                # every claude_code record much earlier (the unconditional
                # definition_any resolution above) — this branch is
                # unreachable from here on and has been removed.

                # r11-B2: for brief-bearing records reuse the already-resolved
                # definition_any result (which resolves DISABLED specialists),
                # so a disabled-definition engagement with an INCOMPLETE pair
                # heals instead of silently not healing. Brief-LESS records keep
                # today's get() behaviour EXACTLY (None for disabled → refuse).
                defn = brief_defn if has_brief else executor_registry.get(
                    rec.role_or_type)
                if defn is None:
                    logger.warning(
                        "boot replay: cannot heal engagement %s — executor "
                        "type %r not registered; leaving UNDERGOING",
                        rec.id[:8], rec.role_or_type,
                    )
                    continue

                # §3.8 (missing recorded artifacts) already validated above
                # (#314) — every recorded --plugin-dir target exists.

                # Containment Stage 2 (Task 7 parity): the fresh-launch path
                # gates on _preflight_uid_drop, which verifies each pinned
                # --plugin-dir is readable+traversable by the allocated uid
                # BEFORE the CLI (dropped to that uid) tries to open it. In
                # practice ``plugin_boot.heal_and_freeze_store`` re-freezes
                # artifacts o+rx before replay runs, so this should already
                # hold — but replay must CHECK rather than assume, the same
                # way the fresh path does, so a miss refuses this record's
                # resume instead of rendering/starting a run script that
                # crash-loops the moment the dropped-uid CLI can't read its
                # own --plugin-dir. Reuses the SAME check _preflight_uid_drop
                # uses (shared helper) rather than a divergent copy.
                try:
                    _check_plugin_dirs_readable(_rec_uid, rec.plugin_artifacts)
                except UidDropRefused as exc:
                    await _refuse_brief_resume(
                        rec,
                        f"plugin dir not readable by uid {_rec_uid}: {exc}",
                        kind="refuse_plugin_dir_unreadable",
                    )
                    continue

                # Clear stale/legacy/torn dirs first — write_service_dir
                # mkdirs with exist_ok=False (a surviving -log sibling would
                # otherwise collide).
                s6_rc.remove_service_dir(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=rec.id,
                )

                # Re-render run + log scripts. Task 6/Stage 2: render WITH the
                # record's uid so the migrated run script drops privilege via
                # setpriv (render refuses the UNALLOCATED_UID sentinel — the uid
                # was established/backfilled above, so this fails closed for an
                # unallocated record rather than emitting a root CLI).
                run_script = render_run_script(
                    engagement_id=rec.id,
                    permission_mode=defn.permission_mode or "acceptEdits",
                    extra_dirs=list(defn.extra_dirs or []),
                    plugin_dirs=[pa["path"] for pa in rec.plugin_artifacts],
                    uid=_rec_uid, gid=_rec_uid,
                )
                log_script = render_log_run_script(engagement_id=rec.id)
                s6_rc.write_service_dir(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=rec.id,
                    run_script=run_script,
                    depends_on=["init-setup-configs"],
                    log_run_script=log_script,
                )
                # Ensure FIFO exists — it might have been wiped alongside the
                # svc dir.
                # #342/Sol r2-1/Sol r3-2: a failed recreation refuses the
                # resume through the checked-teardown ladder (down +
                # pair removal + terminal mark when containment is
                # unconfirmed) — never "continue" into a doomed start.
                if not await _ensure_stdin_fifo(rec):
                    continue
                logger.info(
                    "boot replay: healed engagement %s (%s)",
                    rec.id[:8], rec.role_or_type,
                )
            except Exception as exc:  # noqa: BLE001 — per-record isolation
                # Containment Stage 2 (Task 10, design §6d): a heal that failed
                # part-way (e.g. render/chown/write raised after step 0 downed
                # the service) must NEVER be started below — a not-yet-migrated
                # engagement stays DOWN, never resurrected as root. Refuse it so
                # the start + background loops skip it (the service is already
                # down from step 0; the compile-path prune keeps sources sane).
                refused_ids.add(rec.id)
                logger.warning(
                    "boot replay: heal failed for engagement %s: %s — refusing "
                    "resume (kept down, not started)",
                    rec.id[:8], exc,
                )

        # 3. Single compile + update pass.
        await s6_rc._compile_and_update_locked()

        # 4. v0.79.0 (§5/F7): adopt the pinned summary BEFORE starting each
        # service, then start. §5 forbids a running engagement without a
        # summary, so an adoption failure ABORTS that engagement (mark error +
        # skip start) rather than starting it summary-less — fail-closed, not
        # the old fail-open "log and continue after start". A fresh v0.79 record
        # (summary already persisted) or a topic-less one adopts as a no-op.
        adopt = getattr(driver, "adopt_summary_if_missing", None)
        for rec in undergoing:
            if rec.id in refused_ids:        # Sol F5: no service was written
                continue
            if adopt is not None:
                try:
                    await adopt(rec)
                except Exception as exc:  # noqa: BLE001 — §5 abort rule
                    logger.warning(
                        "boot replay: summary adopt-on-attach failed for %s: "
                        "%s — aborting resume (not starting summary-less)",
                        rec.id[:8], exc,
                    )
                    refused_ids.add(rec.id)
                    try:
                        await registry.mark_error(
                            rec.id, kind="summary_adopt_failed",
                            message=(
                                "resume aborted: pinned-summary adoption failed "
                                f"({exc})"
                            ),
                        )
                    except Exception:  # noqa: BLE001 — best-effort terminal mark
                        logger.warning(
                            "boot replay: mark_error(summary_adopt_failed) "
                            "failed for %s", rec.id[:8], exc_info=True,
                        )
                    try:
                        await s6_rc.ensure_service_down(engagement_id=rec.id)
                    except Exception:  # noqa: BLE001 — best-effort teardown
                        logger.warning(
                            "boot replay: ensure_service_down after adopt "
                            "failure for %s failed", rec.id[:8], exc_info=True,
                        )
                    continue
            try:
                await s6_rc.start_service(engagement_id=rec.id)
                # #369 (Sol diff-gate r2): the rebuild is complete only now —
                # fresh session, floor workspace, running service. Best-effort
                # here: a failed strict clear leaves the flag set, and the
                # first turn's rebuild branch (idempotent) clears it then.
                if rec.id in pending_rebuild_started_ids:
                    try:
                        await registry.clear_context_rebuild_pending(rec.id)
                    except Exception:  # noqa: BLE001 — turn-path clears later
                        logger.warning(
                            "boot replay: rebuild-flag clear failed for %s "
                            "(first turn will rebuild again)", rec.id[:8],
                            exc_info=True)
            except Exception as exc:  # noqa: BLE001
                # #342: refuse — the background loop below must not build
                # spool/relay/summary machinery for an engagement whose
                # service never started (operator messages would be
                # accepted into an engagement with no CLI consumer).
                logger.warning(
                    "boot replay: start_service(%s) failed: %s — refusing "
                    "resume (no background attach)", rec.id[:8], exc,
                )
                refused_ids.add(rec.id)
                try:
                    await registry.mark_error(
                        rec.id, kind="start_service_failed",
                        message=f"resume aborted: s6 start failed ({exc})",
                    )
                except Exception:  # noqa: BLE001 — best-effort mark
                    logger.warning(
                        "boot replay: mark_error(start_service_failed) "
                        "failed for %s", rec.id[:8], exc_info=True,
                    )
                try:
                    await s6_rc.ensure_service_down(engagement_id=rec.id)
                except Exception:  # noqa: BLE001 — best-effort teardown
                    logger.warning(
                        "boot replay: ensure_service_down after start "
                        "failure for %s failed", rec.id[:8], exc_info=True,
                    )

    # 5. Background tasks OUTSIDE the lock (long-lived).
    for rec in undergoing:
        if rec.id in refused_ids:            # Sol F5: refused resume / F7 abort
            continue
        # v0.79.0 (§5, F7): the pinned-summary adopt-on-attach now runs in the
        # start loop ABOVE (BEFORE service start, aborting the resume on failure)
        # — a summary-less running engagement is no longer possible. Background
        # tasks build the controller that adopts the (now-guaranteed) summary id.
        try:
            # §A3(b): thread the PRE-SERVICE snapshot + shared claimed-set + the
            # Telegram-readiness event so the attached record's reconcile CLAIMS
            # itself (one reconciler/boot) and runs only after channel readiness.
            driver._spawn_background_tasks(
                rec,
                reconcile_snapshot=reconcile_snapshots.get(rec.id),
                reconcile_claimed=reconcile_claimed,
                telegram_ready=telegram_ready,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "boot replay: background tasks for %s failed: %s",
                rec.id[:8], exc,
            )

    # §A3(b) boot reconciliation owner — REFUSED-undergoing records (Sol r7-3):
    # an undergoing record that REFUSED attachment (missing workspace/artifacts,
    # refused brief resume, summary-adoption failure that mark_error'd it) never
    # got the attached ``_spawn_background_tasks`` reconcile pass and would stay
    # visibly live forever. casa_core owns whatever remains UNCLAIMED (terminal
    # records were already claimed pre-lock; attached records claimed themselves).
    if _schedule_reconcile is not None:
        for _eid, _snap in reconcile_snapshots.items():
            if _eid in reconcile_claimed:
                continue
            _rec = registry.get(_eid)
            if _rec is None:
                continue
            try:
                _schedule_reconcile(
                    _rec, _snap, telegram_ready, claimed=reconcile_claimed)
            except Exception as exc:  # noqa: BLE001 — best-effort per record
                logger.warning(
                    "boot replay: casa_core-owned open-question reconcile for "
                    "%s failed to schedule: %s", _eid[:8], exc,
                )


async def reconcile_terminal_spools(*, registry, driver) -> None:
    """v0.79.0 (§3): terminal boot-reconciliation owner.

    Alongside the active-engagement replay scan, drain the inbound spools of
    TERMINAL engagements that still hold pending receipts/notices — a drain
    that crashed after the terminal commit, or a Telegram send that failed
    before finalize. Each drains to the topic if it still exists, else
    WARN-drops (the topic is gone; nothing to notify into). Pending entries
    therefore retry across restarts until sent or their topic disappears.
    """
    reconcile = getattr(driver, "reconcile_terminal_spool", None)
    if reconcile is None:
        return
    for rec in registry.terminal_records():
        if rec.driver != "claude_code":
            continue
        try:
            await reconcile(rec)
        except Exception as exc:  # noqa: BLE001 — best-effort per record
            logger.warning(
                "terminal spool reconcile failed for %s: %s",
                rec.id[:8], exc,
            )


# ------------------------------------------------------------------
# /hooks/resolve — CC hook_proxy.sh loopback endpoint
# ------------------------------------------------------------------


def _build_cc_hook_policies(hook_policies: dict) -> dict:
    """Build {policy_name: (matcher_regex, async_callback)} from HOOK_POLICIES.

    Plan 4a.1: policies are now two-tier {matcher, factory}; we invoke each
    factory with no kwargs (the HTTP path does not accept per-call params —
    parameterization lives in the executor's hooks.yaml, which the SDK path
    also consumes verbatim). Unknown-param executor hooks.yaml entries still
    surface through the SDK-path's resolve_hooks validation at executor load
    time; the HTTP path inherits whatever configuration the factory
    with-defaults produces.

    These are the DEFAULT-configured callbacks and serve as the fallback for
    the HTTP path. H3 (v0.53.0): per-executor ``hooks.yaml`` parameters (e.g.
    plugin-developer's ``path_scope`` writable/readable prefixes) are wired in
    separately by :func:`_build_executor_cc_hook_policies` and preferred at
    request time by the /internal/hooks/resolve handler when the engagement
    resolves from the payload cwd; this dict is only the fallback when no
    executor-specific callback applies.
    """
    cc_policies: dict = {}
    for name, entry in hook_policies.items():
        matcher = entry["matcher"]
        callback = entry["factory"]()  # default-configured HookCallback
        cc_policies[name] = (matcher, callback)
    return cc_policies


def _build_executor_cc_hook_policies(executor_registry) -> dict:
    """H3 (v0.53.0): ``{executor_type: {policy_name: (matcher, callback)}}``.

    For every ``claude_code`` executor with a ``hooks.yaml``, parse the file
    and build parameterised ``(matcher, callback)`` entries so the HTTP hook
    path (hook_proxy.sh -> /hooks/resolve) enforces the executor's declared
    ``path_scope`` prefixes / ``commit_size_guard`` limit instead of the
    deny-all factory defaults.

    Built at boot AND rebuilt in place by ``reload.reload_executors`` (#340 —
    the resolve handlers capture the dict instance stashed on
    ``runtime.executor_cc_policies``, so reload mutates that same object).
    #442: a per-executor parse failure is NOT skipped — skipping fell that
    executor back to the default callbacks, which enforce less than any
    declaration it could have made. It gets a marked deny-all map instead.
    An executor that legitimately declares no parameters is recorded
    positively, so the resolver can tell it apart from one that never loaded
    (see the resolver's missing-entry refusal in ``internal_handlers``).

    #315: iterates ``list_types_any()``/``definition_any()`` — boot replay
    resumes existing brief-bearing engagements of DISABLED executors, and
    those must resolve their declared hook parameters too, not the deny-all
    defaults (default ``path_scope`` has empty prefix lists, denying every
    workspace Read/Write/Edit).
    """
    from hooks import UsesDefaultPolicies, build_policy_callbacks_from_hooks_yaml

    out: dict = {}
    for t in executor_registry.list_types_any():
        defn = executor_registry.definition_any(t)
        if defn is None or defn.driver != "claude_code" or not defn.hooks_path:
            # #442 r3: say "known, and declares nothing" POSITIVELY. The
            # resolver refuses an executor its map does not represent, and an
            # absence is what every failure mode looks like too — so a
            # legitimate no-parameters executor has to be distinguishable
            # from one that never loaded.
            out[t] = UsesDefaultPolicies()
            continue
        try:
            # Task 3 (#360): build from the load-time-validated snapshot
            # (ExecutorDefinition.hooks_document), never by re-reading
            # hooks_path — a fresh read here would let a post-load edit
            # (e.g. a config-editable hooks_file: repoint) reach the HTTP
            # enforcement path without going through reload's
            # re-validate-and-re-snapshot.
            data = defn.hooks_document
            out[t] = build_policy_callbacks_from_hooks_yaml(data)
        except Exception as exc:  # noqa: BLE001
            # #442 r2 (Sol/Terra P1): omitting the executor is NOT neutral —
            # the resolver then answers from the default-configured map, per
            # POLICY, and casa_config_guard's default forbids no write path at
            # all. A document that fails to build would therefore enforce LESS
            # than the operator declared, which is the same fail-open shape
            # #442 exists to close. Deny everything for this executor instead
            # until the file is fixed. (Reload keeps the pre-reload callbacks
            # instead — there a KNOWN-GOOD set exists; at boot there is none.)
            logger.error(
                "executor %r hooks.yaml param build failed: %s — denying every "
                "guarded call for it (defaults would enforce less)", t, exc,
            )
            out[t] = _deny_all_cc_policies(t, exc)
    return out


def _deny_all_cc_policies(executor_type: str, exc: object) -> dict:
    """A ``{policy: (matcher, callback)}`` map whose every callback denies.

    Covers exactly the HOOK_POLICIES names, so the separately-wired
    ``engagement_permission_relay`` / ``engagement_buttons_reminder`` still
    fall through to their live defaults — the executor can still surface a
    permission request, it just cannot pass a guard.
    """
    from hooks import DenyAllPolicyMap, HOOK_POLICIES, make_always_deny_hook

    reason = (
        f"executor {executor_type!r} declares a hooks.yaml that could not be "
        f"built ({exc}); every guarded tool call is denied until it is fixed."
    )
    return DenyAllPolicyMap(
        (name, (entry["matcher"], make_always_deny_hook(reason)))
        for name, entry in HOOK_POLICIES.items()
    )


def _bus_loop_targets(agents: dict) -> list[str]:
    """H4 (v0.53.0): bus targets that need a ``run_agent_loop`` consumer.

    Residents (``agents`` roles) + the ``telegram`` outbound target + the
    ``observer`` target. ``observer`` was previously missing, so every
    engagement event sent to target='observer' (subprocess_respawn,
    idle_detected, error tool_results) enqueued forever with no consumer —
    events lost, queue leaked. ``dict.fromkeys`` dedupes while preserving
    order (guards a hypothetical user resident literally named "observer").
    """
    return list(dict.fromkeys(list(agents.keys()) + ["telegram", "observer"]))


def _wire_engagement_permission_relay(
    cc_hook_policies: dict,
    *,
    engagement_registry,
    telegram_channel,
) -> dict:
    """Inject engagement_permission_relay into a built cc_hook_policies dict.

    v0.37.2 (C-1): the relay needs live ``engagement_registry`` +
    ``telegram_channel``, so it can't be wired via the parameter-free
    factory pattern used by HOOK_POLICIES. Inject it directly into the
    built ``(matcher, callback)`` dict instead.

    Mutates and returns ``cc_hook_policies`` for caller convenience.
    """
    from hooks import make_engagement_permission_relay

    cc_hook_policies["engagement_permission_relay"] = (
        r".*",
        make_engagement_permission_relay(
            engagement_registry=engagement_registry,
            telegram_channel=telegram_channel,
        ),
    )
    return cc_hook_policies


def _wire_engagement_buttons_reminder(
    cc_hook_policies: dict,
    *,
    engagement_registry,
) -> dict:
    """Inject engagement_buttons_reminder into a built cc_hook_policies dict.

    R4 (v0.89.0, buttons-always): a PreToolUse(Skill) salience backstop that
    needs the live ``engagement_registry`` (to resolve an ACTIVE engagement
    from the CC payload's cwd), so — like ``engagement_permission_relay`` — it
    can't be built via the parameter-free HOOK_POLICIES factory pattern. The
    matcher is ``Skill`` so only Skill loads reach the callback (the executor's
    generated .claude/settings.json registers the same Skill matcher, and
    ``build_policy_callbacks_from_hooks_yaml`` skips this policy — no factory —
    so the HTTP resolver falls back to this wired default).

    Mutates and returns ``cc_hook_policies`` for caller convenience.
    """
    from hooks import make_engagement_buttons_reminder

    cc_hook_policies["engagement_buttons_reminder"] = (
        r"Skill",
        make_engagement_buttons_reminder(
            engagement_registry=engagement_registry,
        ),
    )
    return cc_hook_policies


async def _drain_broker_before_channel_shutdown(channel_manager: Any) -> None:
    """Graceful-shutdown barrier (r4-B1/B3): resolve every live
    ``verdict_broker`` request as ``cancelled`` and let its keyboard-edit
    finish-hook flush, BEFORE the channels (Telegram bot, etc.) tear down.

    Must run immediately before ``channel_manager.stop_all()`` — a finish
    hook that fires after the channel is stopped can't edit anything.

    Pinned order (r5-B2): cancel the broker records FIRST so a still-draining
    authorization-challenge setup driver can only find a cancelled request
    (never posts a fresh keyboard during shutdown); THEN await the coordinator
    drivers; THEN flush the broker finish hooks; THEN stop the channels.

    #411 (design r3, Terra TOCTOU): wipe admission is FROZEN before any of
    that — a consent finish hook firing during this drain can then only be
    refused, never spawn a wipe after the wipe drain below already looked —
    and any RUNNING wipe is drained after the hooks but before the channels
    stop (its report edit needs the channel) and before semantic memory
    closes (its bank delete needs the seam).
    """
    import memory_wipe
    from verdict_broker import BROKER
    memory_wipe.freeze_wipes()
    BROKER.cancel_all(reason="casa_shutdown")
    await CHALLENGES.drain()
    await BROKER.drain_hooks()
    await memory_wipe.drain_wipe_task()
    await channel_manager.stop_all()


_STATUS_PAGE = """\
<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>Casa</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, system-ui, sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 2rem; }}
  .container {{ max-width: 480px; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 1.5rem; color: #f8fafc; }}
  h1 span {{ color: #3b82f6; }}
  .card {{ background: #1e293b; border-radius: 12px; padding: 1.25rem;
           margin-bottom: 1rem; }}
  .card h2 {{ font-size: 0.75rem; text-transform: uppercase;
              letter-spacing: 0.05em; color: #64748b; margin-bottom: 0.75rem; }}
  .row {{ display: flex; justify-content: space-between; padding: 0.35rem 0;
          border-bottom: 1px solid #334155; }}
  .row:last-child {{ border-bottom: none; }}
  .label {{ color: #94a3b8; }}
  .value {{ color: #f1f5f9; font-weight: 500; }}
  .value.on {{ color: #4ade80; }}
  .value.off {{ color: #64748b; }}
  .actions {{ display: flex; gap: 0.75rem; margin-top: 1.5rem; }}
  a.btn {{ display: inline-block; padding: 0.6rem 1.2rem; border-radius: 8px;
           text-decoration: none; font-weight: 500; font-size: 0.9rem;
           transition: opacity 0.15s; }}
  a.btn:hover {{ opacity: 0.85; }}
  a.btn.primary {{ background: #3b82f6; color: #fff; }}
  a.btn.disabled {{ background: #334155; color: #64748b;
                    pointer-events: none; cursor: default; }}
  .footer {{ margin-top: 2rem; font-size: 0.75rem; color: #475569; text-align: center; }}
</style>
</head><body>
<div class="container">
  <h1><span>Casa</span> Agent</h1>

  <div class="card">
    <h2>Agents</h2>
    {agent_rows}
  </div>

  <div class="card">
    <h2>Channels</h2>
    {channel_rows}
  </div>

  <div class="card">
    <h2>System</h2>
    {system_rows}
  </div>

  <div class="actions">
    <a class="btn {terminal_class}" href="{ingress_path}/terminal/">Terminal</a>
    <a class="btn primary" href="{ingress_path}/healthz">Health Check</a>
  </div>

  <div class="footer">Casa v{version}</div>
</div>
</body></html>"""


def _row(label: str, value: str, css: str = "") -> str:
    cls = f' class="value {css}"' if css else ' class="value"'
    return f'<div class="row"><span class="label">{label}</span><span{cls}>{value}</span></div>'


# ------------------------------------------------------------------
# Pure helpers (extracted for testability; see tests/test_casa_core_helpers.py)
# ------------------------------------------------------------------


def _env_int_or(name: str, default: int, *, min_value: int = 0,
                max_value: int | None = None,
                env: dict[str, str] | None = None) -> int:
    """Read a non-negative int from env; fall back to *default* on bad input.

    ``min_value``/``max_value`` clamp the parsed value to the same rails the
    HA add-on schema validates (defence in depth — HA schema-validates normal
    config, but a direct env override or a schema drift must not slip past).

    Extracted as a module-level helper so future items that need the same
    shape (spec 5.2 §9.3 has more env vars coming in item I) can reuse
    it. Mirrors retry._env_int but stays on casa_core until a second
    caller appears — then promote to a shared `env.py` module.
    """
    env = env if env is not None else os.environ
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default
    if value < min_value:
        logger.warning(
            "%s=%d below minimum %d; using %d",
            name, value, min_value, min_value,
        )
        return min_value
    if max_value is not None and value > max_value:
        logger.warning(
            "%s=%d above maximum %d; using %d",
            name, value, max_value, max_value,
        )
        return max_value
    return value


def _telegram_supergroup_id_from_env(
        env: dict[str, str] | None = None) -> int:
    """Parse TELEGRAM_ENGAGEMENT_SUPERGROUP_ID; garbage → 0 (disabled).

    Not :func:`_env_int_or`: real Telegram supergroup IDs are negative
    (-100xxxxxxxxxx) and that helper's ``min_value`` clamp would silently
    zero them. #325 defence-in-depth behind svc-casa/run's "null"
    normalization — a stray "null" here must not be boot-fatal.
    """
    env = env if env is not None else os.environ
    raw = env.get("TELEGRAM_ENGAGEMENT_SUPERGROUP_ID", "")
    try:
        return int(raw or "0")
    except ValueError:
        logger.warning(
            "Invalid TELEGRAM_ENGAGEMENT_SUPERGROUP_ID=%r; "
            "engagement supergroup disabled", raw,
        )
        return 0


def _env_float_or(name: str, default: float, *, min_value: float = 0.0,
                   env: dict[str, str] | None = None) -> float:
    """Read a non-negative float from env; fall back to *default* on bad
    input. Float counterpart to :func:`_env_int_or` — Task 6 (spec §4.6)
    needs one for ``SPECIALIST_COST_ALERT_THRESHOLD`` (a USD figure)."""
    env = env if env is not None else os.environ
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default
    if not math.isfinite(value):
        logger.warning("Non-finite %s=%r; using default %s", name, raw, default)
        return default
    if value < min_value:
        logger.warning(
            "%s=%s below minimum %s; using %s",
            name, value, min_value, min_value,
        )
        return min_value
    return value


def _maybe_register_n8n(
    mcp_registry: "McpServerRegistry",
    env: dict[str, str] | None = None,
) -> dict[str, object] | None:
    """Register the ``n8n-workflows`` HTTP MCP server if ``N8N_URL`` is set.

    Generic shared infrastructure — any agent (resident or specialist) that
    declares ``n8n-workflows`` in ``mcp_server_names`` can reach it; the
    per-agent ``tools.allowed`` list governs which workflows each agent
    may invoke. Matches the shape of the ``homeassistant`` env-gated
    block in ``main()``.

    Returns the registered server config dict, or ``None`` when
    ``N8N_URL`` is unset or whitespace-only.
    """
    env = env if env is not None else os.environ
    url = (env.get("N8N_URL") or "").strip()
    if not url:
        return None
    api_key = (env.get("N8N_API_KEY") or "").strip()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    mcp_registry.register_http(
        name="n8n-workflows",
        url=url,
        headers=headers,
    )
    logger.info("Registered n8n-workflows MCP server")
    return mcp_registry.resolve(["n8n-workflows"]).get("n8n-workflows")


async def wire_tina_ha_facade(
    mcp_registry: "McpServerRegistry",
    facade: Any,
    agents: Mapping[str, Any],
    *,
    tina_role: str = "butler",
) -> None:
    """Publish Tina's eager HA schema and retire her stale SDK clients."""
    mcp_registry.register_role_sdk(
        "homeassistant", tina_role, facade.server_config,
    )
    agent = agents.get(tina_role)
    if agent is not None:
        await agent.invalidate_tool_surface()


async def _start_tina_ha_facade(
    mcp_registry: "McpServerRegistry",
    role_configs: Mapping[str, Any],
    agents: Mapping[str, Any],
    *,
    ha_mcp_url: str,
    supervisor_token: str,
    tina_role: str = "butler",
) -> HomeAssistantFacade | None:
    """Start and publish Tina's eager Home Assistant facade.

    v0.125.0 (#228): the facade is unconditional. ``tina_ha_facade_enabled``
    was a diagnostic fallback to the raw Home Assistant MCP connection; the
    facade has been the shipped path throughout and the raw connection is not
    a configuration an operator should be choosing.
    """
    tina_config = role_configs.get(tina_role)
    if (
        not supervisor_token
        or tina_config is None
        or "ha_voice" not in (getattr(tina_config, "channels", ()) or ())
    ):
        return None

    facade: HomeAssistantFacade

    async def _schema_changed() -> None:
        await wire_tina_ha_facade(
            mcp_registry, facade, agents, tina_role=tina_role,
        )

    facade = HomeAssistantFacade(
        ha_mcp_url,
        {"Authorization": f"Bearer {supervisor_token}"},
        on_schema_change=_schema_changed,
    )
    try:
        await facade.start()
    except Exception:  # noqa: BLE001 — raw upstream details may hold secrets
        try:
            await facade.aclose()
        except Exception:  # noqa: BLE001 — degraded boot remains available
            pass
        logger.warning("ha_facade_initialization_failed status=degraded")
        return None
    await wire_tina_ha_facade(
        mcp_registry, facade, agents, tina_role=tina_role,
    )
    return facade


async def _close_tina_ha_facade(
    facade: Any | None,
    *,
    timeout: float = 15.0,
) -> None:
    """Close the optional eager HA facade without wedging Casa shutdown."""
    if facade is None:
        return
    try:
        await asyncio.wait_for(facade.aclose(), timeout=timeout)
    except Exception:  # noqa: BLE001 — shutdown must remain available
        logger.warning("ha_facade_close_failed")


# Max webhook request body (spec A3). Larger requests are rejected before read.
_WEBHOOK_BODY_MAX = 64 * 1024


def _make_webhook_handler(
    *,
    webhook_rate_limiter: Any,
    webhook_secret: str,
    trigger_registry: Any,
    default_role: str,
    bus: Any,
    secrets_dir: str | Path = "/data/webhook_secrets",
):
    """Build the wildcard ``/webhook/{name}`` handler.

    Request pipeline (spec A3): rate limit → bounded body read (64 KiB) →
    name lookup (unknown ⇒ 404) → PER-TRIGGER auth verify (fail ⇒ 401) →
    dispatch a SCHEDULED bus message to the registered role.

    Auth is per-trigger (spec A1): each webhook trigger declares an ``auth``
    policy (``hmac_body`` uses the global ``webhook_secret``; ``static_header``/
    ``timestamped_hmac`` use a per-trigger secret under ``secrets_dir``). An
    empty/absent secret fails closed. 404 precedes auth (names are non-secret,
    r3 design decision) so the policy can be selected by name.

    Extracted from ``main()`` so it is unit-testable; see
    ``tests/test_webhook_handler.py``.
    """
    import webhook_auth
    secrets_dir = Path(secrets_dir)
    if webhook_secret:
        import log_redact as _lr
        _lr.register_secret(webhook_secret)

    import log_redact

    def _secret_for(name: str, policy: dict) -> bytes:
        mode = policy.get("mode", "hmac_body")
        if mode == "hmac_body":
            return webhook_secret.encode() if webhook_secret else b""
        owner = policy.get("secret_owner", "casa")
        # #609 — READ-ONLY. This used to mint-if-absent, which made the first
        # inbound REQUEST the thing that created a casa secret: a trigger was
        # reported live and committed while its secret did not exist, and the
        # only way to create it was a call that could not succeed without it.
        # Minting now happens at registration (`resident_trigger_secrets`), and
        # this path must not be a second mechanism — otherwise a route can be
        # served from whatever file happens to survive. An unprovisioned route
        # is a 401, not a mint.
        #
        # Sol shipB-r1 P1-6 still applies: a filesystem failure here
        # (unreadable/full secrets dir) must degrade to an EMPTY secret — which
        # never authenticates (401) — not a 500.
        try:
            got = webhook_auth.read_secret(
                name, owner=owner, secrets_dir=secrets_dir)
        except Exception:  # noqa: BLE001 — fail closed, never fail open/500
            logger.warning("webhook secret read failed (%s)", name,
                           exc_info=True)
            return b""
        if got:
            # Register for exact-value log redaction (spec A2) so a per-trigger
            # secret can never surface in Casa's application logs.
            try:
                log_redact.register_secret(got.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001 — redaction is best-effort
                pass
        return got or b""

    def _verify(request: web.Request, body: bytes, name: str, policy: dict) -> bool:
        return webhook_auth.verify(
            policy.get("mode", "hmac_body"),
            body=body,
            headers=request.headers,
            secret=_secret_for(name, policy),
            header_name=policy.get("header", "X-Webhook-Signature"),
            tolerance_secs=int(policy.get("tolerance_secs", 300)),
            now=int(time.time()),
        )

    async def webhook_handler(request: web.Request) -> web.Response:
        limited = rate_limit_response(webhook_rate_limiter, "global")
        if limited is not None:
            return limited

        # Bounded body read (spec A3): reject a declared oversize Content-Length
        # early, AND stream-read with a hard cap so a chunked/Transfer-Encoding
        # request cannot buffer past 64 KiB (Terra ship-review P1).
        if request.content_length is not None and request.content_length > _WEBHOOK_BODY_MAX:
            return web.json_response({"error": "payload too large"}, status=413)
        chunks: list[bytes] = []
        read = 0
        async for chunk in request.content.iter_chunked(8192):
            read += len(chunk)
            if read > _WEBHOOK_BODY_MAX:
                return web.json_response({"error": "payload too large"}, status=413)
            chunks.append(chunk)
        body = b"".join(chunks)

        name = request.match_info.get("name", "")
        target_role = trigger_registry.get_webhook_target(name)
        if target_role is None:
            return web.json_response(
                {"error": "unknown webhook"}, status=404,
            )

        policy = trigger_registry.get_auth_policy(name) or {"mode": "hmac_body"}
        if not _verify(request, body, name, policy):
            return web.json_response(
                {"error": "invalid signature"}, status=401,
            )

        # Parse the ALREADY-READ body (the streaming cap above consumed
        # request.content, so request.json() would re-read empty — Terra
        # ship-review P2). Fall back to raw text for non-JSON payloads.
        try:
            payload = json.loads(body)
        except Exception:
            payload = body.decode("utf-8", errors="replace")

        # #204: attribute the turn to the trigger that fired it. Resolved
        # BEFORE the message is built so an unstampable ingress fails the
        # request (#203) instead of dispatching under the blind ``system``
        # identity. The peer is per-trigger and never the operator's.
        try:
            trusted_origin = ingress_identity(
                "webhook_trigger", webhook_name=name,
                clearance=trigger_registry.get_clearance(name),
            )
        except IngressIdentityError:
            logger.error(
                "webhook %r could not be given a trusted identity; refusing "
                "to dispatch it unattributed", name, exc_info=True,
            )
            return web.json_response(
                {"error": "ingress identity unavailable"}, status=500,
            )

        msg = BusMessage(
            type=MessageType.SCHEDULED,
            source="webhook",
            target=target_role,
            content=f"Webhook '{name}' triggered with payload: {payload}",
            channel="webhook",
            trusted_user_origin=trusted_origin,
            context={
                "webhook_name": name,
                "cid": request.get("cid") or new_cid(),
                # Release A: server-set, unspoofable containment markers. A
                # webhook_trigger turn is UNTRUSTED (third-party content) → the
                # restricted runtime + public-floored recall clearance. Fresh
                # UUID chat_id makes each dispatch a one-shot that can never
                # resume another session.
                "_origin_route": "webhook_trigger",
                "_origin_clearance": trigger_registry.get_clearance(name),
                "chat_id": str(uuid.uuid4()),
            },
        )
        await bus.send(msg)
        return web.json_response({"status": "accepted"})

    return webhook_handler


def _make_telegram_update_handler(*, get_telegram_channel, webhook_secret: str):
    """Build the ``POST /telegram/update`` webhook handler.

    Extracted from ``main()`` so it is unit-testable; see
    ``tests/test_telegram_update_handler.py``.

    L4: the ``X-Telegram-Bot-Api-Secret-Token`` header is compared with
    ``hmac.compare_digest`` (constant-time) rather than ``!=`` to avoid a
    timing side-channel on the shared webhook secret. BOTH sides are
    encoded to bytes: ``compare_digest`` raises ``TypeError`` on non-ASCII
    ``str`` inputs, and the header value is attacker-controlled (and a
    user-supplied ``webhook_secret`` may be non-ASCII), so a non-ASCII
    header must yield 403, not a 500.
    """

    async def telegram_update_handler(request: web.Request) -> web.Response:
        telegram_channel = get_telegram_channel()
        if telegram_channel is None:
            return web.json_response({"error": "telegram not configured"}, status=404)
        # #193: fail-closed when no webhook secret is configured. The Telegram
        # webhook transport always carries an X-Telegram-Bot-Api-Secret-Token
        # (set via setWebhook); with no secret this route would accept forged,
        # unsigned updates and reach the assistant. Reject unconditionally
        # rather than skip the check — in polling mode the route is registered
        # but unused, so rejecting is harmless; in webhook mode a secret must be
        # set. Mirrors the /invoke fail-closed treatment (403 = route disabled,
        # not merely mis-signed).
        if not webhook_secret:
            return web.json_response(
                {"error": "webhook auth disabled"}, status=403)
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(
            token.encode("utf-8"), webhook_secret.encode("utf-8"),
        ):
            return web.Response(status=403)
        payload = await request.json()
        outcome = await telegram_channel.process_webhook_update(payload)
        # #428: keep the bare 200 (Telegram's redelivery contract; the body is
        # reserved for invoking a Bot API method) but tell programmatic
        # callers whether the update was queued, deduped, or ignored.
        return web.Response(status=200, headers={"X-Casa-Update": outcome})

    return telegram_update_handler


def _make_invoke_handler(
    *,
    webhook_rate_limiter: Any,
    webhook_secret: str,
    bus: Any,
    assistant_role: str,
    role_configs: Mapping[str, Any],
):
    """Build the ``POST /invoke/{agent}`` direct-invocation handler.

    Extracted from ``main()`` so it is unit-testable; see
    ``tests/test_invoke_handler_body_validation.py``. L3: a body that
    parses to a non-dict (``[1]``, ``"hi"``, ``42``, ``null``) is
    rejected with the same 400 the handler already uses for malformed
    JSON, and an explicit ``"context": null`` is normalized to ``{}``
    instead of raising ``TypeError`` at item-assignment.

    Fail-closed channel-capability gate (spec A3): only a resident that
    declares ``webhook`` in its ``channels:`` list is invoke-reachable.
    """

    def _verify(request: web.Request, body: bytes) -> bool:
        if not webhook_secret:
            return True
        sig = request.headers.get("X-Webhook-Signature", "")
        expected = hmac.new(
            webhook_secret.encode(), body, hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    async def invoke_handler(request: web.Request) -> web.Response:
        limited = rate_limit_response(webhook_rate_limiter, "global")
        if limited is not None:
            return limited

        # Release A (spec A1): /invoke is fail-closed. With no global secret
        # (webhook auth disabled) the route is effectively OFF — it must never
        # accept an unauthenticated arbitrary-prompt request. Returns 403 rather
        # than 401 to signal the route is disabled, not merely mis-signed.
        if not webhook_secret:
            return web.json_response(
                {"error": "webhook auth disabled"}, status=403)

        body = await request.read()
        if not _verify(request, body):
            return web.json_response({"error": "invalid signature"}, status=401)

        agent_role = request.match_info.get("agent", assistant_role)
        cfg = role_configs.get(agent_role)
        if cfg is None or not agent_allowed_on("webhook", cfg):
            return web.json_response({"error": "unknown agent"}, status=404)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"error": "invalid JSON body"}, status=400)

        prompt = payload.get("prompt", "")
        if not prompt:
            return web.json_response({"error": "missing 'prompt' field"}, status=400)

        context = payload.get("context")
        if not isinstance(context, dict):
            context = {}
        # #324: caller-supplied cid wins (build_invoke_message's documented
        # contract) — stamp the middleware request cid only when the caller
        # provided none, so external systems can thread their own trace ids.
        caller_cid = context.get("cid")
        if not (isinstance(caller_cid, str) and caller_cid.strip()):
            context["cid"] = request["cid"]
        payload["context"] = context
        msg = build_invoke_message(agent_role, prompt, payload)
        try:
            result = await bus.request(msg, timeout=300)
            return web.json_response({"response": str(result.content)})
        except asyncio.TimeoutError:
            return web.json_response({"error": "timeout"}, status=504)
        except BusShutdownError:
            # #316: the bus gate refused (or abandoned) the request
            # because the container is shutting down.
            return web.json_response(
                {"error": "shutting down"}, status=503)

    return invoke_handler


def build_invoke_message(
    agent_role: str,
    prompt: str,
    payload: dict[str, Any],
) -> BusMessage:
    """Build a webhook invoke BusMessage with a guaranteed-unique session key.

    Callers may pass ``context.chat_id`` in the payload to pin the session
    (e.g. to continue a prior conversation). Otherwise a fresh UUID is
    assigned so two concurrent invocations do not collide on
    ``webhook:default``.

    Every invoke also gets a fresh correlation id (spec 5.2 §7.2).
    Caller-supplied ``context.cid`` wins so external systems can thread
    their own trace ids through; missing or empty entries are replaced.

    Sanitize-and-preserve (A:§3.5): the caller-supplied ``context`` is an
    EXTERNAL dict (webhook payload) — it is stripped of Casa-reserved
    provenance keys via ``sanitize_external_context`` before Casa's own
    keys (``chat_id``, ``cid``) are merged in, so a caller can never spoof
    ``execution_role``/``message_type``/``source``/etc. Every other
    caller-supplied key (e.g. a caller's own ``cid`` above) is preserved.
    """
    context = sanitize_external_context(payload.get("context"))
    if not context.get("chat_id"):
        context["chat_id"] = str(uuid.uuid4())
    if not context.get("cid"):
        context["cid"] = new_cid()
    # Release A: stamp the unspoofable origin route AFTER sanitization so a
    # caller cannot forge it. /invoke is operator-signed (HMAC) → the trusted
    # "invoke" route (private clearance, full runtime); distinct from the
    # "webhook_trigger" route stamped by the /webhook/{name} dispatch.
    context["_origin_route"] = "invoke"
    return BusMessage(
        type=MessageType.REQUEST,
        source="webhook",
        target=agent_role,
        content=prompt,
        channel="webhook",
        context=context,
        # #204: an operator-signed call is still a MACHINE caller — the global
        # HMAC secret is a bearer credential, so this is an ``invoke_caller``
        # automation, never the operator himself. Server-created like
        # ``_origin_route`` above; a caller cannot reach it through the body.
        trusted_user_origin=ingress_identity("invoke"),
    )


# ------------------------------------------------------------------
# Memory backend selection (spec 2.2b §2)
# ------------------------------------------------------------------

from dataclasses import dataclass as _dataclass


@_dataclass
class _SemanticMemoryChoice:
    """Long-term (SemanticMemory) backend pick: hindsight | noop."""
    backend: str            # hindsight | noop
    base_url: str = ""


def resolve_semantic_memory_choice(env: dict[str, str]) -> _SemanticMemoryChoice:
    """Resolve the SemanticMemory backend. ``MEMORY_BACKEND=hindsight`` requires
    ``HINDSIGHT_API_URL`` (no hardcoded ``hindsight`` host — spec §8.8); anything
    else → noop."""
    backend = env.get("MEMORY_BACKEND", "").strip().lower()
    base_url = env.get("HINDSIGHT_API_URL", "").strip()
    if backend == "hindsight":
        if not base_url:
            raise ValueError(
                "MEMORY_BACKEND=hindsight requires HINDSIGHT_API_URL "
                "(Hindsight is reached via its hassio alias/IP, not 'hindsight')"
            )
        return _SemanticMemoryChoice(backend="hindsight", base_url=base_url)
    if backend and backend != "noop":
        logger.warning("MEMORY_BACKEND=%r unrecognized; using noop", backend)
    return _SemanticMemoryChoice(backend="noop")


def build_semantic_memory(choice: _SemanticMemoryChoice) -> "SemanticMemory":
    from semantic_memory import NoOpSemanticMemory
    if choice.backend == "hindsight":
        from hindsight_memory import HindsightSemanticMemory
        logger.info("Hindsight semantic memory initialized (url=%s)", choice.base_url)
        return HindsightSemanticMemory(base_url=choice.base_url)
    logger.info("Semantic memory: NoOp (long-term disabled)")
    return NoOpSemanticMemory()


# ------------------------------------------------------------------
# Agent loader
# ------------------------------------------------------------------


def _build_role_registry(
    *,
    residents: dict,
    specialists: dict,
) -> dict:
    """Merge resident and specialist role→AgentConfig dicts. Fail on overlap.

    Returns a single dict the renamed delegate_to_agent tool resolves
    against. Roles must be globally unique across both tiers — colliding
    roles are a configuration bug (e.g. someone created
    agents/specialists/butler/ while a butler resident already exists).
    """
    overlap = set(residents) & set(specialists)
    if overlap:
        raise ValueError(
            f"duplicate role(s) across residents and specialists: "
            f"{sorted(overlap)} — each role must be unique"
        )
    merged = {}
    merged.update(residents)
    merged.update(specialists)
    return merged


# ------------------------------------------------------------------
# Config-sync operator notification
# ------------------------------------------------------------------


async def notify_config_sync(
    bus: Any,
    *,
    report_path: str = "/data/config-sync-report.json",
) -> None:
    """If the boot reconciler overwrote any runtime customization, push a
    heads-up directly to the operator over the ``telegram`` outbound bus
    target (``_telegram_outbound`` → operator's default chat), then mark the
    report notified to avoid duplicate alerts on an svc-only restart.

    Delivered via the deterministic ``telegram`` outbound router (not an
    LLM turn): a config-overwrite is a system event the operator must always
    see, so we bypass the assistant's turn — which could stay silent (the
    G-3 ``<silent/>`` doctrine-bleed history) and, with ``channel=""``, would
    resolve to no channel and drop the text entirely (``agent.py:230,296``).
    Non-fatal. Spec: 2026-06-08-config-sync-reconciler-design.md §3.6.
    """
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError):
        return

    if report.get("notified"):
        return
    # #398: entry-level reconcile added two more ways to lose local content —
    # an entry displaced/overridden by the merge, and an entry dropped for
    # failing the schema. Both must reach the operator; a merge that only ADDS
    # the image's entries must not, or the ordinary case alerts every update.
    destructive_merges = [
        m["path"] for m in report.get("merged", [])
        if m.get("displaced_local") or m.get("conflicted")
        or m.get("reinserted") or m.get("top_level_changed")
    ]
    over = bool(
        report.get("conflicts") or report.get("schema_forced")
        or report.get("casabak") or report.get("entries_dropped")
        or destructive_merges
    )
    if not over:
        return

    paths = list(dict.fromkeys(
        [c["path"] for c in report.get("conflicts", [])]
        + [c["path"] for c in report.get("schema_forced", [])]
        + [d["path"] for d in report.get("entries_dropped", [])]
        + destructive_merges
        + list(report.get("casabak", []))
    ))
    ver = report.get("image_version", "the latest update")
    listed = ", ".join(paths[:8]) + ("…" if len(paths) > 8 else "")
    content = (
        f"Heads up: applying {ver} overwrote {len(paths)} of your config "
        f"customization(s) so casa would keep booting: {listed}. "
        "Say 'reconcile config' and I'll show what changed (via git history) "
        "and carry any of it back."
    )

    # Route to the "telegram" outbound target if a telegram channel exists.
    if "telegram" in getattr(bus, "queues", {"telegram": None}):
        await bus.notify(BusMessage(
            type=MessageType.NOTIFICATION,
            source="config_sync",
            target="telegram",
            content=content,
            channel="telegram",
            context={"cid": new_cid()},
        ))

    report["notified"] = True
    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh)
    except OSError as exc:
        logger.warning("config_sync notify: could not mark report notified: %s", exc)


async def _boot_mint_resident_trigger_secrets(
    *, role_configs: dict, secrets_dir: Any,
) -> list[tuple[str, str]]:
    """Boot seam for #609: create the missing casa-owned per-trigger webhook
    secrets right after resident triggers register.

    Extracted from ``main()`` so it is unit-testable — ``main()`` has no
    execution coverage past its third statement, and step 13b sits outside
    every ``try`` in it.

    NEVER fatal, and that is a measured decision rather than a preference. On
    a healthy install this is a pure READ: ``ensure_secret`` is only reached
    for a slot the probe called ``absent``, so the only boot that can fail is
    one where a secret is missing AND ``/data`` is unwritable. Aborting there
    would trade Telegram, voice, every reminder and every engagement for one
    webhook that would 401 either way — and an exception escaping ``main()``
    does not restart Casa, it STOPS the app (``svc-casa/finish`` calls
    ``bashio::addon.stop`` for any exit code but 0/256). Every sibling boot
    degradation in this file does the same. The failure is carried by a WARN
    and by the report on the next reload, which is also the retry.
    """
    try:
        import resident_trigger_secrets
        failures = await asyncio.to_thread(
            resident_trigger_secrets.mint_for_specs,
            [t for cfg in role_configs.values() for t in (cfg.triggers or [])],
            secrets_dir=secrets_dir,
        )
        for name, reason in failures:
            logger.warning(
                "boot: webhook trigger %r has no usable secret and could not be "
                "minted (%s) — requests to it will be refused until the next "
                "reload or restart succeeds", name, reason)
        return failures
    except Exception:  # noqa: BLE001 — never fatal; see the docstring
        logger.warning("boot resident trigger-secret mint failed", exc_info=True)
        return []


async def _boot_reconcile_plugin_triggers(
    *, trigger_registry: Any, role_configs: dict,
) -> None:
    """Release B boot seam: derive + route the plugin-declared webhook
    trigger overlay AFTER resident triggers register and BEFORE the site
    serves (so /webhook/plg-… routes exist from the first request).

    Prompting is deferred (``prompt=False`` — Telegram is not polling yet;
    the operator-consent DM fires on the next lifecycle reconcile instead);
    when the reconcile surfaces trigger issues, the health report — freshly
    written WITHOUT them by the pre-service plugin_boot oneshot — is
    regenerated so the post-boot health DM announces e.g.
    ``trigger_pending_ack``. Never fatal: a reconcile failure boots with an
    empty overlay (fail-closed for ingress), not a dead Casa.
    """
    try:
        import trigger_reconcile
        issues = await trigger_reconcile.reconcile_plugin_triggers(
            trigger_registry=trigger_registry, role_configs=role_configs,
            channel_manager=None, prompt=False)
        if issues:
            from tools import _regenerate_plugin_health
            await asyncio.to_thread(_regenerate_plugin_health, [])
    except Exception:  # noqa: BLE001
        logger.warning("boot plugin-trigger reconcile failed", exc_info=True)


async def _boot_reconcile_plugin_callbacks(
    *, trigger_registry: Any, role_configs: dict,
) -> None:
    """Boot seam (paired with :func:`_boot_reconcile_plugin_triggers`):
    derive + route the plugin-declared authorization-callback overlay AND run
    the spool's boot maintenance, AFTER resident triggers register and BEFORE
    the setup-episode worker starts.

    Ordering is load-bearing: the reconcile publishes a fully-routed
    plugin's ready marker (and clears its ``callback_*`` health issues) BEFORE
    ``_pse.start_worker()`` can settle a round and check ``routes_live`` — so a
    settlement dispatch never races ahead of the marker.

    Also runs, in order: (1) a boot spool recovery pass restoring claims/temps
    stranded by a crash mid-handler, (2) a boot attempts pass materializing the
    per-flow ledger from the artifact union, (3) the overlay reconcile, and
    (4) a GATED orphan GC of spool dirs for plugins no longer in the registry.
    All are off-loop (the lock-stall ruling) and never fatal — a reconcile
    failure boots with an empty callback overlay (fail-closed for ingress),
    not a dead Casa.
    """
    import callback_spool

    # (1) Boot spool recovery — restore stranded claims/temps. boot=True: no
    # in-flight grace is needed at boot (nothing is claimed yet). Off-loop per
    # the lock-stall ruling (the pass holds the spool lock for a whole scan).
    try:
        spool = callback_spool.get_spool()
        if spool is not None:
            await asyncio.to_thread(spool.recovery_pass, now=time.time(),
                                    boot=True)
    except Exception:  # noqa: BLE001
        logger.warning("boot callback-spool recovery failed", exc_info=True)

    # (2) Boot attempts pass — materialize the ledger from the artifact union
    # (spec §3.3).
    try:
        spool = callback_spool.get_spool()
        if spool is not None:
            await asyncio.to_thread(spool.attempts_pass, now=time.time(),
                                    boot=True)
    except Exception:  # noqa: BLE001
        logger.warning("boot callback-attempts pass failed", exc_info=True)

    # (3) Route the overlay + publish markers.
    try:
        import callback_reconcile
        issues = await callback_reconcile.reconcile_plugin_callbacks(
            trigger_registry=trigger_registry, role_configs=role_configs,
            channel_manager=None, prompt=False)
        if issues:
            from tools import _regenerate_plugin_health
            await asyncio.to_thread(_regenerate_plugin_health, [])
    except Exception:  # noqa: BLE001
        logger.warning("boot plugin-callback reconcile failed", exc_info=True)

    # (4) Gated orphan GC — remove spool dirs of plugins no longer installed.
    # A NO-OP unless the registry loaded cleanly (a membership set from a
    # failed load would vaporize every plugin's spool). Membership keys on
    # registry ENTRIES, matching the spool's own contract.
    try:
        import plugin_registry
        spool = callback_spool.get_spool()
        if spool is not None:
            snap = plugin_registry.snapshot_registry()
            members = {e.get("name") for e in snap.entries
                       if isinstance(e, dict) and isinstance(e.get("name"), str)}
            await asyncio.to_thread(
                spool.gc_orphan_dirs, registry_valid=bool(snap.valid),
                member_plugins=members, now=time.time())
    except Exception:  # noqa: BLE001
        logger.warning("boot callback-spool orphan GC failed", exc_info=True)


async def _boot_reconcile_plugin_events(*, role_configs: dict) -> None:
    """Boot seam (paired with :func:`_boot_reconcile_plugin_triggers` /
    :func:`_boot_reconcile_plugin_callbacks`): derive + publish the
    plugin-declared event-subscription routing map.

    Unlike triggers/callbacks this reconciler needs no ``trigger_registry``
    — event routing is derived purely from the plugin registry + the
    subscriber's own role assignment (``event_reconcile.compute_desired``)
    — and there is no spool boot-recovery / legacy-migration step to run
    HERE: ``event_episodes.recovery(boot=True)`` (called in ``main()`` after
    the worker's seams are configured, deliberately AFTER this function
    returns) is what reconstructs the per-flow delivery ledger from the
    artifacts, using the routing THIS reconcile just published — ordering
    that pass after this one is what lets its boot-time reconstruction see
    live routing instead of the ``ROUTING_UNAVAILABLE`` sentinel default.

    Never fatal: a reconcile failure boots with event_reconcile's own
    fail-closed sentinel published (never an authoritative empty map), not a
    dead Casa.
    """
    try:
        import event_reconcile
        issues = await event_reconcile.reconcile_plugin_events(
            None, role_configs=role_configs, channel_manager=None,
            prompt=False)
        if issues:
            from tools import _regenerate_plugin_health
            await asyncio.to_thread(_regenerate_plugin_health, [])
    except Exception:  # noqa: BLE001
        logger.warning("boot plugin-event reconcile failed", exc_info=True)


def _event_registry_entry(subscriber: str) -> "dict | None":
    """``event_episodes``'s ``resolve_registry_entry`` seam. Same source as
    :func:`_setup_registry_entry` (targets + artifact id from the resolved
    plugin + registry entry) PLUS the resolved MANIFEST dict — the worker's
    pre-send gate recomputes the declaration digest from it
    (``event_episodes._gate_ok``)."""
    import plugin_registry as _pr
    res = _pr.resolve_all()
    rp = next((p for p in res.plugins if p.name == subscriber), None)
    if rp is None:
        return None
    snap = _pr.snapshot_registry()
    entry = next(
        (e for e in snap.entries
         if isinstance(e, dict) and e.get("name") == subscriber), None)
    return {
        "targets": list((entry or {}).get("targets") or []),
        "artifact_id": rp.artifact_id,
        "manifest": rp.manifest,
    }


def _event_installed() -> set:
    """``event_episodes``'s ``get_installed`` seam. Fail-closed under an
    invalid registry snapshot — mirrors the gated orphan-GC lesson at
    :func:`_boot_reconcile_plugin_callbacks`'s step (5) /
    ``casa_core.py:2152-2156``: a membership set derived from a failed load
    must never look like "nothing is installed" and license the worker's
    sweep to vaporize every subscriber's spool state.
    :func:`_event_registry_valid` is what actually gates the worker's use
    of this set; this function itself just never fabricates membership from
    an invalid snapshot."""
    import plugin_registry as _pr
    snap = _pr.snapshot_registry()
    if not snap.valid:
        return set()
    return {e.get("name") for e in snap.entries
            if isinstance(e, dict) and isinstance(e.get("name"), str)}


def _event_registry_valid() -> bool:
    """``event_episodes``'s ``get_registry_valid`` seam."""
    import plugin_registry as _pr
    return bool(_pr.snapshot_registry().valid)


def _event_emitters() -> set:
    """``event_episodes``'s ``get_emitters`` seam (Critical-1): every
    installed plugin that currently declares at least one ``casa.emits``
    entry — the set the worker provisions spool dirs (and a ready marker)
    for on every pass/recovery, closing the "ensure_emitter_dirs has zero
    production callers" gap. Fail-closed under an invalid resolve, mirroring
    :func:`_event_installed`: an unresolvable declaration set never looks
    like "nobody emits" and get silently skipped forever — provisioning
    simply retries next pass, exactly like every other seam here. A single
    plugin's own bad ``casa.emits`` declaration is that plugin's concern
    (surfaced elsewhere as a health issue) and never aborts enumeration for
    the rest."""
    import plugin_registry as _pr
    import plugin_store
    res = _pr.resolve_all()
    if not getattr(res, "registry_valid", False):
        return set()
    out = set()
    for rp in res.plugins:
        try:
            emits = plugin_store.manifest_emits(rp.manifest, rp.name)
        except Exception:  # noqa: BLE001
            continue
        if emits:
            out.add(rp.name)
    return out


def _callback_and_trigger_routes_live(plugin: str) -> bool:
    """Setup-dispatch route gate. A plugin's setup tool must not be
    dispatched while EITHER its trigger OR its callback markers are dark.

    The trigger-only gate is PERMISSIVE for callbacks: a plugin whose callbacks
    are dark for a NON-consent reason (``callback_no_target`` / ``_invalid`` /
    ``_base_url_invalid``) contributes no consent-round member, so the trigger
    approval alone would settle the round and dispatch setup at an endpoint
    whose callback ingress is not routed. Requiring NO outstanding ``callback_*``
    issue (its markers are published) closes that hole. Per-plugin
    all-or-nothing means one issue keeps the whole plugin's set dark, exactly
    as the trigger gate treats trigger issues.

    Both halves report ``(ok, issues, observed)`` and a NOT-ok half keeps the
    plugin dark (#453). An empty issue list is the positive claim "no gap"; a
    recomputation that could not run at all produces the same empty list, so
    reading it as a verdict is how the one check that must fail closed would
    fail open.

    ``ok`` alone is not enough, because it reports that the computation RAN, not
    that it SAW this plugin (#457). Two states produce a successful, issue-free
    computation a plugin is simply absent from: an invalid registry, where both
    reconcilers return an empty result by design (and the pass that follows swaps
    in an EMPTY overlay, so every plugin webhook 404s), and a single artifact
    that fails to resolve, which becomes a ``stage="resolve"`` issue in place of
    the plugin — named by no ``trigger_*``/``callback_*`` row the gate matches
    on. Both read as "this plugin has no gap" for a plugin the recomputation
    never looked at. So membership in ``observed`` is required POSITIVELY, and
    absence of an issue is only read as a verdict about a plugin the computation
    actually iterated. The setup worker's own three-state registry resolution
    still runs earlier and still defers — but it is a separate, unpinned read,
    so it was a shield rather than a property of this gate.

    ONE pinned registry resolution serves both halves (#454): the gate is a
    single decision, and composing a trigger position read from one registry
    generation with a callback position read from another is the same defect
    inside the gate that the reconcilers fixed inside a pass. Blocking I/O —
    this reads the secret sidecars and the marker pair — so callers on the event
    loop must run it in a thread.
    """
    import callback_reconcile as _cr
    import plugin_registry as _pr
    import trigger_reconcile as _tr
    pinned = _pr.pinned_resolver()
    for state, prefix in ((_tr.issue_state(pinned), "trigger_"),
                          (_cr.issue_state(pinned), "callback_")):
        if not state.ok:
            return False
        if plugin not in state.observed:
            logger.info(
                "setup route gate: plugin %s absent from the %s recomputation "
                "— holding", plugin, prefix.rstrip("_"))
            return False
        if any(str(getattr(i, "reason_code", "")).startswith(prefix)
               and getattr(i, "name", "") == plugin for i in state.issues):
            return False
    return True


def _setup_ack_lookup_union(identity: str) -> str | None:
    """Setup-round crash-recovery ack lookup. Trigger and callback
    acks live in DISJOINT sha256 identity spaces, so a stranded round whose
    open member is a CALLBACK consent only heals if the lookup unions BOTH
    stores — a trigger-only lookup would leave it stranded forever. Returns the
    ack's generation from whichever store owns the identity, or ``None``."""
    import callback_acks as _ca
    import trigger_acks as _ta
    for store in (_ta.ACKS, _ca.ACKS):
        rec = store.get(identity)
        if rec and rec.get("gen"):
            return str(rec["gen"])
    return None


async def operator_notify(channel_manager: Any, text: str) -> None:
    """The operator-notice seam shared by the setup/callback/event episode
    workers (#532): deliver *text* to the operator DM, or RAISE.

    Pre-fix this was a best-effort closure over ``send_response``, which
    log-and-drops while the PTB app is not started (`is_ready`,
    telegram.py) — a FALSE SUCCESS that made every notify-then-mark
    caller mark a dropped notice as delivered, permanently losing it
    (observed live: an exhaustion notice dispatched one second after
    "event-spool initialised", before the channel started). Raising makes
    delivery OBSERVED: the workers' removal/exhaustion scans leave the
    record un-noted and retry, and their advisory ``_note`` paths degrade
    to honest logging."""
    import trigger_consent as _tc
    ch = channel_manager.get("telegram") if channel_manager else None
    if ch is None or not getattr(ch, "is_ready", True):
        raise RuntimeError("operator notify: telegram channel not ready")
    # Address the operator DM explicitly (falls back to the channel's
    # default chat — also the operator — when identity is unresolvable).
    op = _tc.operator_identity(ch)
    ctx = {"chat_id": op[0]} if op is not None else {}
    await ch.send_response(text, ctx)


# #556: every out-of-band operator notice runs the same sequence — read state,
# await a send, mark state. That sequence spans an await, so without a
# reservation two callers read the same pending work and both send it. It was
# found twice, in two different notifiers (Sol r1/r2, Terra r2), so it gets ONE
# guard rather than one per notifier. Invariant, stated once: at most one
# operator notice is in flight, and no notice is marked before it is confirmed.
# Every holder must RE-READ its state after acquiring — the whole point is that
# the state may have been marked by the caller that went first.
_OPERATOR_NOTICE_LOCK = asyncio.Lock()


# #513: files already announced to the operator this process. Per-file, per
# process, which is what the issue asks for ("inform, not nag"). Persisting it
# is deliberately out of scope.
_placeholder_notified: set[str] = set()

# #573: strong references for the boot scheduled-ask reconcile task.
_SCHEDULED_ASK_TASKS: set = set()


async def notify_placeholder_rewrites(channel_manager: Any) -> None:
    """Tell the operator that a ``${...}``-bearing triggers.yaml was rewritten.

    Cleanup warns and PROCEEDS when it must rewrite such a file, because
    refusing strands a delivered reminder into redelivering forever. The person
    that warning concerns is the operator — it is their hand-written config
    whose entries may now resolve differently — but it only ever existed at
    log_level, where they never see it (#513).

    Peek-and-confirm, never drain: a path leaves the pending set on exactly one
    condition — it was delivered, or it was already announced in this process.
    An exception out of ``send`` therefore leaves it pending by construction,
    with no ``finally`` to undo that.

    Retry is "the next rewrite": if a notice fails and no further rewrite
    happens in this process, the operator is not told. That is a deliberate
    stopping point — closing it needs either persistence (which #513 rules out)
    or a retry worker, and a retry worker would be a fourth delivery path
    carrying the same confirm-before-consume obligation that this batch exists
    to get right ONCE.
    """
    import reminders
    channel = (channel_manager.get("telegram")
               if channel_manager is not None else None)
    if channel is None or not getattr(channel, "is_ready", True):
        return  # nothing consumed — retried on the next rewrite
    async with _OPERATOR_NOTICE_LOCK:
        for path in reminders.peek_placeholder_notices():
            # Re-checked INSIDE the lock: a caller that went first may have
            # announced this very path while this one was queued.
            if path in _placeholder_notified:
                reminders.clear_placeholder_notice(path)
                continue
            outcome = await channel.send(
                f"⚠️ Updated {path}, which uses ${{...}} placeholders — a "
                f"placeholder entry there may now resolve differently. "
                f"Worth a look.", {"cid": new_cid()})
            if outcome is not DeliveryOutcome.NOT_DELIVERED:
                _placeholder_notified.add(path)
                reminders.clear_placeholder_notice(path)


async def notify_plugin_health(
    channel_manager: Any,
    *,
    path: str = "/data/plugin-health.json",
) -> None:
    """Push an operator DM for NEW plugin-health issues (§3.10). Deduped by
    STRUCTURED fingerprints (not free text). Marks notified ONLY after a
    CONFIRMED send, so a Telegram-down boot/mutation retries next time.
    Non-fatal.

    #342: delivery is a direct, awaited ``channel.send`` — NOT a bus
    enqueue. The ``telegram`` bus target is registered unconditionally by
    ``main()`` and its outbound handler silently drops when no channel is
    configured, so "enqueued" never meant "sent": a boot without Telegram
    marked the fingerprints notified and the alert was lost forever even
    once Telegram was configured later.
    """
    import plugin_health
    async with _OPERATOR_NOTICE_LOCK:
        await _notify_plugin_health_locked(channel_manager, path)


async def _notify_plugin_health_locked(channel_manager: Any, path: str) -> None:
    """The body of :func:`notify_plugin_health`, run under
    ``_OPERATOR_NOTICE_LOCK``. The report is read HERE, after the lock is
    acquired, so a caller that queued behind another sees the fingerprints the
    first one marked instead of re-sending them (#556)."""
    import plugin_health
    report = plugin_health.load_report(path)
    if not report:
        return
    fps = plugin_health.new_fingerprints(report)
    if not fps:
        return
    fp_set = set(fps)
    # Sol #17: the body must span issues AND warnings — new_fingerprints now
    # includes warning fingerprints, so filtering only `issues` would announce a
    # count of 0 for a warning-only change (and mark it notified anyway).
    entries = [e for e in (list(report.get("issues", []))
                           + list(report.get("warnings", [])))
               if e.get("fingerprint") in fp_set]
    # #551: the DM addresses the same human as the in-band notice, so it shares
    # the one operator-facing renderer rather than printing reason codes. It
    # carries the detail (an unresolved var name, a setup episode's last_error)
    # for the same reason the notice does — #533/#554, nothing else conveys it.
    # The old "See /data/plugin-health.json" tail is gone — it named a door that
    # did not open; since #555 the door is plugin_status, which the recipient's
    # own assistant can call, so a truncated "+N more" tail is now answerable.
    #
    # The SENTENCE is plugin_health.render_line, not a second implementation of
    # it: this path used to build its own and had already drifted from the
    # in-band one (it announced an all-reload_required set as a generic fault).
    # The limit stays 5 here — a DM is a message of its own, and naming five is
    # information the operator has no other way to get in that moment.
    _DM_LIMIT = 5
    content = plugin_health.render_line(entries, limit=_DM_LIMIT)
    # #559: what this message NAMES is what it may claim to have delivered.
    # `render_line` names the first `_DM_LIMIT` rows and turns the rest into
    # "and N more", but every new fingerprint used to be marked — so a row the
    # operator never saw the name of was recorded as announced, which removed
    # it from this surface permanently and (now that the in-band notice filters
    # on the same field) from that one too. Marking the named prefix instead
    # leaves the remainder UNRECORDED, which is all this can promise: nothing
    # schedules a follow-up, so those rows are named whenever the next
    # regeneration-and-notify happens, and the in-band notice shows only the
    # subset it selects (blocking issues for its own role or targetless, up to
    # its own limit). `plugin_status` reports the whole standing set unfiltered
    # for exactly that reason. Issues precede warnings here, so a blocking row
    # is never the one deferred behind a warning.
    named_fps = [e.get("fingerprint") for e in entries[:_DM_LIMIT]
                 if e.get("fingerprint")]
    channel = (channel_manager.get("telegram")
               if channel_manager is not None else None)
    # Sol r1-2: channel.send() log-and-drops while the PTB app is not
    # started — is_ready must gate the "notified" mark too, or a
    # bring-up/reconnect window counts a dropped alert as delivered.
    if channel is None or not getattr(channel, "is_ready", True):
        return  # no deliverable channel — retry next boot/mutation
    try:
        outcome = await channel.send(content, {"cid": new_cid()})
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugin_health notify: send failed: %s", exc)
        return  # not marked → retried next boot/mutation
    # #556: the is_ready pre-check above is a cheap early-out, not proof — the
    # app can be torn down between that check and this send. Only a PROVEN
    # negative withholds the mark; UNKNOWN (a channel not on the contract,
    # returning None) keeps today's behavior, or a non-reporting channel would
    # re-DM the same issue on every boot forever.
    if outcome is DeliveryOutcome.NOT_DELIVERED:
        return  # not marked → retried next boot/mutation
    # #559: fenced on the generation of the report this message DESCRIBED. A
    # regeneration during the send means the rows may no longer be the ones
    # just named, so nothing is marked and the DM re-fires on the next pass —
    # a bounded duplicate rather than a fingerprint resurrected onto a row that
    # resolved, which is how a later recurrence went unannounced.
    plugin_health.mark_notified(named_fps, path,
                                generation=report.get("generation"))


# ------------------------------------------------------------------
# Engagement-topic retention sweep (v0.65.0 [AR-8])
# ------------------------------------------------------------------

# Once-per-boot flag for the "grant me Delete messages" operator nag — the
# notify_config_sync-style dedupe, held in module state (not a report file)
# because the underlying condition (needs_permission) re-trips on every
# 6-hour sweep until the operator grants the right. Consumed only on a
# successful notify — a failed delivery is retried at the next sweep.
_topic_permission_notified = False


async def _sweep_engagement_topics(channel_manager: Any) -> None:
    """Periodic topics pass — runs right after the workspace sweep [AR-8].

    Deletes due terminal-engagement topics recorded in the topic ledger
    through the telegram channel. When telegram is unconfigured (no
    channel, or no engagement supergroup) the pass skips cleanly: entries
    kept, no warning spam. Never raises — sweep_topics handles per-entry
    telegram errors itself but can raise on a broken channel object, and a
    broken pass must not kill the shared scheduler job.
    """
    global _topic_permission_notified  # noqa: PLW0603 — once-per-boot dedupe

    channel = channel_manager.get("telegram") if channel_manager else None
    if channel is None or not getattr(channel, "engagement_supergroup_id", None):
        return

    import topic_ledger

    try:
        res = await topic_ledger.sweep_topics(
            channel,
            chat_id=channel.engagement_supergroup_id,
            scope="due",
        )
    except Exception as exc:  # noqa: BLE001 — never kill the scheduler job
        logger.warning("topic sweep failed: %s", exc)
        return

    if res.get("deleted"):
        logger.info(
            "topic sweep: deleted=%s kept=%s dropped_mismatched=%s",
            res.get("deleted"), res.get("kept"), res.get("dropped_mismatched"),
        )

    if res.get("needs_permission") and not _topic_permission_notified:
        content = (
            'Casa needs the "Delete messages" admin right in the engagement '
            "supergroup to clean up finished topics — for now they are only "
            "closed, not deleted. Grant it via the group's admin settings "
            "for the bot (DOCS.md Setup step 6) and I'll retry at the next "
            "sweep."
        )
        # #342: deterministic operator delivery as a DIRECT awaited channel
        # send — never an LLM turn. The flag is consumed only when the SEND
        # succeeds; the old bus enqueue consumed it before the outbound
        # dispatch ran, so a transient Telegram failure suppressed the
        # reminder for every later sweep that boot. Sol r1-2: is_ready
        # gates the flag too — send() log-and-drops on an unstarted app.
        if not getattr(channel, "is_ready", True):
            return
        try:
            await channel.send(content, {"cid": new_cid()})
            _topic_permission_notified = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "topic sweep: permission nag notify failed: %s", exc,
            )


# ------------------------------------------------------------------
# Authorization-grant TTL sweep (A:§3.3) — hourly, beside the engagement
# daily sweep. GrantStore has no private loop of its own (unlike
# session_sweeper.py, which owns one) — this scheduler job is its only
# sweep seam.
# ------------------------------------------------------------------


async def _authz_grant_sweep() -> None:
    """Drop every authorization grant past its TTL.

    A grant that is never consumed (the operator never taps Approve, or
    taps after the tool call was abandoned) would otherwise sit in
    memory forever — GrantStore has no other reaper. Never raises: a
    sweep failure must not kill the shared scheduler job.
    """
    try:
        removed = GRANTS.sweep()
        if removed:
            logger.info("authz grant sweep: dropped %d expired grant(s)", removed)
    except Exception:  # noqa: BLE001 — never kill the scheduler job
        logger.warning("authz grant sweep failed", exc_info=True)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------


def _recovery_delivery_ack(job_registry, job):
    """The delivery acknowledgement a recovery notice carries (#701).

    Clears whichever durable marker the row was announced for. Both are cleared
    because both are idempotent and the two can only ever be set one at a time
    (the orphan marker is written by the live-row conversion, the terminal one
    by a terminal that was already reached) — asking which is which twice would
    be a second place to get it wrong.

    Deliberately no exception handling here: the agent's acknowledgement seam
    owns that, and its rule is that a failed acknowledgement leaves the marker
    set so the announcement is repeated at the next boot.
    """
    async def _ack() -> None:
        if job.orphan_notification_pending:
            await job_registry.ack_orphan_notification(job.id)
        if job.terminal_notification_pending:
            await job_registry.ack_terminal_notification(job.id)
    return _ack


async def _notify_recovered_delegations(
    recovered_jobs,
    job_registry,
    bus,
    *,
    assistant_role: str,
) -> None:
    """Announce every Telegram row that still owes its creator a notice.

    #701: "then durably acknowledge" is exactly what this used to do and no
    longer does. `bus.notify` reports only that a message was ACCEPTED onto a
    queue; the turn that owes the operator its narration runs later, in a
    dispatch task nothing gathers at shutdown. Acknowledging here cleared the
    durable obligation while nothing had been delivered, and since recovery
    converts only LIVE rows, the announcement was lost permanently rather than
    retried. Each notice now carries an `on_delivery` callback the resident
    invokes once its channel reports the turn reached the transport, and THAT
    clears the marker. Boot does not wait for any of it — a resident turn can
    take minutes — so this returns as soon as the last message is enqueued.

    Two shapes are announced. A row carrying a failure envelope (a restart
    orphan, or a delegation that FAILED during the stop) replays its own typed
    kind. A row that SUCCEEDED during the stop replays as a success whose
    answer is unavailable: Casa does not retain a non-voice specialist's answer
    (#688, decided), and the row carries everything a truthful notice needs.
    """
    from specialist_registry import DelegationComplete

    for job in recovered_jobs:
        if job.creator_peer != "telegram":
            continue
        if not (job.orphan_notification_pending
                or job.terminal_notification_pending):
            continue
        target_role = job.creating_role or assistant_role
        if target_role not in bus.queues:
            logger.error(
                "Orphan delegation %s targets unknown role %r — retained for retry",
                job.id[:8], target_role,
            )
            continue

        # This compatibility signal carries only the stable failure envelope
        # and origin metadata. No specialist output is reintroduced into the
        # resident's context during restart recovery. #688 decided that as a
        # RETENTION posture too: a non-voice specialist's answer is not kept
        # across a restart, so a successful replay reports the fact of the
        # answer rather than the answer, and never presents its empty stored
        # result as one.
        succeeded = job.failure is None
        synthetic = DelegationComplete(
            delegation_id=job.id,
            agent=job.specialist_role,
            status="ok" if succeeded else "error",
            kind="" if succeeded else job.failure.kind,
            message="" if succeeded else job.failure.message,
            # No replay carries an answer: nothing was retained to carry.
            result_available=False,
            origin={
                "role": job.creating_role,
                "channel": job.creator_peer,
                "chat_id": job.scope_id,
                "cid": job.origin_route_id or "-",
                "user_text": job.task,
                # #485: restored from the durable field, so a scheduled turn
                # resumed after a restart can still deliver media. Added only
                # when stored — a legacy row has no field and stays text-only.
                **({"_scheduled_delivery": True}
                   if job.scheduled_delivery is True else {}),
            },
            elapsed_s=0.0,
        )
        try:
            await bus.notify(BusMessage(
                type=MessageType.NOTIFICATION,
                source=job.specialist_role,
                target=target_role,
                content=synthetic,
                channel=job.creator_peer,
                context={
                    "cid": job.origin_route_id or "-",
                    "chat_id": job.scope_id,
                    "delegation_id": job.id,
                },
                on_delivery=_recovery_delivery_ack(job_registry, job),
            ))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad recovery must not block later jobs
            # Do not log exception text/tracebacks: connector failures can
            # include payload or credential material. The durable pending bit
            # retains enough state for the next boot to retry.
            logger.error(
                "Orphan notification failed: id=%s phase=notify — retained",
                job.id[:8],
            )
            continue

        logger.warning(
            "Orphan delegation recovered: id=%s agent=%s — NOTIFICATION posted",
            job.id[:8], job.specialist_role,
        )


# §8: the password-typed app options, whose values may be an `op://` reference
# the boot path resolves once, before any consumer reads them.
# OP_SERVICE_ACCOUNT_TOKEN is deliberately absent — it is the credential the
# resolution itself needs, and an `op://` reference to it could never resolve.
_PASSWORD_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "WEBHOOK_SECRET",
    # #277: context7_api_key is password-typed too — exported by svc-casa/run;
    # without this entry an op:// value reached the context7 MCP server as the
    # literal reference.
    "CONTEXT7_API_KEY",
)


def _resolve_password_options(environ=None) -> "list[str]":
    """Resolve each password-typed option's ``op://`` reference IN PLACE.
    Returns the variables whose resolution failed (order preserved).

    What happens to a variable that could not be resolved differs by who reads
    it, and the rule is the ranked one (#580):

    * A variable a **plugin** may reference is UNSET. Those are exactly the
      names in ``plugin_store.CASA_OWNED_ENV_OPTIONS`` — the map of env names a
      plugin may name in its ``.mcp.json`` and Casa supplies — so their consumer
      is an MCP server, and the CLI hands it whatever the variable holds. A bare
      ``${VAR}`` reference then withholds the plugin (INV-PLUG-008), and a
      ``${VAR:-default}`` reference takes its default; keeping the literal
      launches the server on a placeholder credential, which is the failure the
      withhold gate exists to prevent.
    * A variable **Casa itself** consumes keeps the raw reference. Each of those
      fails loudly on a meaningless credential, while ABSENCE would be silent
      and worse: ``TELEGRAM_BOT_TOKEN`` is read as ``if telegram_token:``, so
      unsetting it builds no Telegram channel at all — removing the operator's
      only notification surface, including the plugin-health DM. The webhook
      secret has its own, narrower treatment at the point of use (#333):
      ``webhook_auth`` refuses an ``op://`` value as an HMAC key and the
      discovery publisher withdraws what it published.
    """
    from plugin_store import CASA_OWNED_ENV_OPTIONS
    from secrets_resolver import resolve as _resolve_secret

    env = os.environ if environ is None else environ
    failed: list[str] = []
    for var in _PASSWORD_ENV_VARS:
        raw = env.get(var, "")
        if not raw:
            continue
        try:
            resolved = _resolve_secret(raw)
        except RuntimeError as exc:
            failed.append(var)
            if var in CASA_OWNED_ENV_OPTIONS:
                env.pop(var, None)
                logger.warning(
                    "secrets_resolver: %s op:// resolution failed: %s — "
                    "leaving it unset; a plugin requiring it is withheld and a "
                    "defaulted reference takes its default", var, exc)
            else:
                logger.warning(
                    "secrets_resolver: %s op:// resolution failed: %s — "
                    "using raw value; credential will likely be rejected",
                    var, exc)
            continue
        if resolved != raw:
            env[var] = resolved
    return failed


async def main() -> None:
    """Async entry point for the Casa add-on."""

    # 1. Logging (correlation ids + secret redaction, spec 5.2 §7).
    _log_level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    _log_level = getattr(logging, _log_level_name, logging.INFO)
    install_logging(level=_log_level)
    # P-4 (v0.68.2): detached SDK control-request tasks racing subprocess
    # teardown die with an unretrieved CLIConnectionError that asyncio GC
    # would log at ERROR on every engagement close.
    from sdk_logging import install_sdk_task_noise_filter
    install_sdk_task_noise_filter(asyncio.get_running_loop())
    logger.info("Casa core starting up")

    # 1b. #203: every external ingress must be able to name the author of its
    # turns. A deterministic defect in that table (a dropped route, an empty
    # peer, an unrecognized strategy) is a programming error that would send
    # the affected ingress silently back to the unattributed ``system``
    # identity, so it fails startup here rather than degrading in the field.
    # Unconditional and early ON PURPOSE — v0.125.0 crash-looped production
    # from a defect inside `if telegram_token:`, a branch no unit test enters.
    # The validator is pure, narrow (it reads no runtime data) and directly
    # unit-tested; per-request identity failures are handled at the ingress,
    # never here.
    validate_ingress_identity_table()

    voice_delivery_config = load_voice_delivery_config()

    observed_cli = await asyncio.to_thread(verify_effective_cli)
    observed_version = observed_cli.split(maxsplit=1)[0]
    logger.info(
        "Claude CLI verified path=%s expected=%s observed=%s",
        CLAUDE_CLI_PATH,
        CLAUDE_CLI_VERSION,
        observed_version,
    )

    # 1a. §8: universal op:// resolution for password-typed addon options.
    # OP_SERVICE_ACCOUNT_TOKEN is already in env (exported by svc-casa/run from
    # the onepassword_service_account_token addon option). Resolve all
    # password-typed options in-place now, before any consumer reads them. What
    # an unresolvable reference leaves behind is per-variable — see
    # _resolve_password_options.
    from secrets_resolver import resolve as _resolve_secret
    _resolve_password_options()

    # 1b. §5.5 / §8.3: source plugin-env.conf into process env.
    # Resolved after OP_SERVICE_ACCOUNT_TOKEN is available so that op://
    # references inside plugin-env.conf can be resolved by the same `op` CLI.
    from plugin_env_conf import read_entries as _read_plugin_env
    _plugin_env_entries = _read_plugin_env()
    for _var, _value in _plugin_env_entries.items():
        try:
            os.environ[_var] = _resolve_secret(_value)
        except RuntimeError as _exc:
            logger.warning(
                "plugin-env: %s unresolved: %s — plugin's MCP server will fail to start",
                _var, _exc,
            )
    # M22 (v0.49.0): seed reload's deletion-diff snapshot so the first
    # casa_reload(scope='plugin_env') can DROP keys applied at boot that
    # were later removed from plugin-env.conf. Seeding keys whose op://
    # resolution failed above is safe — reload's removal path uses
    # os.environ.pop(var, None), a no-op for absent vars.
    from reload import note_boot_plugin_env as _note_boot_plugin_env
    _note_boot_plugin_env(set(_plugin_env_entries))

    # 2. Long-term semantic memory (spec §5/§4.2): the only memory path —
    # Hindsight or noop (resolve_semantic_memory_choice).
    semantic_memory = build_semantic_memory(resolve_semantic_memory_choice(dict(os.environ)))

    def _agent_home_dir(role: str) -> str:
        """Resident transcript cwd (encoded-cwd dir for get_session_messages /
        delete_session). Matches agent.py's agent_home WHEN ``config.cwd`` is
        unset — the prod default (all shipped configs have ``cwd: ""``). If a
        resident ever sets a non-empty ``config.cwd``, its transcript lands
        there instead and the reaper/save would look in the wrong dir; keep
        ``config.cwd`` empty for residents. (Formula also duplicated in
        session_sweeper/session_saver/agent.py — consolidate in a cleanup.)

        Task 9: the reaper/sweeper read the stored ``agent`` field, which now
        holds the canonical role_id (``resident:butler``). Route it through
        ``agent_home_for_role_id`` (which returns the SAME bare-slug path
        agent.py writes transcripts to); a legacy short-role entry falls back
        to the bare-slug formula."""
        from agent import agent_home_for_role_id
        try:
            return agent_home_for_role_id(role)
        except ValueError:
            return f"/config/agent-home/{role}"

    # 3. Message bus
    bus = MessageBus()

    # #573: durable pending-ask records. Installed BEFORE any trigger can fire
    # (the scheduler starts later) so a scheduled ask never runs against an
    # uninitialised store; reconciled after the Telegram channel is ready.
    import scheduled_asks
    scheduled_asks.init_store(os.path.join(DATA_DIR, "scheduled_asks.json"))

    # 4. Session registry + TTL sweeper (spec 5.2 §6)
    sessions_path = os.path.join(DATA_DIR, "sessions.json")
    session_registry = SessionRegistry(sessions_path)
    # Release A / Layer 4: purge every persisted webhook session at boot —
    # a stored webhook session's origin route is unknowable after restart.
    # Only persists when something actually changed.
    _purged_webhook_sessions = session_registry.purge_webhook_sessions()
    if _purged_webhook_sessions:
        logger.info(
            "session_registry: purged %d persisted webhook session(s) at boot",
            _purged_webhook_sessions,
        )
        await session_registry.save()
    session_sweeper = SessionSweeper(
        registry=session_registry,
        session_ttl_days=_env_int_or("SESSION_TTL_DAYS", 30, min_value=1),
        webhook_session_ttl_days=_env_int_or(
            "WEBHOOK_SESSION_TTL_DAYS", 1, min_value=1,
        ),
        directory_for=_agent_home_dir,
    )
    freshness_reaper = FreshnessReaper(
        registry=session_registry,
        semantic_memory=semantic_memory,
        directory_for=_agent_home_dir,
    )

    # 5. MCP server registry
    mcp_registry = McpServerRegistry()

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    ha_mcp_url = os.environ.get(
        "CASA_HA_MCP_URL",
        "http://supervisor/core/api/mcp",
    )
    if supervisor_token:
        mcp_registry.register_http(
            name="homeassistant",
            url=ha_mcp_url,
            headers={"Authorization": f"Bearer {supervisor_token}"},
        )
        logger.info("Registered Home Assistant MCP server (url=%s)", ha_mcp_url)

    _maybe_register_n8n(mcp_registry)

    # 5b. Config git repo — initialise (idempotent) and snapshot any
    # manual edits that landed between boots. #337 (Sol r2): scrub legacy
    # plaintext secret keys from persisted specialist tuple snapshots FIRST,
    # so the boot snapshot below can never commit them to the config repo.
    try:
        from specialist_install import sanitize_specialist_snapshots
        sanitize_specialist_snapshots()
    except Exception:  # noqa: BLE001 — best-effort; never boot-fatal
        logger.warning("specialist snapshot scrub failed", exc_info=True)
    try:
        init_repo(CONFIG_DIR)
        snapshot_manual_edits(CONFIG_DIR)
    except Exception as exc:
        logger.warning("config_git bootstrap failed: %s", exc)

    # 6. Channel manager
    channel_manager = ChannelManager()

    # 7. Framework tools
    from tools import create_casa_tools, init_tools
    import specialist_registry as specialist_registry_module
    from specialist_registry import InstalledSpecialistIndex, SpecialistRegistry
    from job_registry import JobRegistry

    # One durable owner for both delegated execution and voice-delivery state.
    # Load and recover before constructing the compatibility facade so no
    # second lifecycle table can observe or publish a divergent state.
    job_registry = JobRegistry(
        os.path.join(DATA_DIR, "jobs.json"),
        result_ttl_seconds=voice_delivery_config.delivery_ttl_s,
    )
    await job_registry.load()
    recovered_jobs = await job_registry.recover_after_restart()

    # Task 13: the NEW installed-specialist data model — a SEPARATE tree
    # (/config/specialists/<slug>/) and a SEPARATE object from the legacy
    # SpecialistRegistry below (bundled /config/agents/specialists/). Wired
    # here, before any channel/bus loop starts, so the module-level accessor
    # (specialist_registry.live_collision_slugs/live_installed_specialist_slugs)
    # is populated for the rest of boot — same ordering guarantee Task 8
    # established for the four compiled-personality registries.
    #
    # Task N1b Step 25 (Round-4 fix, finding #2): moved BEFORE
    # SpecialistRegistry construction/.load() (was after, Task 13 baseline)
    # so this SAME index doubles as the source current_specialist_roles_dir
    # reconciles the roles overlay from below — one InstalledSpecialistIndex
    # object, not two, and boot self-heals any installed specialist's
    # operational files (not just the roles overlay) the identical way every
    # casa_reload call site does (specialist_materialize.py, reload.py).
    installed_specialist_index = InstalledSpecialistIndex(
        os.path.join(CONFIG_DIR, "specialists"),
    )
    installed_specialist_index.load()
    specialist_registry_module.set_active_installed_index(installed_specialist_index)

    # Round 6, F3: current_specialist_roles_dir acquires MATERIALIZE_LOCK (a
    # threading.Lock) for its in-lock index reload + op-file self-heal + overlay
    # rebuild. Boot is one-time single-threaded init, but to keep the
    # no-sync-lock-on-the-event-loop invariant ABSOLUTE (no boot exception), run
    # it in a worker thread — the index reload + in-lock publish (publish=True,
    # round 6 F2) + reconcile all move off the loop in one hop. publish=True lets
    # the in-lock body republish the SAME object set_active_installed_index
    # already tracks above (idempotent; authoritative publish is now in-lock).
    from specialist_materialize import current_specialist_roles_dir
    roles_overlay = await asyncio.to_thread(
        current_specialist_roles_dir,
        installed_index=installed_specialist_index,
        specialists_dir=Path(os.path.join(CONFIG_DIR, "specialists")),
        agents_specialists_dir=Path(os.path.join(CONFIG_DIR, "agents", "specialists")),
        publish=True,
    )

    specialist_registry = SpecialistRegistry(
        os.path.join(CONFIG_DIR, "agents", "specialists"),
        job_registry=job_registry,
    )
    specialist_registry.load(roles_dir=str(roles_overlay))

    from engagement_registry import EngagementRegistry
    # Containment Stage 2 (Task 10): the persistent, monotonic, never-reused
    # per-engagement uid allocator. Constructed here and injected into the
    # registry so create() can allocate a uid under the registry lock; then
    # RECONSTRUCTED from every uid source below (after load(), which populates
    # the records the reconstruct scan reads).
    uid_allocator = UidAllocator(os.path.join(DATA_DIR, "engagement-uids.json"))
    # #283: the agent-spawn occupancy limiter, constructed BEFORE the
    # registry so load() can restore one token per live marked record
    # (design r3, Sol: registry load used to precede limiter construction —
    # restoration would silently no-op). Shared with tools via init_tools.
    from specialist_limits import AgentSpawnLimiter
    from tools import _AGENT_SPAWN_CAP
    agent_spawn_limiter = AgentSpawnLimiter(max_spawns=_AGENT_SPAWN_CAP)
    engagement_registry = EngagementRegistry(
        tombstone_path=os.path.join(DATA_DIR, "engagements.json"),
        bus=bus,
        uid_allocator=uid_allocator,
        agent_spawn_limiter=agent_spawn_limiter,
    )
    await engagement_registry.load()

    # Reconstruct the uid high-water from the max of every source that could
    # have handed out a uid the allocator must never reissue (design §2):
    #   - the persisted counter file (read inside reconstruct),
    #   - the ``allocated_uid`` of EVERY record incl. terminal/retained (NOT
    #     just live ones — a pruned-but-lingering process still holds its uid),
    #   - the owner uid of every on-disk /data/engagements/*,
    #     /data/engagement-ctl/* AND /data/plugin-outbox-eng/* directory (a
    #     workspace/control/outbox dir chowned to a uid the counter never
    #     recorded — S1 r7 completed the outbox class, the last per-engagement
    #     /data artifact that was unscanned). The EXISTENCE of any such
    #     per-engagement artifact root is also a prior-existence signal, so a
    #     both-durable-copies-lost boot with a leftover artifact fails closed
    #     rather than resetting to base (see UidAllocator.reconstruct).
    # A UidStateError (missing/malformed/inconsistent counter) is FAIL-CLOSED:
    # log CRITICAL and leave the allocator un-reconstructed, so allocate()
    # raises for every create()/backfill this boot — new engagements and
    # legacy-uid migrations are refused rather than risk reissuing a live uid.
    # Boot itself continues (matching the surrounding best-effort boot-error
    # convention): existing already-uid'd engagements still replay.
    try:
        _known_uids, _dir_owner_uids = _gather_reconstruct_evidence(
            engagement_registry, data_dir=DATA_DIR)
        uid_allocator.reconstruct(
            known_uids=_known_uids, dir_owner_uids=_dir_owner_uids)
    except UidStateError:
        logger.critical(
            "Engagement uid allocator could NOT reconstruct its high-water "
            "mark — the counter file is missing/malformed/inconsistent. New "
            "engagements and legacy-uid migrations will be REFUSED this boot "
            "(fail-closed) rather than risk reissuing a live uid. Repair "
            "%s to restore allocation.",
            os.path.join(DATA_DIR, "engagement-uids.json"),
            exc_info=True,
        )

    from executor_registry import ExecutorRegistry
    executor_registry = ExecutorRegistry(
        os.path.join(CONFIG_DIR, "agents", "executors"),
    )
    executor_registry.load()

    # Scheduler + trigger registry constructed here so the get_schedule
    # tool can see the registry via init_tools. The per-role
    # register_agent loop stays below (needs role_configs).
    app = web.Application(middlewares=[cid_middleware])
    scheduler = AsyncIOScheduler(
        timezone=resolve_tz(),
        job_defaults={
            "misfire_grace_time": 600,   # 10 min — covers short Casa restarts
            "coalesce": True,            # collapse missed fires to one
            "max_instances": 1,          # no overlap of same job
        },
    )
    # #396: when an agent-owned one_shot reminder fires, its triggers.yaml
    # entry must go with it — otherwise a reboot would resurrect an
    # already-delivered reminder. Injected rather than done inside the registry,
    # which must not learn to write YAML; the registry only calls this for
    # entries carrying ``managed_by: agent``, so an operator's own one-shot is
    # never deleted here. CONFIG_DIR is used directly (rather than the
    # ``agents_dir`` local defined below) so this closure has no late-binding
    # dependency on statements that run after it.
    async def _remove_fired_reminder(role: str, name: str) -> None:
        import reminders
        # Off the loop: remove_entry takes trigger_write_lock.PASS_LOCK, which a
        # config_sync pass may hold, and this runs inside the scheduler's _fire
        # coroutine — a blocking acquire here would stall the loop (#458).
        outcome = await asyncio.to_thread(
            reminders.remove_entry,
            reminders.triggers_path(os.path.join(CONFIG_DIR, "agents"), role),
            name,
        )
        if outcome != "removed":
            # Not fatal: the sweep redelivers, which is the at-least-once
            # choice. Logged because it means the entry is still on disk.
            logger.warning(
                "one-shot cleanup for %s:%s reported %s", role, name, outcome,
            )
        # #513: if that rewrite touched a ${...}-bearing file, the operator is
        # owed a word about it. Never fatal — a cleanup must not fail because a
        # courtesy notice could not be sent.
        try:
            await notify_placeholder_rewrites(channel_manager)
        except Exception:  # noqa: BLE001
            logger.warning("placeholder-rewrite notice failed", exc_info=True)

    trigger_registry = TriggerRegistry(
        scheduler=scheduler, app=app, bus=bus,
        on_one_shot_fired=_remove_fired_reminder,
    )

    # Initialise the authorization-callback spool BEFORE the first
    # callback reconcile (which reads get_spool() to publish ready/index
    # markers) — the process-wide singleton, like the trigger registry above.
    # Root honours CASA_CALLBACK_SPOOL_ROOT, default /data/callbacks.
    import callback_spool
    callback_spool.init_spool()

    # Initialise the plugin-event spool BEFORE the first event reconcile —
    # the process-wide singleton, mirroring the callback spool immediately
    # above. Root honours CASA_EVENT_SPOOL_ROOT, default /data/events.
    import event_spool
    event_spool.init_spool()

    # 8. Load agent configs by role
    from agent import Agent

    agents_dir = os.path.join(CONFIG_DIR, "agents")
    policy_lib = load_policies(
        os.path.join(CONFIG_DIR, "policies", "disclosure.yaml"),
    )
    # Round 6, F1 (loop-safety consistency): load_all_agents reconciles every
    # resident's binding via personality_binding.reconcile_resident_binding, which
    # now acquires MATERIALIZE_LOCK (a threading.Lock) around its stage/commit/
    # discard. Run the boot scan in a worker thread so that acquisition never
    # happens on the event loop — matching the F3 treatment of
    # current_specialist_roles_dir above and the reload paths (which already reach
    # load_agent_from_dir via asyncio.to_thread). Pure sync function; returns the
    # same role_configs dict.
    role_configs = await asyncio.to_thread(
        load_all_agents, agents_dir, policies=policy_lib,
    )

    specialist_configs = specialist_registry.all_configs()
    from agent_registry import AgentRegistry
    agent_registry = AgentRegistry.build(
        residents=role_configs, specialists=specialist_configs,
    )
    # Task C.2: build the CasaRuntime container.
    from runtime import CasaRuntime
    # Task 14: constructed exactly once at boot, preserved verbatim across
    # every reload (reload.py mutates runtime.role_configs/agents in place
    # and never reconstructs CasaRuntime).
    from explanation_store import ExplanationStore
    explanation_store = ExplanationStore(Path(DATA_DIR) / "explanations")
    runtime = CasaRuntime(
        agents={},                            # populated below at line ~984
        role_configs=role_configs,
        specialist_registry=specialist_registry,
        executor_registry=executor_registry,
        engagement_registry=engagement_registry,
        agent_registry=agent_registry,
        trigger_registry=trigger_registry,
        mcp_registry=mcp_registry,
        session_registry=session_registry,
        channel_manager=channel_manager,
        bus=bus,
        engagement_driver=None,               # set after step 10 InCasaDriver build
        claude_code_driver=None,              # set after step 10b ClaudeCodeDriver build
        policy_lib=policy_lib,
        config_dir=CONFIG_DIR,
        agents_dir=agents_dir,
        home_root="/config/agent-home",
        defaults_root="/opt/casa",
        semantic_memory=semantic_memory,
        job_registry=job_registry,
        explanation_store=explanation_store,
    )
    # Personality Phase A, Task 8 / GH #356: derive the four read-only
    # persona/binding maps from the loaded resident configs. The SAME method
    # reload handlers call after every role_configs mutation — boot and
    # reload share one derivation, so a hot-added resident's maps can never
    # drift from what a restart would build.
    runtime.refresh_personality_maps()
    # Task 6 (spec §4.6): specialist concurrency cap + per-role cost
    # telemetry. `specialist_max_concurrency` bounds delegations in flight
    # fleet-wide; the per-scope cap (exactly 1) is hard-coded inside
    # SpecialistLimiter, not an option. `specialist_cost_alert_threshold`
    # is the cumulative per-role USD figure past which every further
    # delegation for that role also logs a WARNING. Both env vars are a
    # placeholder read pending Task 7's real HA-options wiring.
    from specialist_limits import SpecialistLimiter, SpecialistTelemetry
    # Clamp to the add-on schema's [1, 20] rail (defence in depth — see
    # _env_int_or). The per-scope cap (exactly 1) is not configurable.
    specialist_max_concurrency = _env_int_or(
        "SPECIALIST_MAX_CONCURRENCY", 2, min_value=1, max_value=20)
    specialist_cost_alert_threshold = _env_float_or(
        "SPECIALIST_COST_ALERT_THRESHOLD", 5.0, min_value=0.0)
    specialist_limiter = SpecialistLimiter(max_global=specialist_max_concurrency)
    specialist_telemetry = SpecialistTelemetry(
        cost_alert_threshold=specialist_cost_alert_threshold)

    init_tools(
        channel_manager, bus, specialist_registry, mcp_registry,
        agent_role_map=_build_role_registry(
            residents=role_configs, specialists=specialist_configs,
        ),
        agent_registry=agent_registry,
        trigger_registry=trigger_registry,
        engagement_registry=engagement_registry,
        executor_registry=executor_registry,
        runtime=runtime,
        specialist_limiter=specialist_limiter,
        specialist_telemetry=specialist_telemetry,
        agent_spawn_limiter=agent_spawn_limiter,  # #283 — same instance load() restored into
        voice_job_route_cap=voice_delivery_config.route_cap,
    )
    mcp_registry.register_sdk_factory(
        "casa-framework",
        lambda _role, grants: create_casa_tools(grants),
    )
    logger.info("Registered casa-framework MCP tools")

    # Plugin media outbox (v0.73.0 §3.4): init FDs + boot-reap + register the
    # hourly sweep — all BEFORE channels/HTTP go live (steps 12–13) so send_media
    # is ready the moment a turn can fire. One call, unit-tested with a fake
    # scheduler (plugin_outbox.wire); never blocks boot.
    import plugin_outbox
    await plugin_outbox.wire(
        scheduler, os.environ.get("CASA_PLUGIN_OUTBOX_DIR", "/data/plugin-outbox"))

    # Plan 4b §5.1 — ensure every loaded in_casa resident or specialist
    # agent has an agent-home with default plugins seeded from
    # plugins.yaml. Idempotent — runs every boot. Executors deliberately
    # excluded (different cwd; see agent_home.provision_all_homes
    # docstring).
    from agent_home import provision_all_homes
    provision_all_homes(
        role_configs=role_configs,
        specialist_configs=specialist_configs,
        home_root=Path("/config/agent-home"),
        defaults_root=Path("/opt/casa"),
    )

    if "assistant" not in role_configs:
        raise RuntimeError(
            f"No agent with role 'assistant' found in {agents_dir}. "
            "Casa cannot start without a primary assistant. Check that "
            "agents/assistant/ exists and runtime.yaml declares "
            "`role: assistant`."
        )

    # Unified plugin architecture (§3.9): load the process-local resolver
    # snapshot from disk ONCE before constructing agents (init-plugin-store
    # already imported bundled artifacts + seeded/migrated the registry). Each
    # Agent's _get_plugin_resolution then reads this snapshot.
    import plugin_registry
    await asyncio.to_thread(plugin_registry.reload_snapshot)

    agents: dict[str, Agent] = {}
    loop_tasks: list[asyncio.Task] = []

    for role, cfg in role_configs.items():
        agent = Agent(
            config=cfg,
            semantic_memory=semantic_memory,
            session_registry=session_registry,
            mcp_registry=mcp_registry,
            channel_manager=channel_manager,
            agent_registry=agent_registry,
        )
        bus.register(role, agent.handle_message)
        agents[role] = agent
        logger.info(
            "Agent '%s' registered (name=%s, model=%s, memory=%s)",
            role,
            cfg.character.name,
            cfg.model,
            cfg.memory.read_strategy,
        )

    ha_facade = await _start_tina_ha_facade(
        mcp_registry,
        role_configs,
        agents,
        ha_mcp_url=ha_mcp_url,
        supervisor_token=supervisor_token,
    )
    runtime.agents = agents  # share the dict reference; reload handlers mutate this directly.

    assistant_role = "assistant"

    # 9. Webhook secret (auto-generated if auth enabled, see setup-configs.sh)
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    if not webhook_secret:
        secret_path = os.path.join(DATA_DIR, "webhook_secret")
        if os.path.exists(secret_path):
            with open(secret_path, "r", encoding="utf-8") as fh:
                webhook_secret = fh.read().strip()
    # #333 (Terra r1): if op:// resolution failed above, the env (and the file,
    # for an op://-valued option) still holds the literal reference — a
    # predictable string that must never become the HMAC verification key.
    # Blank it: every authenticated request is rejected loudly instead.
    from webhook_auth import usable_webhook_secret
    _usable = usable_webhook_secret(webhook_secret)
    # #609: the report must be able to say that an `hmac_body` webhook rides a
    # global secret that is BLANK — in which case every request to it 401s
    # permanently. The handlers close over the `main()` local below, so this is
    # the only place the fact is known; WEBHOOK_SECRET is restart-only, so a
    # boot-frozen flag is exactly as fresh as the closure.
    runtime.webhook_global_secret_usable = bool(_usable)
    if webhook_secret and not _usable:
        logger.error(
            "webhook secret is an unresolved op:// reference — refusing to use "
            "the literal as an HMAC key; webhook/voice authentication will "
            "reject all requests until resolution succeeds (check the "
            "1Password service-account token) or the secret is inlined")
    webhook_secret = _usable
    if webhook_secret:
        logger.info("Webhook secret loaded (%d chars)", len(webhook_secret))

    # 9b. Rate limiters (spec 5.2 §8). capacity=0 disables for a channel.
    _telegram_rate_cap = _env_int_or("TELEGRAM_RATE_PER_MIN", 30, min_value=0)
    _voice_rate_cap = _env_int_or("VOICE_RATE_PER_MIN", 20, min_value=0)
    _webhook_rate_cap = _env_int_or("WEBHOOK_RATE_PER_MIN", 60, min_value=0)
    telegram_rate_limiter = RateLimiter(capacity=_telegram_rate_cap, window_s=60.0)
    voice_rate_limiter = RateLimiter(capacity=_voice_rate_cap, window_s=60.0)
    webhook_rate_limiter = RateLimiter(capacity=_webhook_rate_cap, window_s=60.0)
    logger.info(
        "Rate limits: telegram=%s, voice=%s, webhook=%s",
        f"{_telegram_rate_cap}/min" if telegram_rate_limiter.enabled else "off",
        f"{_voice_rate_cap}/min" if voice_rate_limiter.enabled else "off",
        f"{_webhook_rate_cap}/min" if webhook_rate_limiter.enabled else "off",
    )

    # 10. Telegram channel
    public_url = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    if public_url in ("null", "None"):
        public_url = ""
    if public_url:
        logger.info("Public URL: %s", public_url)
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_channel = None
    if telegram_token:
        from channels.telegram import TelegramChannel

        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        telegram_engagement_supergroup_id = _telegram_supergroup_id_from_env()

        # Derive transport: webhook only possible when public_url is set
        telegram_transport = os.environ.get("TELEGRAM_TRANSPORT", "polling")
        if telegram_transport == "webhook" and not public_url:
            logger.warning(
                "telegram_transport is 'webhook' but public_url is not set; "
                "falling back to polling"
            )
            telegram_transport = "polling"

        webhook_url = public_url if telegram_transport == "webhook" else ""

        telegram_channel = TelegramChannel(
            bot_token=telegram_token,
            chat_id=telegram_chat_id,
            default_agent=assistant_role,
            bus=bus,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            rate_limiter=telegram_rate_limiter,
            engagement_supergroup_id=telegram_engagement_supergroup_id or None,
        )
        channel_manager.register(telegram_channel)
        # NOTE: setup_engagement_features() needs the bot, which is only
        # built once channel_manager.start_all() runs _rebuild(). We defer
        # the call until after start_all() (see step 12 below). v0.18.2
        # fix — was previously called here and silently failed with
        # "'NoneType' object has no attribute 'get_me'", leaving
        # engagement_permission_ok=False forever.
        logger.info(
            "Telegram channel registered (transport=%s, chat_id=%s)",
            telegram_transport,
            telegram_chat_id,
        )

    # Register "telegram" as a bus target for outbound routing
    async def _telegram_outbound(msg: BusMessage) -> None:
        ch = channel_manager.get("telegram")
        if ch is not None:
            await ch.send(str(msg.content), msg.context)

    bus.register("telegram", _telegram_outbound)

    # Engagement infrastructure: InCasaDriver + Observer
    from drivers.in_casa_driver import InCasaDriver

    # Phase 3b: in_casa engagements stream via TopicStreamHandle (per-turn
    # edit-in-place, 1s throttle, mirror Ellen's create_on_token pattern
    # in channels/telegram.py:739-859). Bug 1 fix.
    def _topic_stream_factory(topic_id: int):
        assert telegram_channel is not None, (
            "InCasaDriver requires a configured telegram channel"
        )
        return telegram_channel.create_topic_stream(topic_id)

    # Task 6 (spec §4.6): observe interactive specialist ResultMessages so
    # their cost/usage reaches SpecialistTelemetry too (ephemeral sync/async
    # delegations are captured in tools._run_delegated_agent). Only
    # kind="specialist" engagements feed specialist telemetry; executor
    # engagements are out of scope for this counter.
    def _specialist_result_observer(engagement, result_msg) -> None:
        if getattr(engagement, "kind", "") != "specialist":
            return
        from tokens import extract_usage
        specialist_telemetry.record_cost(
            engagement.role_or_type,
            cost_usd=float(getattr(result_msg, "total_cost_usd", 0.0) or 0.0),
            usage=extract_usage(result_msg),
        )

    engagement_driver = InCasaDriver(
        topic_stream_factory=_topic_stream_factory,
        persist_session_id=engagement_registry.persist_session_id,
        result_observer=_specialist_result_observer,
        # #369: last-instant launch gate — start() re-reads the live record
        # before delivering the initial prompt.
        record_lookup=engagement_registry.get,
        # #690: the in_casa half of INV-ENG-009. The registry's SYNCHRONOUS
        # admission decision, called inside the driver's per-turn lock
        # immediately before the client hand-off. Same seam claude_code has
        # used since #588; injected so the driver stays importable without a
        # registry.
        begin_turn_delivery=engagement_registry.begin_turn_delivery,
    )

    # claude_code driver: send_to_topic doubles as the live TopicStreamRelay's
    # send_message primitive (W1), so it must RETURN the posted message_id (the
    # relay edits the rolling message by id). Notice/warning callers ignore it.
    async def _send_to_topic(
        thread_id: int, text: str, reply_to_message_id: int | None = None,
    ) -> int | None:
        if telegram_channel is not None:
            # R2a (v0.89.0): route narration/notice topic sends through the RICH
            # primitive so markdown renders as MessageEntity spans (plain text is
            # sent verbatim — render() returns entities=None and falls back).
            if reply_to_message_id is not None:
                # v0.79.0 (§3): reply-quote the operator's message (Sol-verified
                # PTB 22.7 spelling).
                from telegram import ReplyParameters
                return await telegram_channel.send_to_topic_rich(
                    thread_id, text,
                    reply_parameters=ReplyParameters(
                        message_id=reply_to_message_id,
                        allow_sending_without_reply=True,
                    ),
                )
            return await telegram_channel.send_to_topic_rich(thread_id, text)
        return None

    async def _send_to_topic_paged(
        thread_id: int, text: str,
    ) -> int | None:
        # v0.109.0 (G5): paged rich send for ONE-SHOT terminal posts (the
        # completion notice) — a summary over the 4096/100-entity caps ships as
        # several rendered pages instead of raw markdown. Returns the LAST
        # page's message_id (bottom-most — correct high-water anchor).
        if telegram_channel is not None:
            return await telegram_channel.send_response_to_topic(
                thread_id, text)
        return None

    async def _edit_topic_message(
        thread_id: int, message_id: int, text: str, *, clear_keyboard: bool = False,
    ) -> bool:
        if telegram_channel is not None:
            # R2a (v0.89.0): route narration edits through the RICH primitive so
            # EVERY edit re-renders markdown (plain text edits verbatim).
            return await telegram_channel.edit_topic_message_rich(
                thread_id, message_id, text, clear_keyboard=clear_keyboard)
        return False

    async def _delete_topic_message(thread_id: int, message_id: int) -> bool:
        if telegram_channel is not None:
            return await telegram_channel.delete_topic_message(
                thread_id, message_id)
        return False

    async def _send_topic_message_markup(
        thread_id: int, text: str, markup, reply_to: int | None = None,
    ) -> int | None:
        # A9 (v0.83.0): markup-capable discrete send (OutputSequencer.post_discrete).
        if telegram_channel is not None:
            return await telegram_channel.send_topic_message_markup(
                thread_id, text, markup, reply_to=reply_to)
        return None

    async def _edit_topic_message_markup(
        thread_id: int, message_id: int, text, markup,
    ) -> bool:
        # A9 (v0.83.0): markup-capable discrete edit (OutputSequencer.edit_discrete).
        if telegram_channel is not None:
            return await telegram_channel.edit_topic_message_markup(
                thread_id, message_id, text, markup)
        return False

    async def _pin_topic_message(thread_id: int, message_id: int) -> bool:
        # v0.79.0 (§5): best-effort pin of the live summary message.
        if telegram_channel is not None:
            return await telegram_channel.pin_topic_message(thread_id, message_id)
        return False

    # Expose on the agent module so tools.emit_completion / cancel_engagement
    # can find it without circular imports.
    import agent as agent_mod
    agent_mod.active_engagement_driver = engagement_driver
    agent_mod.active_semantic_memory = semantic_memory   # resident long-term (Hindsight seam)
    agent_mod.active_session_registry = session_registry  # #411 — wipe orchestrator input
    agent_mod.active_executor_registry = executor_registry
    runtime.engagement_driver = engagement_driver

    # Plan 4a: claude_code driver. Shares send_to_topic with in_casa.
    from drivers.claude_code_driver import ClaudeCodeDriver

    # Plan 4b/3.6: workspaces reach the casa-framework MCP via svc-casa-mcp
    # on port 8100.
    _casa_framework_mcp_url = os.environ.get(
        "CASA_FRAMEWORK_MCP_URL",
        "http://127.0.0.1:8100/mcp/casa-framework",
    )

    claude_code_driver = ClaudeCodeDriver(
        engagements_root="/data/engagements",
        send_to_topic=_send_to_topic,
        casa_framework_mcp_url=_casa_framework_mcp_url,
        # W1: relay edit/delete primitives + registry (advance_interaction_state
        # seam for Task 7's inbound one-turn queue).
        edit_topic_message=_edit_topic_message,
        delete_topic_message=_delete_topic_message,
        # A9 (v0.83.0): markup-capable discrete send/edit for post_discrete /
        # edit_discrete (keyboard-bearing writes through the single writer).
        send_topic_message_markup=_send_topic_message_markup,
        edit_topic_message_markup=_edit_topic_message_markup,
        # v0.109.0 (G5): paged rich sender for terminal completion posts.
        # Explicitly None without a Telegram channel so the sequencer keeps
        # its ordinary _post_notice_locked path (Sol r2: a non-None sender is
        # authoritative — never inject a wrapper that can only return None).
        send_to_topic_paged=(
            _send_to_topic_paged if telegram_channel is not None else None),
        # v0.79.0 (§5): best-effort pin primitive for the live summary.
        pin_topic_message=_pin_topic_message,
        registry=engagement_registry,
        # O-5 (v0.37.9): capture-and-persist SDK session_id so a Casa
        # restart mid-engagement preserves conversation continuity.
        # The driver writes <workspace>/.session_id from its log-tail
        # capture; this hook keeps EngagementRecord.sdk_session_id in
        # lockstep with the on-disk file the run script reads on resume.
        persist_session_id=engagement_registry.persist_session_id,
        # #369: rebuild_fresh_context re-renders CLAUDE.md after a clearance
        # downgrade — resolve the defn the same way boot replay does.
        executor_defn_lookup=executor_registry.definition_any,
    )

    # Wire bus sink so subprocess_respawn events reach the observer.
    async def _publish_driver_bus_event(event: dict) -> None:
        await bus.notify(BusMessage(
            type=MessageType.NOTIFICATION,
            source="claude_code_driver",
            target="observer",
            content=event,
            context={"engagement_id": event.get("engagement_id", "-")},
        ))
    claude_code_driver._publish_bus_event = _publish_driver_bus_event
    agent_mod.active_claude_code_driver = claude_code_driver
    # #599: the registry schedules the uid-quiesce ladder the instant a terminal
    # transition wins — including the direct ``mark_*`` mutators, which have no
    # finalize funnel behind them. Wired HERE because the owner is the driver
    # and the driver only exists at this point in boot.
    engagement_registry.set_quiesce_owner(claude_code_driver.quiesce)
    runtime.claude_code_driver = claude_code_driver
    # Stash runtime on agent module so reload handlers and tools find it.
    agent_mod.active_runtime = runtime

    # Plan 4a: boot replay for claude_code engagements.
    # v0.83.0 (§A3(b), Sol r9-2/r10-3): the open-question reconcilers are
    # scheduled here (pre-service snapshot) but their EXECUTION is gated on the
    # Telegram channel's readiness event — replay runs long before the channel
    # starts (start_all() below), and an ungated attach-time reconcile would fire
    # its confirmed settle edits against a None bot and fail closed. The channel
    # sets this at its first successful _rebuild; a None channel yields no event
    # (reconciles then run ungated, matching the no-Telegram deploy).
    _telegram_ready = (
        telegram_channel.ready_event if telegram_channel is not None else None)
    try:
        await replay_undergoing_engagements(
            registry=engagement_registry,
            driver=claude_code_driver,
            executor_registry=executor_registry,
            telegram_ready=_telegram_ready,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Plan 4a boot-replay failed — claude_code engagements may be "
            "in an inconsistent state: %s", exc,
        )

    # #573: reconcile durable scheduled-ask records. Gated on the same Telegram
    # readiness event as the replay above — every disposition either edits a
    # keyboard or restores one, and both need a live bot. A restored record's
    # keyboard stays honest across the restart; an expired, orphaned or
    # operator-changed one is settled and its scheduled session told.
    if _telegram_ready is not None:
        async def _reconcile_scheduled_asks() -> None:
            try:
                await _telegram_ready.wait()
                await scheduled_asks.reconcile_at_boot(telegram_channel)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never fatal at boot
                logger.warning("scheduled-ask reconciliation failed: %s", exc)

        _sa_task = asyncio.create_task(_reconcile_scheduled_asks())
        # Strong reference for the task's lifetime (a bare create_task result
        # is garbage-collectable mid-flight).
        _SCHEDULED_ASK_TASKS.add(_sa_task)
        _sa_task.add_done_callback(_SCHEDULED_ASK_TASKS.discard)

    # v0.79.0 (§3): terminal boot-reconciliation — drain terminal engagements
    # whose inbound spool still holds pending receipts/notices.
    try:
        await reconcile_terminal_spools(
            registry=engagement_registry, driver=claude_code_driver,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("terminal spool reconciliation failed: %s", exc)

    from observer import Observer
    observer = Observer(
        bus=bus,
        engagement_registry=engagement_registry,
        model_name=os.environ.get("SECONDARY_AGENT_MODEL", "haiku"),
    )
    await observer.subscribe()
    # L68/L17: stash so _finalize_engagement can prune per-engagement
    # interjection-budget bookkeeping on terminal transition.
    agent_mod.active_observer = observer

    if telegram_channel is not None:
        telegram_channel._engagement_registry = engagement_registry
        telegram_channel._engagement_driver = engagement_driver
        telegram_channel._observer = observer
        telegram_channel._session_registry = session_registry
        telegram_channel._semantic_memory = semantic_memory

        async def _driver_send_user_turn(rec, text, *, tg_message_id=None,
                                         inbound_token=None):
            # §A3 (Sol r10-2): PROPAGATE the enqueue disposition so
            # _deliver_turn_bg can promote (accepted) vs roll back (rejected)
            # the answered reservation.
            if rec.driver == "claude_code":
                return await claude_code_driver.send_user_turn(
                    rec, text, tg_message_id=tg_message_id)
            # in_casa driver has no durable spool / reply-threading (§7
            # follow-up) — drop the id; no reservation disposition. #649:
            # thread the seam-admitted ticket so the driver adopts it.
            await engagement_driver.send_user_turn(
                rec, text, inbound_token=inbound_token)
            return None
        telegram_channel._driver_send_user_turn = _driver_send_user_turn

        # #649: in_casa admission-ticket seam trio. SYNCHRONOUS, taken by the
        # Telegram entry points before their first await; claude_code keeps
        # its own spool + ingress-reservation accounting (admit returns None).
        def _driver_admit_inbound(rec, text):
            if rec.driver == "claude_code":
                return None
            return engagement_driver.admit_inbound(rec.id, text)
        telegram_channel._driver_admit_inbound = _driver_admit_inbound

        def _driver_discharge_inbound(rec, token):
            if rec.driver != "claude_code" and token is not None:
                engagement_driver.discharge_inbound(rec.id, token)
        telegram_channel._driver_discharge_inbound = _driver_discharge_inbound

        def _driver_inbound_held(rec, token):
            if rec.driver != "claude_code" and token is not None:
                return engagement_driver.inbound_token_held(rec.id, token)
            return False
        telegram_channel._driver_inbound_held = _driver_inbound_held

        # #692: SYNCHRONOUS read of the in_casa follow-up-turn observation for
        # one exact admission ticket. ``getattr``-guarded on purpose — the
        # accessor is deliberately NOT on DriverProtocol, so a driver without
        # it reads as "nothing to report" rather than raising AttributeError
        # into a delivery task that would surface a failure for a healthy
        # turn. claude_code has its own failure ownership and returns "".
        telegram_channel._driver_turn_incomplete = (
            lambda rec, token: read_followup_incomplete(
                engagement_driver, rec, token))

        # #369: clearance-downgrade context revocation. ``invalidate`` tears
        # the pre-clamp session down (the durable context_rebuild_pending flag,
        # set by the clamp itself, is what refuses resume if any step fails);
        # ``rebuild`` establishes a fresh session at the clamped floor and
        # returns the preamble prepended to the next delivered turn. The
        # rebuilt session deliberately re-imports NOTHING from the launch —
        # the record's task/brief/context were withheld by the clamp, and
        # memory stays reachable through clearance-gated recall.
        _REBUILD_NOTE = (
            "[Context reset: this engagement's earlier working context is no "
            "longer available. Continue from the messages in this topic; use "
            "recall_memory if you need prior facts.]"
        )

        async def _driver_invalidate_session(rec):
            if rec.driver == "claude_code":
                await claude_code_driver.invalidate_session(rec)
            else:
                await engagement_driver.invalidate_session(rec)

        async def _engagement_context_rebuilder(rec) -> str:
            # Sol diff-gate r1: both branches RE-VERIFY teardown before
            # opening anything fresh (invalidate is idempotent) — the flag is
            # cleared by the caller only after this returns, so a rebuild can
            # never bless a session whose predecessor was not confirmed gone.
            if rec.driver == "claude_code":
                await claude_code_driver.rebuild_fresh_context(rec)
            else:
                await engagement_driver.invalidate_session(rec)
                await engagement_driver.open_fresh(rec)
            return _REBUILD_NOTE
        telegram_channel._driver_invalidate_session = _driver_invalidate_session
        telegram_channel._engagement_context_rebuilder = (
            _engagement_context_rebuilder)

        # v0.83.0 (§A3, Sol r7-1): the answered-RESERVATION seam. reserve is
        # SYNCHRONOUS (set in the handler's same section as the high-water
        # advance); rollback is a CAS. Only claude_code engagements have the
        # spool/anchor machinery — in_casa is a no-op.
        def _driver_reserve_answer(rec):
            if rec.driver == "claude_code":
                return claude_code_driver.reserve_answer(rec.id)
            return None
        telegram_channel._driver_reserve_answer = _driver_reserve_answer

        # G4 D2 (v0.96.0): SYNCHRONOUS inbound-ingress reservation — taken at
        # trusted handler entry (under the topic lock, before the background
        # delivery task exists) and released after the spool enqueue resolves.
        # Terminalization refuses while reservations > 0, closing the
        # accepted-but-not-yet-spooled completion race.
        def _driver_reserve_inbound(rec, *, command=False):
            if rec.driver == "claude_code":
                claude_code_driver.reserve_inbound(rec.id, command=command)
                return True
            return False
        telegram_channel._driver_reserve_inbound = _driver_reserve_inbound

        def _driver_release_inbound(rec, *, command=False):
            if rec.driver == "claude_code":
                claude_code_driver.release_inbound_reservation(
                    rec.id, command=command)
        telegram_channel._driver_release_inbound = _driver_release_inbound

        async def _driver_rollback_answer_reservation(
                rec, token, *, suppress_reanchor=False):
            if rec.driver == "claude_code" and token is not None:
                return await claude_code_driver.rollback_answer_reservation(
                    rec.id, token, suppress_reanchor=suppress_reanchor)
            return False
        telegram_channel._driver_rollback_answer_reservation = (
            _driver_rollback_answer_reservation)

        # v0.79.0 (§3): seal open narration at inbound-handler entry for
        # claude_code engagements (the T1 high-water seam).
        async def _driver_advance_high_water(rec, msg_id):
            if rec.driver == "claude_code":
                await claude_code_driver.advance_topic_high_water_for_inbound(
                    rec.id, msg_id)
        telegram_channel._driver_advance_high_water = _driver_advance_high_water

        # v0.79.0 (§3, F2): route platform-origin topic notices (command
        # replies, resume errors) through the engagement's OUTPUT SEQUENCER so
        # they seal narration + advance the high-water under the single writer.
        # Non-claude_code engagements have no sequencer — post directly.
        async def _driver_post_notice(rec, text):
            if rec.driver == "claude_code":
                await claude_code_driver.post_topic_notice(rec, text)
            else:
                # v0.109.0 (G3): notices carry markdown — render rich.
                await telegram_channel.send_to_topic_rich(rec.topic_id, text)
        telegram_channel._driver_post_notice = _driver_post_notice

        async def _finalize_cancel(rec, reason="user"):
            # F2 (whole-branch r2): PROPAGATE _finalize_engagement's bool so the
            # terminal command path can gate the answered-reservation re-anchor
            # suppression on a successful strict terminal transition.
            from tools import _finalize_engagement
            driver = (claude_code_driver if rec.driver == "claude_code"
                      else engagement_driver)
            return await _finalize_engagement(
                rec, outcome="cancelled", text=f"Cancelled by {reason}.",
                artifacts=[], next_steps=[],
                driver=driver,
            )
        telegram_channel._finalize_cancel = _finalize_cancel

        async def _finalize_complete_user(rec):
            # F2 (whole-branch r2): PROPAGATE the finalize bool (see above).
            from tools import _finalize_engagement
            driver = (claude_code_driver if rec.driver == "claude_code"
                      else engagement_driver)
            return await _finalize_engagement(
                rec, outcome="completed", text="User-marked complete.",
                artifacts=[], next_steps=[],
                driver=driver,
            )
        telegram_channel._finalize_complete_user = _finalize_complete_user

    # 10b. Voice channel
    voice_sse_enabled = os.environ.get(
        "VOICE_SSE_ENABLED", "true",
    ).lower() == "true"
    voice_ws_enabled = os.environ.get(
        "VOICE_WS_ENABLED", "true",
    ).lower() == "true"
    voice_sse_path = os.environ.get("VOICE_SSE_PATH", "/api/converse")
    voice_ws_path = os.environ.get("VOICE_WS_PATH", "/api/converse/ws")
    _default_voice_idle = (
        role_configs["butler"].session.idle_timeout
        if "butler" in role_configs
        else 300
    )
    voice_idle_timeout = int(os.environ.get(
        "VOICE_IDLE_TIMEOUT_SECONDS", str(_default_voice_idle),
    ))

    voice_channel = None
    if voice_sse_enabled or voice_ws_enabled:
        from channels.voice import VoiceChannel
        from channels.voice.channel import VoiceHandoffCoordinator
        from channels.voice.delivery import VoiceDeliveryCoordinator
        from channels.voice.routes import VoiceRouteRegistry

        voice_routes = VoiceRouteRegistry(
            secret_present=bool(webhook_secret),
            agent_configs=role_configs,
            freshness_s=voice_delivery_config.route_freshness_s,
        )
        voice_delivery = VoiceDeliveryCoordinator(job_registry, voice_routes)
        voice_handoff = VoiceHandoffCoordinator(job_registry, voice_routes)
        runtime.voice_route_registry = voice_routes
        runtime.voice_delivery_coordinator = voice_delivery
        runtime.voice_handoff_coordinator = voice_handoff

        voice_channel = VoiceChannel(
            bus=bus,
            default_agent="butler" if "butler" in role_configs else assistant_role,
            webhook_secret=webhook_secret,
            sse_path=voice_sse_path,
            ws_path=voice_ws_path,
            agent_configs=role_configs,
            memory=semantic_memory,
            idle_timeout=voice_idle_timeout,
            sse_enabled=voice_sse_enabled,
            ws_enabled=voice_ws_enabled,
            rate_limiter=voice_rate_limiter,
            route_registry=voice_routes,
            delivery_coordinator=voice_delivery,
            handoff_coordinator=voice_handoff,
        )
        channel_manager.register(voice_channel)
        logger.info(
            "Voice channel registered (sse=%s, ws=%s, idle=%ss)",
            voice_sse_enabled, voice_ws_enabled, voice_idle_timeout,
        )

    # 11. Webhook endpoints

    # N-1 + N-2 (v0.36.0): wildcard /webhook/{name} consults the trigger
    # registry's per-boot allowlist. Unknown names → 404; known names
    # dispatch to the registered role (no longer hardcoded to
    # assistant_role).
    webhook_handler = _make_webhook_handler(
        webhook_rate_limiter=webhook_rate_limiter,
        webhook_secret=webhook_secret,
        trigger_registry=trigger_registry,
        default_role=assistant_role,
        bus=bus,
    )

    invoke_handler = _make_invoke_handler(
        webhook_rate_limiter=webhook_rate_limiter,
        webhook_secret=webhook_secret,
        bus=bus,
        assistant_role=assistant_role,
        role_configs=role_configs,
    )

    # 11. Telegram webhook route (only used when webhook_url is set).
    # L4: constant-time secret-token comparison lives in the extracted
    # factory. The lambda preserves the closure over the local
    # ``telegram_channel`` (assigned earlier in main()).
    telegram_update_handler = _make_telegram_update_handler(
        get_telegram_channel=lambda: telegram_channel,
        webhook_secret=webhook_secret,
    )

    # 12. Status dashboard
    terminal_enabled = os.environ.get("ENABLE_TERMINAL", "false").lower() == "true"
    version = os.environ.get("CASA_VERSION", "dev")

    async def dashboard(request: web.Request) -> web.Response:
        ingress_path = request.headers.get("X-Ingress-Path", "")

        # Agent rows
        agent_rows = ""
        for role, agent in agents.items():
            model = agent.config.model.replace("claude-", "")
            parts = model.split("-")
            if len(parts) >= 3:
                model = f"{parts[0].capitalize()} {parts[1]}.{parts[2]}"
            display = (
                agent.config.character.name
                if agent.config.character.name
                and not agent.config.character.name.startswith("${")
                else role.capitalize()
            )
            agent_rows += _row(display, model)

        # Channel rows
        channel_rows = ""
        if telegram_channel is not None:
            tg_mode = "webhook" if telegram_channel._webhook_url else "polling"
            channel_rows += _row("Telegram", tg_mode, "on")
        else:
            channel_rows += _row("Telegram", "not configured", "off")

        if voice_channel is not None:
            transports = []
            if voice_sse_enabled:
                transports.append("SSE")
            if voice_ws_enabled:
                transports.append("WS")
            channel_rows += _row(
                "Voice", ", ".join(transports) or "disabled",
                "on" if transports else "off",
            )
        else:
            channel_rows += _row("Voice", "not configured", "off")

        # System rows
        system_rows = ""
        if public_url:
            system_rows += _row("Public URL", public_url, "on")
        else:
            system_rows += _row("Public URL", "not set", "off")
        try:
            _sem_backend = resolve_semantic_memory_choice(dict(os.environ)).backend
        except Exception:  # noqa: BLE001 — dashboard must never crash on a memory misconfig
            _sem_backend = "noop"
        mem_type = {"hindsight": "Hindsight", "noop": "none"}.get(_sem_backend, _sem_backend)
        system_rows += _row("Memory", mem_type, "on" if _sem_backend != "noop" else "off")
        system_rows += _row("Webhook auth", "enabled" if webhook_secret else "disabled",
                            "on" if webhook_secret else "off")
        total_triggers = sum(len(cfg.triggers) for cfg in role_configs.values())
        system_rows += _row(
            "Triggers", f"{total_triggers} registered",
            "on" if total_triggers else "off",
        )
        system_rows += _row("Terminal", "enabled" if terminal_enabled else "disabled",
                            "on" if terminal_enabled else "off")

        html = _STATUS_PAGE.format(
            agent_rows=agent_rows,
            channel_rows=channel_rows,
            system_rows=system_rows,
            terminal_class="primary" if terminal_enabled else "disabled",
            ingress_path=ingress_path,
            version=version,
        )
        return web.Response(text=html, content_type="text/html")

    # 13. aiohttp app — app was constructed earlier (above init_tools) so
    # trigger_registry could be wired in. Route registrations that reference
    # closures (dashboard, handlers) happen here once those closures exist.
    if voice_channel is not None:
        voice_channel.register_routes(app)
    app.router.add_get("/", dashboard)
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/webhook/{name}", webhook_handler)
    # Authorization callbacks. ORDER IS LOAD-BEARING: the static
    # done page must be registered BEFORE the wildcard, which would otherwise
    # match "/callback/done" as a callback name and answer it with a 303 back
    # to itself. Both handlers are turn-free — no ingress identity, no
    # clearance, no provenance — because a browser redirect is not an
    # authenticated principal; every outcome is the same neutral 303.
    # INV-CB-006: an over-long request line raises LineTooLong BELOW the
    # handler, and aiohttp logs the offending bytes (query + code) at ERROR on
    # the aiohttp.server logger. This filter strips the callback query from
    # that record's message and traceback before any handler formats it.
    callback_http.install_callback_log_redaction()
    app.router.add_get("/callback/done", callback_http.make_done_handler())
    app.router.add_get("/callback/{name}", callback_http.make_callback_handler(
        trigger_registry=trigger_registry,
    ))
    app.router.add_post("/invoke/{agent}", invoke_handler)
    app.router.add_post("/telegram/update", telegram_update_handler)
    # H3 (v0.53.0): per-executor hooks.yaml params for the hook resolve path.
    _executor_cc_policies = _build_executor_cc_hook_policies(executor_registry)
    # #340: stash the SAME instance the internal resolve handler captures on
    # the runtime, so reload_executors can refresh it in place (clear+update) —
    # without this, the map stays frozen at boot: a reloaded-in executor
    # falls back to deny-all defaults and a tightened policy keeps enforcing
    # the old broader callbacks until restart.
    runtime.executor_cc_policies = _executor_cc_policies

    # 13b. Per-agent trigger registration. Registry + scheduler were
    # constructed earlier (needed by init_tools for get_schedule).
    # Register before runner.setup() so webhook routes land in *app*
    # while the router is still mutable.
    for role, cfg in role_configs.items():
        if cfg.triggers:
            trigger_registry.register_agent(
                role=role, triggers=cfg.triggers, channels=cfg.channels,
            )
            logger.info(
                "Registered %d trigger(s) for agent '%s'",
                len(cfg.triggers), role,
            )

    # 13b'. #609: mint the per-trigger webhook secrets for the triggers just
    # registered. AFTER registration, so `register_agent`'s cross-role webhook
    # name collision test has already refused a name this role may not have —
    # minting first would write a casa token into another role's slot for a
    # registration that is then rejected.
    import trigger_reconcile as _trigger_reconcile
    await _boot_mint_resident_trigger_secrets(
        role_configs=role_configs, secrets_dir=_trigger_reconcile.SECRETS_DIR,
    )

    # 13c. Release B: plugin-declared webhook triggers — reconcile the
    # overlay after resident triggers register, before the server starts.
    await _boot_reconcile_plugin_triggers(
        trigger_registry=trigger_registry, role_configs=role_configs,
    )
    # 13c'. Paired plugin-declared authorization-callback reconcile
    # + spool boot maintenance. Runs BEFORE _pse.start_worker() below so a
    # routed plugin's ready marker is published before any settlement dispatch
    # checks routes_live.
    await _boot_reconcile_plugin_callbacks(
        trigger_registry=trigger_registry, role_configs=role_configs,
    )
    # 13c''. Paired plugin-declared event-subscription reconcile — derives +
    # publishes the routing map the delivery worker's fold/sweep/pre-send
    # gate read every pass. Runs BEFORE event_episodes.recovery()/
    # start_worker() below (see _boot_reconcile_plugin_events's own
    # docstring) so the worker's boot recovery reconstructs the ledger
    # against LIVE routing, never the ROUTING_UNAVAILABLE sentinel default.
    await _boot_reconcile_plugin_events(role_configs=role_configs)

    # 13d. v0.112.0 (elevenlabs#2): durable post-consent setup episodes —
    # wire the seams (all late-binding: the channel is resolved at call
    # time) and start the supervised worker; pending episodes from a prior
    # boot re-dispatch immediately (crash-safe at-least-once).
    import plugin_setup_episodes as _pse

    def _setup_registry_entry(plugin: str) -> dict | None:
        import plugin_grants as _pg
        import plugin_registry as _pr
        import plugin_store as _pstore
        res = _pr.resolve_all()
        rp = next((p for p in res.plugins if p.name == plugin), None)
        if rp is None:
            return None
        snap = _pr.snapshot_registry()
        entry = next(
            (e for e in snap.entries
             if isinstance(e, dict) and e.get("name") == plugin), None)
        try:
            setup = _pstore.manifest_setup_tool(rp.manifest)
        except Exception:  # noqa: BLE001 — malformed manifest ⇒ no hook
            setup = None
        return {
            "artifact_id": rp.artifact_id,
            "targets": list((entry or {}).get("targets") or []),
            "granted_tools": _pg.grants_for_resolved(rp),
            "setup_tool": setup,
        }

    async def _setup_dispatch(role: str, text: str, context: dict) -> bool:
        import trigger_consent as _tc
        ch = channel_manager.get("telegram") if channel_manager else None
        op = _tc.operator_identity(ch) if ch is not None else None
        if op is None:
            return False
        op_chat, op_user = op
        msg = BusMessage(
            type=MessageType.CHANNEL_IN, source="telegram", target=role,
            content=text, channel="telegram",
            context={
                "chat_id": op_chat, "user_id": op_user, "cid": new_cid(),
                **context,  # reserved synthetic/plugin_setup markers —
                            # Casa-composed internal, never external ingress
            },
        )
        return await bus.send_checked(msg) == "accepted"

    async def _setup_notify(text: str) -> None:
        await operator_notify(channel_manager, text)

    def _setup_secrets_ready(plugin: str) -> bool:
        # #423: hold a settled episode until every env var the plugin's
        # .mcp.json references is resolved in the effective environment —
        # the installing engagement wires secrets AFTER the consent round
        # can settle, and a setup MCP server spawned before plugin_env
        # reload runs with literal ${VAR} placeholders. An unresolvable
        # plugin reads not-ready (the dispatch-time registry gate owns
        # that path's messaging/retries).
        #
        # #429: the gate asks for the BLOCKING subset only. Waiting on a var
        # the plugin's own setup tool is supposed to CREATE is a deadlock —
        # setup cannot run until the credential exists, and the credential
        # only exists after setup runs — and nothing re-kicks it, because
        # the retries fire on plugin_env and agent reloads, neither of which
        # can supply a value only setup produces. A declared
        # casa.setupProvides var is therefore not held for;
        # the session builder pins it to "" so the setup tool still never
        # sees a literal ${VAR}.
        import plugin_registry as _pr
        from plugin_grants import setup_secrets_ready as _ready
        return _ready(_pr.resolve_all(), plugin)

    def _setup_execution_ready(role: str, plugin: str,
                               artifact_id: str) -> bool:
        # #423 r2 (Sol 1 / Terra 1): the executing agent's next session
        # build must carry the episode's exact artifact — a binding
        # published while the plugin was env-withheld keeps excluding it
        # until an agent reload, and dispatching into that session consumes
        # the episode against a session without the tool. Late-binding: the
        # runtime registry is read at call time (same pattern as verify).
        import agent as _agent_mod
        import plugin_dispatch as _pd
        runtime = getattr(_agent_mod, "active_runtime", None)
        agents = getattr(runtime, "agents", {}) or {}
        return _pd.execution_ready(agents.get(role), plugin, artifact_id)

    _pse.configure(
        dispatch=_setup_dispatch, notify_operator=_setup_notify,
        resolve_registry_entry=_setup_registry_entry,
        # Union BOTH ack stores (trigger + callback identities are
        # disjoint) so a stranded round with a callback member heals.
        ack_lookup=_setup_ack_lookup_union,
        # Gate setup dispatch on BOTH trigger AND callback markers —
        # a callback dark for a non-consent reason contributes no round member,
        # so the trigger gate alone would settle + dispatch with it unrouted.
        routes_live=_callback_and_trigger_routes_live,
        secrets_ready=_setup_secrets_ready,
        execution_ready=_setup_execution_ready,
    )
    _pse.start_worker()

    # 13d'. Durable authorization-callback delivery. The worker owns no store
    # of its own: the spool's per-flow ATTEMPT LEDGER is the durable state, and
    # every pass re-derives it from the artifacts before dispatching the nudges
    # it schedules. Reuses the same late-binding dispatch/notify/registry seams
    # as the setup worker (the callback context markers differ but the shapes
    # match). start_worker() kicks one immediate pass, which drains whatever
    # the ledger already owes after a crash and establishes the timed wake.
    import callback_episodes as _cbep
    _cbep.configure(
        dispatch=_setup_dispatch, notify_operator=_setup_notify,
        resolve_registry_entry=_setup_registry_entry,
        get_spool=callback_spool.get_spool,
    )
    _cbep.start_worker()

    # 13d''. Durable plugin-event delivery. Structurally the sibling of the
    # callback delivery wiring above — same late-binding dispatch/notify/
    # registry-entry seams, no store of its own beyond the spool's per-flow
    # delivery ledger — but with three ADDITIONAL live-callable closures the
    # worker's pre-send identity gate needs (Task 8 signature): get_routed,
    # get_installed, get_registry_valid.
    import event_acks
    import event_episodes as _evep
    import event_reconcile as _evrec

    _evep.configure(
        dispatch=_setup_dispatch, notify_operator=_setup_notify,
        resolve_registry_entry=_event_registry_entry,
        get_routed=_evrec.get_routed,
        get_installed=_event_installed,
        get_registry_valid=_event_registry_valid,
        get_emitters=_event_emitters,
        get_acks=lambda: event_acks.ACKS,
        get_spool=event_spool.get_spool,
        sleep=asyncio.sleep,
    )
    # Boot recovery pin: MUST run before start_worker() — it reconstructs
    # the per-flow delivery ledger from the artifacts (against the routing
    # _boot_reconcile_plugin_events already published above) before the
    # worker's first pass can dispatch anything.
    await _evep.recovery(boot=True)
    _evep.start_worker()

    runner = web.AppRunner(
        app,
        access_log_class=CasaAccessLogger,
        access_log=logging.getLogger("casa.access"),
    )
    await runner.setup()
    # H1 (defense-in-depth): bind the backend to loopback only. The sole
    # remote consumer is nginx in the SAME container (proxy_pass
    # http://127.0.0.1:8099) and in-container workspace subprocesses reach
    # it via 127.0.0.1; nothing legitimately connects to 8099 over the
    # hassio bridge, so 0.0.0.0 needlessly exposed it to peer containers.
    site = web.TCPSite(runner, "127.0.0.1", 8099)
    await site.start()
    logger.info("HTTP server listening on 127.0.0.1:8099")

    # Plan 4b/3.6: second AppRunner for the Unix-socket internal API
    # consumed by svc-casa-mcp.
    from hooks import HOOK_POLICIES as _HOOK_POLICIES_FOR_INTERNAL
    _internal_hook_policies = _build_cc_hook_policies(_HOOK_POLICIES_FOR_INTERNAL)
    # v0.37.2 (C-1): engagement_permission_relay needs live deps that the
    # parameter-free factory pattern can't supply; inject via the helper.
    _wire_engagement_permission_relay(
        _internal_hook_policies,
        engagement_registry=engagement_registry,
        telegram_channel=telegram_channel,
    )
    # R4 (v0.89.0): buttons-always PreToolUse(Skill) salience backstop.
    _wire_engagement_buttons_reminder(
        _internal_hook_policies,
        engagement_registry=engagement_registry,
    )
    from tools import CASA_TOOLS as _CASA_TOOLS_FOR_INTERNAL
    _internal_tool_dispatch = {
        t.name: t.handler for t in _CASA_TOOLS_FOR_INTERNAL
    }
    # E-12 (v0.37.0): pass the live TelegramChannel built at line ~1162 so
    # start_internal_unix_runner can register the /internal/channel/* family.
    # `telegram_channel` is the local variable; None when no TELEGRAM_TOKEN is
    # set (test/fallback boots), in which case the channel routes are skipped.
    internal_runner = await start_internal_unix_runner(
        socket_path="/run/casa/internal.sock",
        tool_dispatch=_internal_tool_dispatch,
        engagement_registry=engagement_registry,
        hook_policies=_internal_hook_policies,
        executor_hook_policies=_executor_cc_policies,
        runtime=runtime,
        telegram_channel=telegram_channel,
    )
    # Track for shutdown.
    runners: list[web.AppRunner] = [runner, internal_runner]

    # 12. Start all channels
    await channel_manager.start_all()

    # #532: the event worker's boot pass (13d'' above) ran BEFORE the
    # channels started, so any operator notice it owed (an exhausted
    # delivery due at boot, a removal note) failed against a not-yet-ready
    # channel. Re-kick now that sends can land — prompt delivery on the
    # common path; the 5-minute event_spool_recovery job is the backstop
    # for every later unready window (e.g. a supervisor rebuild).
    _evep.kick_all()

    # 12a. E-F (v0.30.0): engagement-feature setup is now wired into
    # TelegramChannel._rebuild() as a final step after `self._app = app`.
    # Pre-fix, this boot-time call could fire before `_app` was populated
    # if the first `set_webhook` blipped — leaving `engagement_permission_ok`
    # permanently False until manual restart. The new location makes it
    # self-healing on every successful rebuild. Removed redundant call here.

    # 13. Agent loop tasks. H10/H11 (v0.49.0): spawn through the bus so
    # the consumer tasks are tracked — reload reuses the same seam for
    # roles added after boot and cancels tracked tasks on eviction.
    # H4 (v0.53.0): _bus_loop_targets adds "observer" so the observer queue
    # (subscribed above) actually gets drained; observer.subscribe() ran
    # earlier so its queue already exists here.
    for name in _bus_loop_targets(agents):
        if name in bus.queues:
            loop_tasks.append(bus.start_agent_loop(name))

    # 13b. Restart-orphan notifications come from the recovered durable job
    # failures. Voice jobs remain READY for their delivery coordinator; the
    # compatibility notification below is only for the legacy Telegram route.
    await _notify_recovered_delegations(
        recovered_jobs, job_registry, bus, assistant_role=assistant_role,
    )

    # 13c. Surface any default-sync overwrites to the operator (direct
    # telegram outbound — see notify_config_sync).
    await notify_config_sync(bus)
    # init-plugin-store's health report is resolver-only (it ran before
    # agents/executor-registry existed). Now that they are constructed,
    # regenerate with RUNTIME verification (authorization, effective secrets,
    # system requirements, active bindings) so a plugin with missing auth/secret
    # is not green until a mutation. Never block boot on it.
    try:
        import tools as _tools_mod
        # #582 batch (Sol design r1): unlike the boot RECONCILE regenerations
        # (steps 13c/13d, which run before the HTTP server, the channels and
        # the agent loops start), this one runs after all three are live, so a
        # plugin mutation can be in flight beside it. The report lock orders
        # the write, not the computation before it, so this takes the same
        # guard every mutation holds — across the notify too, matching the
        # plugin_env reload scope.
        async with _tools_mod._plugin_tools_guard():
            await asyncio.to_thread(_tools_mod._regenerate_plugin_health, [])
            await notify_plugin_health(channel_manager)
    except Exception:  # noqa: BLE001 — the notify is now inside this arm too
        # (it was previously outside it): boot must not die on an operator
        # notification, and the function's own contract is already non-fatal.
        logger.warning("boot plugin-health regen/notify failed", exc_info=True)

    # 14. Kick off timers.
    # AsyncIOScheduler's AsyncIOExecutor schedules coroutine functions on
    # the running loop directly. A sync lambda that calls create_task
    # gets dispatched to a worker thread instead, where no loop is bound,
    # raising RuntimeError on every fire (silent regression from v0.13.0).
    # Pass the coroutine functions directly with kwargs.
    session_sweeper.start()
    freshness_reaper.start()
    # D-4 (v0.69.0): reap stale engagements FIRST, then run the idle pass —
    # a record past the reap TTL is cancelled outright and must not receive
    # a pointless idle reminder in the same daily run.
    async def _engagement_daily_sweep() -> None:
        # reap resolves the per-record driver itself (claude_code executors
        # need the claude_code driver — v0.69.6); no driver arg here.
        from tools import reap_stale_engagements
        try:
            reaped = await reap_stale_engagements()
            if reaped:
                logger.info("engagement sweep: reaped %d stale engagement(s)", reaped)
        except Exception:  # noqa: BLE001 — reap failure must not starve the idle pass
            logger.warning("engagement reap failed", exc_info=True)
        await engagement_registry.sweep_idle_and_suspend(driver=engagement_driver)

    scheduler.add_job(
        _engagement_daily_sweep,
        trigger="cron",
        id="engagement_idle_sweep",
        hour=8, minute=0,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # A:§3.3 — authorization-grant TTL sweep, hourly. GrantStore has no
    # private loop of its own (unlike session_sweeper.py); this job is
    # its only reap seam.
    scheduler.add_job(
        _authz_grant_sweep,
        trigger="interval",
        id="authz_grant_sweep",
        hours=1,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    # Plan 4a.1 §8: workspace sweeper — every 6 hours, removes terminal
    # engagement workspaces past retention. v0.65.0 [AR-8]: the same job
    # then sweeps due terminal-engagement topics off the Telegram sidebar
    # (topic_ledger) — topics and workspaces expire together.
    from drivers.workspace import _sweep_workspaces as _sweep_ws

    async def _sweep_workspaces_and_topics() -> None:
        # Per-side-effect isolation: a workspace-sweep failure must not
        # starve the topics pass (the topics helper itself never raises).
        try:
            await _sweep_ws(engagements_root="/data/engagements")
        except Exception:  # noqa: BLE001
            logger.warning("workspace sweep failed", exc_info=True)
        await _sweep_engagement_topics(channel_manager)

    scheduler.add_job(
        _sweep_workspaces_and_topics,
        trigger="interval",
        id="workspace_sweep",
        hours=6,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    # Authorization-callback spool maintenance.
    #
    # LOCK-STALL AVOIDANCE: the callback HTTP handler runs
    # spool.claim/publish_result INLINE on the event loop (each is O(1) — a
    # handful of syscalls), while sweep()/recovery_pass() hold
    # CallbackSpool._lock for a whole per-plugin SCAN. So the SCHEDULED scans
    # run OFF the loop via asyncio.to_thread; the handler's fast path stays
    # inline. A scan can therefore never stall the loop, and the fast path is
    # never needlessly moved off it. recovery_pass(boot=False) is safe to run
    # in a thread while the loop is handling an inline claim: the in-process
    # in-flight set + the 60 s grace protect a just-minted claim the
    # scan would otherwise reap.
    async def _callback_spool_sweep() -> None:
        spool = callback_spool.get_spool()
        if spool is None:
            return
        try:
            await asyncio.to_thread(spool.sweep, now=time.time())
        except Exception:  # noqa: BLE001
            logger.warning("callback-spool sweep failed", exc_info=True)

    async def _callback_spool_recovery() -> None:
        spool = callback_spool.get_spool()
        if spool is None:
            return
        try:
            await asyncio.to_thread(spool.recovery_pass, now=time.time(),
                                    boot=False)
        except Exception:  # noqa: BLE001
            logger.warning("callback-spool recovery failed", exc_info=True)
        # Reconcile the attempt ledger against whatever the recovery restored
        # and wake the delivery worker. boot=False is load-bearing: this pass
        # runs while the handler may hold a claim mid-publish, so it must keep
        # recovery's in-flight skip (the boot seam, which has no live handlers,
        # is the only caller that reconciles every hash).
        await _cbep.recovery(spool, boot=False)

    scheduler.add_job(
        _callback_spool_sweep,
        trigger="interval",
        id="callback_spool_sweep",
        minutes=10,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        _callback_spool_recovery,
        trigger="interval",
        id="callback_spool_recovery",
        minutes=5,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # Plugin-event spool maintenance. Same lock-stall-avoidance shape as the
    # callback pair above (the spool's own scans run off the loop via
    # asyncio.to_thread); cadence mirrors it too (sweep every 10 min,
    # recovery every 5).
    async def _event_spool_sweep() -> None:
        spool = event_spool.get_spool()
        if spool is None:
            return
        try:
            routed = _evrec.to_spool_shape(_evrec.get_routed())
            installed = _event_installed()
            valid = _event_registry_valid()
            await asyncio.to_thread(
                spool.sweep, routed, installed, valid, time.time())
        except Exception:  # noqa: BLE001
            logger.warning("event-spool sweep failed", exc_info=True)

    async def _event_spool_recovery() -> None:
        # Liveness (Task 10): a transient reconcile-COMPUTE failure fail-
        # closes to the ROUTING_UNAVAILABLE sentinel (event_reconcile
        # decision 26), under which the worker does no destructive OR
        # forward-moving work and waits on kicks alone — the next PLUGIN
        # LIFECYCLE mutation is otherwise the only thing that would retry
        # the compute, which could strand delivery indefinitely on a purely
        # transient failure. Self-heal here: fire the SAME kick the
        # pre-send gate uses on a consent mismatch, which schedules a fresh
        # reconcile off the live runtime (a no-op, never raising, when no
        # runtime is bound yet).
        if _evrec.get_routed() is event_spool.ROUTING_UNAVAILABLE:
            _evrec.kick()
        await _evep.recovery(boot=False)

    scheduler.add_job(
        _event_spool_sweep,
        trigger="interval",
        id="event_spool_sweep",
        minutes=10,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        _event_spool_recovery,
        trigger="interval",
        id="event_spool_recovery",
        minutes=5,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # #396 / INV-TRIG-008: the scheduler has NO persistent job store, so a
    # reminder whose fire time fell while the add-on was down was never
    # recorded and is otherwise lost outright. An agent-owned one-shot still
    # present in triggers.yaml with a past time IS the record that delivery is
    # owed; this sweep is what redeems it.
    async def _reminder_sweep() -> None:
        from datetime import datetime

        import reminders
        try:
            await reminders.sweep_reminders(
                runtime, datetime.now(resolve_tz()))
        except Exception:  # noqa: BLE001
            logger.warning("reminder sweep failed", exc_info=True)

    scheduler.add_job(
        _reminder_sweep,
        trigger="interval",
        id="reminder_sweep",
        minutes=5,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # Boot sweep: deliver anything owed from the downtime we just ended, now
    # rather than up to five minutes from now. Runs before start() so it
    # cannot race the interval job's first pass.
    await _reminder_sweep()

    scheduler.start()

    # 15. Graceful shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    try:
        loop.add_signal_handler(signal.SIGTERM, _signal_handler)
        loop.add_signal_handler(signal.SIGINT, _signal_handler)
    except NotImplementedError:
        logger.warning("Signal handlers not supported on this platform")

    # 16. Wait for stop
    logger.info("Casa core running -- waiting for shutdown signal")
    await stop_event.wait()
    # #671: declare the graceful stop BEFORE anything can tear a delegation
    # down. From here on the registry defers a live row's cancellation terminal
    # to the next boot instead of overwriting it with a creator-cancel shape the
    # boot reconciliation then skips — which is how a clean stop came to be
    # measurably worse than a crash. First statement of the block on purpose:
    # the bounded Agent.aclose() loop, the agent-loop cancels, job_registry.
    # close()'s voice arm and asyncio.run()'s final _cancel_all_tasks all come
    # after it, and that last one runs after this coroutine has returned. It
    # stays HERE rather than moving into the cleanup coroutine below: two
    # committed cases pin it as the statement immediately following the stop
    # wait, in main's own body.
    job_registry.begin_shutdown()
    await _shutdown_cleanup(
        job_registry=job_registry,
        engagement_registry=engagement_registry,
        scheduler=scheduler,
        session_sweeper=session_sweeper,
        freshness_reaper=freshness_reaper,
        runtime=runtime,
        ha_facade=ha_facade,
        bus=bus,
        loop_tasks=loop_tasks,
        channel_manager=channel_manager,
        runners=runners,
        semantic_memory=semantic_memory,
    )


async def _shutdown_cleanup(
    *,
    job_registry: Any,
    engagement_registry: Any,
    scheduler: Any,
    session_sweeper: Any,
    freshness_reaper: Any,
    runtime: Any,
    ha_facade: Any,
    bus: Any,
    loop_tasks: Any,
    channel_manager: Any,
    runners: Any,
    semantic_memory: Any,
) -> None:
    """Casa's graceful stop, from the stop signal to "shutdown complete".

    Extracted from ``main`` under #698, and the extraction is a gain rather
    than a refactor for its own sake: this block was executed by no test in the
    repository — ``job_registry.begin_shutdown()``'s call site has been unpinned
    since #671 — so every claim any invariant made about "a graceful stop"
    rested on a sequence nothing ran. It is now a production coroutine a red
    case can drive end to end, which is what lets INV-ENG-015 say "a launch
    that Casa's graceful-stop cleanup finds registered" and mean it.

    The statement ORDER is unchanged from the block it replaces, with exactly
    one addition: the engagement-launch step, at the top, beside the job
    ledger's own declaration.
    """
    # #698: the ENGAGEMENT ledger's twin of the line above, and in the same
    # place for the same reason. It records `casa_shutdown` against every
    # in-flight launch BEFORE cancelling it — cause carried, never inferred
    # from the shape of a cancellation — then waits for those launches and
    # their death reports. Here, at the top, because everything a dying launch
    # needs is still up: Telegram, the bus, the drivers and the registry. Left
    # to `asyncio.run`'s final sweep instead, the launch is cancelled with no
    # cause (its workspace destroyed) and its reporter is killed mid-notice.
    import tools as _tools_mod
    await _tools_mod.stop_engagement_launches(engagement_registry)

    # 17. Cleanup
    logger.info("Shutting down...")
    scheduler.shutdown(wait=False)
    await session_sweeper.stop()
    await freshness_reaper.stop()
    # NOTE: deliberately do NOT close the plugin outbox here. Its dir-FDs are
    # O_CLOEXEC and process-lived (the OS reclaims them on exit); closing the
    # live singleton during shutdown — before HTTP ingress + agent turns are
    # drained below — would let an in-flight send_media fall through to a
    # dir_fd=None (CWD-relative) op. close() is for test teardown only.

    # AR-9: close every resident/specialist Agent's SDK client pool so no
    # warm subprocess outlives container shutdown. Bounded per-agent so one
    # hung drain can't block the rest of the shutdown sequence.
    for _role, _agent in list(getattr(runtime, "agents", {}).items()):
        aclose = getattr(_agent, "aclose", None)
        if aclose is not None:
            try:
                await asyncio.wait_for(aclose(), timeout=15)
            except Exception:  # noqa: BLE001 — shutdown must complete
                logger.warning("agent %s aclose failed/timed out", _role)
    await _close_tina_ha_facade(ha_facade)

    # #316: gate the bus BEFORE cancelling its consumers — the HTTP
    # listeners stay open until runner.cleanup() below, and a signed
    # /invoke or voice request landing in that window used to enqueue for
    # a consumer that no longer exists, hanging the handler until the bus
    # timeout and stalling runner.cleanup() to aiohttp's shutdown bound.
    # New requests now fail fast with a typed error instead.
    bus.begin_shutdown()

    # H10 (v0.49.0): include consumers spawned after boot by reload
    # (bus.start_agent_loop) — the local loop_tasks list only has the
    # boot-time ones. cancel() is idempotent for already-evicted tasks.
    all_loop_tasks = set(loop_tasks) | set(bus.agent_loop_tasks())
    for task in all_loop_tasks:
        task.cancel()
    await asyncio.gather(*all_loop_tasks, return_exceptions=True)

    # #316: unstrand any request that was already awaiting a reply when
    # its consumer was cancelled above — resolve those futures now so
    # in-flight ingress handlers return before runner.cleanup() drains.
    bus.fail_pending()

    # Channel teardown (Telegram bot, voice, etc.) — resolve in-flight broker
    # work first, then stop the channels.
    await _drain_broker_before_channel_shutdown(channel_manager)

    # HTTP ingress teardown: close BOTH AppRunners (public 8099 + the internal
    # unix socket) BEFORE draining the force cleanups below. runner.cleanup()
    # drains in-flight requests then closes the listener, so once it returns no
    # webhook/voice/internal inbound can reach an agent. Moved AHEAD of the
    # force-cleanup drain (and of semantic_memory.close) to make the drain point
    # INGRESS-QUIESCENT — see the F1 rationale below.
    for _r in runners:
        await _r.cleanup()

    # F1 (v0.83.0 whole-branch gate, wave 2): bounded LOOP-drain of the
    # claude_code driver's force-suspend OWNERS (`_force_tasks`) + CANCEL-EXEMPT
    # post-SIGTERM cleanups (`_force_cleanups` — extinction poll + SIGKILL
    # escalation) so a SIGTERM-resistant engagement subprocess is verified
    # extinct rather than orphaned by a premature process exit.
    #
    # Placed AFTER every ingress surface is quiesced — the agent loops are
    # cancelled (above), the channels stopped, and now the HTTP/socket listeners
    # closed — so nothing can spawn a fresh force-suspend or fire an operator-
    # away clear that hands a new cleanup off after the loop-drain settles. The
    # drain itself re-snapshots both surfaces each iteration to catch a handoff
    # that lands mid-drain. Kept BEFORE semantic_memory.close so the memory
    # client outlives any in-flight cleanup. Bounded + truthful — never wedges
    # shutdown (getattr-guarded so a driver without the seam is a no-op).
    _cc_driver = getattr(runtime, "claude_code_driver", None)
    _drain_force = getattr(_cc_driver, "drain_force_cleanups", None)
    if _drain_force is not None:
        try:
            await _drain_force()
        except Exception:  # noqa: BLE001 — shutdown must complete
            logger.warning("force-cleanup drain failed", exc_info=True)

    # No new ingress can bind a job now. Cancel/wait process-local ownership;
    # each task's done callback remains the sole concurrency-permit releaser.
    try:
        await job_registry.close()
    except Exception:  # noqa: BLE001 — shutdown must complete
        logger.warning("job registry close failed", exc_info=True)

    # Close the shared Hindsight client session (L32) so aiohttp does not
    # warn about an unclosed session; no-op for NoOp/other backends.
    try:
        await semantic_memory.close()
    except Exception:  # noqa: BLE001
        logger.warning("semantic memory close failed", exc_info=True)
    logger.info("Casa core shutdown complete")


def run() -> None:
    """Synchronous entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
