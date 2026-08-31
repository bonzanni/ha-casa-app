---
last_reviewed: 2026-08-13
---

# Hook resolution and the containment floor

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a workspace subprocess's hook calls are resolved and authenticated, how per-executor
hook policies are built and fall back, and the load-time containment floor for
`claude_code` executors. The tool dispatch path and its grants live in
[`architecture/mcp-and-tools.md`](mcp-and-tools.md).

## Mental model

**The bridge runs as its own supervised service** so that the bridge *connection* survives a
restart of the main application. Its own client is a thin shell shim, and the failure
semantics are two-layered and opposite: the shim **fails open** — when its own HTTP call to
the bridge fails, it returns an allow decision rather than blocking (an unreachable Casa
should not wedge a running engagement) — but the bridge itself **fails closed**: when it is
up and the main application's socket is not, it answers with an explicit deny, and the shim
relays that deny. So hook policy is unenforced only when the *bridge* is unreachable; a
main-application restart denies rather than allows. Anything that must hold regardless
belongs in a tool, not in a hook.

The hooks document an executor carries is a mutable trust surface, and its translation to
workspace settings treats malformed shapes as absent rather than fatal: a non-mapping
document root, a non-list hook section, a non-mapping list member, or an unparseable
per-hook timeout is skipped instead of crashing engagement provisioning, and the
code-mandatory guard entries are emitted regardless of what the document declares.

There are two of them, and the second is there for a reason worth stating: the floor
guarantees that `path_scope` is *present*, not what it admits — its writable prefixes are
the executor's own declaration, empty by default and `/config/agents` on the shipped
configurator. An executor whose declaration admits the resident tree would therefore be
refused by nothing else when it writes a resident's unserved prompt file, and a scope
denial could not name the corrective recipe anyway. The guard that refuses that file
(file tools only — it routes no `Bash`) is emitted here, and resolved to a callback by the
endpoint half, because either alone resolves to nothing: a settings entry naming a policy
the resolver does not know invokes a proxy for no callback. Its two neighbours, the
trigger-file and response-shape guards, are deliberately *not* emitted here — their shell
halves recognise a bare file name anywhere in a command, so an executor writing a file of
that name inside its own engagement workspace would start being refused.

**For a `claude_code` executor, that mutability stops at the containment floor.**
`block_dangerous_bash` and `path_scope` are not optional entries a hooks file happens to
carry — a `claude_code` executor that does not declare both fails to *load* at all
(INV-MCP-008), and what it declared at that moment is captured once, verbatim, onto its
definition. Every surface that later turns a hooks document into enforced Claude Code
settings — workspace provisioning, the HTTP hook-resolve endpoint, and boot replay — reads
that captured document rather than the file on disk, and forces the floor's matchers to the
values `HOOK_POLICIES` declares regardless of what the document says. Editing `hooks.yaml`
after that point changes nothing already provisioned from it; only a reload or a fresh load
sees the edit, and a hollowed edit is refused there exactly as it would be refused at boot.

## Contracts & invariants

**INV-MCP-006**: Hook resolution binds an engagement only via the per-engagement credential — a known id with a missing or mismatched token is refused, an unauthenticated request selects no executor hook parameters and reaches no identity-consuming hook policy, and an authenticated identity contradicting the payload's working-directory claim is refused.

The same verification function as INV-MCP-004, on the hook route. The shim sends the
credential pair from its own workspace `.mcp.json` as headers; the bridge rebuilds the
forwarded body from those headers alone, so a body-borne identity claim cannot bypass
it. The resolver threads the authenticated identity to the policy
callback in-process; the permission relay and the buttons reminder act only on that
identity, which is what stops a forged working directory from posting a permission
keyboard into another engagement's topic or borrowing another executor's hook parameters.

What it does not cover: an id the registry does not know proceeds unauthenticated under the
default-configured policies (mirroring INV-MCP-004's unknown-id clause); the shim's fail-open
transport is unchanged (an unreachable bridge still allows); and, as with INV-MCP-004, a
caller class stage 2 does not cover, or casa-core itself, is out of scope.

**INV-MCP-007**: A hook policy parameter of the wrong type fails the build of that policy, and an authenticated hook resolution naming an executor the per-executor policy map does not represent is refused rather than answered from the default-configured policies.

The hooks schema leaves per-policy parameters open, so a mistyped value is schema-valid and
reaches the policy factory intact. The scope parameters are lists of path prefixes and every
consumer iterates them — and a string is iterable. A `writable: /config` written without the
list dash therefore expanded character by character into a prefix set containing a bare `/`,
which prefix-matches every absolute path: the scope guard admitted precisely the writes it
was configured to refuse. A wrong type now raises out of the factory, where an unrecognised
parameter *name* already did, and the executor fails closed at load rather than enforcing a
widened scope at runtime. The rule is deliberately uniform across every parameter in the
registry, including where coercion would have been available and harmless-looking: the
boolean gating resident deletion is required to be a boolean, because truthiness is not a
type check in either direction — a falsy non-boolean would silently disable the guard and a
truthy one silently enable it — and the commit-size limit is required to be a whole number
rather than coerced, because a coercion is a guess at what the author meant.

The second clause is what makes the first one worth having, and it is the harder half.
Refusing to build is not by itself a refusal to run: the HTTP hook path builds its
per-executor policy map separately, and the resolver falls back **per policy** to the
default-configured map, whose `casa_config_guard` forbids no write path at all. So an
executor simply *missing* from that map enforced less than the operator wrote — the same
fail-open shape, one layer out.

The rule is stated at the point of use rather than as a list of failure modes, because
that list turned out not to be enumerable. Three attempts at it each closed one arm and
left another: the file fails to build on the second read; the file fails to *load*, so the
registry publishes no definition and nothing iterating loaded executors names it at all;
the whole executor directory fails to scan, so no type name survives to be marked as
having failed. Each is a different way to be absent, and the fallback treated absence as
consent. So absence is now the refusal: an authenticated request naming an executor the
map does not represent is denied, whatever made it missing. The corollary is that an
executor which legitimately declares no parameters must be represented **positively** — it
is, by an explicit marker — since otherwise "known, and needs nothing" and "never loaded"
are the same silence. The separately-wired permission relay is not in the per-executor
map, so a broken configuration still leaves the executor able to ask rather than only to
fail.

Denial is the answer only when there is nothing better. Two cases have something better
and take it: an executor whose *document* failed to build gets a deny-all map naming that
reason, and a *reload* keeps the known-good pre-reload policy set — built from the last
file that did load — in preference to both the defaults and deny-all, because evicting it
would take live engagements down over an edit that was never accepted. So the guarantee is
about what is *never* answered from the defaults, not that every failure ends in a denial.

The reload preference is per executor type, and it is driven by *evidence* that a type
failed, which is a narrower thing than the type having failed. An executor root that
raises on being read aborts the reload before any rebuild, and the whole known-good map
survives. But a root that is merely absent scans successfully and reports nothing: no
definitions, and no failures either. Every previously known type then looks genuinely
removed rather than unproven, its entry is dropped, and its guarded calls are refused from
then on. The pre-reload callbacks still existed and were still good — what was missing was
any evidence to tell "gone" apart from "unproven". Refusing is the fail-closed side of
that ambiguity and is the deliberate choice, but it is a real cost: a configuration
directory that goes missing under a running Casa stops guarded work for live engagements
until it comes back.

What it does not cover: a well-typed but wrong value. A list of prefixes that is simply too
broad builds and enforces exactly what it says — this rule constrains the shape of a
parameter, never its meaning. Nor does it cover an *unauthenticated* resolution, which
selects no executor parameters at all and is governed by INV-MCP-006.

**INV-MCP-008**: For a `driver: claude_code` executor, the containment floor (`block_dangerous_bash`, `path_scope`) is a load-time declaration requirement, snapshotted once and emitted with canonical per-policy matchers by every surface that turns it into settings, never by re-reading the mutable hooks file.

The validated document is snapshotted once at load, and every surface that emits Claude Code
hook settings from it — provisioning, the HTTP hook-resolve path, and boot replay — builds
from that snapshot with canonical per-policy matchers rather than re-reading the mutable
hooks file.

`load_all_executors` reads a `claude_code` executor's declared `pre_tool_use` policy names
and refuses to load — `LoadError`, not a narrower synthesized default — when either floor
policy is absent. What loaded is then captured verbatim onto `ExecutorDefinition`'s
`hooks_document` field, `{}` only when the executor carries no hooks file at all — an empty
snapshot would make the in-casa SDK path resynthesize a wider, `/config`-rooted `path_scope`
than the executor actually declared, which is the fail-open shape this rule exists to close.
Provisioning (`drivers/workspace.py`), the in-casa build (`tools.py`) and the HTTP
policy-map builder (`_build_executor_cc_hook_policies`) all read that field; none re-reads
`hooks_path` off disk, which is what closes the window between a document validating at load
and a later read of the same path seeing something else.

Matchers are forced independently of the snapshot, at the point of emission:
`translate_hooks_to_settings` routes every `HOOK_POLICIES`-backed policy — not only the
floor — to `canonical_matcher_for`'s regex regardless of what the yaml under it declares,
so a stray `matcher: Read` on `block_dangerous_bash` can no longer misroute which tool calls
the policy sees. Only the two policies with no registry entry (`engagement_permission_relay`,
`engagement_buttons_reminder`) keep a yaml-declared matcher, because there is no canonical
one to override them with. Any floor policy still missing after that pass — reachable only
if something other than `load_all_executors` produced the document — is appended with its
canonical matcher and no synthesized parameters, mirroring the mandatory
`managed_component_guard` append already described above.

Boot replay closes the last gap: a resumed `claude_code` record's settings are regenerated
from `hooks_document` and diffed against what is on disk for every record `definition_any`
resolves, not a policy-affecting subset — a difference forces a confirmed service down/up
cycle, and a snapshot that itself does not carry the floor refuses to resume
(`refuse_workspace_cycle_failed`) rather than restart a workspace the floor cannot protect.

What it does not cover: an `in_casa` executor's floor is not load-gated the same way — the
`driver == "claude_code"` check in `load_all_executors` does not apply to it, and its
enforcement instead comes from `resolve_hooks` synthesizing the two floor entries whenever a
`HooksConfig` declares no `pre_tool_use` at all, over the same immutable snapshot. Nor does
this cover a hooks document that fails to build at all, whatever the floor — that is
INV-MCP-007's per-policy fallback, which this rule assumes has already resolved to something
constructible before the floor check runs. And a reload rebuilds through the same load path
the boot scan uses, so a hollowed edit is rejected there exactly as at boot; a *valid* edit
replaces the snapshot for future emissions but does not retroactively touch a workspace
already provisioned from the one before it — boot replay's diff-and-cycle is the mechanism
that reconciles a resumed session against the current snapshot, not the reload itself.

## Failure behavior

**The bridge service is unreachable.** The shim returns an allow decision. A hook that would
have denied the call does not run, so the call proceeds. This is why a hook is not the right
place for a constraint that must never be bypassed.

**The bridge is up but the main application is not.** The opposite of the case above: the
bridge answers hook resolution with an explicit deny, and the shim relays it. Hook-gated
calls fail closed for the duration of a main-application restart.

**A hook runs long.** Hook forwarding is deliberately unbounded at the transport, governed
by per-policy timeouts instead.

## Extension points

**A new hook policy** joins the registry with typed parameters; the factory's uniform
type-refusal (INV-MCP-007) then covers it — do not coerce, and do not accept a policy
parameter shape the registry does not declare.

**A new surface that turns a hooks document into settings** must read the load-time
snapshot, never the file, and route registry-backed policies through the canonical
matchers (INV-MCP-008).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/internal_handlers.py::_make_internal_hooks_resolve_handler`
- `casa/rootfs/opt/casa/hooks.py::resolve_hooks`
- `casa/rootfs/opt/casa/drivers/hook_bridge.py::translate_hooks_to_settings`
- `casa/rootfs/opt/casa/casa_core.py::_build_executor_cc_hook_policies`

**Tests**
- `tests/test_hook_proxy_endpoint.py`
- `tests/test_hooks_policy_param_types.py`
- `tests/test_hooks.py`
- `tests/test_hook_bridge.py`

**Related**
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
<!-- END SOURCEMAP -->
