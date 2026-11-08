"""Casa core entry point -- wires everything together."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import signal
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
from channels import (ChannelManager, DeliveryOutcome,
                      OperatorNotifyBeforeStart)
from claude_runtime import (
    CLAUDE_CLI_PATH,
    CLAUDE_CLI_VERSION,
    verify_effective_cli,
)
from config import AgentConfig
from config_git import commit_config, init_repo, snapshot_manual_edits
from ingress_identity import (
    IngressIdentityError,
    ingress_identity,
    validate_ingress_identity_table,
)
from log_cid import install_logging, new_cid
from casa_core_middleware import cid_middleware, CasaAccessLogger
from ha_mcp_facade import HomeAssistantFacade
from mcp_registry import McpServerRegistry
from policies import load_policies
import private_state
from output_boundary import casa_text
from provenance import sanitize_external_context
from session_registry import SessionRegistry
from session_sweeper import SessionSweeper
from rate_limit import RateLimiter, rate_limit_response
from timekeeping import resolve_tz
from trigger_registry import TriggerRegistry
from voice_delivery_config import load_voice_delivery_config

logger = logging.getLogger(__name__)

CONFIG_DIR = "/config"
DATA_DIR = "/data"


# ---------------------------------------------------------------------------
# Plan 4b/3.6: internal Unix-socket AppRunner (broker and /admin/* routes)
# ---------------------------------------------------------------------------


async def start_internal_unix_runner(
    *,
    socket_path: str,
    runtime=None,
) -> "web.AppRunner":
    """Build and start a second aiohttp AppRunner bound to a Unix socket.

    Routes:
      POST /internal/broker/*      -> result_broker / operator_calls handlers
      POST /admin/reload           -> build_admin_reload_handler(...)
        (Task E.1 -- casactl operator CLI dispatch)
      POST /admin/memory/*         -> the memory wipe/export/restore handlers

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
        build_admin_memory_wipe_handler,
        build_admin_memory_export_handler,
        build_admin_memory_restore_handler,
        build_admin_reload_handler,
        admin_peercred_middleware,
    )

    # #467: gate the operator-only /admin/* family to a root peer via
    # SO_PEERCRED.
    internal_app = web.Application(middlewares=[admin_peercred_middleware])
    # #792: the result broker's two routes — a plugin's MCP server (a child of
    # the SDK-spawned CLI, running as casa-main's user) deposits a capability
    # here during a declared tool call and a declared consumer redeems it.
    # Unix-socket-only (0600, root): never registered on the public 8099 app
    # and never forwarded anywhere.
    from result_broker import (
        build_broker_deposit_handler, build_broker_redeem_handler,
    )
    internal_app.router.add_post(
        "/internal/broker/deposit", build_broker_deposit_handler(),
    )
    internal_app.router.add_post(
        "/internal/broker/redeem", build_broker_redeem_handler(),
    )
    # Browser executor §5.3–§5.6: during one of its `callsCasa` calls a
    # plugin's code learns whom it serves, requests run admission, queues a
    # continuation and reads a delivery's outcome — the same socket, the same
    # client-id binding.
    import operator_calls as _ops
    internal_app.router.add_post("/internal/broker/requester",
                                 _ops.build_requester_handler())
    internal_app.router.add_post("/internal/broker/run-admission",
                                 _ops.build_run_admission_handler())
    internal_app.router.add_post("/internal/broker/run-admission-status",
                                 _ops.build_run_admission_status_handler())
    internal_app.router.add_post("/internal/broker/continue",
                                 _ops.build_continue_handler())
    internal_app.router.add_post("/internal/broker/delivery-status",
                                 _ops.build_delivery_status_handler())
    # Casa Browser action model: the check of one browsing action, asked by
    # the plugin's code during the same kind of call.
    internal_app.router.add_post("/internal/broker/action-judgement",
                                 _ops.build_action_judgement_handler())
    # Task E.1 (granular-reload plan): casactl operator CLI POSTs here
    # over the unix socket. Same dispatch path as the casa_reload MCP tool.
    internal_app.router.add_post(
        "/admin/reload",
        build_admin_reload_handler(runtime=runtime),
    )
    # Operator memory wipe (casactl memory-wipe --yes). Root-gated by the
    # same peercred middleware.
    internal_app.router.add_post(
        "/admin/memory/wipe",
        build_admin_memory_wipe_handler(),
    )
    # Memory export and restore (casactl memory-export / memory-restore),
    # root-gated alike. A restore is staged here and carried out by the
    # container-start step after the app restart.
    internal_app.router.add_post(
        "/admin/memory/export",
        build_admin_memory_export_handler(),
    )
    internal_app.router.add_post(
        "/admin/memory/restore",
        build_admin_memory_restore_handler(),
    )

    # Task 14 (personality Phase A): lean inspection/explain admin routes —
    # POST /admin/personality/{inspect,render,diff}, /admin/specialist/status,
    # /admin/explain. Unix-socket-only: registered on internal_app alone,
    # NEVER on the public 8099 app (see casa_core.py's `app` router further
    # down). Skipped when runtime is None (some test/fallback boot paths).
    if runtime is not None:
        from personality_admin_handlers import register_personality_admin_routes

        register_personality_admin_routes(internal_app, runtime=runtime)

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


def read_followup_incomplete(engagement_driver, rec, token) -> str:
    """#692: the ``_driver_turn_incomplete`` seam's whole body, at module
    level so it can be tested as production code rather than reimplemented in
    a test double.

    SYNCHRONOUS. Returns ``""`` for any driver that does not expose the
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
    fn = getattr(engagement_driver, "followup_turn_incomplete", None)
    return fn(rec.id, token) if callable(fn) else ""


def _repair_private_state() -> None:
    """Private-state mode repair — GHSA-569r-7crq-xr43, INV-STATE-005.

    ``main()`` calls this once on every boot, before any channel or agent
    starts, so nothing that could start a privilege-dropped process runs ahead
    of it.

    This pass, not the write sites, is what repairs an already-deployed
    install: every affected file already exists at 0644 and atomic_io
    preserves an existing file's mode, so a file whose next write is days away
    would otherwise stay exposed. Never raises: ``enforce`` reports instead of
    raising, and anything unexpected is logged rather than allowed to stop boot.
    """
    try:
        report = private_state.enforce()
    except Exception as exc:  # noqa: BLE001 — a repair failure must not stop boot
        logger.error("private-state: the boot repair pass failed: %s", exc)
        return
    if report.changed:
        logger.info(
            "private-state: repaired %d path mode(s) on this boot",
            len(report.changed),
        )


def _bus_loop_targets(agents: dict) -> list[str]:
    """H4 (v0.53.0): bus targets that need a ``run_agent_loop`` consumer.

    Residents (``agents`` roles) + the ``telegram`` outbound target.
    ``dict.fromkeys`` dedupes while preserving order.
    """
    return list(dict.fromkeys(list(agents.keys()) + ["telegram"]))


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
    """
    from verdict_broker import BROKER
    BROKER.cancel_all(reason="casa_shutdown")
    await CHALLENGES.drain()
    await BROKER.drain_hooks()
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


async def wire_butler_ha_facade(
    mcp_registry: "McpServerRegistry",
    facade: Any,
    agents: Mapping[str, Any],
    *,
    butler_role: str = "butler",
) -> None:
    """Publish the butler's eager HA schema and retire its stale SDK clients.

    The config and its surface digest are read together, with no await
    between them, so the registry pairs them from one facade commit (#1091):
    a turn armed after this publication gates its resume on these tools."""
    mcp_registry.register_role_sdk(
        "homeassistant", butler_role, facade.server_config,
        surface_digest=facade.surface_digest,
    )
    agent = agents.get(butler_role)
    if agent is not None:
        await agent.invalidate_tool_surface()


async def _start_butler_ha_facade(
    mcp_registry: "McpServerRegistry",
    role_configs: Mapping[str, Any],
    agents: Mapping[str, Any],
    *,
    ha_mcp_url: str,
    supervisor_token: str,
    butler_role: str = "butler",
) -> HomeAssistantFacade | None:
    """Start and publish the butler's eager Home Assistant facade.

    v0.125.0 (#228): the facade is unconditional. ``tina_ha_facade_enabled``
    was a diagnostic fallback to the raw Home Assistant MCP connection; the
    facade has been the shipped path throughout and the raw connection is not
    a configuration an operator should be choosing.
    """
    butler_config = role_configs.get(butler_role)
    if (
        not supervisor_token
        or butler_config is None
        or "ha_voice" not in (getattr(butler_config, "channels", ()) or ())
    ):
        return None

    facade: HomeAssistantFacade

    async def _schema_changed() -> None:
        await wire_butler_ha_facade(
            mcp_registry, facade, agents, butler_role=butler_role,
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
    await wire_butler_ha_facade(
        mcp_registry, facade, agents, butler_role=butler_role,
    )
    return facade


async def _close_butler_ha_facade(
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


# Default max webhook request body (spec A3). Larger requests are rejected
# before auth. #1156: a plugin route may declare its own (``max_body_kib``); the
# route record carries it, and this is the fallback for a record without one.
_WEBHOOK_BODY_MAX = 64 * 1024

# #1142: Casa's OWN constant, appended to an opted-in (`deliver: operator`)
# webhook turn's content — selected by the route's enum, never trigger prose, so
# INV-TRIG-013 (a webhook trigger carries no prompt) stands. Guidance only: what
# keeps the turn to one send path is that `send_message` is not offered to it
# (INV-TRIG-018), not this line.
WEBHOOK_DELIVER_LINE = (
    "(Casa: your final reply is delivered to the operator on Telegram; "
    "reply <silent/> if nothing warrants telling them.)"
)


# #1158: the line for a `deliver: operator_always` fire. It never invites
# <silent/>: such a fire always ends in one operator message, and a reply that
# is silent anyway is replaced by Casa's own fallback line (INV-TRIG-021).
WEBHOOK_DELIVER_ALWAYS_LINE = (
    "(Casa: your final reply is delivered to the operator on Telegram, and "
    "every fire must reach them — always reply, even if only briefly.)"
)


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

    Request pipeline (spec A3): rate limit → name lookup (unknown ⇒ 404) →
    body read bounded by the route's cap (64 KiB unless a plugin trigger
    declares ``max_body_kib``, #1156) → PER-TRIGGER auth verify (fail ⇒ 401) →
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

    def _secret_for(name: str, route: dict) -> bytes:
        policy = route.get("auth") or {"mode": "hmac_body"}
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
        #
        # #620 (INV-TRIG-016): a casa-owned RESIDENT route reads only bytes its
        # mint receipt certifies for the role that routes the name — one read,
        # no re-read. A slot Casa cannot prove it minted, or minted for another
        # role, is an empty secret (401) and is reported `unproven_blocked`;
        # it is never destroyed here. Provider-owned slots and the plugin
        # overlay (whose backing is the `.ident` binding) keep the plain read.
        try:
            if route.get("resident") and owner == "casa":
                got = webhook_auth.read_certified_secret(
                    name, role=route["role"], secrets_dir=secrets_dir)
            else:
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

    def _verify(request: web.Request, body: bytes, route: dict,
                secret: bytes) -> bool:
        policy = route.get("auth") or {"mode": "hmac_body"}
        return webhook_auth.verify(
            policy.get("mode", "hmac_body"),
            body=body,
            headers=request.headers,
            secret=secret,
            header_name=policy.get("header", "X-Webhook-Signature"),
            tolerance_secs=int(policy.get("tolerance_secs", 300)),
            now=int(time.time()),
        )

    async def webhook_handler(request: web.Request) -> web.Response:
        limited = rate_limit_response(webhook_rate_limiter, "global")
        if limited is not None:
            return limited

        name = request.match_info.get("name", "")
        # #620 (seam S1): ONE atomic route record per request — role, auth
        # policy, body cap, both clearance uses and resident/plugin authority
        # all come from it, never from a second registry read. Three separate
        # getters could straddle a concurrent re-registration and stamp one
        # message from two routes (target from the old, clearance from the new).
        # #1156: read BEFORE the body, because the cap is the route's; names
        # are non-secret, so a 404 here discloses nothing new.
        route = trigger_registry.webhook_route(name)
        if route is None:
            return web.json_response(
                {"error": "unknown webhook"}, status=404,
            )
        max_body = int(route.get("max_body") or _WEBHOOK_BODY_MAX)
        # #1156 (diff r1 S1): the key is read WITH the route, before the body
        # is awaited. Read after a slow body, it could belong to an approval
        # that replaced this route mid-request — one request verified by the
        # new approval's key under the old route's role and clearance.
        secret = _secret_for(name, route)

        # Bounded body read (spec A3): reject a declared oversize Content-Length
        # early, AND stream-read with a hard cap so a chunked/Transfer-Encoding
        # request cannot buffer past the route's cap (Terra ship-review P1).
        if request.content_length is not None and request.content_length > max_body:
            return web.json_response({"error": "payload too large"}, status=413)
        chunks: list[bytes] = []
        read = 0
        async for chunk in request.content.iter_chunked(8192):
            read += len(chunk)
            if read > max_body:
                return web.json_response({"error": "payload too large"}, status=413)
            chunks.append(chunk)
        body = b"".join(chunks)
        target_role = route["role"]
        clearance = route.get("clearance", "public") or "public"
        # #1142: from the SAME route record — never a second registry read.
        # #1158: the same enum, one value more; anything else reads "none".
        deliver = (route.get("deliver")
                   if route.get("deliver") in ("operator", "operator_always")
                   else "none")

        if not _verify(request, body, route, secret):
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
                clearance=clearance,
            )
        except IngressIdentityError:
            logger.error(
                "webhook %r could not be given a trusted identity; refusing "
                "to dispatch it unattributed", name, exc_info=True,
            )
            return web.json_response(
                {"error": "ingress identity unavailable"}, status=500,
            )

        content = f"Webhook '{name}' triggered with payload: {payload}"
        if deliver == "operator":
            content = f"{content}\n\n{WEBHOOK_DELIVER_LINE}"
        elif deliver == "operator_always":
            content = f"{content}\n\n{WEBHOOK_DELIVER_ALWAYS_LINE}"
        msg = BusMessage(
            type=MessageType.SCHEDULED,
            source="webhook",
            target=target_role,
            content=content,
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
                "_origin_clearance": clearance,
                # #1142: server-set from the route record, like the two
                # markers above; reserved, so no external context can set it.
                "_webhook_deliver": deliver,
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
# Agent loader
# ------------------------------------------------------------------


def _build_role_registry(
    *,
    residents: dict,
    specialists: dict,
) -> dict:
    """Merge resident and specialist role→AgentConfig dicts, with the
    configurator, through the builder every reload uses
    (``tools.build_agent_role_map``). Fail on overlap.

    Returns a single dict the renamed delegate_to_agent tool resolves
    against. Roles must be globally unique across both tiers — colliding
    roles are a configuration bug (e.g. someone created
    agents/specialists/butler/ while a butler resident already exists).
    """
    from tools import build_agent_role_map
    return build_agent_role_map(residents, specialists, strict=True)


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


async def _boot_reconcile_resident_trigger_secrets(
    *, role_configs: dict, secrets_dir: Any, agents_dir: Any,
    trigger_registry: Any = None,
) -> dict:
    """Boot seam for #609 + #620: right after resident triggers register,
    retire every Casa-minted slot the declarations no longer entitle, create the
    missing casa-owned slots, then retire the certified slots of any role whose
    agent directory is gone.

    Per resident, under the route lifecycle lock: ``retire_for_role`` (both
    arms — the boot `config_sync` oneshot's entry drops and any un-reloaded edit
    land here), and if it refuses or the inventory is unreadable that role is
    UNWOUND (``reregister_for(role, [], [])``) so the state a reload would
    refuse to publish is not served either; otherwise the mint. Then the
    directory-gone sweep, keyed on Casa's own receipts and the existence of the
    role's directory of either tier — an `OSError` other than ENOENT there
    reads as "exists". Returns ``{"retired", "failed", "mint_failures"}`` (plus
    ``inventory_unavailable`` when the inventory could not be enumerated).

    Extracted from ``main()`` so it is unit-testable — ``main()`` has no
    execution coverage past its third statement, and step 13b sits outside
    every ``try`` in it.

    NEVER fatal, and that is a measured decision rather than a preference. On
    a healthy install this is a pure READ; the only boot that can fail is one
    where ``/data`` is unwritable. Aborting there would trade Telegram, voice,
    every reminder and every engagement for one webhook that would 401 either
    way — and an exception escaping ``main()`` does not restart Casa, it STOPS
    the app (``svc-casa/finish`` calls ``bashio::app.stop`` for any exit code
    but 0/256). The failure is carried by a WARN and by the report on the next
    reload, which is also the retry.
    """
    import stat as _stat
    result: dict = {"retired": [], "failed": [], "mint_failures": []}
    try:
        import reload as _reload
        import resident_trigger_secrets as rts

        def role_dir_exists(role: str) -> bool:
            for d in (os.path.join(agents_dir, role),
                      os.path.join(agents_dir, "specialists", role)):
                try:
                    st = os.stat(d)
                except FileNotFoundError:
                    continue
                except OSError:
                    return True          # fail closed: cannot say it is gone
                if _stat.S_ISDIR(st.st_mode):
                    return True
            return False

        def work() -> None:
            for role, cfg in role_configs.items():
                specs = list(cfg.triggers or [])
                with _reload._ROUTE_LIFECYCLE_LOCK:
                    try:
                        out = rts.retire_for_role(
                            role, declared=specs, staged=rts.webhook_names(specs),
                            secrets_dir=secrets_dir)
                    except rts.InventoryUnavailable as exc:
                        result["inventory_unavailable"] = True
                        out = rts.RetireOutcome([], [])
                        logger.warning("boot: %s; role=%s left unrouted", exc, role)
                        refused = True
                    else:
                        refused = bool(out.failed)
                    result["retired"].extend(out.retired)
                    result["failed"].extend(out.failed)
                    if refused:
                        logger.warning(
                            "boot: webhook secret retirement refused for role=%s "
                            "(%s); the role is left with no triggers until a reload "
                            "retries it", role, out.failed)
                        if trigger_registry is not None:
                            trigger_registry.reregister_for(role, [], [])
                        continue
                    for name, reason in rts.mint_for_specs(
                            specs, secrets_dir=secrets_dir, role=role):
                        result["mint_failures"].append((name, reason))
                        logger.warning(
                            "boot: webhook trigger %r has no usable secret and "
                            "could not be minted (%s) — requests to it will be "
                            "refused until the next reload or restart succeeds",
                            name, reason)
            with _reload._ROUTE_LIFECYCLE_LOCK:
                try:
                    out = rts.retire_for_roles_without_directory(
                        secrets_dir=secrets_dir, role_dir_exists=role_dir_exists)
                except rts.InventoryUnavailable as exc:
                    result["inventory_unavailable"] = True
                    logger.warning("boot: %s; the directory-gone sweep is skipped", exc)
                    return
            result["retired"].extend(out.retired)
            result["failed"].extend(out.failed)
            for name in out.retired:
                logger.info("boot: retired the webhook secret of %r — its role has "
                            "no agent directory", name)
            for name in out.failed:
                logger.warning("boot: webhook secret %r could not be read or "
                               "removed; left in place", name)

        await asyncio.to_thread(work)
        return result
    except Exception:  # noqa: BLE001 — never fatal; see the docstring
        logger.warning("boot resident trigger-secret reconcile failed", exc_info=True)
        return result


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


def _applied_plugin_routing() -> "tuple[bool, int] | None":
    """#803: the applied plugin routing state the setup-dispatch worker reads
    beside the route gate above — ``None`` when no runtime registry is bound
    (the recomputation alone decides, exactly as before), else
    ``(published, generation)``: ``published`` is False while EITHER applied
    overlay carries :data:`trigger_registry.ROUTING_UNAVAILABLE` (identity,
    via the registry's own predicates), ``generation`` counts every overlay
    publication of either kind. No I/O, no lock, no yield — safe from the
    gate's thread and from the loop. Raises propagate: a swallowed exception
    would have to become ``None`` (which reads as "no registry", fail-open)
    or an invented tuple, so each caller fails CLOSED itself.

    Reads the LIVE registry, never a captured one: a consent-tap reconcile
    publishes to the registry it captured, which may be stale after a full
    reload, and the worker must decide against what requests route through.
    """
    import agent as _agent_mod
    runtime = getattr(_agent_mod, "active_runtime", None)
    registry = getattr(runtime, "trigger_registry", None) if runtime else None
    if registry is None:
        return None
    published = not (registry.plugin_overlay_unavailable()
                     or registry.callback_overlay_unavailable())
    return published, int(registry.routing_generation())


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
    to honest logging.

    #930: before ``start_all`` has completed, the not-ready raise is
    :class:`OperatorNotifyBeforeStart` (a ``RuntimeError``), so those sites
    can log a designed retry at INFO. A manager that does not say whether
    its start completed gets the ordinary error."""
    import trigger_consent as _tc
    ch = channel_manager.get("telegram") if channel_manager else None
    if ch is None or not getattr(ch, "is_ready", True):
        if getattr(channel_manager, "start_completed", True) is False:
            raise OperatorNotifyBeforeStart(
                "operator notify: telegram channel not ready")
        raise RuntimeError("operator notify: telegram channel not ready")
    # Address the operator DM explicitly (falls back to the channel's
    # default chat — also the operator — when identity is unresolvable).
    op = _tc.operator_identity(ch)
    ctx = {"chat_id": op[0]} if op is not None else {}
    await ch.send_response(casa_text(text), ctx)


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
            outcome = await channel.send(casa_text(
                f"⚠️ Updated {path}, which uses ${{...}} placeholders — a "
                f"placeholder entry there may now resolve differently. "
                f"Worth a look."), {"cid": new_cid()})
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
    # #1053: an install walks its plugin through states that are expected on
    # the way (consent pending, a setup-provided value not wired yet, setup
    # still to run…). While the configurator run installing that plugin holds
    # its lease, its install-phase rows are neither named nor marked — so they
    # stay new, and the pass run when the lease is released
    # (configurator_run._release) names whatever still stands. Any other row
    # is a fault and is sent as ever.
    try:
        import tools as _tools_mod
        installing = _tools_mod.plugins_under_live_install()
    except Exception:  # noqa: BLE001 — unknown → defer nothing
        logger.warning("plugin_health notify: live-install lookup failed",
                       exc_info=True)
        installing = set()
    if installing:
        entries = [e for e in entries
                   if not (e.get("name") in installing
                           and plugin_health.is_install_phase(
                               e.get("reason_code")))]
        if not entries:
            return
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
        outcome = await channel.send(casa_text(content), {"cid": new_cid()})
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
    """Periodic topics pass — the ``workspace_sweep`` job [AR-8].

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
            await channel.send(casa_text(content), {"cid": new_cid()})
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


def _short_engagement_id(rec) -> str:
    """A log-safe short id for a record that came off disk.

    `rec.id` is a string by convention only: a hand-edited or corrupt tombstone
    row can carry an integer, and `rec.id[:8]` then raises `TypeError`. That
    matters most in the one place it must never raise — the replay owner's own
    per-record GUARD, whose whole job is to keep one bad row from stopping a
    boot. A guard that raises while reporting a failure is not a guard.
    """
    rid = getattr(rec, "id", None)
    return rid[:8] if isinstance(rid, str) else repr(rid)[:16]


def _engagement_delivery_ack(registry, engagement_id: str):
    """#766: the delivery acknowledgement a replayed engagement outcome carries.

    A FACTORY, not an inline closure over the loop variable. A closure written
    in the loop body binds the LAST record, so delivering the first notice
    acknowledges the last one and every other outcome is discarded unread —
    reproduced in review, and invisible to any assertion that counts callbacks
    rather than naming the id each one cleared.

    Deliberately no exception handling here, exactly as
    ``_recovery_delivery_ack`` has none: the agent's acknowledgement seam owns
    that, and its rule is that a failed acknowledgement leaves the marker set so
    the announcement is repeated at the next boot.
    """
    async def _ack() -> None:
        await registry.ack_terminal_notification(engagement_id)
    return _ack


async def _close_disabled_specialist_engagements(engagement_registry: Any,
                                                specialist_registry: Any) -> set:
    """#1095 boot pass: close the open conversations of every specialist the
    registry loaded and reads DISABLED. A specialist that failed to load is not
    disabled (``is_disabled`` excludes load failures) and keeps its
    conversations. A failed close is told to the operator by the close helper
    and remembered for the next ``agents`` sweep. Returns the ids of the
    disabled specialists' conversations it could not close, so the background-
    job resume that follows leaves them alone. Never raises."""
    import reload as reload_mod
    import tools as tools_mod
    left_open: set = set()
    try:
        roles = sorted({r.role_or_type for r in engagement_registry.active_and_idle()
                        if r.kind == "specialist"})
    except Exception:  # noqa: BLE001 — boot continues
        logger.exception("boot: listing open specialist engagements failed")
        return left_open
    for role in roles:
        try:
            if specialist_registry.is_disabled(role) is not True:
                continue
            rows = await tools_mod.close_disabled_specialist_engagements(role)
            failed = {r["engagement_id"] for r in rows if r["outcome"] == "close_failed"}
            if failed:
                left_open |= failed
                reload_mod._note_retirement_outcome(role, ["close_engagements"])
            logger.info("boot: closed %d open conversation(s) of disabled specialist %s",
                        sum(r["outcome"] == "closed" for r in rows), role)
        except Exception:  # noqa: BLE001 — boot continues
            logger.exception("boot: closing disabled specialist %s's conversations failed",
                             role)
            left_open |= {r.id for r in engagement_registry.active_and_idle()
                          if r.kind == "specialist" and r.role_or_type == role}
    return left_open


async def _resume_background_jobs(registry, channel, skip=frozenset()) -> None:
    """Continue open in-casa jobs once channels and residents are running.
    *skip*: engagements the boot close of a disabled specialist could not close
    (#1095) — a disabled specialist's job is never resumed; the next ``agents``
    sweep retries closing it."""
    from background_jobs import start_next_batch
    from tools import _post_engagement_notice

    if channel is None:
        return
    for rec in registry.active_and_idle():
        if rec.id in skip:
            logger.warning("not resuming job %s: its specialist is disabled and "
                           "closing it failed", rec.id[:8])
            continue
        if rec.driver == "in_casa" and rec.origin.get("job"):
            try:
                title = rec.origin["job"]["title"]
                await _post_engagement_notice(
                    channel, rec, f'↻ Resuming "{title}" after a restart.')
            except Exception:
                logger.warning("background job resume notice failed for %s", rec.id[:8],
                               exc_info=True)
            try:
                await start_next_batch(rec, channel)
            except Exception:  # noqa: BLE001 — one job must not stop the boot
                logger.warning("resuming background job %s failed", rec.id[:8],
                               exc_info=True)


async def _notify_recovered_engagement_outcomes(
    registry,
    bus,
    *,
    assistant_role: str,
) -> None:
    """#766: announce every engagement outcome that still owes its engager one.

    Before this there was NO boot replay of an engagement outcome at all. So a
    completed or cancelled engagement could be
    durably terminal on disk while the party that asked for the work was never
    told, permanently, across restarts, with no log line on the commonest arm
    (``MessageBus.send_checked`` returns ``"no_target"`` for an unregistered
    role and ``send`` discards it).

    What a replay may say is bounded by what the tombstone actually keeps. The
    record carries NO completion text, artifacts or next_steps — no such fields
    exist on it — so this reports the FACT of the outcome and says plainly that
    the report itself is gone. ``result_available=False`` carries that on the
    success arm; the non-ok arms carry an explicit message, because their
    rendering interpolates one and the live path feeds it from the non-durable
    text, which would otherwise render "Delegation failed (cancelled): " with
    nothing after the colon. Fabricating narration from ``task`` or from a
    stale ``summary_message_id`` would be the #688 mistake.

    Addressed from the record's own PERSISTED origin, filtered by
    ``_persistable_origin`` — role, channel, chat_id, cid and the original user
    text all survive — so the notice reaches the resident that asked, on the
    channel it asked from.

    Boot does not wait for any of it: a resident turn can take minutes, so this
    returns as soon as the last message is enqueued. And enqueueing is not
    telling — the obligation is cleared by ``on_delivery`` alone, so a message
    this boot never delivers is replayed by the next one.

    #1093: it walks what the PREVIOUS process left owed — the records owed
    when this process loaded the registry, still owed as each is reached —
    never what is owed when it runs. An engagement that ends in this process
    before the walk was told live, and replaying it too told it twice, the
    second copy claiming an outcome reached before a restart.
    """
    for rec in registry.records_owed_at_load():
        try:
            await _replay_one_engagement_outcome(
                registry, bus, rec, assistant_role=assistant_role)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad row must not block boot
            # The guard covers the WHOLE per-record body, not only the send:
            # this owner is awaited UNGUARDED from `main`, and a payload built
            # out of a tombstone row is exactly where an unexpected type
            # surfaces. No exception text is logged — a connector failure can
            # include payload or credential material — and the durable bit
            # retains enough state for the next boot to retry.
            logger.error(
                "Engagement outcome replay failed: id=%s — retained",
                _short_engagement_id(rec),
            )


async def _replay_one_engagement_outcome(
    registry, bus, rec, *, assistant_role: str,
) -> None:
    """#766: announce ONE owed engagement outcome. Raises; the caller retains."""
    from specialist_registry import DelegationComplete

    if not isinstance(rec.id, str) or not rec.id:
        # Every downstream consumer treats the id as a string — the ack keys
        # off it, the notice carries it, and the logs slice it. A row that does
        # not have one cannot be announced or acknowledged coherently.
        logger.error(
            "Engagement row owes an outcome but has no usable id (%s) — "
            "retained, not announced", _short_engagement_id(rec),
        )
        return

    origin = dict(getattr(rec, "origin", None) or {})

    # The obligation is only ever armed WITH a terminal status, so a row
    # carrying it over a live status is a corrupt or hand-edited tombstone —
    # and boot rewrites `active` to `idle` at load, so `idle` is the shape it
    # would take. Announcing it would tell the engager that an engagement which
    # is still resumable ended in an error, and the delivery would then
    # discharge the obligation. Fail CLOSED: retain it and say so.
    if rec.status not in ("completed", "cancelled", "error"):
        logger.error(
            "Engagement %s owes an outcome but is not terminal (status=%r) — "
            "retained, not announced", _short_engagement_id(rec), rec.status,
        )
        return

    # `origin` came off disk as JSON, so `role` is only a string by convention.
    # An unhashable value — a list, say — raises out of the membership test
    # below, and this owner is awaited unguarded from `main`.
    _role = origin.get("role")
    target_role = _role if isinstance(_role, str) and _role else assistant_role
    if target_role not in bus.queues:
        logger.error(
            "Engagement %s owes its outcome to unknown role %r — retained for "
            "retry", rec.id[:8], target_role,
        )
        return

    succeeded = rec.status == "completed"
    if succeeded:
        message = ""
    elif rec.status == "cancelled":
        message = ("The engagement was cancelled, and this recovery notice "
                   "does not carry its report.")
    else:
        _err = origin.get("error_message")
        message = (_err if isinstance(_err, str) and _err
                   else "The engagement ended in an error.")
    synthetic = DelegationComplete(
        delegation_id=rec.id,
        agent=rec.role_or_type,
        status="ok" if succeeded else "error",
        # The EXACT outcome. Reporting a cancellation as a generic failure
        # misstates what happened to the engager's work.
        kind="" if succeeded else rec.status,
        message=message,
        # Nothing was retained on the record to carry, on any arm.
        result_available=False,
        origin=origin,
        elapsed_s=0.0,
        # #1087: this is a boot replay, so it says so, with the time the
        # record went terminal — the synthesizer renders the same outcome
        # statement and recorded time as for a delegation replay (#1084). The
        # value is passed as persisted: the synthesizer renders anything that
        # is not a usable time as unknown, and nothing here can raise on it.
        # Only this producer sets them; the shared envelope below and every
        # live engagement notice leave both at their defaults.
        replayed_after_restart=True,
        terminal_at=rec.completed_at,
    )
    # §2.A: the ONE bus envelope for an engagement outcome (routing only;
    # this payload — no text, result_available=False, elapsed 0, and the
    # replay mark above — is the replay's own).
    import tools as _tools_mod
    await _tools_mod.send_engagement_outcome(
        bus, complete=synthetic, origin=origin,
        ack=_engagement_delivery_ack(registry, rec.id),
        source=rec.role_or_type, fallback_role=assistant_role)

    logger.warning(
        "Engagement outcome recovered: id=%s status=%s — NOTIFICATION posted",
        rec.id[:8], rec.status,
    )


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
    kind. A row that SUCCEEDED during the stop replays as a success carrying
    the answer the row retained for exactly this moment (#688, INV-JOB-015) —
    or, when it retained none, as a success whose answer is unavailable, which
    is what a legacy row and a row written by a non-announcing arm still do.
    """
    from specialist_registry import DelegationComplete

    for job in recovered_jobs:
        if job.creator_peer != "telegram":
            continue
        if not (job.orphan_notification_pending
                or job.terminal_notification_pending):
            continue
        # #1084: the log names the row's kind. The orphan marker is written
        # only by the live-row conversion, and the two markers never share a
        # row, so a terminal-marker row went terminal while the previous
        # process was up — it is not an orphan. True orphans keep their
        # historical lines; the terminal row carries its fixed status token.
        is_orphan = bool(job.orphan_notification_pending)
        row_status = "ok" if job.failure is None else "error"
        target_role = job.creating_role or assistant_role
        if target_role not in bus.queues:
            if is_orphan:
                logger.error(
                    "Orphan delegation %s targets unknown role %r — retained for retry",
                    job.id[:8], target_role,
                )
            else:
                logger.error(
                    "Unannounced delegation outcome %s status=%s targets "
                    "unknown role %r — retained for retry",
                    job.id[:8], row_status, target_role,
                )
            continue

        # #688: the notice reports what the ROW holds, rather than a fixed
        # retention posture. A delegation whose answer was in hand when it
        # completed retained that answer on the row precisely so this replay
        # could carry it; a row that retained none — a legacy row, an orphan
        # conversion, a terminal written by an arm that owed no announcement —
        # still replays as a success whose answer is unavailable, and its empty
        # stored result is still never presented as the answer. Availability is
        # the row's own durable fact, NEVER `bool(job.result)`: a specialist
        # that legitimately answered with nothing is not a row that kept
        # nothing.
        succeeded = job.failure is None
        answer_available = bool(succeeded and job.result_available)
        synthetic = DelegationComplete(
            delegation_id=job.id,
            agent=job.specialist_role,
            status="ok" if succeeded else "error",
            text=(job.result or "") if answer_available else "",
            kind="" if succeeded else job.failure.kind,
            message="" if succeeded else job.failure.message,
            result_available=answer_available,
            # #926: the one producer that knows this notice is a replay says
            # so, on every shape it builds; which arm renders the fact is the
            # synthesizer's decision. A live producer never sets this.
            replayed_after_restart=True,
            # #1084: when the registry recorded the row terminal, so the
            # replay can say how old the outcome is (for an orphan, when the
            # loss was recorded). Same producer rule as the flag above.
            terminal_at=job.terminal_at,
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
                # #1038: the brief's resolved disclosure, restored from the
                # row so the narration turn after a restart still carries it.
                **({"_inherited_note": job.output_note}
                   if job.output_note else {}),
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
            if is_orphan:
                logger.error(
                    "Orphan notification failed: id=%s phase=notify — retained",
                    job.id[:8],
                )
            else:
                logger.error(
                    "Unannounced delegation outcome notification failed: "
                    "id=%s status=%s phase=notify — retained",
                    job.id[:8], row_status,
                )
            continue

        if is_orphan:
            logger.warning(
                "Orphan delegation recovered: id=%s agent=%s — NOTIFICATION posted",
                job.id[:8], job.specialist_role,
            )
        else:
            logger.warning(
                "Unannounced delegation outcome replayed: id=%s agent=%s "
                "status=%s — NOTIFICATION posted",
                job.id[:8], job.specialist_role, row_status,
            )


# §8: the password-typed app options, whose values may be an `op://` reference
# the boot path resolves once, before any consumer reads them.
# OP_SERVICE_ACCOUNT_TOKEN is deliberately absent — it is the credential the
# resolution itself needs, and an `op://` reference to it could never resolve.
_PASSWORD_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "WEBHOOK_SECRET",
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


def _load_agents_at_boot(agents_dir: str, *, policies, config_dir: str) -> dict:
    """Load every resident, then record the binding writes that load made.

    #1391: a resident whose role or persona changed is re-bound inside
    ``load_all_agents`` (``active.yaml`` written, ``active.prior.yaml``
    rotated), after the boot snapshot. Committing ``bindings/`` here gives
    that re-bind its own commit instead of leaving it for the next unrelated
    one to sweep up.
    """
    role_configs = load_all_agents(agents_dir, policies=policies)
    try:
        commit_config(config_dir, "casa: resident binding re-bound at boot",
                      paths=["bindings"])
    except Exception as exc:  # noqa: BLE001 — history only; never boot-fatal
        logger.warning("config_git: could not commit the boot re-bind: %s", exc)
    return role_configs


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

    # 1c. A pending memory wipe, before anything that reads memory starts.
    # With an attempt recorded (the container-start step failed or was cut
    # off), memory may be partly deleted: notification-only mode opens no
    # memory and starts nothing. With none recorded, memory is untouched:
    # boot normally and ask for the app restart again once the channels are
    # up.
    from memory import maintenance as memory_maintenance
    try:
        _maintenance_intent = await asyncio.to_thread(
            memory_maintenance.read_intent)
    except (memory_maintenance.IntentUnreadable, OSError):
        # A record that exists but cannot be read is never a normal boot.
        _maintenance_intent = memory_maintenance.unreadable_intent()
    if _maintenance_intent is not None and _maintenance_intent["attempts"]:
        await memory_maintenance.notification_only(_maintenance_intent)
        return

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

    # 2a. The memory store: FTS5 check and schema migration. A forward
    # migration runs only after an automatic export of the file as it is. A
    # failure leaves the file untouched and memory unavailable; Casa boots
    # either way. A pending container-start maintenance (wipe or restore)
    # must be settled before this point: this open is the store's first
    # consumer at boot.
    from memory import capture as memory_capture
    from memory import export as memory_export
    from memory.store import MemoryStore
    memory_store = MemoryStore()
    await asyncio.to_thread(
        memory_store.open, before_migration=memory_export.before_migration)
    # Boot check of the derived files: a missing or unreadable search index
    # is rebuilt from the store, with no model call.
    from memory import search as memory_search
    await asyncio.to_thread(memory_search.ensure, memory_store)
    # Live capture writes here, or to the spool while the store is
    # unavailable or busy.
    memory_capture.configure(store=memory_store)
    # The writer files captured events into the ledgers in the background;
    # its task starts with the timers (step 14).
    from memory import nightly as memory_nightly
    from memory import writer as memory_writer
    from memory.model_port import SdkModelPort
    memory_writer.configure(memory_store, SdkModelPort())

    def _agent_home_dir(role: str) -> str:
        """Resident transcript cwd (encoded-cwd dir for delete_session). Matches agent.py's agent_home WHEN ``config.cwd`` is
        unset — the prod default (all shipped configs have ``cwd: ""``). If a
        resident ever sets a non-empty ``config.cwd``, its transcript lands
        there instead and the reaper/save would look in the wrong dir; keep
        ``config.cwd`` empty for residents. (Formula also duplicated in
        session_sweeper/agent.py — consolidate in a cleanup.)

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
    # #1312: the remembered keyed deliveries — a repeated plugin post is not
    # sent twice, across a restart too
    import delivery_keys
    delivery_keys.init_store(os.path.join(DATA_DIR, "delivery_keys.json"))

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
    # plaintext secret keys from persisted specialist tuple snapshots FIRST.
    # The config repo's whitelist admits no tuple file (#1097), so the boot
    # snapshot below commits none; the scrub heals the live files and does
    # not clean history for a tuple path an operator force-added.
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
    # Engagement trim stage 4: a configurator run the last process did not
    # finish left its lease file; it is removed now, so no run is blocked by
    # it, and announced once after the channels start (below).
    import configurator_run
    interrupted_configurator = configurator_run.claim_interrupted()

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
    # #597: off the loop, like every reload-side refresh (reload.py) and like
    # current_specialist_roles_dir above. The specialist activation now takes
    # MATERIALIZE_LOCK (a threading.Lock) when it rewrites a binding whose
    # role checksum moved for model reasons — and that lock is never acquired
    # on the event loop, boot included. SpecialistRegistry.load is synchronous
    # local file work that publishes by rebinding (#439), built for a worker
    # thread.
    await asyncio.to_thread(specialist_registry.load, roles_dir=str(roles_overlay))

    from engagement_registry import EngagementRegistry
    # #283: the agent-spawn occupancy limiter, constructed BEFORE the
    # registry so load() can restore one token per live marked record
    # (design r3, Sol: registry load used to precede limiter construction —
    # restoration would silently no-op). Shared with tools via init_tools.
    from specialist_limits import AgentSpawnLimiter
    from tools import _AGENT_SPAWN_CAP
    agent_spawn_limiter = AgentSpawnLimiter(max_spawns=_AGENT_SPAWN_CAP)
    engagement_registry = EngagementRegistry(
        tombstone_path=os.path.join(DATA_DIR, "engagements.json"),
        agent_spawn_limiter=agent_spawn_limiter,
    )
    await engagement_registry.load()

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
        _load_agents_at_boot, agents_dir, policies=policy_lib,
        config_dir=CONFIG_DIR,
    )

    specialist_configs = specialist_registry.all_configs()
    # The memory meter reports each served role's view of the ledger, and
    # the nightly pass regroups a ledger that overflows one of them.
    from memory import meter as memory_meter
    memory_meter.configure({
        role: getattr(getattr(cfg, "memory", None), "token_budget", 0)
        for role, cfg in {**role_configs, **specialist_configs}.items()})
    # One ledger per clearance a role is served at (M6): ``private``
    # always, a lower one while a reader at it has a budget (the butler on
    # voice reads ``friends``).
    from memory import ledgers as memory_ledgers
    from sensitivity import clearances_served
    logger.info("memory ledgers maintained: %s", ", ".join(
        memory_ledgers.configure_roles(
            [*role_configs.values(), *specialist_configs.values()],
            clearances_served)))
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
        engagement_registry=engagement_registry,
        agent_registry=agent_registry,
        trigger_registry=trigger_registry,
        mcp_registry=mcp_registry,
        session_registry=session_registry,
        channel_manager=channel_manager,
        bus=bus,
        engagement_driver=None,               # set after step 10 InCasaDriver build
        policy_lib=policy_lib,
        config_dir=CONFIG_DIR,
        agents_dir=agents_dir,
        home_root="/config/agent-home",
        defaults_root="/opt/casa",
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

    # #486: the plugin file handoff folder — provisioned + swept like the
    # outbox, before any turn can publish or capture. Never blocks boot: a
    # failure leaves no folder and producers get handoff_unavailable.
    import casa_handoff
    import plugin_handoff
    await plugin_handoff.wire(scheduler, casa_handoff.root_dir())

    # Browser executor §5.4 (Plan 3 C11): once a minute, ask every plugin that
    # declares a driver sweep which of its runs nothing is driving.
    import plugin_continuations
    plugin_continuations.register_sweep(scheduler)

    # The Telegram channel's default agent. Hoisted above agent construction
    # (#1036) so ONE variable decides both who receives Telegram DMs and who may
    # read the files sent in them — they can never drift apart.
    assistant_role = "assistant"

    # #1036: the inbound-file folder for that agent. Provisioned BEFORE agents
    # are built (each Agent resolves its read grant from it at construction)
    # and before channels go live (its boot pass reclaims staging, which must
    # happen before the upload handler exists). Never blocks boot: a failure
    # leaves the role with no folder and therefore no read grant.
    import agent_inbox
    await agent_inbox.wire(
        scheduler, os.path.join(DATA_DIR, "agent-inbox"), role=assistant_role)
    # S6 §2.3: one inbox per specialist the Telegram default agent declares as a
    # delegate, so a file replied to that specialist's post is stored for IT.
    if assistant_role in role_configs:
        await asyncio.to_thread(
            agent_inbox.provision_delegate_inboxes,
            os.path.join(DATA_DIR, "agent-inbox"), role_configs[assistant_role],
            specialist_roles=set(specialist_configs))

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

    ha_facade = await _start_butler_ha_facade(
        mcp_registry,
        role_configs,
        agents,
        ha_mcp_url=ha_mcp_url,
        supervisor_token=supervisor_token,
    )
    runtime.agents = agents  # share the dict reference; reload handlers mutate this directly.

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
            # Casa-composed notices only (config-sync and the like); a
            # model turn's reply never reaches this bus target (#1038).
            await ch.send(casa_text(str(msg.content)), msg.context)

    bus.register("telegram", _telegram_outbound)

    # Engagement infrastructure: InCasaDriver
    from drivers.in_casa_driver import InCasaDriver

    # Phase 3b: in_casa engagements stream via TopicStreamHandle (per-turn
    # edit-in-place, 1s throttle, mirror the assistant's create_on_token pattern
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
        # S8: the profile a rebuilt session enforces is persisted before it opens.
        persist_plugin_profiles=engagement_registry.update_plugin_profiles,
        result_observer=_specialist_result_observer,
        # #369: last-instant launch gate — start() re-reads the live record
        # before delivering the initial prompt.
        record_lookup=engagement_registry.get,
        # #690: the in_casa half of INV-ENG-009. The registry's SYNCHRONOUS
        # admission decision, called inside the driver's per-turn lock
        # immediately before the client hand-off. Injected so the driver stays
        # importable without a registry.
        begin_turn_delivery=engagement_registry.begin_turn_delivery,
    )

    # Expose on the agent module so tools.emit_completion / cancel_engagement
    # can find it without circular imports.
    import agent as agent_mod
    agent_mod.active_engagement_driver = engagement_driver

    async def _memory_spool_notice(text: str) -> None:
        await operator_notify(channel_manager, text)
    memory_capture.configure(notify=_memory_spool_notice)
    memory_writer.active().notify = _memory_spool_notice
    agent_mod.active_session_registry = session_registry  # #411 — wipe orchestrator input
    runtime.engagement_driver = engagement_driver

    # Stash runtime on agent module so reload handlers and tools find it.
    agent_mod.active_runtime = runtime

    # INV-STATE-005: the private-state mode repair, once per boot, before any
    # channel or agent starts.
    _repair_private_state()

    # The Telegram channel's readiness event: set at its first successful
    # _rebuild, long after this point (start_all() below); a None channel yields
    # no event.
    _telegram_ready = (
        telegram_channel.ready_event if telegram_channel is not None else None)

    # #573: reconcile durable scheduled-ask records. Gated on the Telegram
    # readiness event — every disposition either edits a
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

    if telegram_channel is not None:
        telegram_channel._engagement_registry = engagement_registry
        telegram_channel._engagement_driver = engagement_driver
        telegram_channel._session_registry = session_registry

        async def _driver_send_user_turn(rec, text, *, tg_message_id=None,
                                         inbound_token=None, batch=None):
            # §A3 (Sol r10-2): PROPAGATE the enqueue disposition so
            # _deliver_turn_bg can promote (accepted) vs roll back (rejected)
            # the answered reservation.
            # The in_casa driver has no durable spool / reply-threading (§7
            # follow-up) — the id only keys the message's memory capture; no
            # reservation disposition. #649: thread the seam-admitted ticket
            # so the driver adopts it. #1033: and the job batch number a batch
            # turn carries.
            await engagement_driver.send_user_turn(
                rec, text, tg_message_id=tg_message_id,
                inbound_token=inbound_token, batch=batch)
            return None
        telegram_channel._driver_send_user_turn = _driver_send_user_turn

        # #649: in_casa admission-ticket seam trio. SYNCHRONOUS, taken by the
        # Telegram entry points before their first await.
        def _driver_admit_inbound(rec, text):
            return engagement_driver.admit_inbound(rec.id, text)
        telegram_channel._driver_admit_inbound = _driver_admit_inbound

        def _driver_discharge_inbound(rec, token):
            if token is not None:
                engagement_driver.discharge_inbound(rec.id, token)
        telegram_channel._driver_discharge_inbound = _driver_discharge_inbound

        def _driver_inbound_held(rec, token):
            if token is not None:
                return engagement_driver.inbound_token_held(rec.id, token)
            return False
        telegram_channel._driver_inbound_held = _driver_inbound_held

        # #692: SYNCHRONOUS read of the in_casa follow-up-turn observation for
        # one exact admission ticket. ``getattr``-guarded on purpose — the
        # accessor is deliberately NOT on DriverProtocol, so a driver without
        # it reads as "nothing to report" rather than raising AttributeError
        # into a delivery task that would surface a failure for a healthy
        # turn.
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
        # memory stays reachable through the clearance-gated memory tools.
        _REBUILD_NOTE = (
            "[Context reset: this engagement's earlier working context is no "
            "longer available. Continue from the messages in this topic; use "
            "search_memory if you need prior facts.]"
        )

        async def _driver_invalidate_session(rec):
            await engagement_driver.invalidate_session(rec)

        async def _engagement_context_rebuilder(rec) -> str:
            # Sol diff-gate r1: RE-VERIFY teardown before opening anything
            # fresh (invalidate is idempotent) — the flag is cleared by the
            # caller only after this returns, so a rebuild can never bless a
            # session whose predecessor was not confirmed gone.
            await engagement_driver.invalidate_session(rec)
            await engagement_driver.open_fresh(rec)
            return _REBUILD_NOTE
        telegram_channel._driver_invalidate_session = _driver_invalidate_session
        telegram_channel._engagement_context_rebuilder = (
            _engagement_context_rebuilder)

        async def _finalize_cancel(rec, reason="user"):
            # F2 (whole-branch r2): PROPAGATE _finalize_engagement's bool so the
            # terminal command path can gate the answered-reservation re-anchor
            # suppression on a successful strict terminal transition.
            from tools import _finalize_engagement
            return await _finalize_engagement(
                rec, outcome="cancelled", text=f"Cancelled by {reason}.",
                artifacts=[], next_steps=[],
                driver=engagement_driver,
            )
        telegram_channel._finalize_cancel = _finalize_cancel

        async def _finalize_complete_user(rec):
            # F2 (whole-branch r2): PROPAGATE the finalize bool (see above).
            from tools import _finalize_engagement
            return await _finalize_engagement(
                rec, outcome="completed", text="User-marked complete.",
                artifacts=[], next_steps=[],
                driver=engagement_driver,
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

    # 13b'. #609/#620: retire the per-trigger webhook secrets the declarations
    # no longer entitle, mint the missing ones, and sweep the certified slots
    # of roles whose directory is gone. AFTER registration, so
    # `register_agent`'s cross-role webhook name collision test has already
    # refused a name this role may not have.
    import trigger_reconcile as _trigger_reconcile
    await _boot_reconcile_resident_trigger_secrets(
        role_configs=role_configs, secrets_dir=_trigger_reconcile.SECRETS_DIR,
        agents_dir=os.path.join(CONFIG_DIR, "agents"),
        trigger_registry=trigger_registry,
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

    def _setup_registry_entry_fresh(plugin: str) -> dict | None:
        # #928: a FRESH read of the registry file, deliberately not the cached
        # snapshot `_setup_registry_entry` serves — `plugin_remove` stamps the
        # obligation row before its first await and the snapshot is refreshed
        # only after it, so the cache alone can show a removed plugin as still
        # installed to a sweep overlapping that window. Unreadable or invalid
        # reads as "not listed": the mark is then retained, never consumed.
        import plugin_registry as _pr
        data = _pr.load_registry()
        if not data.valid:
            return None
        entry = next((e for e in data.entries
                      if isinstance(e, dict) and e.get("name") == plugin),
                     None)
        return dict(entry) if entry is not None else None

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

    def _setup_courier_ready(courier: str, specialist: str) -> bool:
        # #1051: a courier turn delegates to the specialist, and the
        # delegation ACL refuses it until the courier's live declarations
        # name that specialist — hold until they do. Late-binding, like the
        # binding check above.
        import tools as _tools
        return _tools.declares_delegate(courier, specialist)

    # #1046: the erase episode dispatches through the same seam — a
    # Casa-authored turn addressed to the configured operator.
    import plugin_erasure as _perase
    _perase.configure(dispatch=_setup_dispatch)

    _pse.configure(
        dispatch=_setup_dispatch, notify_operator=_setup_notify,
        resolve_registry_entry=_setup_registry_entry,
        # #928: the second resolution a removal mark's consumption needs.
        registry_entry_fresh=_setup_registry_entry_fresh,
        # Union BOTH ack stores (trigger + callback identities are
        # disjoint) so a stranded round with a callback member heals.
        ack_lookup=_setup_ack_lookup_union,
        # Gate setup dispatch on BOTH trigger AND callback markers —
        # a callback dark for a non-consent reason contributes no round member,
        # so the trigger gate alone would settle + dispatch with it unrouted.
        routes_live=_callback_and_trigger_routes_live,
        # #803: the APPLIED overlays and their publication generation — read
        # before the recompute and again, yield-free, before the send, so a
        # setup is never dispatched against ingress the sentinel has closed
        # or a publication has changed under the recompute.
        applied_routing=_applied_plugin_routing,
        secrets_ready=_setup_secrets_ready,
        execution_ready=_setup_execution_ready,
        courier_ready=_setup_courier_ready,
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
    # http://127.0.0.1:8099); nothing legitimately connects to 8099 over the
    # hassio bridge, so 0.0.0.0 needlessly exposed it to peer containers.
    site = web.TCPSite(runner, "127.0.0.1", 8099)
    await site.start()
    logger.info("HTTP server listening on 127.0.0.1:8099")

    # Plan 4b/3.6: second AppRunner for the Unix-socket internal API (the
    # result broker's routes and the root-gated /admin/* family).
    internal_runner = await start_internal_unix_runner(
        socket_path="/run/casa/internal.sock",
        runtime=runtime,
    )
    # Track for shutdown.
    runners: list[web.AppRunner] = [runner, internal_runner]

    # 12. Start all channels
    await channel_manager.start_all()

    # 12'. The memory wipe's lines reach the operator from here on. A wipe
    # intent still waiting for its restart (a respawn, or a crash before the
    # restart request) asks for it again now; a wipe the container-start
    # step completed is reported once.
    memory_maintenance.configure(
        notify=telegram_channel.notify_operator
        if telegram_channel is not None else None)
    if _maintenance_intent is not None:
        await memory_maintenance.request_restart(
            kind=_maintenance_intent.get("kind"))
    try:
        _maintenance_line = await asyncio.to_thread(
            memory_maintenance.claim_outcome_notice, memory_store)
    except Exception:  # noqa: BLE001 — a notice must not stop the boot
        logger.warning("memory maintenance line not claimed", exc_info=True)
        _maintenance_line = None
    if _maintenance_line is not None and telegram_channel is not None:
        await telegram_channel.notify_operator(_maintenance_line)

    # #532: the event worker's boot pass (13d'' above) ran BEFORE the
    # channels started, so any operator notice it owed (an exhausted
    # delivery due at boot, a removal note) failed against a not-yet-ready
    # channel. Re-kick now that sends can land — prompt delivery on the
    # common path; the 5-minute event_spool_recovery job is the backstop
    # for every later unready window (e.g. a supervisor rebuild).
    _evep.kick_all()
    # #1014: the setup worker's boot pass ran before the channels started
    # too; a note it could not send then is retained, and this pass sends it
    # again now.
    _pse.kick()

    # 12a. E-F (v0.30.0): engagement-feature setup is now wired into
    # TelegramChannel._rebuild() as a final step after `self._app = app`.
    # Pre-fix, this boot-time call could fire before `_app` was populated
    # if the first `set_webhook` blipped — leaving `engagement_permission_ok`
    # permanently False until manual restart. The new location makes it
    # self-healing on every successful rebuild. Removed redundant call here.

    # 13. Agent loop tasks. H10/H11 (v0.49.0): spawn through the bus so
    # the consumer tasks are tracked — reload reuses the same seam for
    # roles added after boot and cancels tracked tasks on eviction.
    for name in _bus_loop_targets(agents):
        if name in bus.queues:
            loop_tasks.append(bus.start_agent_loop(name))

    # 13b. Restart-orphan notifications come from the recovered durable job
    # failures. Voice jobs remain READY for their delivery coordinator; the
    # compatibility notification below is only for the legacy Telegram route.
    await _notify_recovered_delegations(
        recovered_jobs, job_registry, bus, assistant_role=assistant_role,
    )
    # The interrupted configurator run, once — unless the replay above already
    # tells the operator about that same delegation.
    await configurator_run.announce_interrupted(
        interrupted_configurator,
        recovered_ids={getattr(j, "id", None)
                       for j in recovered_jobs or ()},
        notify=lambda text: operator_notify(channel_manager, text),
        delete=getattr(channel_manager.get("telegram") if channel_manager else None,
                       "delete_dm_message", None))

    # 13b'. #766: and every ENGAGEMENT outcome that still owes its engager a
    # telling. Beside the delegation replay for the same reason it sits here:
    # the obligation is discharged by a resident actually delivering its turn,
    # so the notice has to be enqueued where a resident can run and deliver —
    # after start_all() above and after the agent loops were started. Both
    # existing terminal-record boot walks run far earlier, before either.
    await _notify_recovered_engagement_outcomes(
        engagement_registry, bus, assistant_role=assistant_role,
    )
    # 13b''. #1095 (INV-CFG-013): a specialist disabled while Casa was down
    # never passed a reload's retirement, so its open conversations are closed
    # here — after the channels started (a closure armed in this process is
    # told live, never replayed: INV-ENG-018) and before background jobs
    # resume (a disabled specialist's job must not be resumed).
    left_open = await _close_disabled_specialist_engagements(
        engagement_registry, specialist_registry)
    await _resume_background_jobs(engagement_registry, telegram_channel, skip=left_open)

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
        await _tools_mod._regenerate_plugin_health_guarded(
            then=lambda: notify_plugin_health(channel_manager))
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
    # D-4 (v0.69.0): reap stale engagements FIRST, then run the idle pass —
    # a record past the reap TTL is cancelled outright and must not also be
    # suspended in the same daily run.
    async def _engagement_daily_sweep() -> None:
        # reap resolves the per-record driver itself; no driver arg here.
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
    from background_jobs import sweep_jobs

    scheduler.add_job(
        sweep_jobs,
        args=(engagement_registry, telegram_channel),
        trigger="interval",
        minutes=1,
        id="background_job_sweep",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    # v0.65.0 [AR-8]: every 6 hours, sweep due terminal-engagement topics off
    # the Telegram sidebar (topic_ledger). The job id is historical.
    async def _sweep_workspaces_and_topics() -> None:
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
    # #1162: a finished in_casa engagement's CLI transcripts — same cadence,
    # and first at scheduler start: an interval clock restarts with the
    # process, so an install restarted more often than every six hours would
    # otherwise never reach a pass. The registry was loaded long before here.
    from datetime import datetime as _dt, timezone as _tz
    from engagement_transcript_reaper import reap_engagement_transcripts

    scheduler.add_job(
        reap_engagement_transcripts,
        args=(engagement_registry,),
        trigger="interval",
        id="engagement_transcript_reap",
        hours=6,
        next_run_time=_dt.now(_tz.utc),
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    # INV-ENG-028: a delegated run's transcript is deleted only by this pass
    # (or a wipe) — the session its confirmed row names, once its retention
    # has passed — on the same cadence, first at scheduler start.
    from memory import transcript_runs as _transcript_runs

    async def _delegated_transcript_retention() -> None:
        try:
            await asyncio.to_thread(_transcript_runs.retention_pass,
                                    memory_store)
        except Exception:  # noqa: BLE001 — the next pass retries
            logger.warning("delegated transcript retention pass failed",
                           exc_info=True)

    scheduler.add_job(
        _delegated_transcript_retention,
        trigger="interval",
        id="delegated_transcript_retention",
        hours=6,
        next_run_time=_dt.now(_tz.utc),
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

    # #746 / INV-TRIG-015: the trigger/callback twin of the event self-heal
    # above. Those two overlays fail closed to their own ROUTING_UNAVAILABLE
    # sentinel on a transient compute failure, and every path that reconciles
    # them is human- or agent-initiated — so without this, plugin webhook and
    # callback ingress (and any released setup episode waiting on its route)
    # stayed shut until someone happened to touch the system. One coalesced
    # pass heals BOTH halves from the live runtime, prompt-free; the module
    # docstring carries the lock-order and regeneration reasoning.
    import plugin_routing_recovery
    plugin_routing_recovery.register(scheduler)

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

    # The memory writer's nightly pass only queues writer jobs (condense,
    # regroup, failed-job retries); the writer task runs them.
    scheduler.add_job(
        memory_nightly.run,
        trigger="cron",
        id="memory_nightly",
        hour=memory_nightly.HOUR, minute=memory_nightly.MINUTE,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    memory_writer.start()
    # The nightly export: a verified copy in /data/memory/exports (the last
    # seven kept), so an HA backup carries one.
    scheduler.add_job(
        memory_export.run_nightly,
        trigger="cron",
        id="memory_export_nightly",
        hour=memory_export.HOUR, minute=memory_export.MINUTE,
        args=[memory_store],
        replace_existing=True,
        misfire_grace_time=3600,
    )

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
        runtime=runtime,
        ha_facade=ha_facade,
        bus=bus,
        loop_tasks=loop_tasks,
        channel_manager=channel_manager,
        runners=runners,
    )


async def _shutdown_cleanup(
    *,
    job_registry: Any,
    engagement_registry: Any,
    scheduler: Any,
    session_sweeper: Any,
    runtime: Any,
    ha_facade: Any,
    bus: Any,
    loop_tasks: Any,
    channel_manager: Any,
    runners: Any,
) -> None:
    """Casa's graceful stop, from the stop signal to "shutdown complete".

    Extracted from ``main`` under #698, and the extraction is a gain rather
    than a refactor for its own sake: this block was executed by no test in the
    repository — ``job_registry.begin_shutdown()``'s call site has been unpinned
    since #671 — so every claim any invariant made about "a graceful stop"
    rested on a sequence nothing ran. It is now a production coroutine a red
    case can drive end to end.

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
    # cause and its reporter is killed mid-notice.
    # #882: declare the graceful stop to the turn path BEFORE anything can
    # await — synchronously, so no turn can be admitted in the window between
    # entering this coroutine and the declaration. From here a turn the pool
    # refuses because it is CLOSING is refused rather than served on the
    # unpooled bypass, which would start a CLI subprocess after the stop began
    # and answer onto channels this routine closes a few steps later. It is
    # deliberately not job_registry's flag (that one is the ledger's) and not
    # per-Agent state: a reload-retired generation still serving a dispatched
    # turn, and a specialist installed after this point, must both see it.
    import agent as _agent_mod
    _agent_mod.begin_process_stop()

    import tools as _tools_mod
    await _tools_mod.stop_engagement_launches(engagement_registry)

    # #767 (INV-JOB-011), first of two: settle every delegation whose verdict
    # had already landed when the stop began — write its terminal and enqueue
    # its announcement HERE, while the bus consumers, every resident's SDK
    # pool and the channels are still up, so a resident can still tell it
    # rather than leaving it for the next boot's replay. Left un-awaited, the
    # tail is killed by `asyncio.run`'s final sweep before its write and the
    # SUCCEEDED delegation boots as "lost on restart" — the opposite of what
    # INV-JOB-009 publishes. Nothing here waits on a delegated RUN.
    # Browser executor (Plan 3 C8): continuations still waiting or running are
    # cancelled first — the driver sweep re-finds their runs after the boot.
    import plugin_continuations
    plugin_continuations.stop_sweep()              # slice 5k: no sweep pass after the stop
    await plugin_continuations.CONTINUATIONS.drain()
    import plugin_stop
    await plugin_stop.drain()                       # a stop call still tearing down its stdio
    await _tools_mod.drain_delegation_settlements()

    # 17. Cleanup
    logger.info("Shutting down...")
    scheduler.shutdown(wait=False)
    await session_sweeper.stop()
    # NOTE: deliberately do NOT close the plugin outbox here. Its dir-FDs are
    # O_CLOEXEC and process-lived (the OS reclaims them on exit); closing the
    # live singleton during shutdown — before HTTP ingress + agent turns are
    # drained below — would let an in-flight send_media fall through to a
    # dir_fd=None (CWD-relative) op. close() is for test teardown only.

    # AR-9: close every resident/specialist Agent's SDK client pool so no warm
    # subprocess outlives container shutdown. Bounded per-agent so one hung
    # drain can't block the rest of the shutdown sequence.
    #
    # #881, CONCURRENTLY: the bound is per agent, so a serial loop spends up to
    # 15 s EACH and the close portion of the stop grows with the number of
    # specialists — and a cancelled close now also finishes its own bounded
    # cut, which would multiply the same way. Started together and gathered,
    # the close portion is one window whatever the fleet size, well inside the
    # container's own stop timeout. The position is unchanged: still before
    # bus.begin_shutdown() below, still every agent, still one bound and one
    # warning each.
    #
    # The bound this step actually spends, stated because 15 is only half of
    # it: the wait_for cancels the close at 15 s, and a cancelled pool close
    # then finishes its own bounded forced cut (SALVAGE_TIMEOUT) before it
    # propagates — so the wait_for returns at up to 15 s + that window. Run
    # concurrently, that sum is what the step costs for the WHOLE fleet, and it
    # is well inside the container's own stop timeout (`casa/config.yaml`).
    async def _close_agent(role, agent) -> None:
        aclose = getattr(agent, "aclose", None)
        if aclose is None:
            return
        try:
            await asyncio.wait_for(aclose(), timeout=15)
        except Exception:  # noqa: BLE001 — shutdown must complete
            logger.warning("agent %s aclose failed/timed out", role)

    _agent_closes = [
        _close_agent(_role, _agent)
        for _role, _agent in list(getattr(runtime, "agents", {}).items())
    ]
    if _agent_closes:
        await asyncio.gather(*_agent_closes)
    await _close_butler_ha_facade(ha_facade)

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
    # unix socket). runner.cleanup() drains in-flight requests then closes the
    # listener, so once it returns no webhook/voice/internal inbound can reach
    # an agent.
    for _r in runners:
        await _r.cleanup()

    # #767 (INV-JOB-011), second of two — the ledger-close boundary: every
    # delegation whose verdict landed DURING the stop (in the bounded aclose
    # window above, or later, from a run that outlived it) is settled now,
    # after every ingress surface is quiesced and before `close()` cancels
    # the reconciliation retries a failed write would have relied on. Its
    # notice lands in a queue whose consumer is gone (the put never blocks)
    # and rides the durable marker to the next boot (INV-JOB-010). There is no
    # await between this returning and `close()` being entered. A run still
    # running here is left live for the boot reconciliation (INV-JOB-009).
    await _tools_mod.drain_delegation_settlements()

    # No new ingress can bind a job now. Cancel/wait process-local ownership;
    # each task's done callback remains the sole concurrency-permit releaser.
    try:
        await job_registry.close()
    except Exception:  # noqa: BLE001 — shutdown must complete
        logger.warning("job registry close failed", exc_info=True)

    # #895 (INV-CONC-006): the stop cancels and gathers the bus's CONSUMER
    # tasks; every turn runs in a DISPATCH task, in a map nothing above reads.
    # So this line used to be written while turns the process had admitted were
    # still running, with `asyncio.run`'s final sweep their only remaining
    # owner. It still is — nothing here awaits or cancels them, deliberately:
    # every delivery surface is already closed and `fail_pending` above cleared
    # `pending`, so a drain would recover no answer, and a cancel would push a
    # "flush what we have" path into turn tails whose writes are atomic today.
    # What changes is that the record says so. Counted HERE, after the last
    # cleanup await and with no await before the log call, because turns finish
    # during the broker drain, the ingress cleanup and the delegation settle:
    # the honest number is the one true at the instant the line is written.
    _abandoned = len(bus.live_dispatch_tasks())
    logger.info(
        "Casa core shutdown complete (%d dispatched turn(s) still running)",
        _abandoned,
        extra={"event": "casa_shutdown_complete",
               "abandoned_turns": _abandoned,
               "abandoned_reason": "neither awaited nor cancelled by the stop; "
                                   "any answer they produce is discarded"},
    )


def run() -> None:
    """Synchronous entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
