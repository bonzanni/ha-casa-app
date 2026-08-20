---
last_reviewed: 2026-08-14
---

# The tool interface

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The mutation interface Casa exposes to its agents: how the tool surface is assembled, what
a tool result promises, how engagement and plugin mutations sequence their side effects,
and which failures roll back versus which merely report. It covers families and contracts,
not a per-tool catalog — the registry tuple is the authority on what exists. Dispatch and
authorization live in `architecture/mcp-and-tools.md`; this file is about what the tools
themselves guarantee.

## Mental model

**One registry is the whole surface.** A single module-level tuple of handlers drives both
the SDK server an agent sees and the bridge's dispatch map. Adding a tool there exposes it
on both transports; grant filtering is a separate, later cut against fully-qualified names —
with one coarse escape: a *server-level* `mcp__casa-framework` grant exposes every tool at
once, and only its absence makes filtering per-tool. The SDK server also loads eagerly
(`alwaysLoad`), so every added tool's schema lands in every SDK session's up-front context
— adding a tool is a prompt-cost decision, not just a dispatch one.

**A result has two layers, and the outer one is inferred.** A tool returns a payload
serialized into one text content block; the wrapper marks the outer result as an error only
when the payload says `status: "error"` or `ok: false`. Statuses like *unavailable*,
*pending* and *acknowledged* deliberately ride as successes — they are outcomes, not
failures. And the wrapper is a convention, not a law: at least one tool returns raw
envelopes without it, so "every error becomes `is_error`" is not a property of the surface.

**Argument-schema validation depends on the transport.** The SDK path registers the
decorated tools with their schemas, and MCP validation there rejects a missing required
argument or a bad enum before the handler runs — code and tests rely on it. The internal
bridge route checks only that the name is known and passes the arguments through, so a
tool reachable both ways must carry its own validation for the bridge side.

**Reading plugin state and changing it are separate grants.** Every tool that mutates the
plugin registry refuses a caller outside the privileged configuration roles, and that has
not changed. What changed is that *reading* is no longer bundled with mutating: a zero-argument,
read-only status tool is granted to the assistant resident, so an operator asking why a
plugin is not working is answered in the turn rather than by spawning a configurator
engagement to ask on their behalf. It reads through the health and setup-episode modules, never
by widening a filesystem scope — `/data` holds secrets and is deliberately never opened
broadly — and it is outside the specialist dispatch ceiling, so a third-party bundle cannot
reach it. It answers two different questions from two stores: what is standing wrong now, and
what happened during a past setup, which only the episode row's recorded error can say.

**One table is the whole media surface, and a kind is not a file type.** The
`send_media` capability derives everything type-specific — the schema's `kind` enum, the
argument check, the content gate, the delivered-filename extension allowlist, the size cap
and the send-method dispatch — from a single policy table, so a new delivery kind is one
entry rather than a change spread across three modules. Two consequences are easy to get
backwards. First, several *kinds* can share one *send method*: PDF, zip and text all leave
as Telegram documents, so the kind is what was gated, never the method — and the kind named
`document` therefore means PDF specifically, a name kept only because it is public API.
Widening it into a catch-all would dissolve the magic gate it exists to be. Second, the
gate's two halves do different work, and for some kinds it is the extension list, not the
content predicate, that confines the kind: a zip signature is equally an OOXML document, a
`.jar` or an `.apk`, so `.zip`-only is the actual boundary. Each predicate is total over
content — every admitted byte string yields a verdict rather than an exception — but
totality is a claim about content, not about the machine: an allocation failure is left to
propagate as an internal error rather than being reported as a verdict about the bytes,
because "these bytes are not valid" is a claim the evidence would not support. A kind may
also validate less than its extensions suggest; the text kind checks encoding, never
structure, so a malformed `.json` is delivered as the text file it is.

**A lifecycle surface is only finished when it can also undo.** Personas install, apply and
publish through consent-bound tools; removing one, sweeping the versions nothing refers to,
enumerating what is installed and revoking a stored approval are tools too — not filesystem
work, because the tree they live in is denied to the configurator precisely so that the
sequencing stays owned by code. The removal tools refuse rather than force, and a refusal
names its referrers; the sweep reports what it kept and why, so "some were skipped" is never
the whole story an operator gets. What each refusal means, and why removal is decided by
references rather than by use, belongs to [`architecture/personality.md`](personality.md).

**Engagement mutation is a funnel, not parallel paths.** Completion and cancellation
converge on one finalize path whose strict registry transition picks a single winner
(INV-ENG-001); everything observable — permits, brokers, topics, notifications, retention —
happens after, best-effort, and is not transactional.

**Plugin mutation is persist-then-converge.** Identity, source and requirement guards run
before the registry is touched; after the registry commits, reload and verification try to
make the runtime match. A failure *before* the commit leaves the registry unchanged. A
failure *after* it does not roll anything back — the honest outcome is
committed-but-not-ready, and the envelope says so.

## Contracts & invariants

**INV-TOOL-001**: The result wrapper marks an outer error only for a payload with status "error" or ok false; every other status is a successful outcome.

Enforced in the wrapper itself, which serializes the payload into exactly one content
block.

What it does not cover: tools that do not call the wrapper. A handler returning raw
envelopes carries its own error semantics.

**INV-TOOL-002**: Internal tool calls bind engagement authority only for an active record; completion alone may bind a terminal record, so a duplicate completion gets its truthful already-terminal answer.

Enforced by the internal handler's binding check against an explicit terminal-binding
allowlist containing exactly the completion tool.

What it does not cover: it does not authorize any other tool after termination, and it is
a binding rule, not an argument or schema check.

**INV-TOOL-003**: Plugin mutations serialize under one lock, and a failure before registry activation leaves the registry unchanged, reported in a pinned envelope shape.

Enforced by the shared mutation lock across all five ordinary plugin tools and by the
guard-resolve-publish-then-save ordering in the synchronous cores. The pinned fields —
kind, activation-committed, runtime-ready, verify — make the failure phase machine-readable.

What it does not cover: published store artifacts and installed system requirements are not
unwound by a later refusal; only the registry is untouched.

**INV-TOOL-004**: A reload or verification failure after the registry commit yields committed-but-not-ready; nothing rolls the registry back.

Enforced by the converge step reporting `activation_committed: true, runtime_ready: false`
rather than compensating. The next reload — or an explicit verify — is the repair path.

What it does not cover: it makes no promise about *when* the runtime converges, only that
the registry's word is already given.

**INV-TOOL-005**: No plugin-mutation result, completion hand-back or shipped prompt states whether a plugin's integration is live — neither that it is, nor that it is not.

Casa cannot see the external side of an integration. It knows whether it re-minted a secret
and whether it has queued a setup run — neither of which establishes that the service is or is
not reachable. A plugin's credential need not be artifact-bound at all: one gated by
`casa.callbacks` keeps its consent ack across an update (the ack binds the *declaration*, not
the artifact) and holds its credential outside the replaced artifact, so it is commonly
serving throughout an update Casa has just performed.

Enforced in the shipped prose: `recipes/plugin/add.md` and `update.md` tell the engager to say
only that the setup tool still needs to run and that its own result is what to go on, and the
assistant is forbidden to relay another party's verdict about a connection. That prohibition
sits in the assistant's *role doctrine*, in the core section every projection selects, because
a persona-bound resident is served the compiled bundle and never reads the composed prompt —
so a rule written only there has no force on the normal configuration, which is the same
failure `architecture/personality.md` describes for declared response limits. Before this,
`update.md` instructed the engager to report that "the update succeeded but the integration is
dead" whenever the setup tool was not Casa-run — which, for a callback-gated plugin, was every
update. A Gmail that was serving throughout was announced as down and the operator was asked
to re-authorize it (#443).

The failure is symmetric and that is why the rule is two-sided: announcing a fault that does
not exist costs the operator the same trust as missing one that does. An unfounded "it's
fine" is the same defect as an unfounded "it's dead".

**The setup tool's result is not automatically a liveness verdict either**, and the prompts say
so rather than deferring to it as though it were. Its authoring contract is idempotent
*provisioning* — argument-free, re-runnable, `setup_`-prefixed — and it is not *required* to
test what it provisioned. A given tool may check more; only its own output says. So the rule is
to relay what it returned rather than restate it as a verdict, which is the same discipline the
invariant applies to Casa's own claims.

What it does not cover: **which** runner executes the setup tool — because there is no longer a
choice to make. Casa runs a declared `casa.setupTool` and nothing else does; a mutation result
reports the declared tool but routes nothing, and no completion or prompt hands it to an agent.
See [`plugin-setup.md`](plugin-setup.md) (INV-PLUG-010) for what releases the run. That
matters to this invariant for a reason beyond tidiness: a hand-back the plugin did not need used
to cause an unnecessary run, and idempotence means repeat calls converge on the same state, not
that a call is side-effect-free — an unnecessary run can rewrite the provider's configuration,
spend rate budget, or briefly interrupt delivery, and what it costs depends on the plugin.

This invariant is also pinned as *wording*: the tests
assert each shipped surface carries the prohibition and has not reverted to a
previously-shipped phrasing, which is not the same as proving no new phrasing can express the
claim.

## Failure behavior

**A malformed request.** Invalid JSON, a missing or unknown name, a non-object request,
and any non-object `params` or `arguments` — truthy or falsy alike — all come back as
typed JSON-RPC-style error objects from the route, before any tool runs (an absent or
null value defaults to an empty object instead); a tool that raises becomes an error
object rather than a transport failure.

**Ill-typed arguments, though, reach the tool.** The route validates the request
*envelope*, not the argument types a tool declares: the internal forwarding path does not
enforce a tool's declared schema, so a handler receives whatever the caller sent. A
declared `bool` can therefore arrive as a string, and Python's truthiness makes the string
`"false"` indistinguishable from `True` to a `bool(...)` coercion. Any argument that acts
as an authorization — a `force` that permits a destructive path, a flag that waives a
refusal — must be type-checked in the handler and rejected as a bad request, not coerced.
Treating the declaration as if something upstream enforced it is how a refusal becomes
opt-out by typo.

**A completion is invalid or refused.** Bad arguments, the plugin-developer release guard,
and the unread-inbound veto all leave the engagement active; a duplicate completion is
acknowledged as already terminal; a failed strict persist reports retryable.

**An `in_casa` completion's own acknowledgement can be lost.** A winning in_casa
`emit_completion` tears down the client hosting the tool call itself, and the control
transport can close before the tool result is written; the durable finalization side
effects still complete, detached (INV-ENG-010 in
[`engagement-finalization.md`](engagement-finalization.md)).
The pre-terminal refusals above all precede the teardown and always return their results.

**A launch's first turn ends without leaving an artifact.** The engagement-launch tools
answer `pending` when the driver's `start` returns, and that answer used to be given even
when the first turn had been cut off mid-flight — the record then sat active with nothing
posted. The `in_casa` launch branches now ask the driver what the turn left behind and, when
it left nothing, hand the death to one owner that records a distinct `launch_turn_incomplete`
error kind, tells the operator in the topic before closing it, and reports the failure in the
envelope. Only that outcome changes: a launch whose turn ran to its end still answers
`pending`, and so does one that lost the terminal race to its own completion, because the
engagement really did report itself. The kind is deliberately its own rather than the generic
driver-start failure — a reader who cannot tell "the driver never got going" from "the turn
ran and then died" cannot act on either. See INV-ENG-011 in
[`engagements.md`](engagements.md).

**A delivery is uncertain.** The send classifiers separate definitive refusal from
uncertainty, and an uncertain Telegram send is deliberately not retried — a duplicate
message is worse than a missing one that the operator can see is missing.

**A question cannot be delivered.** `ask_user`'s operator-DM arm registers its request
before it posts the keyboard, so a delivery failure is discovered when the request already
exists: the broker unregisters it and the tool answers with a typed delivery failure rather
than a request id nobody can answer. Two consequences a caller can rely on. The arm retires a
live *human* question in that DM at registration, in the same indivisible step as its own —
a replacement can never race a tap. It displaces a waiting *machine-timed* question only
after its own keyboard is confirmed on screen and still live, so a question that never
arrived, or that was retired while its post was in flight, costs the operator nothing; the
lane rule itself is in [`jobs-and-delivery.md`](jobs-and-delivery.md).

**A specialist context exhausts its media-send budget.** `send_media` in specialist
context — a specialist engagement, or a delegated turn keyed by its server-stamped
delegation id — carries a code-owned lifetime budget per context, debited synchronously
before the delivery await so parallel calls cannot overrun it. Attempts count; failed and
uncertain deliveries are not refunded (a refund path would let delivery errors mint
budget). Over budget is its own typed refusal telling the agent to reference remaining
artifacts in its result text; a specialist-classified call with *no* quota key is refused
rather than passed unmetered. Residents and executor engagements are unmetered. The
counters are in-memory: a restart refills them — this is a quota, not an authorization.

**Memory cannot answer.** The recall tools report unavailability as its own status and
refuse blank queries outright; neither is ever a fake empty result (INV-MEM-001's
tool-level face).

## Extension points

**A new tool** is a decorated handler added to the registry tuple — that alone puts it on
both transports, which is why the addition is also a security decision; grant filtering and
the coverage ledger pick it up from there.

**An expired or missed plugin-consent DM is recovered by `consent_reprompt`** — the
on-demand, prompt-only re-issue for all three consent kinds (trigger, callback, event), and
the only way to re-surface a committing consent keyboard outside a plugin mutation or
reload: a consent question relayed any other way (`ask_user`, an engagement ask) accepts the
tap, acks it, and commits nothing. The tool never reconciles — no overlay swap, no
setup-round sealing or re-arming — it recomputes each kind's pending set under that kind's
reconcile lock, re-reads the ack store per row (a concurrent Approve earns no fresh
keyboard), threads the sealed setup-round member's nonce back in read-only, and reports
delivery from each keyboard's actual settled post outcome, never from pending rows: when
keyboards were needed and none could be delivered, the result is a typed
`delivery_failed`, not success. Consents the operator explicitly *denied* on a keyboard are
skipped and reported `denied` rather than re-asked — the in-process `consent_denials`
registry records the latest decision in the same synchronous commit step that persists the
ack (Approve clears, Deny records, expiry writes nothing), so agent-driven re-issue can
never nag past a Deny while mutations and reloads re-ask as they always did.

**A new plugin lifecycle operation** follows the established split: synchronous
disk-and-registry ordering in a core, then the async wrapper that takes the lock, reloads,
verifies and pins the envelope.

**A new terminal side effect** for engagements goes after the winner is decided in the
finalize path, and must tolerate running on a record whose other side effects partially
failed.

**A new delivery medium** touches the media policies, filename validation and the send
classifier together — the classifier is where refusal-versus-uncertainty is decided, and
that distinction is the contract.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::create_casa_tools`
- `casa/rootfs/opt/casa/tools.py::_result`
- `casa/rootfs/opt/casa/tools.py::select_casa_tools`
- `casa/rootfs/opt/casa/tools.py::emit_completion`
- `casa/rootfs/opt/casa/tools.py::plugin_add`
- `casa/rootfs/opt/casa/internal_handlers.py::_make_internal_tools_call_handler`
- `casa/rootfs/opt/casa/mcp_envelope.py::_tool_schema`

**Tests**
- `tests/test_internal_handlers.py`
- `tests/test_plugin_tools.py`
- `tests/test_emit_completion_tool.py`
- `tests/test_assistant_prompts.py`

**Related**
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
<!-- END SOURCEMAP -->
