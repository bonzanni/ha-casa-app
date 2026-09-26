---
last_reviewed: 2026-09-16
---

# The dispatched setup turn

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What the turn Casa dispatches to run a plugin's setup tool evidences, how that evidence
settles or returns the obligation, how a courier turn's delegation is correlated, and whose
identity the turn carries. Who owes the setup run and the consent verdict that releases it
are [`plugin-setup.md`](plugin-setup.md); the gate a released obligation passes through
before the send is [`plugin-setup-dispatch-gate.md`](plugin-setup-dispatch-gate.md); what a
link the setup tool produces becomes is [`plugin-result-contract.md`](plugin-result-contract.md).

## Mental model

**Bus acceptance is not evidence.** Acceptance marks the obligation `dispatched` before the
turn runs, and that used to be terminal even when the turn had no setup tool to call. Now
the turn reports back what it actually evidenced: a non-error result from the tool, or a
completed turn whose session listed the tool, keeps a resident row consumed; anything
less returns it to `pending` with its released verdict kept, under a bounded budget, and
exhaustion fails it with a note naming the manual run. A courier turn — the assistant
delegating a specialist's setup — is judged on its delegation, by target, and on nothing
else. An ordinary turn in which the resident ran the setup tool itself settles the row
from that evidence, so the next reload does not ask again.

**The turn carries the operator's identity, for exactly the role it is addressed to.**
The dispatch is composed with the configured operator's chat and user, and the dispatch
worker stamps the role the obligation is for beside the setup marker. The grant identity
derivation reads that stamp before either of its returns: the executing role must equal
it, the ids must still be the operator's as configured at that moment, and an engagement
never inherits it. That identity is what lets a setup tool declared as a capability
deposit a sign-in link and have Casa deliver it to the operator's chat, and what lets a
protected tool the target calls be approved through the operator's keyboard.

## Contracts & invariants

**INV-PLUG-012**: A resident-execution setup obligation rests consumed (`dispatched`) only when its dispatched turn positively evidenced the setup tool — the tool produced a non-error result, or the turn completed with the session's init listing the tool and no attempted call erring without one; a turn with no such evidence (including one that raised or was cancelled, whose listed-but-uncalled tool evidences nothing because no reply was produced) returns the obligation to `pending` with its released verdict intact, boundedly, and past the bound it fails with an operator note rather than being silently spent.

**INV-PLUG-023**: A released resident-execution setup obligation is also consumed when its setup tool produces a non-error result in any turn of the executing resident, provided the invocation is proven to follow the release and to run in a session built on the obligation's exact artifact — a finite `tool_use` timestamp not earlier than the row's finite `released_ts`, and the executing agent instance's own plugin binding carrying that artifact while the registry still resolves to it; a row so settled (`settled_by`) is never re-dispatched, a dispatched turn that finds it settled once it holds the session gate does not run, and a later toolless report from the dispatched turn cannot reopen it — evidence that proves less (an earlier invocation, an unstamped or non-finite stamp, another artifact, another role, another tool, a specialist target) settles nothing, and evidence from a delegated session settles no `pending` or `dispatched` row.

**INV-PLUG-030**: A `failed` or `stale` setup obligation of the current installation — released, unsettled, and not stamped by a removal — is cleared (`dispatched`, `settled_by`, with the role that ran it) when its setup tool produces a non-error result in a session of the plugin's executing role, of either tier, whether an ordinary resident turn or a delegated session: the invocation must not precede the row's finite `released_ts` (for a row released before that stamp existed, its finite failure stamp), the session's own binding must carry the row's artifact while the registry still resolves to it, and the tool must be the one the dispatch composes; a cleared row leaves plugin health, the persisted report is regenerated under the plugin lock every live regeneration holds and waited for, boundedly, before the turn that ran the tool continues, and it is neither re-dispatched nor re-armed by the reconcile sweep; a `refused` row, a removal-stamped row, and evidence from any other role, artifact or tool clear nothing.

**INV-PLUG-024**: A specialist-target setup obligation, whose dispatched turn is a courier turn asking the assistant to delegate the setup to the specialist, rests consumed (`dispatched`) only when that courier turn produced a non-error `delegate_to_agent` result whose target, canonicalised as the delegation ACL resolves it, is the row's specialist — a delegation to any other agent, an errored one, and a listed but uncalled delegation tool all evidence nothing for a courier, whether or not the turn completed; a courier turn with no such result (including one that raised, was cancelled, or replied with silence) returns the obligation to `pending` with its released verdict intact, under the same bounded budget as a resident turn, and past the bound it fails with an operator note naming the delegation that could not be made; the delegation tool is recorded under its own key, never as the row's expected setup tool, so no ordinary turn's delegation result can settle a specialist-target row; and a `dispatched` row that carries neither an expected tool nor a courier key and no settlement mark — one no turn will ever report on — is retired by the next worker pass as `failed` with a reason naming the manual run and one operator note, never re-dispatched.

**INV-PLUG-027**: An origin carrying Casa's `plugin_setup` marker yields a grant identity only when it is Telegram-shaped, addressed to the operator as configured at the time of the call, read on a direct or delegated turn, and its Casa-stamped `plugin_setup_target` equals the executing role; the same origin read from an engagement record yields none, at launch and on resume; every other synthetic marker yields none; the marker and the target cannot be supplied from outside Casa; and a turn carrying the marker can delegate only in `sync` mode, so no engagement is ever created from it.

The dispatched turn is addressed to the configured operator's own chat, and it now has
the identity that lets a setup tool declared as a capability deposit a link and have it
delivered there (INV-PLUG-025). The dispatch worker stamps, beside the marker, the plain
role id the obligation is for — the resident for a resident-target row, the specialist
for a courier row. The stamp is a reserved context key, copied by the agent onto the
turn's origin, inherited by every delegated child and copied into any engagement record,
and the identity derivation reads it first, before either of its returns: a
resident-target row gives the resident's own turn an identity; a courier row gives the
resident's turn none (it only delegates), the named specialist's sync-delegated turn
one, and a delegation to any other role none. An engagement whose stored origin carries
the marker has none, whatever its ambient origin says, because an engagement outlives the
turn that could have created it. The ids are compared to the operator as configured at
the moment of the call, read from the live Telegram channel, so a snapshot is never
trusted where a live authority exists; no channel up means no identity. The one widening
this brings is deliberate: a protected tool the dispatched target calls on a setup turn
becomes approvable through the operator's keyboard, where it was refused for lack of
identity — the operator approved the plugin's consent, the turn is addressed to their
chat, and the challenge still goes to them. The `sync`-only rule is enforced in the
delegation tool's mode gate, before target resolution or any topic: an interactive launch
would return a non-error pending result that the courier report counts as a delegation
that went through, while the engagement's own setup tool is refused for identity.

What it does not cover: turns with no identity at all — voice, webhook, scheduled,
callback-nudge and every other synthetic marker, and executor sessions — which refuse a
capability tool before it runs, as before; a renewal a scheduled reminder tries to start
cannot mint a link, and the resident asks the operator instead.

## Failure behavior

**The dispatch is accepted but the session cannot run the tool.** Bus acceptance marks the
obligation `dispatched` before the turn runs, and until v0.184.0 that was terminal even
when the turn then had no setup tool to call — observed live when a just-published
artifact's MCP server failed to come up in a session built moments after an agent
reconstruction, so the one automatic run was silently spent. Now the turn itself reports
back (INV-PLUG-012): the agent correlates the episode marker on the dispatched turn with
what the turn actually evidenced — a non-error result from the tool consumes the
obligation; a completed turn whose init listed the tool consumes it too (an available tool
the agent chose not to call is its reply's business); anything else — the tool absent,
availability unknown, every attempted call an error, the turn raising or cancelled before
it could reply — returns the row to `pending` with its released verdict kept, and the next
reload or reconcile kick re-dispatches. Completion matters for the availability rule
alone: that rule rests on the agent's reply reporting what it chose, and a raising or
cancelled turn produced no reply, so listing without a call evidences nothing there while
a non-error result collected before the cancel still counts. Deliberately no immediate retry: the broken session is
usually a warm one that would fail identically, and the healer in practice is the next
agent reload. The budget is bounded; exhausting it fails the obligation with a note naming
the manual run.

**The assistant runs the setup tool itself before the re-dispatch.** Observed live on
2026-09-16: the dispatched turn ran toolless (a cold session whose plugin MCP server was
still connecting), the row went back to `pending`, and on its next ordinary turn the
assistant ran the setup tool and the operator completed the authorisation — after which
the next reload would have asked for the setup again. The row is now settled from that
evidence (INV-PLUG-023): the agent hands every non-error plugin-tool result of an
ordinary turn to `settle_from_tool_evidence` the moment it observes it, under the
per-session write gate and the client lock, with the wall-clock instant of the
`tool_use` block and the agent instance's own resolved binding. That handover is I/O-free
on the common path: the release captures the composed setup-tool name on the row (the
dispatch captures the same name; an artifact is immutable, so it cannot drift), an
in-memory watch holds the names carried by released unsettled rows — recomputed from the
store on every loop-thread read and every save, never from a reader on another thread,
whose snapshot may predate a release — and only a result for a watched name reads the
file; every other result returns without a store read or a resolver call. The store
settles only
on positive proof — a finite invocation time not before the row's `released_ts`
(stamped at release, so a row released before v0.315.0 is never settled this way), the
binding and the registry both naming the row's artifact, the plugin's execution target
being this resident, and the tool being the one the dispatch would compose. The worker
re-reads the row immediately before every send, a dispatched turn re-checks
`dispatch_still_owed` once it holds the session gate and returns without a client or a
prompt when the obligation is settled, and the dispatched turn's own report is a no-op on
a settled row. Turns on different session keys are not serialised against each other, so
a setup run from another channel while a dispatch is already executing can still be
followed by that dispatch's run. The status tool reads a settled row as "setup ran (the
assistant ran the setup tool itself)" whatever status a later removal leaves on it. A specialist-target dispatch stays delivery-only as far as the SETUP tool goes — the
assistant is just the delegation courier there, and its own session says nothing about the
specialist's — but the courier turn does say whether the delegation went through.

**A setup that failed is run by hand, and succeeds.** Every failure note asks the operator
to run the setup manually — observed live in #1051: the courier's retries were spent while
an install was still wiring the delegation, the row went `failed`, the operator had the
assistant delegate the setup to the specialist, the specialist ran it successfully, and plugin
health still announced that setup "could not finish" afterwards. A successful run now
clears the terminal row (INV-PLUG-030). The same handover that settles a live row carries it
for a resident; a delegated session hands its own successful plugin-tool results over with the
binding of the resolution its session was built from, flagged as delegated, and a delegated
session can clear only a terminal row — it holds no resident session gate, so a live row
stays with the dispatch and its courier. The watch gains the plugin names of clearable rows,
since a courier row carries no expected tool once dispatched, so a session whose binding
names none of them still reads no file. Plugin health is a persisted report, so a clearing
settlement regenerates it before the turn continues; the reply to the manual run reads the
new report for its notice. The regeneration takes the plugin lock like every other live
one — the report lock orders only the write, so a pass that computed from the still-failed
row could otherwise land afterwards and restore the notice. It runs in a task of its own
and the turn waits for it with a bound, since a lock holder may itself be waiting on the
turn; past the bound it finishes in the background. A `refused` row is not cleared this way (its way back is a consent
decision), and a removal-stamped row belongs to the reinstall sweep.

A courier turn is not sent until the assistant declares the specialist as a delegate
([`plugin-setup.md`](plugin-setup.md), INV-PLUG-029), so the courier's retry budget is not
spent on refusals that happen before an install has finished wiring the delegation.

**The courier's delegation is the evidence.** A plugin that targets only a specialist has
its setup composed as a courier turn to the assistant, and the courier session never
carries the specialist's setup tool, so the row records no expected setup tool. What the
courier session does carry is the delegation tool, and the dispatch records that name and
the target specialist on the row under their own keys. The courier turn's report
correlates the delegation by target (INV-PLUG-024): the agent records the target of every
`delegate_to_agent` call it observes, canonicalises the targets of the calls that produced
a non-error result exactly as the ACL resolves them (a persona display name becomes its
role id), and hands the set over; a non-error delegation to the row's own specialist
consumes the row (the specialist's own reply, relayed by the assistant, reports the setup
result), and nothing else does — a delegation to some other agent, an errored one, an
absent or merely listed-but-uncalled one, all return the row to `pending` under the same
bounded budget a resident row has, and exhaustion fails it with a note naming the
delegation that could not be made. The resident rule's "listed but uncalled is the reply's
business" clause is not carried over: that clause rests on the reply reaching the
operator, the report runs before silence suppression and delivery, and for a courier the
delegation *is* the turn, so there is no reply for it to be the business of. The keys are
deliberately not `expected_tool`: that name feeds the ordinary-turn evidence watch, and a
specialist target must stay outside settlement (INV-PLUG-023). A `dispatched` row that
carries neither key and no settlement mark — one dispatched before the courier keys existed,
which no turn will ever report on — is neither left as it is (a silent spend: nothing in
health, no note) nor re-armed (an evidence-free re-dispatch could run a setup tool a second
time against an external service). The next worker pass retires it as `failed` with a
reason naming the manual run; plugin health carries it, the operator is told once, and a
removal followed by a reinstall of the same artifact re-arms it as a fresh attempt
(INV-PLUG-020).

**The dispatch is accepted and the tool runs, but the integration is broken.** Delivery
and in-session execution are what the obligation tracks; the executing agent reports the
tool's own outcome to the operator. Casa makes no claim of its own about whether the
integration works — it cannot see the external side (INV-TOOL-005).

**The courier delegates in a mode other than `sync`.** The delegation tool returns an error
before any engagement or topic exists (`mode_unsupported_on_setup_turn`); an error result
is not a non-error delegation, so the row returns to `pending` under the courier's bounded
budget (INV-PLUG-024) rather than resting consumed with the setup never run.

**The configured operator is not the one the dispatch was composed for.** The setup
identity is refused (INV-PLUG-027); a setup tool that deposits a link has its result
withheld, and a protected tool it calls is denied as on an unsupported origin.

## Extension points

**Changing what evidence consumes a row** means changing what the turn reports and what
the store compares — both sides of one correlation; a rule added on one side alone is how
a row ends up consumed by a turn that did nothing.

**A new session kind that may run a setup tool** must carry the marker and the stamp
through to its origin and refuse the identity wherever it cannot prove the executing role
is the stamped one.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_setup_episodes.py::report_dispatch_outcome`
- `casa/rootfs/opt/casa/plugin_setup_episodes.py::settle_from_tool_evidence`
- `casa/rootfs/opt/casa/plugin_setup_episodes.py::dispatch_still_owed`
- `casa/rootfs/opt/casa/agent.py::Agent._report_setup_outcome`

**Tests**
- `tests/test_plugin_setup_evidence_settlement.py`
- `tests/test_agent_setup_evidence_turn.py`
- `tests/test_delegated_setup_evidence.py`
- `tests/test_authz_grants_setup_identity.py`
- `tests/test_delegate_setup_mode_gate.py`

**Related**
- [`architecture/plugin-setup.md`](../architecture/plugin-setup.md)
- [`architecture/plugin-setup-dispatch-gate.md`](../architecture/plugin-setup-dispatch-gate.md)
- [`architecture/plugin-result-contract.md`](../architecture/plugin-result-contract.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
<!-- END SOURCEMAP -->
