---
last_reviewed: 2026-08-25
---

# Plugin health

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a plugin's problems reach the operator: the standing health report, the two
operator-facing surfaces — the in-band notice and the DM — with the marks that keep
them from repeating themselves, and the read-only status tool. It does not cover what
puts a plugin into a broken state: installation, artifact identity and per-call
authorization are [`plugins.md`](plugins.md); environment resolution and withholding
are [`plugin-runtime.md`](plugin-runtime.md); the setup-episode lifecycle behind a
setup row is [`plugin-setup.md`](plugin-setup.md)'s.

## Mental model

**Plugin failures degrade; they do not stop the container.** The boot path writes health data
and exits successfully whatever it finds. One broken plugin costs that plugin. The health
report's operator-DM dedup (fingerprints already notified) is a read-merge-write over one
file from both the event loop and worker threads; a process-wide lock serializes it, and the
regeneration reads the previous report inside that critical section, so a regeneration
racing a just-delivered notification cannot erase its marker and re-alert. A held-back
plugin's unresolved variables reach the report and the DM by NAME (a bounded `detail` field
on the issue row — names only, never values, and never part of the dedup fingerprint, so a
detail change alone never re-alerts). Each reason draws its detail from the secret status
that produced it, so a variable a setup tool has yet to provision is named by the reason that
says so, and a setup episode that failed carries its own error into the row.

**Health speaks to the operator in what they can do, and does not repeat itself.** Both
operator-facing surfaces — the in-band notice a resident prepends to a reply, and the DM —
render through one translation that never emits a reason code: the codes are internal
identifiers, an open set that grows with every plugin feature. The translation therefore
classifies by families of code rather than enumerating them, so a code minted later still
reads as something actionable, and an unrecognized one degrades to a plain statement rather
than leaking. The shared renderer is now the whole sentence, not merely the per-issue clause:
the two surfaces had each written their own wrapper around the shared clause and those had
already drifted, so a set of stale bindings was announced as an incomplete update in-band
and as a generic fault by DM. What deliberately stays per-surface is how many issues each
names — the in-band line rides on top of a reply and names two, while the DM is a message of
its own and names five, because the operator has no other way to see those names in that
moment. The truncated tail is answerable now: the read-only plugin status tool the assistant
holds reports the full standing set on request.

Repeats are suppressed
by a decaying, in-process memo of the exact line last put in front of each role: any change
to what the operator would read renders immediately, and a role whose issues have resolved
is re-armed at once.

The memo is released — the line offered again on the very next turn, without waiting out its
window — whenever the delivery that carried it can be shown to have displayed nothing. That
was once knowable only from a raise, so a delivery that returned normally after sending
nothing consumed the line silently; a reconnect makes that reachable mid-turn, and no
retrieval path exists for an operator to ask what they missed. Channels that carry these
notices now report delivery explicitly (see `INV-TG-006` in
[`architecture/telegram.md`](telegram.md)), so both a proven failure and a raise-with-nothing-shown
release the memo, while a delivery whose head reached the operator does not — repeating a
line they have already read is the failure in the other direction. A channel that does not
report keeps the old behavior, deliberately: "cannot report" must not read as "failed", or
the notice would repeat forever there.

A transport failure that cannot distinguish "never arrived" from "arrived, acknowledgement
lost" is *not* a proven negative and does not release the memo. Only an established one
does — the application being absent, so no call was made at all, or the API evaluating the
call and refusing it. Erring the other way would repeat the line on every turn that hits a
flaky connection.

The operator DM is gated the same way, and records its fingerprints only on a send the
channel did not report as undelivered — the same proven-negative rule, so a channel that
cannot report either way records rather than repeating forever. The out-of-band notices —
that DM and the reminders one — serialize through a single lock, so neither can be sent
twice by two paths that each read the state before either marked it.

That lock orders the out-of-band notices against each other. Between the DM and the in-band
notice there is no lock, only a shared field: **the DM records the rows it named, and the
notice skips rows already recorded.** That is a rule, not a guarantee of exactly-once, and
the difference is the whole of what follows. Each surface names a bounded prefix and counts
the rest — five for the DM, two for the notice. Nothing schedules a follow-up: further
naming happens when something next regenerates health and notifies, which is a boot, a
plugin mutation or a reload. And a row the DM named goes unrecorded whenever the report
moved during its send, so the notice may repeat it. The read-only status tool is what
answers completely and at once, reporting the whole standing set unfiltered.

Coordinating the two *stores* was tried first and does not work, which is why the rule is a
filter over one field rather than a second store. The surfaces select different rows: the DM
announces fingerprints not yet notified, across every target, while the notice states what
stands for one role — and setup-episode rows are targetless, never decaying while pending,
so they stand for every role during exactly the multi-step setup flow where the duplicate
appears. Two messages carrying the same warning therefore rarely carry the same text, which
defeats suppression keyed on the rendered line; and any separate record of "already
delivered" has a lifetime that must be cleared, which is how a recurrence goes unannounced.
Filtering per row needs neither: the field it reads is pruned to fingerprints still present
on every regeneration, so the report's own resolution pruning *is* the lifetime.

That filter is only as honest as the mark behind it, so the mark is narrowed on both axes.
It covers the rows the message actually **named** — a row behind the "and N more" count was
not something the operator was told, so it stays unrecorded and stays available to be named
later. And it applies only while the report the message described is still current, because
a row that resolves while its DM is in flight would otherwise have its fingerprint written
back into a report that no longer holds it, where the next regeneration's pruning preserves
it the moment the row recurs — a recurrence then reaching nobody. When the report has moved,
nothing is recorded, which can cost a repeat of a message already read. That direction is
chosen deliberately: a duplicate is recoverable and a silence is not.

What the notice can show is narrower than what stays unrecorded, and the difference is where
this is misread. It selects blocking **issues** addressed to its own role or to no target.
A warning, or an issue addressed to an executor, is therefore never in-band under any
condition; it waits to be named by a DM. So does a resident-addressed row sitting behind the
notice's own count. None of those is lost — they stay unrecorded, so a later notification can
name them, five at a time, and the status tool lists them on request — but nothing here
delivers them on a schedule.

Two consequences are deliberate. A `target=None` row is operator-global — one DM naming it
records it for every role. And a changed `detail` on an unchanged fingerprint no longer
re-shows in-band; the DM never re-announced it either, so this closes an undesigned channel
rather than removing a guarantee.

A consent approval rewrites the stored report too — without a notification pass of
its own; the reconcile contract that owes that rewrite is
[`plugin-events.md`](plugin-events.md)'s.

## Contracts & invariants

**INV-PLUG-013**: A plugin-health fingerprint is recorded as announced only for a row an operator message actually named, on a send the channel did not report as undelivered, and only while the report that message described is still current; a fingerprint that is not recorded is suppressed on neither operator surface.

Every failure it forbids is silent, which is why it is pinned rather than left to the
renderers. A mark that outruns what was named removes a row from the surface that would have
named it, permanently — it is filtered out of the notice and is no longer new to the next
message. A mark that lands on a report the message no longer describes is worse: the
fingerprint is written back onto a row that has resolved, where the next regeneration's
pruning keeps it as soon as the row recurs, so the recurrence reaches nobody. The negative
condition is deliberately "not reported as undelivered" rather than "confirmed": a channel
that cannot report either way must not be read as having failed, or its notices would repeat
forever. What holding this costs is a repeated message when a regeneration lands mid-send,
and a large incident being named five rows at a time.

## Failure behavior

A report is normalized as it is read, and normalization filters rather than rejects. External
corruption of the report file — a non-object document, a row that is not a mapping, an
unhashable target — used to raise out of the notice renderer, which runs on a resident's turn
and is not guarded there, so a hand-broken file cost the operator their reply and not merely
their notice. Rejecting the whole document on one bad row would have thrown away a valid
blocking issue sitting beside it, so the bad rows are dropped and the good ones still reach
both surfaces.

## Extension points

**A new health issue code** needs no renderer change: the operator-facing translation
classifies by families of code rather than enumerating them, so a code minted later
still reads as something actionable and an unrecognized one degrades to a plain
statement (see the mental model). What a new reason owes is its `detail`: drawn from
the status that produced it, names only, never values, and never part of the dedup
fingerprint.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_health.py::render_notice`
- `casa/rootfs/opt/casa/plugin_health.py::mark_notified`
- `casa/rootfs/opt/casa/casa_core.py::notify_plugin_health`

**Tests**
- `tests/test_plugin_health.py`
- `tests/test_plugin_health_notify.py`

**Related**
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/plugin-setup.md`](../architecture/plugin-setup.md)
- [`architecture/plugin-events.md`](../architecture/plugin-events.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
<!-- END SOURCEMAP -->
