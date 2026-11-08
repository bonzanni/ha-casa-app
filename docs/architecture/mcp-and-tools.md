---
last_reviewed: 2026-09-19
---

# The MCP surface and the tool boundary

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How tools reach an agent and what stands between a tool call and its execution: the
in-process casa-framework MCP server each session is built with, where authorization
actually happens, and the optional MCP servers the environment switches on. It does not
cover individual tools, nor the MCP protocol itself.

## Mental model

**Each session's casa-framework tools come from one in-process MCP server built from its
own grants.** `create_casa_tools` builds the server for one session from the tool table,
keeping only the tools that session's grants name, so the tools an agent sees are the tools
its session can call. No route outside the process reaches the tool table: the internal Unix
socket serves the administrative and broker routes, not tool calls.

**The agent-level allowlist is a constraint on the agent, not a boundary at the tool.** The
declared tool list is enforced *upstream*, by the SDK's own permission machinery and by
hooks, before a call reaches a handler; the code-mandatory guard hooks and tool-local gates
are separate from the list and keep denying regardless of the session's permission mode.
Individual tools may still refuse individual operations. Those are tool-local gates, not a
universal authorization check.

**Hook policies are their own surface with their own document.** Per-agent hook policies,
how a hooks document is built at load and what a mistyped parameter does live in
[`architecture/hook-resolution.md`](hook-resolution.md) (INV-MCP-007).

**A specialist engagement's tool surface has a code-owned ceiling on top of its grant.**
A third-party specialist bundle may legitimately have granted itself a privileged
casa-framework tool before the install-time ceiling existed, and that grant is pinned on
its engagement record — so the central tool wrapper every casa tool passes through refuses
any casa tool outside the consumer-safe ceiling whenever the bound record is a
specialist. Executor records are not ceilinged: their definitions are image-owned.

**One optional MCP server rides on the environment.** Setting `N8N_URL` registers an n8n
workflow server (bearer-authenticated when `N8N_API_KEY` is set), read by
`casa_core._maybe_register_n8n`; unset, nothing is registered. No manifest option exposes
it — these variables are its only switch.

## Contracts & invariants

**INV-MCP-002**: The internal endpoint is reachable only from inside the container, over a Unix socket with restricted permissions.

This bounds who *outside* the container can reach the socket (nothing). Inside it, the
socket file is mode `0600`, and its `/admin/*` family is further gated on the connecting
peer's credentials ([`architecture/http-surface.md`](http-surface.md)).

What it does not cover: a process already running as root inside the container, which the
socket's permissions do not constrain.

**INV-MCP-003**: An agent's casa-framework MCP surface exposes only the tools its grants name.

The server is built per session from the grants that session resolves; a session whose
grants name the whole server (`mcp__casa-framework`) is given the whole table.

What it does not cover: being offered is not being permitted to do everything — tool-local
gates and the specialist ceiling (INV-MCP-009) still apply on top.

**INV-MCP-009**: A specialist engagement's casa-tool dispatch is bounded by the consumer-safe ceiling, whatever its record grants.

Enforced in the one wrapper every casa tool passes through at registry definition — the
same choke point as the context-rebuild fence — so it covers every casa tool by
construction. The ceiling is the install-time allowlist unioned with the launch-mandatory
grants and `report_job_progress`, which a job engagement's session is granted
([`background-jobs.md`](background-jobs.md)); when the bound engagement record's kind is
specialist and the tool is outside it, the call is refused before the handler runs. This is
the layer that revokes a forbidden grant PINNED on a record before the install and load
ceilings existed — a session's server is built from the record's grants faithfully, so
without this a pre-ceiling self-granted spawn tool would still dispatch.

What it does not cover: executor records (image-owned definitions, deliberately not
ceilinged), unbound callers (residents and ephemeral delegations — their surface is bounded
by the loaded, clamped config), and non-casa tools (CC built-ins are the CLI layer's to
gate). The terminal-binding completion tool bypasses the wrapper but is itself inside the
ceiling.

**INV-MCP-013**: Every Claude CLI session Casa starts hard-denies the CLI's cross-session tools `SendMessage`, `ListAgents` and `PushNotification`, and refuses inbound messages from other CLI sessions, whatever the agent's own tool configuration says.

The pinned CLI offers these tools by default and none of them prompts, so the fail-closed
tool gate never sees a call; `SendMessage` reaches every CLI session the same OS user runs
on the machine, which here is every agent. Agent-to-agent work goes through Casa's
delegation instead. `CROSS_SESSION_TOOLS` in `claude_runtime.py` is merged into the
disallowed list of every options build, and each passes `crossSessionInbound: "refuse"`
through `--settings`. A source sweep pins the exact set of options constructions and refuses one whose
deny list or settings bypasses the shared helpers.

**INV-MCP-014**: Every Claude CLI session Casa starts also hard-denies the pinned CLI's self-scheduling built-ins `ScheduleWakeup`, `CronCreate`, `CronDelete`, `CronList`, `Monitor` and `RemoteTrigger`, whatever the agent's own tool configuration says.

`ScheduleWakeup` and `CronCreate` queue prompts in the CLI's own scheduler, outside
Casa's, without asking for permission. A wake-up is not inert: the pinned CLI runs an
unprompted turn at the due time while its process lives, and a warm client or an
engagement can mistake that turn for the next turn's reply. `SELF_SCHEDULING_TOOLS` in `claude_runtime.py` is merged by the same helper
as `CROSS_SESSION_TOOLS`, so the sweep above covers it too. `Monitor` (gated by a CLI flag)
and `RemoteTrigger` (claude.ai remote sessions only) may be absent from a given session's
surface, and are denied regardless. The names are
those of the pinned CLI, and a CLI upgrade must re-check them: the CLI accepts a disallowed
name it does not offer, so a misspelt name fails silently. An agent schedules through
Casa's `set_reminder`.

The same helper also denies `ShareOnboardingGuide`, `OUTBOUND_SHARE_TOOLS` in
`claude_runtime.py`. The pinned CLI offers it by default; it uploads an onboarding guide
file from the working directory to a claude.ai share link under the session's login,
without a permission prompt.

## Failure behavior

**An unknown tool name.** Resolution fails and the call is refused; nothing is invoked.

**A tool raises.** The failure is returned as the tool's result rather than propagating
out of the session, so a failing tool is a result, not a broken connection.

## Extension points

**A new tool** is added to the tool table, which is what every session's server is built
from. Adding it there makes it dispatchable; making it *reachable* by a given agent is a separate question
of that agent's declared tools.

**A new constraint on tool use** should be placed deliberately. A check in the tool runs for
every caller; a check in the agent's declared list constrains only that agent. If the intent
is "nothing may do this", the tool is the place.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::init_tools`
- `casa/rootfs/opt/casa/tools.py::create_casa_tools`
- `casa/rootfs/opt/casa/tools.py::select_casa_tools`
- `casa/rootfs/opt/casa/casa_core.py::_maybe_register_n8n`

**Tests**
- `tests/test_role_scoped_mcp.py`
- `tests/test_specialist_tool_ceiling.py`
- `tests/test_cross_session_tools.py`
- `tests/test_n8n_registration.py`

**Related**
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/hook-resolution.md`](../architecture/hook-resolution.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
<!-- END SOURCEMAP -->
