---
last_reviewed: 2026-08-08
---

# Triggers and scheduling

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What makes an agent act without a person speaking: scheduled triggers, webhook triggers,
and the plugin-declared triggers that need an operator's approval before they route. It does
not cover what the resulting turn does, nor webhook authentication mechanics, which belong to
the HTTP surface. Plugin-declared *authorization callbacks* share this document's
wildcard-route, overlay and durable-consent shape but produce no turn and grant no access —
they are `architecture/callbacks.md`. **Reminders** are scheduled triggers and ride the
registration, firing and one-shot cleanup described here — not the webhook, overlay or
consent machinery. Who may write one, how a due one is delivered, and what happens to one
whose moment passed while Casa was down are their own subject:
[`architecture/reminders.md`](reminders.md).

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

**Plugin trigger routing is an overlay, replaced atomically.** It is a data structure, not a
set of registered routes, and reconciliation swaps the whole thing at once rather than
mutating entries. Resident registrations are untouched by that swap.

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

**A plugin's declared webhook does not route because the plugin is installed.** Declaration
is only intrinsic validity. Routing additionally requires the target to exist and accept
webhooks, the plugin to be assigned to that target, a secret to back the chosen
authentication mode, and a durable operator approval bound to the exact trigger identity.
Until all of those hold, the name is not in the overlay and the route returns not-found.

**Approval is all-or-nothing per plugin.** If one declared trigger fails any check, none of
that plugin's triggers route. Partial routing is deliberately not offered.

**Approval and the secret it authorizes land at different moments.** The acknowledgement
persists in the operator's tap; the per-trigger secret is minted by the reconcile that
follows. Routing is unaffected — the overlay swaps in the same reconcile — but anything
deciding whether a plugin's ingress is *usable by an external service*, the setup-tool
dispatch gate above all, has to read the minted secret rather than infer it from the
approval (INV-PLUG-011 in [`plugin-setup.md`](plugin-setup.md)).

**One reconcile pass sees one registry snapshot.** The plugins, their manifests and each
target's assignment authority are served from a single pinned resolution, so a registry
reload landing mid-pass cannot make the overlay compose two generations.

**A resident's trigger file has two writers, and only one of them is allowed to be an
agent's hand.** Reminders are ordinary entries in the operator's own `triggers.yaml`, so the
resident's reminder tools and the configurator's trigger edits target the same document. The
configurator runs in a separate CLI child process: it reads the file, decides, and writes
back — an interval that spans model thinking time, and one no lock may be held across. A
reminder set inside it was discarded by the stale rewrite, silently, and the commit that
followed staged the loss. So an agent does not write that file at all. The file tools are
refused for that path and the change is made *inside* Casa, in a read-modify-write held under
`trigger_write_lock.PASS_LOCK` so it cannot interleave with the reminder writer.

This is a bound on agents, not on writers: the operator edits their own file freely. The
config reconciler rewrites it from a worker thread too, and once did so without coordination;
#458 closed that by holding the same `PASS_LOCK` across the whole reconcile pass, so the
reconciler and the in-Casa writers now serialize against each other rather than racing.

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

**INV-TRIG-003**: A plugin's triggers route only as a complete set, and only when target, assignment, secret backing and a persisted operator approval all hold.

Enforced during reconciliation, which computes the desired set and refuses with a specific
reason for each missing precondition.

What it does not cover: intrinsic validation happens earlier and separately, and passing it
means only that the declaration is well-formed.

**INV-TRIG-004**: A trigger approval is persisted and bound to an exact identity, and an unreadable or mismatched approval store yields no approvals.

Enforced by an atomic write, and by a load path that treats anything malformed or
identity-mismatched as zero approvals rather than trusting it. This approval outlives a
restart — as do the specialist and persona install acknowledgements, which keep their own
stores — so it fails closed on read.

"Exact identity" is a specific tuple: plugin, artifact id, effective name, target, and the
normalized auth policy. **Clearance is not in it** — a clearance change on a trigger installs
under the old approval without renewed consent. Everything in the tuple, including any auth
mode, header or tolerance change, does invalidate the approval.

**INV-TRIG-011**: An agent's file-tool write whose path *resolves* to a resident's `triggers.yaml` is refused, and every writer of that file — the typed tools, the reminder tools, and the config reconciler's whole pass — serializes its read-modify-write under one process lock.

Two halves, and both are load-bearing. A code-mandatory PreToolUse guard — carried by every
executor *and* every resident, applied uniformly rather than only to an agent the loader
knows has `Bash` — refuses the write. No shipped resident carries `Bash` today (#460
removed it from the assistant, the last one that did), but the `plugin-developer` executor
still does, and the guard's universality is what makes that fact irrelevant to whether it
runs. The typed replacement then makes the change *inside* Casa, and the read, judgement
and write are held under `trigger_write_lock.PASS_LOCK`, which the `config_sync` reconciler
also holds across its entire pass (#458): a reminder or configurator write can only land
before or after a pass, never in the middle of the reconciler's own read-decide-write, and
vice versa. The lock is what buys the serialization — earlier the tools bought it by refusing
to `await` and holding the event loop, but that left the reconciler, which runs on a worker
thread, uncoordinated; the shared lock covers all three writers and is taken off the loop via
`asyncio.to_thread`, so a held pass waits a worker thread rather than stalling the loop.

**The two tool families the guard covers are not equally decidable, and it asks each of them
a different question.** A `Write`/`Edit`/`MultiEdit`/`NotebookEdit` path is a literal:
resolved against the *session's* working directory — an executor's is `/config`, a
resident's is its agent home, and assuming one of them is how a `../../` spelling read as
allowed — then normalized and symlink-resolved, and refused outright when no working
directory is reported at all, since a relative path is then not resolvable by anything.

That is exact for what it claims — a path that *resolves* to the file — and the wording is
load-bearing. A **hard link** to `triggers.yaml` is the same inode under a different name,
and `realpath` reports the alias, not the target; a symlink **retargeted between the check
and the write** is a race no pre-tool hook can close. Neither is decidable here, both belong
to the same family as the shell residual, and both are recorded in #460.

**The `Bash` half is a backstop and the invariant deliberately excludes it.** Four review
rounds by two independent models produced seventeen bypasses of it. Each round closed the
previous round's spelling and the next found another — a bare basename after a `cd`, quote
splices on operands and then on redirect targets, an opaque interpreter body, a `$PWD` only
bash expands, `command cp` and `bash -c "cp …"` past a list of write verbs, allowlisted
readers that turn out to write (`xxd` takes an output operand, `rg --pre=` runs an arbitrary
preprocessor). That is not a run of bugs. The two judgments it began with — *which* path a
shell writes to, and *whether* it writes — both range over open sets, so both were removed
in favour of predicates over closed ones: does the command name `triggers.yaml` at all, with
quoting stripped from the whole command first, and is it *provably read-only* — every
pipeline segment one of about twenty audited read programs named without a path (an agent can
write an executable called `cat`), no redirect, no substitution of any kind, with `git`
admitted by subcommand through the managed guard's own audited read-only set so an ordinary
`git diff` on an unrelated file still works. That closed sixteen.

What stays open is bash's own quoting, and no parser closes it: `tri$''ggers.yaml` and a
backslash-newline continuation name the file to the shell and something else to a tokenizer,
and ANSI-C quoting can encode any character. Nor is the allowlist truly closed while an agent
can put its own `cat` earlier in `PATH`. So this half catches the *accidental* form —
what a model following a stale recipe would actually type — and claims nothing more. The
real boundary for an agent with broad shell access is filesystem enforcement or not having a
shell over that tree (#460); the settings guard records the identical residual for
`settings.json`. Note that the configurator, whose recipe is the one that used to hand-edit
this file, has no `Bash` at all.

Where the backstop errs it errs closed, and the breadth is the cheap direction: a shell write
to *any* file of that name is refused wherever it lives, and so is a read spelled with a verb
the small list does not name — `sed -n p` reads, `sed -i` writes, and it does not try to tell
them apart. The way through is the file tools, whose paths are literal and resolved exactly.
The reads the recipes actually call for — `cat`, `grep`, `head`, with a benign `2>/dev/null`
— pass.

The red case is the file-tool half inverted: an `Edit` of a resident's trigger file that is
allowed, or a trigger tool that awaits between reading the document and writing it.

What it does not cover: the operator's own edits, deliberately; `config_sync`'s worker
thread, which rewrites the same file with no coordination (#458); the shell residual above;
and an alias the check cannot see through — a hard link, or a symlink retargeted after it
(#460). The claim is about the route an agent is actually told to take, and that route is
closed exactly.

**INV-TRIG-005**: Reconciliation replaces the entire plugin overlay in a single rebind.

There is no window in which the overlay is half-updated. Names absent from the new set stop
routing immediately.

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
INV-JOB-006/007/008 in [`jobs-and-delivery.md`](jobs-and-delivery.md), and the epoch that
stops a removed trigger's still-running turn from raising one rides alongside the marker
from the same shared helper — per role AND per trigger, so cancelling one reminder does not
silence the turns of the role's other schedules. The marker is also read only by those two tools, never by the shared
transport predicate the protected-action approval path gates on, so nothing here widens who
can raise an approval.

## Failure behavior

Reminder-specific failures — an unparseable role file during a sweep, a delivered reminder
whose entry cannot be removed — are in [`architecture/reminders.md`](reminders.md).

**A webhook body is too large.** Requests are hard-capped at 64 KiB — chunked or not — and
refused with 413 *before* authentication or dispatch, so an oversized producer never
reaches its trigger.

**An unknown webhook name.** Not-found, with no turn dispatched — and the name check happens
*after* the body has been read and size-capped, so an unknown name still consumes the
request.

**Authentication fails, the body is too large, or rate limiting applies.** Refused with the
corresponding status. A malformed body that is not valid JSON is *not* refused — it is
absorbed as text and dispatched, once authenticated.

**Reconciliation raises.** The overlay is replaced with an empty one before the exception
propagates, so a failure removes plugin routing rather than leaving a stale set. Note that
some comments elsewhere describe the opposite; the swap is what happens.

**A resident's trigger registration fails at boot.** It is not caught, so it stops boot.
The pre-commit config gate replays the same registration into a throwaway registry, so a
trigger set that passes the schema but cannot register — duplicate names, an undeclared
channel, an out-of-range cron field — is refused at commit time rather than discovered as
a boot loop. Re-registration later behaves differently: the old entries are removed first,
and a failure partway unwinds the partially-installed replacements too, leaving the role
with *no* triggers — the fail-closed state the reload error reports. The one exception is a
scheduler that refuses to *remove* an existing job: re-registration then refuses, the stuck
job stays live and tracked while the role's webhook entries are already unregistered, and
the error names exactly the jobs that remain.

**The approval store is missing or corrupt.** Treated as no approvals. Pending routes stay
absent rather than opening.

## Extension points

**A new resident trigger type** touches the schema, the loader, registration and dispatch —
the current set is four.

**A new resident webhook** needs the trigger declaration and a name outside the reserved
plugin prefix. Declaring the webhook channel on the resident is *not* checked for webhooks —
the channel gate is scheduled-only (see INV-TRIG-001).

**A new plugin trigger** needs the manifest declaration, an assigned target that accepts
webhooks, secret backing, and operator consent — and reconciliation must then run. The
declaration itself has hard rails a plugin author cannot discover from the routing model:
at most eight triggers per plugin, effective names capped at 64 characters, and
provider-owned secrets rejected outright. Secret backing is mode-specific: static-header
and timestamped modes get a per-trigger secret minted eagerly after consent into the
webhook-secrets state directory, while body-HMAC rides the one global webhook secret —
provisioning the wrong kind leaves the plugin unroutable. Resident trigger files have
their own schema rails: v2 forbids a webhook `path` (the wildcard route provides it),
while legacy v1 required one, and a scheduled trigger takes exactly one of an inline
prompt or a prompt file.
An expired consent DM is re-issued on demand by the `consent_reprompt` tool — the
prompt-only re-issue shared by all three consent kinds (documented with the tool
interface), which skips triggers the operator explicitly denied.
Reconciliation is hooked at boot, at plugin lifecycle changes, at consent and revocation,
and at exactly four reload scopes: triggers, agent, agents, and full. The policies and
config-sync reloads refresh agent configuration without reconciling the plugin overlay, so
a routing-relevant change arriving through those leaves the old overlay live until a
covered scope runs.

**Anything relying on a missed schedule being caught up** needs a persistent job store first;
there is none today.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/trigger_registry.py::TriggerRegistry`
- `casa/rootfs/opt/casa/trigger_registry.py::TriggerRegistry.replace_plugin_overlay`
- `casa/rootfs/opt/casa/trigger_reconcile.py::compute_desired`
- `casa/rootfs/opt/casa/trigger_acks.py::TriggerAckStore`
- `casa/rootfs/opt/casa/plugin_triggers.py::parse_and_validate`
- `casa/rootfs/opt/casa/casa_core.py::_make_webhook_handler`
- `casa/rootfs/opt/casa/tools.py::config_trigger_upsert`
- `casa/rootfs/opt/casa/tools.py::config_trigger_delete`
- `casa/rootfs/opt/casa/hooks.py::make_trigger_file_write_guard`

**Tests**
- `tests/test_config_triggers_schema.py`
- `tests/test_trigger_consent.py`
- `tests/test_agent_loader_trigger_auth.py`
- `tests/test_casa_reload_triggers_resident.py`
- `tests/test_config_trigger_tools.py`
- `tests/test_scheduled_media_delivery.py`
- `tests/test_scheduled_delivery_durable.py`

**Related**
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/callbacks.md`](../architecture/callbacks.md)
- [`architecture/reminders.md`](../architecture/reminders.md)
<!-- END SOURCEMAP -->
