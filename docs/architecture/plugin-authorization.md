---
last_reviewed: 2026-09-27
---

# Plugin authorization

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What stands between a plugin's protected tools and an operator's approval: the single-use
grant a protected call consumes, the challenge that mints it, who may answer that challenge,
and what retires it. Which tools are protected, and the registry and store a plugin is
installed through, are [`plugins.md`](plugins.md).

## Mental model

**Approval is per call, not per install, and it does not survive a restart.** A protected
tool call by a resident or specialist consumes a single-use grant bound to a specific
operator, chat, role, artifact, tool name and exact arguments, and it lives for the grant
TTL — the approve edit says so ("once with exactly these arguments in the next 5 minutes").
The grant store is in process memory only.

## Contracts & invariants

**INV-PLUG-004**: A protected tool call from a resident or specialist proceeds only by consuming an exact, single-use grant.

Enforced by the authorization hook, with consumption made atomic in the grant store. The
grant binds operator, chat, tier-stripped role, artifact, full tool name, and a hash of the
canonical arguments — so an approval authorises one action with one argument set, not a
capability.

What it does not cover: unprotected plugin tools, and the executor path entirely. And
"proceeds by consuming" applies to direct resident calls, ephemerally delegated
specialists, and — since #400 — interactive specialist *engagements*: a protected call whose
provenance is an engagement routes through the same DM authorization challenge, but its grant
additionally binds the engagement id, so an approval minted inside one engagement can never
consume a matching call in another. Identity for the engagement path comes from the live
engagement record (its own operator DM), and on approval the challenge resumes that
engagement rather than a resident continuation. Both taps resume it — a denial dispatches its
own continuation so the engagement stops retrying — and both take the engagement's ingress
reservation at the tap's synchronous commit step, before either the approval edit or the
dispatch. That ordering matters here specifically: this arm awaits a message edit *before* it
dispatches, so without the reservation a successful completion committing during that round
trip would take the engagement terminal while the continuation still had no existence anywhere.
The edit still precedes the dispatch — the reservation makes that order harmless rather than
moving it — and if the dispatch does not hand off, the DM is corrected to say that the
engagement could not be resumed to use the approval and to ask for it again, because that
grant can serve no other caller. An engagement that ends before the tap takes its unanswered
challenges with it (INV-PLUG-033).
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md) owns the
reservation's lifetime. A non-authorizable engagement record still denies, fail-closed,
before any grant lookup: authorizable means an active record with a topic and a reachable
operator, and of a kind that carries an approval seam — a specialist, or a resident-hosted
plugin-job worker whose identity is additionally bound to its host role and its pinned
artifact ([`background-jobs.md`](background-jobs.md)).

A *delegated* protected call on the DM path — no engagement id, the enforcing specialist
differing from the resident that delegated — is approved back to that resident, which must
delegate again. Its delegation may still hold the specialist's slot: a sync delegation that
outlives its wait degrades to pending and keeps the slot until the run ends, so a
continuation dispatched at once would be refused `busy`. After the approval edit, the
decision's continuation therefore waits, bounded under the grant TTL
(`SpecialistLimiter.wait_until_free`, through `tools.wait_for_delegation_slot` and the scope
`_prelaunch` computes), until that scope holds no permit, and on timeout dispatches as
before; the wait reserves nothing. It waits on a coordinator-owned task, never inside the
finish hook, because the broker's global hook drain — which engagement finalization and
shutdown both await — must not wait on a specialist; the shutdown drain cuts any such wait
short and delivers at once. Raising or
reusing the challenge also records the delegation id
(`authz_grants.note_delegation_awaiting_approval`) before the post settles, and forgets it
if the post fails or the challenge was retired unanswered while it posted — an answer that
raced the post keeps it. The resident turn later synthesized from that delegation's completion
notification runs at an origin no grant serves, so it takes the record once and is told not
to retry or re-delegate the action and to say nothing about approvals — the approval's own
continuation carries the action out. The record is in-process, capped and advisory: losing
it costs a confusing reply, never an unapproved call.

**A delegated call raised inside a specialist desk turn is approved back to the desk.** The
identity's advisory `desk_role` rides the challenge record as its destination, read at
settle time — so a pending challenge the resident's delegation raised and a desk then raised
again with the identical call is promoted to that desk — and the continuation is dispatched
to the desk without the slot wait above (the desk's own queue and permit admit it), after
re-checking that the approver is the operator and the specialist is still delegable
([`specialist-desk.md`](specialist-desk.md)); the grant key and its single-use binding are
unchanged. On a pinned stored-call turn the stored-call pin runs as the admission hook's FIRST
check, so a protected tool that is not the stored call is denied before the authorization
decision — no challenge is posted for it ([`stored-call-buttons.md`](stored-call-buttons.md)).

Every challenge the coordinator raises — the protected-tool one above and the trigger,
callback, event, specialist-install and persona-install consents alike — shares the
operator's attention lane with any machine-timed question already waiting there. The lane
rule itself lives with the scheduled question in
[`scheduled-asks.md`](scheduled-asks.md) (INV-JOB-008), and the part that matters
here is one line: a challenge retires a waiting scheduled question only once its own
keyboard is on screen, so a challenge that fails to post does not cost the operator the
question that was waiting. A boot reconcile running while the challenge's post is still in
flight reads the lane the same way and restores the waiting question (INV-JOB-014).

The protected-tool challenge body interpolates the model's own tool arguments, so it is
model text: `register_challenge` resolves the scope before it creates the poster — a bound
engagement's first, else the running turn's (`output_boundary.resolve_scope`) — the poster
admits the body under it, and posts it as Casa's own text when neither is bound, so a
challenge raised by a turn that listed an inbound file and read none carries the same Casa
line as the turn's other output ([`output-boundary.md`](output-boundary.md)).

Who may approve is a separate guarantee, INV-PLUG-007: read this invariant as "one
approval authorises one action" and that one as "the approver is the configured operator".

**INV-PLUG-005**: Grants exist only in process memory; a restart revokes every one of them.

There is no persistence path. Revocation additionally happens on consumption, on TTL expiry
plus a periodic sweep, on a chat reset, on role reload or removal, and on plugin update or
removal.

Trigger consent is the deliberate exception: a webhook-trigger acknowledgement is persisted
and re-validated from disk at startup, because it authorises a route rather than a call.

**INV-PLUG-007**: An authorization challenge is posted, and its grant minted, only for a turn whose sender is the configured operator; any other sender's protected call is denied outright, before any grant lookup and with no challenge.

Enforced in the authorization hook through the Telegram channel's single operator rule —
the same sender-id match that decides attribution and clearance — so the person who taps
Approve is the person the deployment names as operator, not whoever asked. Denying without
a challenge is the point: posting one would hand the requester their own approval button.

What it does not cover: with `telegram_chat_id` empty ("accept all chats") there is no
configured operator, so protected tools are denied for every sender — deliberate, and
announced by a warning at channel construction. The in-engagement permission relay now
follows the same rule (its keyboard is answerable only by the configured operator, and
with none configured it denies immediately rather than posting one); in-engagement
*questions* remain answerable by the engagement's creator, since an answer is interaction,
not authorization. Sender identity itself is Telegram's authentication of its user ids,
not an additional Casa-side proof.

**INV-PLUG-033**: An engagement that reaches a terminal state in this process leaves no answerable authorization challenge bound to it — its unanswered challenges are withdrawn.

Enforced by the registry's terminal observer (see
[`engagement-finalization.md`](engagement-finalization.md)): whichever path wins the
terminal transition — completion, cancellation, error — the tools layer calls
`ChallengeCoordinator.cancel_matching(engagement=…, reason="engagement_ended")` in that same
step, so no tap can commit after the transition returns; it
cancels every live challenge whose grant key carries that engagement id, and the keyboard is
retired as "withdrawn when the engagement that asked for it ended". A tap after the end
would otherwise mint a grant bound to an engagement nothing will ever resume. The empty
engagement id of the DM path is never a filter, so resident and delegated challenges are
untouched.

What it does not cover: a tap that committed before the transition — its continuation meets
a terminal record and the DM is corrected to "ask for it again"; an engagement that ended
in an earlier process, whose challenges a restart already dropped with every grant
(INV-PLUG-005); and challenges not bound to an engagement.

## Failure behavior

**A protected tool is called without an approval.** The hook denies the call and posts or
reuses an approval challenge to the operator. The retry must present identical canonical
arguments — a changed argument is a different grant.

**The engagement that raised a challenge ends before the tap.** The challenge is withdrawn
and its keyboard says so (INV-PLUG-033); nothing is resumed.

**A delegated call is approved while its specialist is still running.** The continuation
waits, bounded, for the specialist's delegation slot, then goes to the resident; if the slot
never frees in time it is dispatched anyway and may be refused `busy`, which the unconsumed
grant survives until its TTL.

**The authorization hook itself fails.** Any unexpected exception becomes an explicit deny;
only cancellation is re-raised. The hook fails closed.

## Extension points

**A new engagement kind that may make a protected call** must carry an approval seam and
bind its identity into the grant key, as the plugin-job worker binds its host role and
pinned artifact; until it does, its record is not authorizable and every protected call
from it is denied before any grant lookup (INV-PLUG-004).

**A new challenge the coordinator raises** shares the operator's attention lane, and so
retires a waiting scheduled question only once its own keyboard is on screen
([`scheduled-asks.md`](scheduled-asks.md), INV-JOB-008).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/authz_grants.py::GrantKey`
- `casa/rootfs/opt/casa/authz_grants.py::GrantStore`
- `casa/rootfs/opt/casa/authz_grants.py::ChallengeCoordinator.cancel_matching`
- `casa/rootfs/opt/casa/authz_grants.py::note_delegation_awaiting_approval`
- `casa/rootfs/opt/casa/specialist_limits.py::SpecialistLimiter.wait_until_free`
- `casa/rootfs/opt/casa/tools.py::wait_for_delegation_slot`

**Tests**
- `tests/test_authz_grants.py`
- `tests/test_authz_hook.py`
- `tests/test_approval_outlives_raiser.py`

**Related**
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/scheduled-asks.md`](../architecture/scheduled-asks.md)
- [`architecture/delegation-announcements.md`](../architecture/delegation-announcements.md)
<!-- END SOURCEMAP -->
