# casa/rootfs/opt/casa/internal_handlers.py
"""Internal HTTP handlers -- bound to the casa-main Unix socket
(/run/casa/internal.sock), consumed by svc-casa-mcp.

Body shape (no JSON-RPC envelope, no header dependency):

    POST /internal/tools/call
    {
      "name": "<tool_name>",
      "arguments": {...},
      "engagement_id": "<uuid>" | null
    }

    POST /internal/hooks/resolve
    {
      "policy": "<policy_name>",
      "payload": {...},           # CC PreToolUse payload
      "engagement_id": "<32-hex>" | null,   # #366: from X-Casa-Engagement-Id
      "engagement_token": "<token>" | null  # #366: from X-Casa-Engagement-Token
    }

Responses are bare (no JSON-RPC wrapping):
- tools/call success: {"content": [...]}                          (tool's own shape)
- tools/call known error: {"error": {"code": -32xxx, "message": ...}}
- hooks/resolve allow: {}
- hooks/resolve deny:  {"hookSpecificOutput": {...}}              (CC-native)

The svc-casa-mcp service wraps tools/call results in JSON-RPC envelopes
(adds {"jsonrpc": "2.0", "id": ..., "result": ...} or .error), and on
ClientConnectorError to the Unix socket returns -32000 casa_temporarily_unavailable.
The hook responses are pass-through (CC's hook protocol is already body-only).
"""

from __future__ import annotations

import hmac
import logging
import re as _re
import socket as _socket
import struct as _struct
from typing import Any, Awaitable, Callable

from aiohttp import web

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# #467 — caller-identity gate for /admin/* on the internal Unix socket
# ---------------------------------------------------------------------------
#
# The internal socket serves both the engagement-forwarded family
# (/internal/tools/call, /internal/hooks/resolve, /internal/channel/*) and the
# operator-only /admin/* family (casactl reload + the personality-admin
# inspection routes). The forwarded family is authenticated per-engagement one
# layer up (the svc-casa-mcp bridge grant-gate) and MUST stay reachable by the
# non-root forwarder; the /admin/* family has exactly one legitimate caller —
# casactl, always run by the operator as root. The 8100 MCP forwarder never
# forwards /admin/* (it is not in its route allowlist), and the casa_reload
# tool dispatches through /internal/tools/call, so no non-root path reaches
# /admin/* legitimately.
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

    Non-/admin/* routes pass through untouched — the forwarded family is
    authorized per-engagement elsewhere and its forwarder is not root. Only a
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


def engagement_auth_ok(rec: Any, presented: Any) -> bool:
    """#335: does ``presented`` prove authority over engagement record ``rec``?

    The engagement id alone is NOT an authenticator — it is baked into the
    workspace ``.mcp.json``, appears in logs/topic metadata, and the MCP
    endpoint plus the internal socket are reachable from any in-container
    process, so a lower-privileged engagement could present another
    engagement's id and inherit its role authority. Authority therefore
    requires the per-engagement secret ``auth_token`` generated at record
    creation and provisioned ONLY into that engagement's own workspace.

    Fail-closed on every edge: a record without a token (impossible for
    registry-created/loaded records — ``create()`` generates one and
    ``load()`` backfills — but reachable with a hand-built record) matches
    nothing, and a missing/non-string presented token matches nothing.
    Constant-time compare so the token is not oracle-recoverable.

    Both sides are type- and ASCII-checked BEFORE the compare: a corrupt
    tombstone row (``"auth_token": 123``) or a non-ASCII value would make
    ``hmac.compare_digest`` raise ``TypeError``, turning a fail-closed
    rejection into a 500 — an authentication check must refuse, never crash
    (Terra, review r1).
    """
    expected = getattr(rec, "auth_token", "") or ""
    if not isinstance(expected, str) or not expected.isascii() or not expected:
        return False
    if not isinstance(presented, str) or not presented.isascii() or not presented:
        return False
    return hmac.compare_digest(expected, presented)

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
HookCallback = Callable[[dict[str, Any], Any, dict], Awaitable[dict | None]]

# v0.74.2 (live finding 2026-07-13): tools that bind the engagement record
# even when it is TERMINAL. A duplicate/racing emit_completion delivery that
# lands after the record flips completed must reach the tool's own
# idempotency check (`already_terminal`) instead of getting a lying
# `not_in_engagement` from the active-only gate. Everything else keeps the
# defense-in-depth active-only rule — a finished engagement's leftover CLI
# must not retain authority over privileged tools.
_TERMINAL_BINDING_TOOLS = frozenset({"emit_completion"})


# ---------------------------------------------------------------------------
# tools/call handler factory
# ---------------------------------------------------------------------------


def _make_internal_tools_call_handler(
    *,
    tool_dispatch: dict[str, ToolHandler],
    engagement_registry: Any,
):
    """Build the aiohttp POST handler for /internal/tools/call.

    `tool_dispatch` is a {name -> async-callable} map; in casa-main this is
    built from `tools.CASA_TOOLS` at startup and passed in. Tests inject
    a smaller fake.

    `engagement_registry` is used to look up records by id when the body
    carries `engagement_id`. Bound records with status == "active" populate
    `tools.engagement_var`; other states (or missing record) bind None —
    EXCEPT the _TERMINAL_BINDING_TOOLS allowlist (emit_completion), whose
    terminal records still bind so retries reach the idempotency check
    (v0.74.2).

    #587: the two ways a call can be refused are reported distinctly —
    -32006 `engagement_not_live` when a caller authenticated against a known
    record that is not bindable, -32004 `tool_not_granted` when a bound
    engagement lacks the grant (and for an unbound caller, which has no
    record to describe).
    """
    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"code": -32700, "message": "Parse error"}}
            )

        if not isinstance(body, dict):
            return web.json_response(
                {"error": {"code": -32600, "message": "Invalid Request"}}
            )

        name = body.get("name")
        # #380: any non-object arguments — truthy or falsy — is refused
        # with a typed error rather than forwarded (truthy) or silently
        # coerced to an empty call (falsy). Absent/null defaults to {}.
        arguments = body.get("arguments")
        if arguments is None:
            arguments = {}
        eng_id = body.get("engagement_id")

        if not isinstance(name, str):
            return web.json_response(
                {"error": {"code": -32602, "message": "missing name"}}
            )
        if not isinstance(arguments, dict):
            return web.json_response(
                {"error": {"code": -32602,
                           "message": "arguments must be an object"}}
            )

        fn = tool_dispatch.get(name)
        if fn is None:
            return web.json_response(
                {"error": {"code": -32602, "message": f"Unknown tool: {name}"}}
            )

        # Resolve engagement record. #335: an id claim binds ONLY with the
        # matching per-engagement auth token — the id alone is client-supplied
        # and shared-endpoint-visible, so a mismatch is an explicit REJECT
        # (never a silent unauthenticated fallthrough that would let a forger
        # keep probing tools as "not in engagement"). Then defense-in-depth:
        # only bind when status is still active (mirrors v0.13.1
        # mcp_bridge._dispatch_tool_call) — EXCEPT the _TERMINAL_BINDING_TOOLS
        # allowlist (v0.74.2), so a duplicate/racing emit_completion delivery
        # gets the honest `already_terminal` from the tool's idempotency check.
        engagement = None
        # #587: the record's status, set ONLY when the caller proved possession
        # of a known record's token and the record still did not bind — a
        # refusal the grant-gate below would otherwise report as a missing
        # grant, which is a lie about a record that may hold it. Captured here
        # rather than re-read later so the branch cannot depend on whether
        # `rec` was ever assigned.
        unbound_status: str | None = None
        authenticated_unbound = False
        if eng_id:
            try:
                rec = engagement_registry.get(eng_id)
            except Exception:  # noqa: BLE001
                rec = None
            if rec is not None:
                if not engagement_auth_ok(rec, body.get("engagement_token")):
                    logger.warning(
                        "internal /tools/call: rejected engagement id claim "
                        "for %s (tool=%r): missing/invalid engagement token",
                        str(eng_id)[:8], name,
                    )
                    return web.json_response(
                        {"error": {"code": -32003,
                                   "message": "engagement_auth_failed: "
                                              "invalid engagement token"}}
                    )
                # #369: while a clearance downgrade's context rebuild is
                # pending, the OLD in-flight process may still be issuing
                # calls — refuse them all (fail closed) rather than let it
                # launch new work (delegate/engage children inherit its
                # above-floor task text verbatim) or read on its behalf.
                # Terminal-binding tools stay reachable: a completion's
                # output is in-flight-turn residual, and its idempotency
                # check needs the record.
                if (getattr(rec, "context_rebuild_pending", False)
                        and name not in _TERMINAL_BINDING_TOOLS):
                    logger.warning(
                        "internal /tools/call: refused %r for engagement %s: "
                        "context rebuild pending after clearance downgrade",
                        name, str(eng_id)[:8],
                    )
                    return web.json_response(
                        {"error": {"code": -32005,
                                   "message": "engagement_context_rebuilding: "
                                              "retry shortly"}}
                    )
                if (getattr(rec, "status", None) == "active"
                        or name in _TERMINAL_BINDING_TOOLS):
                    engagement = rec
                else:
                    authenticated_unbound = True
                    unbound_status = getattr(rec, "status", None)

        # #587: an authenticated record that did not BIND is a different
        # refusal from "bound, but not granted this tool", and answering both
        # with `tool_not_granted` describes the first one falsely — the record
        # may well hold the grant; what it is not is LIVE. Binding requires
        # `status == "active"`, so this is the state a caller reaches when its
        # engagement has gone terminal (or, before #588's delivery admission,
        # when a restart left it `idle`) while a turn was still in flight. The
        # refusal itself is unchanged and still fails closed; only its
        # attribution improves, and that attribution cost real investigation
        # time twice while #585 was being diagnosed. Distinct code, following
        # `context_rebuild_pending`'s -32005 precedent. An UNBOUND caller (no
        # id, or an id the registry cannot resolve) keeps -32004: there is no
        # record to describe, and that is the fail-closed case INV-MCP-001
        # states. Terminal-binding tools never arrive here — a terminal record
        # binds for them.
        if authenticated_unbound and name not in _TERMINAL_BINDING_TOOLS:
            status = unbound_status
            logger.warning(
                "internal /tools/call: refused %r for engagement %s: record is "
                "not live (status=%r) — the tool was not run; this is NOT a "
                "grant failure", name, str(eng_id)[:8], status,
            )
            return web.json_response(
                {"error": {"code": -32006,
                           "message": "engagement_not_live: the engagement is "
                                      f"{status!r}, not active"}}
            )

        # v0.166.0 bridge grant-gate: dispatch only a tool the AUTHENTICATED
        # engagement is actually granted, and fail CLOSED when no active
        # engagement is bound. The executor's own (root) shell can reach this
        # socket directly with the workspace token, bypassing the CLI-side
        # allowlist that used to be the "before dispatch" enforcement — so the
        # check must live here. This deliberately inverts the old INV-MCP-001.
        # Terminal-binding tools stay exempt: an emit_completion retry may
        # legitimately arrive unbound (INV-MCP-004's terminal path).
        if name not in _TERMINAL_BINDING_TOOLS:
            from tools import engagement_casa_grant_names
            granted = engagement_casa_grant_names(engagement)
            # None = a server-level casa-framework grant → any casa tool allowed.
            if granted is not None and name not in granted:
                logger.warning(
                    "internal /tools/call: tool %r not granted to engagement "
                    "%s; rejecting", name,
                    (str(eng_id)[:8] if eng_id else "<unbound>"),
                )
                return web.json_response(
                    {"error": {"code": -32004,
                               "message": f"tool_not_granted: {name}"}}
                )

        # Lazy import so monkeypatching `tools.engagement_var` in tests works.
        from tools import engagement_var

        token = engagement_var.set(engagement)
        try:
            result = await fn(arguments)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "internal /tools/call: tool %r raised: %s", name, exc,
            )
            # Distinct code from -32000 (used by svc for socket-down).
            return web.json_response(
                {"error": {"code": -32001,
                           "message": f"Tool {name!r} raised: {exc}"}}
            )
        finally:
            engagement_var.reset(token)

        return web.json_response(result)

    return handler


# ---------------------------------------------------------------------------
# hooks/resolve handler factory
# ---------------------------------------------------------------------------


def _make_internal_hooks_resolve_handler(
    *,
    hook_policies: dict[str, tuple[str, HookCallback]],
    executor_hook_policies: dict | None = None,
    engagement_registry=None,
):
    """Build the aiohttp POST handler for /internal/hooks/resolve.

    `hook_policies` is the {name -> (matcher_regex, async_callback)} dict
    produced by `casa_core._build_cc_hook_policies(HOOK_POLICIES)` — the
    default-configured fallback callbacks.

    H3 (v0.53.0): `executor_hook_policies` (built by
    `casa_core._build_executor_cc_hook_policies`) is
    ``{executor_type: {policy_name: (matcher, callback)}}`` carrying the
    per-executor ``hooks.yaml`` parameters. #366: the handler resolves the
    engagement ONLY from the body's ``engagement_id``/``engagement_token``
    pair, verified against the record via :func:`engagement_auth_ok`; the CC
    payload's ``cwd`` is caller-supplied text used solely as a cross-check
    (an authenticated id contradicting a cwd engagement claim is refused).
    It prefers the authenticated executor's parameterised callback for the
    policy and falls back to the default `hook_policies` callback for
    unauthenticated requests and unknown policies. #442 r3: an AUTHENTICATED
    request naming an executor the per-executor map does not represent is
    **refused** for any policy in ``HOOK_POLICIES`` — falling back there would
    enforce the defaults, whose ``casa_config_guard`` forbids no write path at
    all, in place of what the operator declared. The relay and buttons
    reminder are wired separately and have no factory, so they are absent from
    every per-executor map by construction and still fall back: a broken
    configuration can still ask, it just cannot pass a guard. It threads
    ``{"casa_engagement_id": <id-or-None>}`` to the callback as the
    authenticated-identity context. Both kwargs default to None so existing
    call sites (and tests) keep the original behaviour.
    """
    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        "internal/hooks/resolve: malformed JSON",
                }},
            )

        if not isinstance(body, dict):
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        "internal/hooks/resolve: body must be a JSON object",
                }},
            )

        policy_name = body.get("policy")
        if not isinstance(policy_name, str):
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        f"unknown policy: {policy_name!r}",
                }},
            )
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        "internal/hooks/resolve: payload must be a JSON object",
                }},
            )

        # #366: authenticate any engagement-identity claim BEFORE deriving
        # anything from it. Identity arrives as body fields injected from the
        # X-Casa-Engagement-Id/Token headers (svc-casa-mcp rebuilds the body
        # from headers; hook_proxy.sh reads the credential from its OWN
        # workspace .mcp.json). The payload's cwd is caller-supplied text: it
        # is never an identity source, only a cross-check. Contract mirrors
        # /internal/tools/call (#335):
        #   known id + valid token  -> authenticated (identity threaded below)
        #   known id + bad/missing  -> explicit REJECT, callback never runs
        #   unknown id / no id      -> unauthenticated (default policies;
        #                              identity-consuming callbacks fail closed)
        eng_id_claim = body.get("engagement_id")
        auth_rec = None
        auth_eng_id = None
        if eng_id_claim:
            rec = None
            if engagement_registry is not None:
                try:
                    rec = engagement_registry.get(eng_id_claim)
                except Exception:  # noqa: BLE001
                    rec = None
            if rec is not None:
                if not engagement_auth_ok(rec, body.get("engagement_token")):
                    logger.warning(
                        "internal /hooks/resolve: rejected engagement id "
                        "claim for %s (policy=%r): missing/invalid "
                        "engagement token", str(eng_id_claim)[:8], policy_name,
                    )
                    return web.json_response(
                        {"hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason":
                                "engagement_auth_failed: invalid engagement "
                                "token — the tool was not run",
                        }},
                    )
                auth_rec = rec
                auth_eng_id = str(eng_id_claim)

        # Terra r1: a non-string truthy cwd (list, dict) must not raise in
        # the regex — that TypeError escaped the deny wrapper as a 500,
        # which the shim's transport fail-open converts into an ALLOW.
        # cwd is advisory, never identity: malformed ⇒ treated as absent.
        from hooks import _engagement_id_from_cwd
        raw_cwd = payload.get("cwd")
        cwd_id = _engagement_id_from_cwd(
            raw_cwd if isinstance(raw_cwd, str) else "")
        if (auth_eng_id is not None and cwd_id is not None
                and cwd_id != auth_eng_id):
            logger.warning(
                "internal /hooks/resolve: authenticated engagement %s "
                "presented a cwd claiming %s — refusing",
                auth_eng_id[:8], cwd_id[:8],
            )
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        "engagement cwd does not match the authenticated "
                        "engagement — the tool was not run",
                }},
            )

        # H3 (v0.53.0): prefer the engagement's executor-specific callback
        # (carrying its hooks.yaml params); since #366 the engagement resolves
        # ONLY from the authenticated credential, never from the cwd claim.
        entry = None
        if executor_hook_policies is not None and auth_rec is not None:
            # #442 r3: absence is NOT "use the defaults". Three review rounds
            # each found a different way for an executor to go missing from
            # this map — its hooks.yaml failed to build, it failed to LOAD so
            # the registry published nothing, the whole executor directory
            # failed to scan so no type name survived to be marked failed —
            # and every one of them fell through, per policy, to defaults
            # whose casa_config_guard forbids no write path at all. The list
            # of ways to go missing is not enumerable at the builder, so the
            # decision lives here, where it is used: an authenticated
            # engagement naming an executor this map does not represent is
            # refused. A legitimate no-parameters executor is represented
            # positively (hooks.UsesDefaultPolicies) and still falls back.
            from hooks import HOOK_POLICIES
            role_or_type = getattr(auth_rec, "role_or_type", "")
            declared = executor_hook_policies.get(role_or_type)
            # r4 (both reviewers): scope the refusal to the GUARD policies.
            # engagement_permission_relay and engagement_buttons_reminder have
            # no factory and are wired separately with live dependencies, so
            # they are absent from every per-executor map by construction —
            # including the deny-all one. Refusing them here would make a
            # broken configuration unable to ASK as well as unable to act,
            # and asking is not an enforcement decision.
            if declared is None and policy_name in HOOK_POLICIES:
                logger.error(
                    "internal /hooks/resolve: executor %r has no hook-policy "
                    "entry — refusing rather than enforcing the defaults",
                    role_or_type,
                )
                return web.json_response(
                    {"hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason":
                            "hook enforcement is unavailable for executor "
                            f"{role_or_type!r} (its configuration did not "
                            "load); the tool was not run",
                    }},
                )
            entry = (declared or {}).get(policy_name)
        if entry is None:
            entry = hook_policies.get(policy_name)
        if entry is None:
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        f"unknown policy: {policy_name}",
                }},
            )
        matcher_regex, callback = entry

        tool_name = payload.get("tool_name", "")
        if not isinstance(tool_name, str):
            # Terra r1: non-string tool_name raised in re.fullmatch — a 500
            # the shim fails open on. The matcher is the dispatch gate, so a
            # malformed tool identity is a structured deny, never a crash.
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        "internal/hooks/resolve: tool_name must be a string",
                }},
            )
        if not _re.fullmatch(matcher_regex, tool_name):
            return web.json_response({})  # empty = allow

        try:
            # #366: the context dict is the AUTHENTICATED-identity channel to
            # the callback (None = unauthenticated). Identity-consuming
            # callbacks (permission relay, buttons reminder) read this key
            # and fail closed without it — they no longer parse the cwd.
            result = await callback(
                payload, None, {"casa_engagement_id": auth_eng_id})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        f"policy {policy_name!r} raised: {exc}",
                }},
            )

        if result is None:
            return web.json_response({})
        return web.json_response(result)

    return handler


# ---------------------------------------------------------------------------
# /admin/reload handler factory (Task E.1, granular-reload plan)
# ---------------------------------------------------------------------------


def build_admin_memory_wipe_handler():
    """#411 terminal door — POST /admin/memory/wipe on the internal unix
    socket, root-gated by the same SO_PEERCRED middleware as every
    ``/admin/*`` route (#467). Consent here is being root in the gated
    terminal PLUS an explicit ``{"confirm": true}`` in the body (casactl
    sends it only under ``--yes``). Shares the single-flight slot with the
    agent door: a wipe already running (or admission frozen for shutdown)
    is a 409, never a second wipe. The response carries the WipeReport."""
    async def handler(request: web.Request) -> web.Response:
        import agent as agent_mod
        import memory_wipe as memory_wipe_mod
        from hindsight_ids import bank_id
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            return web.json_response(
                {"status": "error", "kind": "confirm_required",
                 "message": "POST {\"confirm\": true} to wipe long-term memory"},
                status=400,
            )
        registry = getattr(agent_mod, "active_session_registry", None)
        sem = getattr(agent_mod, "active_semantic_memory", None)
        if registry is None or sem is None:
            return web.json_response(
                {"status": "error", "kind": "not_initialized",
                 "message": "memory backend or session registry not bound"},
                status=500,
            )
        task = memory_wipe_mod.start_wipe_task(
            memory_wipe_mod.wipe_long_term_memory(
                registry=registry, semantic_memory=sem,
                fence=memory_wipe_mod.FENCE, bank=bank_id("casa"),
            ),
        )
        if task is None:
            return web.json_response(
                {"status": "error", "kind": "already_running",
                 "message": "a wipe is already running or the app is shutting down"},
                status=409,
            )
        try:
            report = await task
        except memory_wipe_mod.WipeAborted as exc:
            return web.json_response(
                {"status": "error", "kind": "aborted", "message": str(exc)},
                status=503,
            )
        except Exception as exc:  # noqa: BLE001 — report truthfully
            logger.exception("memory wipe failed")
            return web.json_response(
                {"status": "error", "kind": "wipe_failed",
                 "message": f"{exc} — state may be partial"},
                status=500,
            )
        return web.json_response({
            "status": "ok",
            "bank_deleted": report.bank_deleted,
            "spool_records_dropped": report.spool_records_dropped,
            "session_entries_dropped": report.session_entries_dropped,
            "summary": report.summary(),
        })
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
        status_code = 200 if result.get("status") == "ok" else 500
        return web.json_response(result, status=status_code)

    return handler
