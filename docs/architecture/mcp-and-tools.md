---
last_reviewed: 2026-08-08
---

# The MCP surface and the tool boundary

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How tools reach an agent and what stands between a tool call and its execution. It covers the
separate bridge service, the internal dispatch path, and where authorization actually
happens. It does not cover individual tools, nor the MCP protocol itself.

## Mental model

**There are two surfaces onto the same tools, and they filter differently.** The SDK surface
presents an agent a role-filtered set. The HTTP bridge advertises the full static set. Which
tools an agent *sees* therefore depends on which surface it came through.

**The dispatch layer does not enforce the agent-level allowlist.** This is the single most
important thing on this page. The internal tool-call handler looks a name up in the full
dispatch map and invokes it; it never consults an agent's declared tool list. That list is
enforced *upstream*, by the SDK's own permission machinery and by hooks, before a call is
ever dispatched — and that upstream enforcement has modes: an executor engagement running in
an autonomous permission mode short-circuits the relay to allow *before* the declared list is
consulted, so of the two declared lists only the disallowed one still prohibits there. The
code-mandatory guard hooks and tool-local gates are separate from both lists and keep
denying regardless of mode.

The consequence is worth stating plainly: **the allowlist is a constraint on the agent, not a
boundary at the tool.** Anything able to reach full-map dispatch directly is not constrained
by it. There are two such reaching points, not one: the internal endpoint (a
permission-restricted Unix socket), and the standalone MCP bridge for in-container workspace
subprocesses (`svc-casa-mcp`, loopback port 8100), which forwards every call to that same
socket. Both live inside the container: the bridge's listener binds loopback only and
neither nginx listener proxies to it (the external listener additionally 404s the `/mcp/`
and `/hooks/` prefixes as defense in depth), so the boundary around full-map dispatch is
the container wall plus the socket's permissions.

Individual tools may still refuse individual operations. Those are tool-local gates, not a
universal authorization check.

**An engagement identity is authenticated, not merely claimed.** A tool call that names an
engagement id binds that engagement's record — and with it the record-derived role that
tool-local gates authorize against — only when it also presents the per-engagement secret
token minted at record creation and provisioned into that engagement's own workspace. The id
alone is deliberately treated as public information (it appears in the workspace MCP
configuration, in logs, and on shared loopback endpoints): a known id with a missing or
mismatched token is rejected outright rather than downgraded to an unauthenticated call, on
the paths that resolve the record for tool authority — the internal socket handler and the
engagement-channel routes that act on a record's topic and questions. An id the registry does not know still dispatches unbound, so a stale workspace
gets an honest `not_in_engagement` from the tool rather than an authentication error.

**What the token does not contain, stated plainly.** It raises the bar from "know an id" to
"hold a secret". Since stage 2, a `claude_code` subprocess execs under its own never-reused
uid with an empty capability set, and its workspace is chowned `0700` — so a sibling's
subprocess can no longer read that credential file; the boundary is per-uid, so it neither
constrains casa-core (still root, reading workspaces only through `safe_fs.py`'s no-symlink
accessor) nor covers a driver with no isolable OS subprocess (an in-process specialist). The
credential files stay `0600` as defense in depth, and the inspection tool refuses their
contents precisely because that surface is reachable without identity at all. Hook resolution
presents the same credential: the shim sends it from its own workspace `.mcp.json`, and the
resolver authenticates any identity claim before selecting hook parameters (INV-MCP-006).
Treat the token as removing identity forgery from *knowing an id* — containment of a hostile
process is now the uid drop's job.

**Hook resolution is its own surface with its own document.** The bridge service, the
shim's fail-open/bridge fail-closed split, per-executor hook policies and the `claude_code`
containment floor live in [`architecture/hook-resolution.md`](hook-resolution.md)
(INV-MCP-006, INV-MCP-007, INV-MCP-008).

**A specialist engagement's tool surface has a code-owned ceiling on top of its grant.**
A third-party specialist bundle may legitimately have granted itself a privileged
casa-framework tool before the install-time ceiling existed, and that grant is pinned on
its engagement record — so the central tool wrapper both transports pass through refuses
any casa tool outside the consumer-safe allowlist whenever the bound record is a
specialist. Executor records are not ceilinged: their definitions are image-owned.

**Some guards are advisory by construction, and one of them is deliberately imperfect.**
The pre-push self-containment guard inspects the shell command an agent is about to run,
works out which repository the push targets, and scans that tree for anti-patterns. Working
out where a shell command ends up is not decidable from the command text, so the guard
over-approximates: every `cd` token counts, every word of that command is treated as a
possible destination, and the scan covers the union. Extra scanned directories are the
intended cost. What it cannot see are destinations that are not statically resolvable —
parameter and command substitution, `eval`, aliases — and paths adversarially named after
shell syntax. That residual is accepted rather than pursued: the guard advises an
already-trusted in-container channel, it carries a logged `CASA_ALLOW_ANTI_PATTERN=1`
override, and `scripts/gate.sh` is the authoritative check on the real push path. Attempts
to close the residual by adding parser rules have a measured history of generating findings
without reducing risk (eleven review rounds in v0.145.0), so the scope note in the code is
binding: change it in response to an incident, not to a scan.

## Contracts & invariants

**INV-MCP-001**: The internal tool dispatch path enforces the authenticated engagement's own grant — a tool the engagement is not granted is refused before it runs, and an unbound call fails closed (only the terminal-binding subset is exempt).

Stated as an invariant because its enforcement is load-bearing, and because it was once the
opposite. Until v0.166.0 dispatch consulted no allowlist; enforcement was assumed to happen
"before dispatch", in the CLI's own tool gating, contained by INV-MCP-002. That assumption
does not hold for an executor with broad shell: its own (root) process can read the workspace
engagement token and POST this socket directly, bypassing the CLI entirely. So the grant
check moved to dispatch, where it binds the process, not just the model. The granted set is
the engagement record's `tools_allowed` unioned with the kind-mandatory casa-framework grants
(`query_engager`/`emit_completion`, plus `react` for engaged executors) — the same tools the
options builder hands the session, so an interactive specialist's empty record still admits
its own `query_engager`.

What it does not cover: tool-local checks still apply on top; a terminal-binding subset
(`emit_completion`) dispatches even unbound, for completion retries; and it binds the
*engagement identity*, not the OS process. Before stage 2 a root `claude_code` subprocess
could read a live sibling's credential file and authenticate as it; per-engagement uid
allocation plus `0700` workspace ownership close that OS-level path now. The grant check
remains the control for what an authenticated caller — however it got a valid token — invokes.

**INV-MCP-002**: The internal endpoint is reachable only from inside the container, over a Unix socket with restricted permissions.

This bounds who *outside* the container can reach the socket (nothing). Inside it, the socket
file is root-owned mode `0600`; since stage 2 a `claude_code` subprocess execs under a
dropped, capability-stripped uid and cannot open it — so it reaches dispatch only through the
loopback bridge below, never the socket directly. INV-MCP-001 still enforces the
per-engagement grant regardless of transport, since dispatch cannot assume every caller is
uid-dropped.

What it does not cover: the `svc-casa-mcp` bridge on loopback port 8100, forwarding into this
socket for workspace subprocesses — including the `/internal/channel/*` family a
per-engagement channel server posts (`send_to_topic`, `ask`, `ask_cancel`), never
`/admin/reload` or any other admin path. Its listener is loopback-bound and unproxied by
nginx, inside the same container boundary rather than punching through it.

**INV-MCP-003**: The two surfaces expose different tool sets — role-filtered on the SDK side, the full static set advertised over HTTP but grant-filtered at dispatch.

What it does not cover: being advertised is not being permitted. The HTTP advertisement
describes what the bridge can route; INV-MCP-001's dispatch check decides what a particular
engagement may actually invoke.

**INV-MCP-004**: An engagement-id claim binds an engagement record only together with that record's per-engagement auth token; a known id with a missing or mismatched token is rejected without invoking the tool.

The terminal-binding allowlist is inside this rule, not an exception to it: a terminal
record still binds for a completion retry only when the token matches.

What it does not cover: an id the registry does not know — that call dispatches with no
engagement bound (unchanged), and the tool answers for itself. A `claude_code` sibling's
subprocess reading another workspace's credential file is now refused at the OS permission
layer by stage 2's per-engagement uid and workspace ownership; the residual is a caller class
containment does not cover, and casa-core itself. The hook-resolution path states the same
rule, INV-MCP-006.

**INV-MCP-005**: The workspace-inspection tool never returns the contents of a credential-bearing workspace file.

The inspection tool needs no engagement identity to run, so returning `.mcp.json` would hand
any caller the credential that INV-MCP-004 exists to require. The refusal is on the resolved
path's basename, so a symlink or a copy in a subdirectory is refused too; directory listings
still show the name.

What it does not cover: a caller with shell access reads the file directly — this closes the
*tool* surface, not the filesystem.

**INV-MCP-009**: A specialist engagement's casa-tool dispatch is bounded by the consumer-safe ceiling, on both transports, whatever its record grants.

Enforced in the one wrapper every casa tool passes through at registry definition — the
same choke point as the context-rebuild fence — so it covers the in-process SDK dispatch of
an in-casa specialist and the internal-socket path alike, by construction. The ceiling is
the install-time allowlist unioned with the launch-mandatory grants; when the bound
engagement record's kind is specialist and the tool is outside it, the call is refused
before the handler runs. This is the layer that revokes a forbidden grant PINNED on a
record before the install and load ceilings existed — the bridge grant-gate honors
`tools_allowed` faithfully, so without this a pre-ceiling self-granted spawn tool would
still dispatch.

What it does not cover: executor records (image-owned definitions, deliberately not
ceilinged), unbound callers (residents and ephemeral delegations — their surface is bounded
by the loaded, clamped config), and non-casa tools (CC built-ins are the CLI layer's to
gate). The terminal-binding completion tool bypasses the wrapper but is itself inside the
ceiling.

**INV-MCP-010**: An engagement-bound caller can operate only on its own workspace through the workspace tools.

The three bridge-reachable workspace tools execute as casa-core (root) on a
caller-supplied `engagement_id`, so their target was tied to the caller only by grant
configuration staying correct. Now the binding is at the tool layer: when the
authenticated caller is an engagement, inspection and deletion refuse any target but the
caller itself, and the listing filters to the caller's own entry. The load-time fence on
executor definitions and the dispatch grant-gate remain as belt-and-suspenders.

What it does not cover: unbound callers (the resident's in-process turns, the operator's
own surfaces) keep full-range access — the binding constrains engagements, not the
operator's agents.

## Failure behavior

**An unknown tool name.** Resolution fails and the call is refused; nothing is invoked.

**A tool raises.** The failure is returned in the response envelope rather than propagating
as a transport error, so a failing tool is a result, not a broken connection.

**A tool runs long.** Bridge tool forwarding carries a hard three-minute timeout and
answers temporarily-unavailable past it — the server side may still be executing. Hook forwarding's
timeout behavior lives with hook resolution.

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

**A new tool** is added to the tool table, which is what both surfaces are built from. Adding
it there makes it dispatchable; making it *reachable* by a given agent is a separate question
of that agent's declared tools.

**A new constraint on tool use** should be placed deliberately. A check in the tool runs for
every caller; a check in the agent's declared list runs only for agents that go through the
SDK path. If the intent is "nothing may do this", the tool is the place.

**Anything that assumes the allowlist is a security boundary** needs re-examining against the
dispatch path first.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/internal_handlers.py::_make_internal_tools_call_handler`
- `casa/rootfs/opt/casa/svc_casa_mcp.py`
- `casa/rootfs/opt/casa/tools.py::init_tools`

**Tests**
- `tests/test_internal_handlers.py`
- `tests/test_svc_casa_mcp.py`
- `tests/test_mcp_envelope.py`
- `tests/test_casa_engagement_channel.py`

**Related**
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/hook-resolution.md`](../architecture/hook-resolution.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
<!-- END SOURCEMAP -->
