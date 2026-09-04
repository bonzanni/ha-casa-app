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
policy (INV-JOB-013), and it is deliberately timid about the operator's attention —
a machine-timed question yields to a human one in both directions, and only to a human
one that actually reached the screen (INV-JOB-008).

## Contracts & invariants

**INV-JOB-013**: A scheduled question's durable record is written before its keyboard is posted, and the compare-and-set that moves it to `settling` carries the exact terminal text it decided — so a restart never re-dispatches a settled question, and replays exactly that one already-decided, idempotent keyboard edit for every record that carries both the text and a message id.

Enforced by the record's compare-and-set state machine: `posting` before the post, `live`
once the message id is known, `settling` — with the terminal text — before the first terminal
edit, dropped after the terminal continuation is dispatched. Deletion is not the
acknowledgement — `settling` is. The boot reconciler restores a `live` record with its
remaining timeout and the identical broker binding, settles an expired, unconfirmed or
operator-changed one, and re-applies the decided edit to a `settling` one without dispatching
anything.

The text rides with the state because the state alone cannot say what the screen should read.
A crash between "decided" and "edited" leaves a keyboard that still looks answerable, and the
operator taps it and is told the question expired — the same picture whether the outcome was a
cancellation or an answer. Guessing is worse than silence here: an invented "expired" body
would overwrite a keyboard that already reads "Answered: Confirm" with a false account of it.
Persisting the text removes the guess, and replaying it is safe because an identical re-edit is
success rather than an error, so a crash after the edit costs nothing.

Which action the crash window forbids replaying is worth stating rather than implying: the
DISPATCH, and only the dispatch. The keyboard edit is idempotent and, being the text the record
itself decided, cannot say anything the outcome did not.

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
also leave an orphaned keyboard on screen, which a tap answers with "expired". Nor does it
cover a `settling` record that carries no terminal text, or one whose message id was never
captured: neither gives the edit anything truthful to say or anywhere to land, and both are
dropped in silence. Nor does it cover whether the replayed edit LANDS. It is attempted once and
the record is dropped either way, because the transport answers a transient rejection and a
message the operator has deleted with the same failure, so retaining the record to retry would
retain it at every boot for the one that will never succeed. A replay that fails leaves exactly
the state a record with no persisted text leaves: the keyboard as posted, nothing dispatched, and
a tap answered with "expired".

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
delivery instead makes that state unreachable rather than rarer, and there is nothing to
compensate afterwards because nothing was spent. A challenge that displaces therefore
leaves no boot-revocation marker: it has taken the lane in this process, and the marker
exists only to speak for a revocation the live map could not see.

What this rule does not itself decide: how a BOOT reconcile reads the same lane. That is
INV-JOB-014, below — the reconciler answers irreversibly and therefore asks a stricter
question.

What it does not cover: selection from the durable record file. Live decisions read the
broker, which is synchronous; the record file is written after an await and would miss an
ask that had just won its lane.

**INV-JOB-014**: At boot, a durable scheduled question is restored over an occupant of its lane only when that occupant is a human-raised question whose keyboard post has not yet recorded a message id; a delivered occupant, and any machine-timed one, refuses the restore and settles the question `operator_busy`.

The live path and the boot pass ask the same question of the lane and are right to answer it
differently, because a refusal costs a different amount in each. On the live path a refused
machine question is simply not asked, and nothing is lost by treating a request that has just
registered as owning the operator's attention — it is about to post. In the boot pass a refusal
SETTLES the durable record: the keyboard is edited to expired and the session that asked is
told, and none of that can be taken back. A challenge or a human question whose keyboard is
still in flight would therefore destroy a question the operator can currently see, in exchange
for one that may never arrive — and if that post then fails, the operator has neither.

Restoring instead is the cheaper mistake in both directions, and it is not a bet. Where the
occupant displaces on delivery — an authorization challenge, a human `ask_user` — the restored
question is retired at that moment with the reason that actually happened, which is the same
delivery-time displacement INV-JOB-008 already requires; the cost is one extra keyboard edit.
Where it does not, both questions simply remain live and answerable. Neither outcome loses a
question, which is the property this rule protects; refusing the restore does lose one, and
cannot be undone.

A MACHINE-TIMED occupant is excluded, and for a different reason than the cost. A scheduled ask
that is itself still posting won the lane through the admission rule above, against an idle
lane, at a moment when the durable record could not be seen. Restoring over it would put a second
machine question beside the one that had just been admitted properly — defeating from the recovery
path the serialization the live path had already enforced, and leaving two machine questions in a
lane that holds one, with nothing that retires either. It therefore holds the lane from
registration, exactly as every occupant used to.

The predicate is a recorded message id, which is written in two places and means the same thing
in both: after a keyboard post returns one, and for a record restored from disk, whose keyboard
is still on screen from the previous process. It is decided inside the broker's synchronous
registration, in the same no-await block as the insert, so nothing can be admitted between the
question and the answer.

What it does not cover: whether the operator has actually READ the keyboard. A message id says
Telegram accepted the message, not that anyone looked at it — the guarantee is about what
reached the screen, not about attention itself. Nor does it promise that the restored question
is later retired: that happens only where the occupant displaces on delivery, and an occupant
that does not simply leaves both questions standing. Nor does it make the reconcile pass atomic: a
request registering between two records of one pass is judged by the same rule, which is the
point, but the pass still decides each record as it reaches it.

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
record exists before the keyboard does (INV-JOB-013), so a post that never lands is settled
by the next boot's reconcile rather than restored, and a keyboard that did land for a record
the process lost is answered at a tap with "expired" — the crash window resolves toward
at-most-once, deliberately.

**The process dies between deciding a terminal outcome and editing the keyboard.** The record
reads `settling` and carries the text that outcome decided, so the next boot re-applies exactly
that edit and dispatches nothing (INV-JOB-013). A terminal outcome that arrives when no record
can be moved to `settling` — it is gone, or another finisher already owns it — edits nothing and
dispatches nothing, and says so in the log, which is the only trace such a drop leaves.

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
