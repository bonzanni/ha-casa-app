---
last_reviewed: 2026-09-22
---

# Triggers and scheduling

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What makes an agent act without a person speaking: a resident's scheduled and webhook
triggers, how they are registered and fired, and who may write the file they live in. It does
not cover what the resulting turn does, nor webhook authentication mechanics, which belong to
the HTTP surface. The secrets that authenticate a resident webhook — minting, receipts,
the per-slot report — are [`trigger-secrets.md`](trigger-secrets.md)'s. What a **plugin-declared** trigger must satisfy before it routes — the
overlay, its preconditions and the operator approval gating them — is
[`architecture/plugin-triggers.md`](plugin-triggers.md); plugin-declared *authorization
callbacks* share that shape but produce no turn and grant no access, and are
`architecture/callbacks.md`. **Reminders** are scheduled triggers and ride the
registration, firing and one-shot cleanup described here — not the webhook, overlay or
consent machinery. Who may write one, how a due one is delivered, and what happens to one
whose moment passed while Casa was down are their own subject:
[`architecture/reminders.md`](reminders.md). A scheduled entry whose target is a plugin job
instead of a prompt is [`job-triggers.md`](job-triggers.md)'s.
Who may write a resident's trigger file, and the lock its writers share, is
[`trigger-file-writers.md`](trigger-file-writers.md).

## Mental model

**Four trigger types exist for residents — interval, cron, date and webhook — and plugins may
declare webhooks only.**

**A reminder is a trigger, not a separate thing** — an ordinary `triggers.yaml` entry
marked `managed_by: agent`, registered and fired by the machinery described here. What is
particular to reminders is who may write one, that presence on disk is the record delivery
is owed, and that a sweep can redeem an occurrence the scheduler never saw:
[`architecture/reminders.md`](reminders.md).

**Ownership is data, and is never inferred from an entry's content.** The schema permits an
operator to write a `reminder-`-prefixed dated one-shot of their own, so neither the
reserved name prefix, nor the `date` type, nor the `one_shot` flag distinguishes an agent's
entry from an operator's — only the explicit field does. Everything that may sweep,
re-register, or delete an entry keys off it. Inferring ownership from any of those shapes
instead produced a fresh way to delete a live operator trigger every time it was attempted.

**Every webhook trigger arrives on one wildcard route.** There is no route per trigger. The
name in the path is looked up against a registry, and an unknown name is refused before any
authentication happens.

**Cron fields follow the crontab convention, including day-of-week numbering.** A numeric
day-of-week uses cron's 0/7 = Sunday; registration translates it into day names before it
reaches the scheduler, whose own 3.x numbering starts the week on Monday — passing the
number through verbatim is exactly the silent Sunday-fires-Monday misschedule this
translation exists to prevent.

**Scheduled jobs do not survive a restart.** The scheduler is configured with no persistent
job store, so jobs live in process memory. Definitions are rebuilt from configuration at
boot, which makes it *look* durable — but next-run times and any occurrence missed while the
process was down are simply gone. The grace-period setting bounds lateness for a running
process; it cannot resurrect what was never recorded. Past-dated *reminders* are the one
exception, and only because they do not rely on the scheduler to remember — a recurring
reminder's missed occurrence is lost like any other
([`architecture/reminders.md`](reminders.md)).

**Removing a trigger removes the prompt file it named** (#1372), in the same locked step —
only a regular file directly in that role's `prompts/` once symlinks resolve, not
`system.md`, and named by nothing boot still reads (another trigger, or the character's
prompt or card file, after `${VAR}` substitution). Otherwise it is kept and the result says
why: the configurator cannot delete a file itself.

This is a bound on agents, not on writers: the operator edits their own file freely. The
config reconciler rewrites it from a worker thread too, and once did so without coordination;
#458 closed that by holding the same `PASS_LOCK` across the whole reconcile pass, so the
reconciler and the in-Casa writers now serialize against each other rather than racing.

**A scheduled turn's closing text is delivered, so the prompt is where silence is asked
for.** A scheduled turn that sends a message with a tool and then ends with ordinary prose
delivers twice to the same chat: the tool send happens immediately, and the turn's own
closing text then rides the ordinary reply path. What closes that gap is the prompt, per
prompt, and it is a convention rather than a runtime rail; the one closing Casa decides
itself — a last message that is the sentinel after earlier text — is the operator's ruling
on #1075 (INV-OUT-006 in [`output-scope-properties.md`](output-scope-properties.md)). Which prompts carry
the clause, what it says, and how the tools a turn calls decide the question are
[`architecture/scheduled-prompt-endings.md`](scheduled-prompt-endings.md)'s. A webhook
trigger carries no prompt at all (INV-TRIG-013), so the convention does not reach it. The one
line Casa appends to a `deliver: operator` or `operator_always` plugin trigger's turn is
Casa's own constant, selected by the route's enum, not trigger prose (INV-TRIG-018 in
[`webhook-delivery.md`](webhook-delivery.md)).

## Contracts & invariants

**INV-TRIG-001**: A resident's scheduled trigger registers only if the resident declares the channel it names.

Enforced at registration, which raises rather than registering a trigger that would fire into
a channel the agent does not have.

What it does not cover: it does not establish that the channel is working, only that it is
declared. And it is genuinely *scheduled-only*: a resident webhook trigger registers and
dispatches without any channel-declaration check at all.

**INV-TRIG-002**: Webhook trigger names are unique, and the user and plugin namespaces cannot collide.

Enforced by rejecting a name already owned by another role, and structurally by the schema
reserving the plugin prefix so a user trigger can never take a plugin-shaped name.

The disjointness has a second consequence worth knowing here, because the registry's lookup
methods are shared. Each of them consults the resident maps first and the plugin overlay only
as a fallback, and that overlay can be in a state that is neither a route nor an absence: a
marker saying no authoritative routing computation stands behind it. Under it every plugin
lookup answers as though the name were unregistered — no role, no auth policy, and the
default clearance rather than a stale one — while resident routing is completely unaffected,
which is the point of keeping the namespaces apart. What that marker is and when it is
published is [`plugin-triggers.md`](plugin-triggers.md)'s. Every publication of either
overlay — a map or that marker, the marker again — advances one generation counter the
registry exposes, incremented right after the rebind in the same synchronous step; the
setup-dispatch worker compares two readings of it to know that nothing was published across
its route check ([`plugin-setup-dispatch-gate.md`](plugin-setup-dispatch-gate.md), INV-PLUG-016).

**INV-TRIG-013**: A webhook trigger carries no prompt — writing one is refused, while a document already holding one still loads, with a warning, and delivers nothing extra.

A webhook turn is built from the trigger name and the request body; only a scheduled turn
carries stored prose. The two directions must not be unified. The *writer* is strict, so an
instruction the dispatch would discard is refused while the operator is still in the
conversation rather than committed and dropped at every firing. A *reader* is tolerant,
because documents written before the rule exist: judging those strictly would make a
resident's config unloadable — and boot with it — while the config reconciler's entry
salvage would go further and drop the trigger outright. One normalization serves every read
path, so a file cannot pass one and fail another.

What it does not cover: cleanup. A stored prompt stays on disk, warning at each boot, until
the operator replaces that entry through the typed tool, which rewrites it whole.

**INV-TRIG-009**: Firing a one-shot trigger unconditionally drops its scheduler job, and removes its `triggers.yaml` entry only when the agent owns that entry.

Both steps run after the dispatch, in process. Dropping the job is unconditional — a
one-shot that kept its job could fire again, and the id must be freed so the same name can be
registered later. "Drops" means the removal is attempted and the trigger is forgotten either
way: a scheduler that refuses to remove the job is treated as already-gone, so the id is
still freed. That is deliberate, since the alternative is a name that can never be
re-registered, but it means the guarantee is about Casa's own bookkeeping rather than about
the scheduler's internal state. Deleting the *entry* is gated on ownership, because an operator's dated
one-shot lives in the same file and removing their line is not the registry's business. The
red case is either half inverted: a `one_shot` job that survives its own firing, or an
operator's entry deleted because it fired.

An earlier form of this rule promised the entry was always removed. That is no longer true,
and the difference is a deliberate outcome rather than a gap: **an operator's unmarked
one-shot lingers inert after firing** — never re-registered, because a past-dated trigger is
not registered at boot, and never delivered by the sweep, because it carries no ownership
marker.

What it does not cover: it does not promise the entry removal *succeeded*. Cleanup is
deliberately outside the delivery path, so a failure leaves the entry for the sweep rather
than raising back into the scheduled job.

**INV-TRIG-012**: A scheduled turn may deliver media to the operator only when Casa's own time-based dispatch fired it on the Telegram channel and an operator is configured.

A scheduled turn's `chat_id` is a session-keying **label**, not a chat id — it keys the SDK
session and the outbound quota scope, so it is never overwritten with a delivery address.
The turn therefore had nowhere to send a file, and an agent doing scheduled work had to fall
back to asking the operator to say something first.

Eligibility is a reserved **marker**, stamped by the two time-based dispatch sites — the
scheduler and the overdue reminder sweep, through one shared helper so the rule cannot drift
between them — and only for a Telegram-channel trigger. It is deliberately not inferred from
the message type: the authenticated webhook route dispatches *scheduled*-typed turns too, so
an inference would have handed third-party webhook content a direct line to the operator's
DM. Being reserved, the marker is stripped from every externally-supplied context, and a
turn that reaches the tool without it is refused exactly as before.

Who the operator is gets resolved **per call**, from the same fail-closed identity rule
every other approval surface uses, rather than stamped into the message when the trigger
fired. A schedule that fired an hour ago cannot deliver to an identity that has since
changed, and with no operator configured nobody is the operator, so the turn stays text-only.
Delivery also requires genuinely direct execution: a delegated specialist inherits its
parent's whole origin, marker included, and must not inherit the delivery target with it.
The marker *is* carried back through an asynchronous delegation's completion, because the
motivating case is a specialist producing the artifact the resident then sends — and because
a restart resumes that resident from the durable job rather than from the live record, whose
origin it rebuilds field by field, eligibility is also a stored field on that job
(INV-JOB-002 in [`jobs-and-delivery.md`](jobs-and-delivery.md)). Restored from an exact
stored true and nothing else: a job row written before the field existed restores no
eligibility rather than guessing from the scope label's shape.

What it does not cover: the marker admits **media only**. Raising an interactive question
from a scheduled turn is a separate problem — a question outlives the turn that asked it,
which needs durable records and trigger lifecycle ownership — and this invariant makes no
claim about it. That problem is now solved separately, and the same marker is what admits
it: the durable question, its terminal outcomes and its attention-lane manners are
INV-JOB-013/007/008 in [`scheduled-asks.md`](scheduled-asks.md), and the epoch that
stops a removed trigger's still-running turn from raising one rides alongside the marker
from the same shared helper — per role AND per trigger, so cancelling one reminder does not
silence the turns of the role's other schedules. The marker is also read only by those two tools, never by the shared
transport predicate the protected-action approval path gates on, so nothing here widens who
can raise an approval.

The same helper carries a third marker, `_inherited_note` — reserved and stripped from
external context like the other two — which is a reminder's `output_note`: the disclosure
the turn that set it owed, prepended by the firing turn to whatever it sends
([`output-boundary.md`](output-boundary.md), INV-OUT-003). A scheduled trigger's spec is
built by one constructor, `reminders.spec_from_entry`, on every route — boot, live
registration, reconciliation — so a field the entry carries reaches the spec everywhere or
nowhere; a webhook spec is the loader's own.

## Failure behavior

Reminder-specific failures — an unparseable role file during a sweep, a delivered reminder
whose entry cannot be removed — are in [`architecture/reminders.md`](reminders.md).

**A webhook body is too large.** Requests are hard-capped at the route's cap — 64 KiB, or a
plugin trigger's declared `max_body_kib` (INV-TRIG-020 in
[`plugin-triggers.md`](plugin-triggers.md)) — chunked or not, and refused with 413 *before*
authentication or dispatch, so an oversized producer never reaches its trigger.

**An unknown webhook name.** Not-found, with no turn dispatched. The name check happens
*before* the body is read, because the cap is the route's.

**Authentication fails, the body is too large, or rate limiting applies.** Refused with the
corresponding status. A malformed body that is not valid JSON is *not* refused — it is
absorbed as text and dispatched, once authenticated.

**A resident's trigger registration fails at boot.** It is not caught, so it stops boot.
The pre-commit config gate replays the same registration into a throwaway registry, so a
trigger set that passes the schema but cannot register — duplicate names, an undeclared
channel, an out-of-range cron field — is refused at commit time rather than discovered as
a boot loop. Re-registration later behaves differently: the old entries are removed first,
the whole replacement list is validated before anything is installed (so a malformed
later entry installs nothing and retires no webhook secret), and a failure partway through
installation unwinds the partially-installed replacements too, leaving the role with *no*
triggers — the fail-closed state the reload error reports. The one exception is a
scheduler that refuses to *remove* an existing job: re-registration then refuses, the stuck
job stays live and tracked while the role's webhook entries are already unregistered, and
the error names exactly the jobs that remain.

## Extension points

**A new resident trigger type** touches the schema, the loader, registration and dispatch —
the current set is four.

**A new resident webhook** needs the trigger declaration and a name outside the reserved
plugin prefix. Declaring the webhook channel on the resident is *not* checked for webhooks —
the channel gate is scheduled-only (see INV-TRIG-001). Its secret — minted at
registration, receipted, reported per slot — is
[`trigger-secrets.md`](trigger-secrets.md)'s subject.

**A resident trigger file has its own schema rails**: v2 forbids a webhook `path` (the
wildcard route provides it), while legacy v1 required one, and a scheduled trigger takes
exactly one of an inline prompt, a prompt file or a plugin job (`job`; see
[`job-triggers.md`](job-triggers.md)).

**A new plugin trigger** is not this document's. What such a trigger must declare, the
rails on that declaration, how its secret is backed, the operator approval that gates it
and the reconciliation that publishes it are
[`plugin-triggers.md`](plugin-triggers.md)'s.

**Anything relying on a missed schedule being caught up** needs a persistent job store first;
there is none today.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/trigger_registry.py::TriggerRegistry`
- `casa/rootfs/opt/casa/casa_core.py::_make_webhook_handler`
- `casa/rootfs/opt/casa/tools.py::config_trigger_upsert`
- `casa/rootfs/opt/casa/tools.py::config_trigger_delete`
- `casa/rootfs/opt/casa/agent_loader.py::validate_persisted`

**Tests**
- `tests/test_webhook_trigger_prompt_refused.py`
- `tests/test_config_triggers_schema.py`
- `tests/test_agent_loader_trigger_auth.py`
- `tests/test_casa_reload_triggers_resident.py`
- `tests/test_config_trigger_tools.py`
- `tests/test_scheduled_media_delivery.py`
- `tests/test_scheduled_delivery_durable.py`

**Related**
- [`architecture/plugin-triggers.md`](../architecture/plugin-triggers.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/reminders.md`](../architecture/reminders.md)
- [`architecture/trigger-secrets.md`](../architecture/trigger-secrets.md)
- [`architecture/scheduled-prompt-endings.md`](../architecture/scheduled-prompt-endings.md)
- [`architecture/trigger-file-writers.md`](../architecture/trigger-file-writers.md)
<!-- END SOURCEMAP -->
