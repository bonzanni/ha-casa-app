---
last_reviewed: 2026-08-07
---

# Reminders

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an agent comes to nudge someone later: what a reminder is made of, who may create or
remove one, how a due one is delivered, and what happens to one whose moment passed while
Casa was not running. It does not cover the trigger machinery a reminder rides on —
registration, dispatch, webhook routing, plugin consent — which is
[`architecture/triggers.md`](triggers.md). The tools themselves and their result contracts
are [`architecture/tools-interface.md`](tools-interface.md).

## Mental model

**A reminder is a trigger, not a separate thing.** A resident creates one through a narrow
writer that may only touch entries it owns; everything downstream — registration, firing,
listing — is the ordinary trigger path. One-off reminders use the point-in-time `date`
type, because cron has no year field and a dated one-shot written as cron is an *annual*
trigger in disguise.

**One file, ownership per entry.** A reminder is an ordinary entry in the role's
`triggers.yaml`, marked `managed_by: agent`. Reminders once had a file of their own,
because reconciliation resolved an edited image-owned file against a changed shipped
default as "image wins" and would have deleted every pending reminder on such an update.
That file is gone: `triggers.yaml` is now reconciled *per entry*, so an entry the image has
never shipped is preserved rather than dying with the file
([`architecture/configuration.md`](configuration.md)).

**Ownership is data, and is never inferred from an entry's content.** The schema permits an
operator to write a `reminder-`-prefixed dated one-shot of their own, so neither the
reserved name prefix, nor the `date` type, nor the `one_shot` flag distinguishes an agent's
reminder from an operator's trigger — only the explicit field does. Everything that may
sweep, re-register, or delete an entry keys off it. Inferring ownership from any of those
shapes instead produced a fresh way to delete a live operator trigger every time it was
attempted.

**The file is machine-maintained, and only meaning is preserved across a rewrite.**
Operators change triggers by asking the configurator agent, not by hand-editing; both that
agent and per-entry reconciliation already reconstruct the file through a plain YAML dump.
The reminder writer is a third writer of the same kind, so comments, quote styles and key
order are not preserved — an entry's *meaning* is. Environment interpolation is the one
exception, and it is worth understanding why it survives the fix that was supposed to
remove it. A `${VAR}` reference no longer has its value lexed into the document
(INV-CFG-009), so a rewrite can no longer truncate or break one. But *quoting* is what
tells the loader that a scalar consisting only of a reference is text, and quote style is
exactly what a rewrite does not preserve: a field authored as `prompt: "${DETAIL}"` is the
string it holds, while the same field re-emitted unquoted has that value read back as
YAML. The writer therefore still refuses to add a reminder to a file with such a field —
one whose reference is the whole value and is declared text, by quoting or by tag. Only
that, since a reference with text around it is a string either way, a plain lone one is
read back as a value both before and after, and one in a comment reaches no loader at all.
No shipped configuration uses references.

**A one-shot reminder still present with a past fire time is one that is owed.** The entry
is the record and delivery removes it, so there is no second store to keep in sync. This is
what lets a sweep recover an occurrence the process was down for — something the scheduler
itself cannot do. Ownership is exclusive: while a live job exists the scheduler owns
delivery and the sweep leaves it alone.

**The catch-up is one-shots only, and that bound is real rather than incidental.** The
sweep selects on the `date` type, so a *recurring* reminder's missed occurrence is simply
gone: its entry describes a schedule, not a debt, and nothing in the file distinguishes an
occurrence that was delivered from one the process slept through. What survives a restart
is the recurring reminder itself, re-registered for its future occurrences.

**Removing a delivered reminder must never be blocked by something the sweep tolerates.**
Delivery and cleanup are two steps over the same file, so any check present in the second
and absent from the first lets the sweep deliver an entry it cannot then remove — and
presence being the ledger, it redelivers on every pass indefinitely. Both steps therefore
read through one function, and cleanup deliberately skips the whole-document schema check
that creation performs: a validation failure elsewhere in the file is unrelated to the entry
being removed. Reading a file and re-writing it can still fail *independently* — a document
nested deeply enough parses but cannot be re-emitted — so what is actually guaranteed is
narrower and worth stating precisely: every such failure lands inside the handled error
contract, making the worst case the ordinary at-least-once one rather than an aborted pass.

**A pre-existing schema defect in another entry never blocks either operation.** A running
Casa holds the configuration it booted with, so the file on disk can already be invalid while
everything works — the configurator refuses a commit that fails validation but leaves its
edits in the working tree. Refusing to touch such a file protects nothing, since it already
fails to load, while making reminders unavailable and naming an entry nobody asked about. So
creation validates *the entry it is adding*, on its own; cleanup validates nothing. Judging
the new entry alone is exact rather than approximate — the schema has no constraints that
span entries, and the one real cross-entry hazard, a duplicate name, is refused separately.
The entry is still judged under the file's real top level, since the schema version decides
what is legal there, so a defect in the top level itself does refuse creation — a deliberate
boundary, not a leak. And only the *judgment* is scoped this way: a sibling entry that cannot
be read, or cannot be written back out, blocks both operations, because there is then no way
to rewrite the file at all.

**A recurring reminder keeps its first occurrence as an anchor.** The derived cron fields
drive the recurrence — evaluated in the scheduler's timezone, which is what keeps a series
firing at the same local time across a DST boundary — while the anchor becomes the
scheduler's start date, so "every Thursday from the 20th" cannot fire on the 6th.

**What a cron cannot express exactly is refused, not approximated.** A repeating reminder must
land on a whole minute, and a monthly one on the 28th or earlier — cron has minute resolution
and skips months a literal 29th, 30th or 31st is missing from, so "monthly on the 31st" would
fire seven times a year rather than twelve. Every approximation tried here made the time the
user was told differ from the time that fires, so the request is rejected and the agent asks
for something expressible instead.

**Wall-clock fields are read in the scheduler's timezone**, not the caller's offset. The
offset pins which instant is meant; the cron is evaluated in the scheduler's zone, so deriving
the fields from a caller's offset would misschedule by the difference whenever the two
disagree, and drift across a DST boundary.

**The file is the truth; the scheduler is a cache of it.** The sweep reconciles in both
directions — registering any agent-owned reminder with no live job, and dropping any
agent-owned job with no entry left — which heals a divergence without needing a lock. Both
directions are bounded by recorded ownership, never by the name: an operator's own trigger is
neither registered nor dropped here, and matching on the reserved prefix would drop the one
they are allowed to author. A reload re-registering a role from a snapshot taken before a
reminder was written would otherwise drop a *recurring* reminder for good, since only
one-shots are recoverable by delivery; the same race in the other direction would leave a
cancelled reminder firing forever.

Sharing the operator's file makes one previously incidental property load-bearing: a
`triggers.yaml` that cannot be read *or written back* must be contained to its own role
rather than aborting the pass, or one bad file would strand every later role's overdue
reminders. An unreadable file suspends *both* directions for that role — reporting it as
empty would authorise dropping every one of its reminder jobs.

**A past-dated reminder is the one occurrence that survives a restart**, and only because
it does not rely on the scheduler to remember. Scheduled jobs live in process memory
([`architecture/triggers.md`](triggers.md)); a one-shot reminder's entry on disk is the
record that delivery is owed, so a sweep can redeem an occurrence the scheduler never saw.
No other missed occurrence in Casa is recoverable, a recurring reminder's included.

## Contracts & invariants

**INV-TRIG-010**: The reminder writer may only create, and the canceller only remove, entries marked as agent-owned in the calling role's own file.

This is the whole boundary between a resident managing its own reminders and a resident
editing operator configuration. The red case is either tool touching an entry that carries no
ownership marker — the heartbeat, the morning briefing, or a `reminder-`-prefixed dated
one-shot the operator wrote themselves — or reaching another role's file. Creation also
refuses a name already present under any owner, because a duplicate name is refused at
registration and that is uncaught at boot.

An earlier form of this rule bounded both tools by the reserved *name prefix* instead. The
prefix was never sound as an authorization predicate: the schema permits an operator to
author a name carrying it, so it identifies a naming convention and not an owner. It survives
only as the shape of a generated name.

The privileged config-commit path keeps its own separate configurator-only guard; this is a
narrower door beside it, not a widening of that one.

The configurator's trigger edits now come through the same module, by a second pair of
doors bounded the opposite way: they refuse an entry marked agent-owned, in the submitted
entry *and* in the entry they would replace. That is not a convenience — the configurator
runs in a separate process, and its hand edit of this file discarded reminders silently
(INV-TRIG-011 in [`architecture/triggers.md`](triggers.md)).

One-shot firing itself — dropping the scheduler job, and removing the entry only when the
agent owns it — is INV-TRIG-009, and it lives with the registry that implements it
([`architecture/triggers.md`](triggers.md)): it governs every one-shot trigger, including
an operator's own dated one, which is precisely not a reminder. What follows is the half
that is reminder-specific.

**INV-TRIG-008**: A one-shot reminder whose time passed while the process was down is delivered by the next sweep rather than dropped.

Presence is the record: an entry still on disk with a past fire time *is* the evidence that
delivery is owed, which is why removal happens only after a successful send. The red case is
an overdue entry that no sweep ever delivers — and the sharpest form of it is the sweep
reading the wrong file, since a past-dated trigger is deliberately left unregistered *for*
the sweep, so nothing else would ever deliver it. Delivery is consequently at-least-once — a
failed removal redelivers — because a duplicate reminder is a better failure than a missing
one. The scheduler and the sweep never both deliver: the sweep skips any reminder that still
has a live job, so the two never race for one whose time has just passed.

What it does not cover — **"delivered" means placed on the bus, not received by the human.**
The entry is removed once the turn is dispatched, so a reminder lost further down the channel
(a Telegram send that fails while the transport is reconnecting) is not retried. This is the
same contract every other trigger has had since the beginning, and closing it would need an
end-to-end receipt through the whole turn pipeline rather than anything reminder-specific. It
is a known residual, not an oversight.

## Failure behavior

**A role's `triggers.yaml` cannot be parsed while the process is running.** The sweep skips
that role entirely — no delivery, no reconciliation in either direction — and continues with
the others. Setting a reminder fails with the parse error rather than rewriting a file it
cannot read.

**A reminder is delivered but its entry cannot be removed.** This state is reachable and is
not treated as an error to be prevented: a document nested deeply enough to parse but not to
be re-emitted is the clearest case, and a full disk is the ordinary one. The guarantee is
*containment*, not removal — the failure is reported, the entry stays, the remaining roles are
still swept, and the reminder is delivered again on the next pass. That is the at-least-once
contract working as intended, because a duplicate nudge is a better failure than a missed
reminder. What is ruled out is the failure escaping and aborting the pass, which would strand
every later role's overdue reminders too.

**A field in the file is a `${VAR}` reference, whole and declared text.** Setting a reminder
is refused, because re-emitting the file drops the quoting or tag that field's meaning
depends on. No other use of a reference is refused.
Cancelling or sweeping one is *not* refused — it warns and proceeds, since blocking cleanup
is what strands a delivered reminder into redelivering forever.

That rewrite may change what the operator's own entries resolve to, so it is announced to
them on-channel rather than only in a log line they would have to go looking for. The
announcement is recorded *after* the write returns, never when the condition is detected: a
save can fail before the file is replaced, and "updated your file" about a file that was
never written is a false report. It is sent once per file per process — enough to inform,
not so often as to nag — and the record is only consumed once delivery is confirmed, so a
reconnect that swallows the notice does not swallow the fact.

Its retry is the next rewrite. If the announcement cannot be delivered and no further
rewrite happens before restart, it is not delivered at all. Closing that gap needs either
persistence across restarts or a retry loop; both were judged disproportionate for a
courtesy notice, and the honest limitation is recorded here rather than hidden behind
machinery that looks like a guarantee.

**A requested recurrence cannot be expressed as a cron.** Refused with the reason, rather
than rounded to a nearby time the user was not told about.

## Extension points

**A new reminder shape** goes through the same `triggers.yaml` entry vocabulary — there is
no reminder-specific store to extend, and adding one would reintroduce the two-writer
divergence the single file exists to avoid.

**Anything relying on a reminder being *received*** rather than dispatched needs an
end-to-end receipt through the turn pipeline; there is none today (see INV-TRIG-008).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/reminders.py`

**Tests**
- `tests/test_reminders_store.py`
- `tests/test_reminder_tools.py`
- `tests/test_reminder_sweep.py`
- `tests/test_trigger_registry_date.py`

**Related**
- [`architecture/triggers.md`](../architecture/triggers.md)
- [`architecture/configuration.md`](../architecture/configuration.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
<!-- END SOURCEMAP -->
