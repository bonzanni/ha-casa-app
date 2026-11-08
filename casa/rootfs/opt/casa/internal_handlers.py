# casa/rootfs/opt/casa/internal_handlers.py
"""Internal HTTP handlers -- bound to the casa-main Unix socket
(/run/casa/internal.sock).

This module holds the root-gated ``/admin/*`` handlers (casactl reload and
the memory wipe/export/restore doors) and the SO_PEERCRED middleware that
gates them.
"""

from __future__ import annotations

import asyncio
import logging
import socket as _socket
import struct as _struct
from typing import Awaitable, Callable

from aiohttp import web

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# #467 — caller-identity gate for /admin/* on the internal Unix socket
# ---------------------------------------------------------------------------
#
# The internal socket serves the non-root /internal/* family (the broker
# routes) and the operator-only /admin/* family (casactl reload + the
# personality-admin inspection routes). The /admin/* family has exactly one
# legitimate caller — casactl, always run by the operator as root — so no
# non-root path reaches /admin/* legitimately.
#
# SO_PEERCRED reports the connecting peer's credentials as recorded by the
# kernel at connect() time on an AF_UNIX stream socket; an unprivileged client
# cannot forge it. We reject any /admin/* request whose peer is not uid 0, and
# fail CLOSED when the credentials cannot be read (no transport, no socket, or
# getsockopt error) — an unreadable identity is not a root identity.

# struct ucred is {pid_t pid; uid_t uid; gid_t gid;} — three native ints.
_UCRED_FMT = "3i"
_UCRED_SIZE = _struct.calcsize(_UCRED_FMT)


def _peer_uid(request: web.Request) -> int | None:
    """Return the connecting peer's uid via SO_PEERCRED, or None if unknown.

    None signals "identity unavailable" and is treated as non-root by the
    gate (fail-closed). Only meaningful for AF_UNIX stream sockets.
    """
    transport = request.transport
    if transport is None:
        return None
    sock = transport.get_extra_info("socket")
    if sock is None:
        return None
    try:
        raw = sock.getsockopt(
            _socket.SOL_SOCKET, _socket.SO_PEERCRED, _UCRED_SIZE)
    except (OSError, AttributeError, ValueError):
        return None
    try:
        _pid, uid, _gid = _struct.unpack(_UCRED_FMT, raw)
    except _struct.error:
        return None
    return uid


@web.middleware
async def admin_peercred_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Gate /admin/* on the internal socket to a root (uid 0) peer.

    Non-/admin/* routes pass through untouched. Only a
    genuinely-matched admin route is gated: an unmatched /admin/* path keeps
    its 404 (the route table is public, so there is nothing to hide, and a
    stray 403 would only mask a real not-found).
    """
    matched = getattr(request.match_info, "http_exception", None) is None
    if matched and request.path.startswith("/admin/"):
        uid = _peer_uid(request)
        if uid != 0:
            logger.warning(
                "internal socket: refusing %s %s from peer uid=%r "
                "(root required)", request.method, request.path, uid,
            )
            return web.json_response(
                {"status": "error", "kind": "forbidden",
                 "message": "admin routes require a root caller"},
                status=403,
            )
    return await handler(request)


# ---------------------------------------------------------------------------
# /admin/reload handler factory (Task E.1, granular-reload plan)
# ---------------------------------------------------------------------------


def build_admin_memory_wipe_handler():
    """The terminal door — POST /admin/memory/wipe on the internal unix
    socket, root-gated by the same SO_PEERCRED middleware as every
    ``/admin/*`` route (#467). Consent here is being root in the gated
    terminal PLUS an explicit ``{"confirm": true}`` in the body (casactl
    sends it only under ``--yes``). On consent it records the wipe intent and
    asks the Supervisor for the app restart; the wipe runs at the next
    container start. The response carries the lines the operator would read
    in Telegram."""
    async def handler(request: web.Request) -> web.Response:
        from memory import maintenance as memory_maintenance
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            return web.json_response(
                {"status": "error", "kind": "confirm_required",
                 "message": "POST {\"confirm\": true} to wipe memory"},
                status=400,
            )
        lines: list[str] = []

        async def _collect(line: str) -> None:
            lines.append(line)

        try:
            # A restart that fails after this answer is told on Telegram.
            state = await memory_maintenance.request_wipe(reply=_collect)
        except Exception as exc:  # noqa: BLE001 — report truthfully
            logger.exception("memory wipe could not be scheduled")
            return web.json_response(
                {"status": "error", "kind": "not_scheduled",
                 "message": f"{exc} — nothing was deleted"},
                status=500,
            )
        if state == "restore_pending":
            return web.json_response(
                {"status": "error", "kind": "restore_pending",
                 "message": " ".join(lines)},
                status=409,
            )
        return web.json_response({
            "status": "ok", "state": state, "message": " ".join(lines),
        })
    return handler


def build_admin_memory_export_handler():
    """POST /admin/memory/export (``casactl memory-export``), root-gated like
    every ``/admin/*`` route. Drains the capture spool, then writes one
    verified export of the live store to ``/data/memory/exports/`` and
    answers with its directory and row counts. One export runs at a time."""
    async def handler(request: web.Request) -> web.Response:
        from memory import capture as memory_capture
        from memory import export as memory_export
        store = memory_capture.active_store()
        if store is None or not store.available:
            return web.json_response(
                {"status": "error", "kind": "unavailable",
                 "message": "memory is unavailable, so there is nothing to "
                            "export"},
                status=503,
            )
        try:
            path = await asyncio.to_thread(memory_export.export, store)
        except memory_export.ExportBusy as exc:
            return web.json_response(
                {"status": "error", "kind": "busy", "message": str(exc)},
                status=409,
            )
        except memory_export.ExportFailed as exc:
            return web.json_response(
                {"status": "error", "kind": "failed",
                 "message": f"{exc}; nothing was published"},
                status=500,
            )
        manifest = await asyncio.to_thread(memory_export.read_manifest, path)
        return web.json_response({
            "status": "ok", "path": path,
            "counts": manifest["counts"]["tables"],
        })
    return handler


def build_admin_memory_restore_handler():
    """POST /admin/memory/restore (``casactl memory-restore``), root-gated
    like every ``/admin/*`` route. Body ``{"path": <export directory or a
    name in /data/memory/exports>, "confirm": bool}``. The export is
    validated at once; without ``"confirm": true`` the answer is the
    confirmation text and nothing is scheduled. With it, the export is
    staged, the restore intent written and the app restart requested; the
    restore runs at the next container start."""
    async def handler(request: web.Request) -> web.Response:
        from memory import restore as memory_restore
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict) or not isinstance(
                payload.get("path"), str) or not payload["path"].strip():
            return web.json_response(
                {"status": "error", "kind": "bad_request",
                 "message": "POST {\"path\": <export directory>}"},
                status=400,
            )
        lines: list[str] = []

        async def _collect(line: str) -> None:
            lines.append(line)

        try:
            state = await memory_restore.request_restore(
                payload["path"].strip(), reply=_collect,
                confirm=payload.get("confirm") is True)
        except Exception as exc:  # noqa: BLE001 — report truthfully
            logger.exception("memory restore could not be scheduled")
            return web.json_response(
                {"status": "error", "kind": "not_scheduled",
                 "message": f"{exc} — nothing was scheduled"},
                status=500,
            )
        message = " ".join(lines)
        if state == "refused":
            return web.json_response(
                {"status": "error", "kind": "refused", "message": message},
                status=409,
            )
        if state == "needs_confirm":
            return web.json_response(
                {"status": "confirm_required", "message": message},
                status=400,
            )
        return web.json_response(
            {"status": "ok", "state": state, "message": message})
    return handler


def build_admin_reload_handler(*, runtime):
    """Factory -- returns an aiohttp handler that dispatches reload calls.

    Used by the internal-socket aiohttp app (registered in
    ``casa_core.start_internal_unix_runner``). Operator CLI ``casactl``
    POSTs to ``/admin/reload`` over the unix socket; same dispatch path
    as the ``casa_reload(scope=...)`` MCP tool.

    If ``runtime`` is None at registration time, the handler falls back
    to ``agent.active_runtime`` at request time. This handles the case
    where the route is registered before ``casa_core.main`` has bound
    ``active_runtime`` (boot ordering).

    ``reload.dispatch`` is looked up per-request (not at factory time)
    so tests can monkeypatch ``reload.dispatch`` after the route has
    been registered.
    """
    async def handler(request: web.Request) -> web.Response:
        import reload as reload_mod
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "kind": "bad_json",
                 "message": "POST body must be JSON"},
                status=400,
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"status": "error", "kind": "bad_json",
                 "message": "POST body must be a JSON object"},
                status=400,
            )

        scope = (payload.get("scope") or "").strip()
        if not scope:
            return web.json_response(
                {"status": "error", "kind": "scope_required",
                 "message": "missing 'scope' field"},
                status=400,
            )
        role_raw = payload.get("role")
        role = role_raw.strip() if isinstance(role_raw, str) else None
        if role == "":
            role = None
        include_env = bool(payload.get("include_env", False))

        active = runtime
        if active is None:
            import agent as agent_mod
            active = getattr(agent_mod, "active_runtime", None)
        if active is None:
            return web.json_response(
                {"status": "error", "kind": "not_initialized",
                 "message": "CasaRuntime not bound"},
                status=500,
            )

        # Task 10 manual-reload fencing (spec §3.1, ENTRY-POINT-ONLY): the
        # /admin/reload route is the second reload entry point alongside the
        # casa_reload tool. A reload that reaches plugin state must acquire
        # _PLUGIN_TOOLS_LOCK BEFORE dispatch — never inside reload.py beneath
        # its writer/reader lock (AB/BA deadlock). Global order everywhere:
        # _PLUGIN_TOOLS_LOCK -> reload writer/reader lock.
        # #489: the GUARD, not the raw lock — reload_full's include_env arm
        # reaches the plugin_env handler, whose health block re-enters via
        # the same guard.
        # #706: the fenced set is `full`, `executors` and `plugin_env`, and it
        # is decided ONCE, in tools._plugin_tools_reload_guard — this route and
        # the tool used to classify separately, and both classified only `full`.
        import tools as tools_mod
        async with tools_mod._plugin_tools_reload_guard(scope):
            result = await reload_mod.dispatch(
                scope, runtime=active, role=role, include_env=include_env)
        # #824: a reload that ran the paired plugin reconcile re-derives the
        # health report afterwards, so a `trigger_routing_unavailable` row
        # sampled from its own transient marker does not outlive it. After the
        # fence, for the reason the tool entry point states: the reload RW lock
        # is released either way, and the regeneration settles through
        # cancellation in a task of its own, which cannot re-enter a guard the
        # request handler's task holds.
        await tools_mod._regenerate_plugin_health_after_reload(scope, result)
        status_code = 200 if result.get("status") == "ok" else 500
        return web.json_response(result, status=status_code)

    return handler
