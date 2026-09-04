---
last_reviewed: 2026-09-01
---

# Scheduled asks

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The other durable obligation shaped like a job: a question a resident's own schedule asked
the operator and is waiting on — its record on disk, its terminal delivery back to the
session that asked, and its manners in the operator's attention lane. It does not cover the
job ledger it is modelled on ([`architecture/jobs-and-delivery.md`](jobs-and-delivery.md)),
the trigger that fired the schedule ([`architecture/triggers.md`](triggers.md)), or the
authorization challenges that share the lane
([`architecture/plugins.md`](plugins.md)).

## Mental model

**A scheduled question is a durable obligation, not a message.** When a resident's own
schedule asks the operator something, the question outlives the turn that asked it: the
broker holding it is in-memory, so a record on disk is what keeps a keyboard on screen
honest across a restart, and what guarantees the waiting session is eventually told
*something*. It is the same disk-leads discipline as a job, with the opposite duplicate
policy (INV-JOB-006), and it is deliberately timid about the operator's attention —
a machine-timed question yields to a human one in both directions, and only to a human
one that actually reached the screen (INV-JOB-008).

## Contracts & invariants

**INV-JOB-006**: A scheduled question's durable record is written before its keyboard is posted and moved to settling before any terminal action, so a restart restores an unexpired question and never replays a settled one.

Enforced by the record's compare-and-set state machine: `posting` before the post, `live`
once the message id is known, `settling` before the first terminal edit, dropped after the
terminal continuation is dispatched. Deletion is not the acknowledgement — `settling` is.
The boot reconciler restores a `live` record with its remaining timeout and the identical
broker binding, settles an expired, unconfirmed or operator-changed one, and drops a
`settling` one in silence.

One asymmetry is worth stating, because it is the seam between the two halves of this
design. Revocation reads the broker, never the record file (INV-JOB-008) — but from process
start until this reconcile runs, records exist on disk and the broker is empty, so a
revocation landing in that window cancels nothing. Each revocation therefore leaves a
process-local MARKER carrying the selector it used — a role, a role and a trigger's labels,
or a chat — and the reason it gave (INV-JOB-012), and the reconciler settles, rather than
restores, a record that matches one.
Not every retirement is a revocation, and only revocations mark. A DISPLACEMENT — a
human question taking the lane — has nothing to settle: if the live map is empty, the
previous process's keyboard is still on screen, unedited, and nobody was told anything,
so marking the record revoked would assert an event that did not happen. Those callers
deliberately leave no marker and let this reconcile decide from the lane's real
occupancy. The markers are retired once the pass completes, after which every surviving
record is in the broker and a revocation's own scan sees it. The rule stays intact — no live decision
reads the store — and the one state where the broker cannot speak for it is handled where
the store is legitimately read. The selector has to be the revocation's OWN: keying it on
the role's lifecycle epoch, which a single-trigger revocation also bumps, discarded every
other question that role was waiting on.

What it does not cover: exactly-once. The crash window between "decided" and "dispatched"
resolves toward at-most-once — the opposite of INV-JOB-004's choice, and deliberately so: a
duplicated answer makes a resident act twice on one confirmation, while a lost one leaves an
unanswered question in a session that keeps working. A record still `posting` at boot may
also leave an orphaned keyboard on screen, which a tap answers with "expired".

**INV-JOB-007**: Every terminal outcome of a scheduled question — answered, expired, or cancelled for any reason — is delivered back to the session that asked, as a machine-authored scheduled turn.

Enforced by the scheduled ask's finish hook, the single owner of the keyboard edit, the
continuation and the record. The continuation reproduces the *firing* turn's shape (the
session label as chat id, the same scheduled-delivery marker, the epoch the question was
asked under) and carries no trusted user origin: the operator's tap is reported in the
turn's content, never as its speaker, so it cannot relabel a machine-authored session.

What it does not cover: the shutdown cancel, which settles nothing, edits nothing and leaves
the record for the next boot — the keyboard is still on screen and the question is still
honest. Nor does it cover a bus enqueue that is accepted and then never runs.

**INV-JOB-008**: A scheduled question never displaces a live operator question: it is admitted only into an idle attention lane, and a human question retires a live scheduled one only once that human question is itself delivered and still live.

Enforced synchronously in the broker: registration with an idle requirement across both
halves of the lane (plain asks and authorization challenges are separate scopes), and a
predicate cancel run after the keyboard is on screen, guarded by asking the broker's live
map for that exact request id — in one no-await block, so a tap cannot land between the
question and the answer. Refused admission, the tool answers `operator_busy` and asks
nothing. The direction is one-way by design — a human question supersedes a machine one,
never the reverse.

The delivered-and-still-live half is the whole rule, not a detail of it. Retiring a
scheduled question cannot be undone: its finish hook has already edited the keyboard to
expired and delivered the terminal continuation to the session that asked (INV-JOB-007).
So a question that retires one at ADMISSION and then fails to post leaves the operator
with neither — the machine one expired, the human one never on screen. Displacing at
delivery instead makes that state unreachable on the live path rather than rarer (the one
window it does not reach is named below), and there is nothing to compensate afterwards
because nothing was spent. A challenge that displaces therefore
leaves no boot-revocation marker: it has taken the lane in this process, and the marker
exists only to speak for a revocation the live map could not see.

What it does not cover: a boot reconcile that runs strictly BETWEEN a challenge's
registration and its post settling. The idle requirement reads scope occupancy and cannot
tell a posting challenge from a delivered one, so it settles the durable record
`operator_busy` and a post that then fails leaves neither question. That loss belongs to
the reconciler's notion of occupancy, not to the challenge's ordering, and no ordering
change closes it.

What it does not cover: selection from the durable record file. Live decisions read the
broker, which is synchronous; the record file is written after an await and would miss an
ask that had just won its lane.

**INV-JOB-012**: A revocation that lands in the boot window settles the record it selected with the reason its caller passed, so the boot path tells the waiting session the same reason the live path would.

Enforced by the marker itself: it carries the reason alongside the selector, and the reconciler
settles with what it finds rather than a literal of its own. The live path never had this
problem — the broker's predicate cancel carries the caller's reason into the finish hook — so
what this closes is a disagreement between two paths that describe the same event. A trigger
reloaded, a role evicted, a reminder swept away and a reminder cancelled are four different
things to a resident reading its own continuation, and inside the window all four used to
arrive as one.

When two markers match one record the FIRST wins, which is not an ordering left over from the
predicate this replaced: had the record been live, the earliest matching revocation would have
cancelled it through the broker and every later one would have found nothing to select. The
window reproduces the live path's own outcome, reason included.

What it does not cover: the fidelity of a reason to anything outside Casa. The guarantee is that
the boot path repeats the caller's word, not that the word is well chosen.

**Ordinary conversation is not a claim on that lane.** The rule that a plain DM message
resolves a pending question — the text *is* the answer — is true of a question this
conversation asked and false of a machine-timed one, whose answer routes to the session that
asked it and can never be carried by a turn of the operator's own session. Since the
scheduled question moved into the operator's plain-ask scope, a message retired it there
along with everything else, under an ending the promise made for scheduled questions does
not contain. Plain text now retires only the human-raised asks in that DM, selected by the
same `scheduled` marker the boot restore rebuilds — so a restored question is protected by
exactly the rule a live one gets, with no second mechanism to keep in step. The direction
above is unchanged: a human question still supersedes a machine one, never the reverse. What
changed is when — the displacement happens once the replacement is DELIVERED and still live,
not when it is merely registered, so a keyboard whose post fails, or which is cancelled while
that post is in flight, no longer takes a waiting question with it. Since #680 that is the
whole rule, with no exception: an authorization challenge — and every other challenge the
coordinator raises — displaces on delivery too, from its own driver, rather than clearing
the lane at admission.

## Failure behavior

**The keyboard's post fails, or the process dies while a question is still `posting`.** The
record exists before the keyboard does (INV-JOB-006), so a post that never lands is settled
by the next boot's reconcile rather than restored, and a keyboard that did land for a record
the process lost is answered at a tap with "expired" — the crash window resolves toward
at-most-once, deliberately.

**A human question's post fails while a scheduled one is waiting.** The scheduled question
is untouched: displacement happens on delivery, not admission (INV-JOB-008), so a
replacement that never reached the screen has cost nothing.

**The process is stopping.** The shutdown cancel settles nothing, edits nothing and leaves
the record for the next boot (INV-JOB-007's stated exclusion); the keyboard stays on screen
and the question stays honest.

## Extension points

**A new terminal outcome** belongs in the finish hook — the single owner of the keyboard
edit, the continuation and the record — so that every ending still reaches the session
that asked, machine-authored (INV-JOB-007).

**A new lane rule** belongs in the broker's synchronous admission and its predicate cancel,
never in the record file: live decisions read the broker, and the file is written after an
await (INV-JOB-008).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/scheduled_asks.py::ScheduledAskStore`
- `casa/rootfs/opt/casa/scheduled_asks.py::make_finish_hook`
- `casa/rootfs/opt/casa/scheduled_asks.py::reconcile_at_boot`

**Tests**
- `tests/test_scheduled_ask_user.py`
- `tests/test_scheduled_ask_attention_lane.py`
- `tests/test_challenge_delivery_displacement.py`
- `tests/test_scheduled_ask_boot_window_displacement.py`

**Related**
- [`architecture/jobs-and-delivery.md`](../architecture/jobs-and-delivery.md)
- [`architecture/triggers.md`](../architecture/triggers.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
<!-- END SOURCEMAP -->
