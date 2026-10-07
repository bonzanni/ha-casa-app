---
last_reviewed: 2026-10-07
---

# The MCP bridge service

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The separate MCP bridge service (`svc-casa-mcp`): every refusal its hook-resolution route
produces itself, how it answers a long-running forward and a main application that is not
reachable, and the environment variables that move pieces of the MCP topology around it. It
does not cover tool dispatch or where authorization for a tool call happens — those live in
[`architecture/mcp-and-tools.md`](mcp-and-tools.md) — nor hook resolution's policy, which
lives in [`architecture/hook-resolution.md`](hook-resolution.md).

## Mental model

**The bridge is a separate process that never waits.** It is s6-supervised beside the main
application, listens on loopback port 8100 for in-container workspace subprocesses, and
forwards every tool call and hook decision to the main application over its internal Unix
socket, connecting afresh per call. Being separate is what lets it outlive a restart of the
main application: a caller meets an answer rather than a dropped connection. Which tools it
advertises, and why reaching it is reaching full-map dispatch, belong to
[`architecture/mcp-and-tools.md`](mcp-and-tools.md).

## Contracts & invariants

**INV-MCP-011**: Every refusal the bridge's hook-resolution route produces itself is delivered as an HTTP 200 body carrying a `deny` permission decision — an unparseable request body, an unreachable internal socket, a forwarder transport error and any other ordinary exception escaping the forwarder alike — because the calling shim reads any non-2xx as a transport failure and answers allow.

The shim is deliberately fail-open on transport: it is what stops a hook from blocking an
engagement when Casa is unreachable, and its side of the arrangement is described in
[`architecture/hook-resolution.md`](hook-resolution.md). The consequence is that the status
code carries the refusal here. A deny answered as a `400` still reads as a deny in the
handler and still carries its reason; the shim turns it into an allow, and the tool runs. The
route therefore never sets a status on its own answers: the framework's 200 default is the
contract, and the far end's status is not relayed either — a response from casa-main comes
back as its body under the bridge's own 200.

The last of those is why the route holds an unnarrowed catch. An escaping exception is
not a refusal the route decided to make; it is a defect, and the shim would read the
framework's own HTTP 500 as a transport failure and answer allow. So a defect on this
route would grant the permission the route exists to withhold, and the fail-closed logic
would still be sitting there looking correct. The catch converts that into the same 200
deny as the arms around it, and pays for it at the diagnostic layer instead: the arm
logs at ERROR with the traceback, where the transport arms log at WARN, and its reason
names the exception's class and calls the failure a bridge error rather than a relay
one. The exception's message is deliberately not interpolated — a decode failure quotes
the far end's body, and a decision reason is read by the model.

What it does not cover: `asyncio.CancelledError` and anything else outside `Exception`,
which still propagates — a cancelled request has no shim left to answer — and a failure
in delivering the response itself, which is not a refusal the route produced. The far
end is outside this rule entirely — it answers over the internal socket in its own status
codes, and it is the bridge that flattens them.

## Failure behavior

**A tool runs long.** Bridge tool forwarding carries a hard three-minute timeout and
answers temporarily-unavailable past it — the server side may still be executing. Hook forwarding's
timeout behavior lives with hook resolution.

**The main application is not reachable.** The bridge binds its port immediately and never
waits for the main application, so a call can arrive with no internal socket to forward it
to. A restart is one interval in which that happens; a first boot is another, and the
corpus is easy to misread on this point. On a cold boot the socket has never been opened
at all, and boot replay restarts the engagements that were mid-flight before it is opened,
so an engagement the system has just resumed can act inside the interval and be refused.
The window opens when replay starts those engagements and closes when the main application
opens its internal socket. The interval itself is tracked as #880.

The refusal wears three faces, one per surface, and a caller sees only its own:

- When casa-main's internal socket is unreachable, `tools/call` returns JSON-RPC error
  `-32000` with message
  `casa_temporarily_unavailable: casa-main internal socket unreachable`. That is a
  retryable condition the model handles; the bridge itself has no retry loop in the data
  path.
- When casa-main's internal socket is unreachable, `POST /hooks/resolve` returns HTTP 200
  with `hookSpecificOutput.hookEventName: "PreToolUse"`, `permissionDecision: "deny"`, and
  `permissionDecisionReason: "casa_temporarily_unavailable: casa-main internal socket
  unreachable"` — the tool-call face's message verbatim (#880). The mechanism is a verdict
  and not a retryable condition — the tool did not run, and the hook protocol has no answer
  that stops a tool without holding it — but the wording no longer says so, deliberately:
  one condition now tells the model one story whichever face it meets. The shim's own
  opposite, fail-open behavior lives in
  [`architecture/hook-resolution.md`](hook-resolution.md).
- When casa-main's internal socket is unreachable, registered `POST /internal/channel/*`
  routes return HTTP 503 with body
  `{"ok": false, "error": "casa_temporarily_unavailable"}`. A resumed engagement whose
  first act is to post to its operator topic or raise an ask arrives here.

None of the three is a defect the bridge failed to prevent; they are the price of the
bridge never waiting. The alternative is a dropped connection, which the engagement's MCP
client surfaces as a fatal handshake failure on its next request rather than as a
recoverable answer.

**Other bridge hook refusals.** These triggers are independent of socket availability, and
each is a refusal the route produces itself, under INV-MCP-011.

- When the bridge cannot parse the request body as JSON, `POST /hooks/resolve` returns HTTP
  200 with `hookSpecificOutput.hookEventName: "PreToolUse"`, `permissionDecision: "deny"`,
  and `permissionDecisionReason: "svc_casa_mcp /hooks/resolve: malformed JSON"`. The
  forwarder is not reached at all, so an unparseable body never travels to casa-main.
- When hook forwarding raises `aiohttp.ClientError` other than
  `aiohttp.ClientConnectorError`, or raises `asyncio.TimeoutError`, `POST /hooks/resolve`
  returns HTTP 200 with `hookSpecificOutput.hookEventName: "PreToolUse"`,
  `permissionDecision: "deny"`, and `permissionDecisionReason: "Permission relay failed:
  forwarder error talking to casa-main ({type(exc).__name__}: {exc or 'no detail'}). The
  tool was not run."`, with the reason interpolated from the caught `exc`. The exception's
  class name is part of the reason on purpose — it is the only handle an operator gets on
  which transport failure occurred.
- When hook forwarding raises any other exception, `POST /hooks/resolve` returns HTTP 200
  with `hookSpecificOutput.hookEventName: "PreToolUse"`, `permissionDecision: "deny"`, and
  `permissionDecisionReason: "Permission relay failed: unexpected bridge error
  ({type(exc).__name__}). The tool was not run. Check addon logs."`, and the exception is
  logged at ERROR with its traceback. The message is not interpolated into the reason,
  only the class: a decode failure's message quotes the far end's body.

The shipped shim cannot produce the first of those: it builds the request with `jq` and
answers locally when the payload is not JSON, and repointing it through
`CASA_HOOK_RESOLVE_URL` changes its destination, not that construction. The trigger belongs
to another loopback client inside the container, or to an operator probing the port directly.

A body that is valid JSON but is not an object is not a bridge refusal at all. The identity
rebuild is skipped, the body is forwarded verbatim, and the internal handler answers
`internal/hooks/resolve: body must be a JSON object`. The two malformed-request prefixes are
deliberately different: they are how an operator reading a refusal tells which layer produced
it.

A wholly optional MCP server rides on the environment too: setting `N8N_URL` registers an
n8n workflow server (bearer-authenticated when `N8N_API_KEY` is set); unset, nothing is
registered. No manifest option exposes it — these variables are its only switch.

Two environment variables move pieces of this topology, unevenly:
`CASA_FRAMEWORK_MCP_URL` redirects newly provisioned engagement workspaces to a different
framework endpoint, and `CASA_INTERNAL_SOCKET` relocates the socket for the
engagement-channel client *only* — the main application, the bridge and generated
production workspaces hard-code the standard path, so treating it as a system-wide knob
splits the topology.

## Extension points

**A new route on the bridge** meets the same unreachable-socket condition as the others, so
its answer to it belongs under Failure behavior beside the three faces above; if its caller
reads a non-2xx as a transport failure, its refusals take INV-MCP-011's form.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/svc_casa_mcp.py`

**Tests**
- `tests/test_svc_casa_mcp.py`

**Related**
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
- [`architecture/hook-resolution.md`](../architecture/hook-resolution.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
<!-- END SOURCEMAP -->
