---
last_reviewed: 2026-09-09
---

# Plugin result contract and the capability broker

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What stands between a plugin tool's result and the model: the producer-declared result
contract, the escrow broker a declared capability is deposited with, the hooks that refuse
a non-adopting plugin's tools before they run and replace a result that arrives anyway,
and how a capability reference is bound, armed, redeemed and revoked. It does not cover how
a plugin is installed or assigned ([`plugins.md`](plugins.md)), how its setup tool is
dispatched ([`plugin-setup.md`](plugin-setup.md)), nor what an agent's prose rule says
about credentials ([`personality.md`](personality.md)).

## Mental model

**Casa never sees a plugin tool's result on its way to the model.** The CLI talks to a
plugin's MCP server directly, and until this mechanism existed Casa's first sight of a
live capability — a sign-in link, a one-time code, a bearer token — was the model's echo
of it, on its way to the chat. The seam Casa does own is the CLI's hook protocol, and the
boundary is built there, in two halves that must be read together.

**The producer declares; Casa validates the declaration and never infers one.** A plugin
adopts the contract by declaring, in its manifest, every non-setup tool its servers
expose: each is either `safe` (the producer asserts the result carries no live capability)
or `capability` (the tool deposits each declared slot's value with Casa during the call and
returns a reference in its place). A tool may also declare which of its parameters
`consumes` a reference. The declaration is shape-validated at install, at every stored
artifact re-check and at session build, with the same per-plugin degradation the other
producer declarations get; Casa neither supplements nor infers it, the same trust boundary
the protected-tool declaration sits on.

**A plugin that has not adopted the contract has its non-setup tools refused before they
run.** Refusal before execution is what makes this fail-closed: a result that never exists
has no transcript line, no structured-output sidecar and no model-visible bytes. The exact
setup tool the plugin declares is exempt, whoever calls it — its consent page is delivered
to the operator by design. Should a result for such a tool arrive anyway, a second hook
replaces it with a withheld notice that quotes none of its bytes.

**A capability never enters the MCP result.** A conforming `capability` tool posts the
value to the broker over the internal socket, gets an opaque reference, and returns the
reference. The result the model sees, the transcript line the CLI writes and the
`structuredContent` sidecar beside it all carry the reference only. The value lives in
memory, bound to the call's grant identity — exactly whoever could consume an operator
approval for the same call — and dies with the process.

**Redemption is ticketed, by the declared consumer, once.** When the model passes a
reference to a parameter the same plugin declared as consuming it, admission arms the
reference for that one call and rewrites the argument the tool receives to
`<reference>:<ticket>`; the consumer redeems both over the internal socket. The model's own
message carries the bare reference and the transcript records that message, not the
rewritten input, so a parallel tool cannot present the ticket.

**What this is not.** It is not an outbound scrubber on the delivery path, not taint
tracking, and not a defence against a producer that lies in its own declaration — a `safe`
tool that returns a credential, or a `capability` tool that copies the value into a field
it did not declare, is the plugin-author trust boundary the protected-tool ruling already
accepts. Executor sessions of either driver are outside it (#923): they carry no approval
seam, so they have no grant identity to bind a capability to.

## Contracts & invariants

**INV-PLUG-017**: In a resident, delegated-specialist or specialist-engagement session that loads a plugin, a call to a non-setup MCP tool of a plugin that has not adopted the result contract is refused before it runs by a code-registered PreToolUse hook whose reason quotes none of the call's arguments, and a result arriving for such a tool is replaced before the model sees it by a code-registered PostToolUse hook whose replacement quotes none of the result's bytes; the plugin's exact setup tool is exempt from both, and no hooks document can shed either hook.

The two hooks and the failure-event housekeeping matcher are appended in code, beside the
guards the same builders already append, whenever the session's plugin resolution is
non-empty. They are not declarable: the hooks schema still admits only `pre_tool_use`,
and a session with no plugins loads no plugin tool and gets no broker. Admission is ONE
callback that runs, in order, contract admission, then the authorization decision for a
protected tool, then arming — because the SDK dispatches same-event matchers concurrently
and arming must follow the authorization decision, never race it.

What it does not cover: executor sessions of either driver, and a plugin's setup
tool, which passes both hooks unchanged whoever calls it — the manual re-run of a setup
tool is a documented flow.

The executor exclusion is not a gap in the contract's reach, and the reason changed with
#923. Everything an executor loads is Casa's own: an operator cannot give a worker a
plugin, so a worker's set is the bundled set Casa ships. A contract whose purpose is to
hold third-party code to a shape has nothing third-party to hold there — any bundled
plugin that returns a capability is Casa's to make conformant before it ships. If workers
are opened to operator-installed plugins later, this exclusion is the first thing that
has to be revisited.

**INV-PLUG-018**: A capability reference is bound to the grant identity of the call that minted it — operator, chat, enforcement role, artifact and engagement, derived by the same code the authorization hook uses — is single-use, expires after one approval-keyboard window plus one grant window, is redeemable only by a parameter a tool of the same plugin declared as consuming that slot, in a session with the same identity, through a per-call ticket the consumer receives in its rewritten input, and is purged with the artifact or role it was minted under and lost on restart.

The identity is deliberately not a subset: round after round of review found a client-bound
reference too tight (an approved continuation lands in a fresh client) and a role-and-artifact
triple too loose (a second session of the same role could redeem the first's). Binding to
exactly what a grant binds to is the rule, not a third field list. A reference that is
armed for one call and presented again by another call is re-armed for the newer one; the
older ticket dies. An in-flight call and an arm are both time-capped, because a call the
authorization hook denied or the turn cancelled produces no terminal hook event.

What it does not cover: a reference presented in a parameter the plugin did not declare as
consuming — it is passed through as the inert string it is — and the value's fate inside
the consumer plugin once redeemed.

**INV-PLUG-019**: A capability result passes to the model only when every slot the tool declares carries the reference of a deposit bound to that very call; a result missing a slot, carrying a foreign reference, or not parseable as an object is replaced and its deposits dropped.

This is a structural check on what the producer returned, and it is all Casa checks. A
deposit binds to the unique in-flight capability call of its client that provides the
slot — zero or several such calls refuse the deposit — so a value can never attach to the
wrong call, and the deposit request itself carries no identity: the identity was stored on
the in-flight record at admission, atomically. Scope line: this holds for a producer that
keeps its declared contract; a producer that breaks it is the plugin-author trust boundary
above, and Casa claims no detection of one.

What it does not cover: a tool error. An MCP tool that raises fires the failure event, whose
input carries no result and whose output has no replacement field; the error text reaches
the model and the transcript. The housekeeping matcher closes the call and drops its
deposits. A conforming producer never places a capability in an error.

## Failure behavior

**A plugin declares no contract.** Every one of its non-setup tools is refused before it
runs, with a reason that names the contract and asks the model to tell the operator the
plugin needs updating; its setup tool still runs. There is no grace period: a permissive
window is exactly what would stop this being a boundary.

**A plugin's declaration is malformed.** At install or update the mutation is refused; a
stored artifact that carries one is excluded from resolution; at session build the plugin
is represented as not adopting and refused as above. Never a whole-role failure.

**A deposit arrives with no matching call in flight, or with two.** It is refused with a
code and no reference is minted; the producer's result then fails the structural check and
is withheld. The same happens when a slot is deposited twice in one call.

**A redemption is presented with the wrong client, a spent or expired reference, a bad
ticket, or after the consumer call has closed.** It is refused with a code; no value ever
rides on an error. Two racing redemptions of one reference release exactly one value.

**A capability tool is called on a turn that has no grant identity** — a scheduled or
webhook turn, or an engagement whose record is not an active specialist with a topic and an
operator origin. The call is refused before it runs, exactly as a protected tool is denied
on an unsupported origin; nothing is deposited.

**A tool errors.** The failure event fires; the error text reaches the model and the
transcript; the in-flight call is closed and its deposits dropped.

**A plugin is updated, removed or unassigned.** The references minted under the old
artifact, or for the unassigned role, are purged in the same lifecycle step that purges
authorization grants — after the registry mutation commits and before any reload — so an
engagement resuming from its recorded artifact path cannot redeem them.

**Casa restarts.** The store is memory-only; every reference and every in-flight call is
gone. A model holding a reference from before the restart is told it is not redeemable and
must fetch a fresh one.

## Extension points

**A new declaration field** joins the strict extractor beside the existing ones and is
re-checked on every artifact-verification path; per-plugin degradation, never inference.

**A new session builder that loads plugins** appends the broker's three matchers exactly
where it appends the authorization hook, passes the same resolution to both, and puts the
per-client id in the CLI environment it builds — the hooks and the plugin's servers must
agree on it by construction, not by lookup.

**Executor sessions** are the open question (#923): they need an approval identity before
they can carry the contract, and holding them to it today would refuse the bundled
documentation-lookup plugin the plugin-developer relies on.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/result_broker.py`
- `casa/rootfs/opt/casa/plugin_store.py::manifest_result_contract`
- `casa/rootfs/opt/casa/plugin_grants.py::result_contract_map`
- `casa/rootfs/opt/casa/authz_grants.py::resolve_grant_identity`

**Tests**
- `tests/test_result_broker.py`
- `tests/test_result_contract.py`
- `tests/test_result_broker_redcase.py`

**Related**
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/plugin-setup.md`](../architecture/plugin-setup.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
<!-- END SOURCEMAP -->
