---
last_reviewed: 2026-08-26
---

# Engagement launch failure and restart

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What happens when a launch does not become a live engagement, and what a restart does with
the records left behind: which gate refused a delegation, what a `claude_code` launch abort
rolls back and how far it gets, which failure arms answer the calling turn rather than the
operator's topic, and what replay resumes or refuses. The record itself, how a launch is
admitted and the driver protocol are in
[`architecture/engagements.md`](engagements.md), which also states the invariants this
behaviour is measured against; how a turn is admitted to a live engagement is
[`architecture/engagement-turn-admission.md`](engagement-turn-admission.md). How an
engagement *ends* once it is live is
[`architecture/engagement-finalization.md`](engagement-finalization.md); the OS boundary its
workspace and uid live inside is
[`architecture/engagement-containment.md`](engagement-containment.md).

## Mental model

A launch is not an engagement. Between the durable record and a live driver there is a
stretch of work that can fail, be refused at a gate, or be cancelled — and everything in this
document is about that stretch, plus the one place it is re-entered later: a restart, which
finds records whose launch succeeded and whose process did not survive.

Two questions separate the arms below. *Who is owed the answer* — a named fault goes back to
the calling turn, which still owns the launch and is the one answering for it, while an
absence goes to the operator's topic, because a topic that says nothing is what needs
explaining. And *what caused the end* — a provisioning failure and a graceful stop are
different events, and only one of them is a reason to destroy what the executor produced.

## Contracts & invariants

**INV-ENG-019**: A launch-failure arm aborts its engagement's topic only on the strength of a terminal transition this call both WON and durably persisted. A transition another writer won, and a transition whose persist rolled back leaving the record live, each leave the topic open and the record to its owner. The arm still answers its caller with its own named fault, and still posts no notice.

The two refused cases are the two ways the ledger's best-effort mark used to mislead an arm.
A LOST race means another writer already committed a terminal, and that writer owns every
terminal side effect (INV-ENG-001) — so painting `failed` here contradicts the durable record
and closes a topic its owner is about to close for its own reason. A ROLLED-BACK persist means
the record is still live and disk agrees: an open topic over a live record is recoverable, a
closed one is not, and the boot replay that would resume that record would find its topic
gone. All thirteen named arms route through one owner that asks the transition exactly one
question and lets its three outcomes decide; that owner is anchored and shielded, so a
cancellation arriving after the durable commit cannot leave a terminal record behind a
permanently open topic. The caller is told its own named fault on all three outcomes, because
that fault is true of this launch whoever won the record.

The behaviour here is also measured against three invariants stated in
[`architecture/engagements.md`](engagements.md): INV-ENG-011, which keeps a launch that ended
without a terminal artifact on the launch-death path; INV-ENG-014, which says a rollback that
has been entered attempts every removal it was entered to run; and INV-ENG-015, which says
which removals a stop-caused abort is entered to run, and what it records instead of a
cancelled tool call. The containment consequence of a removal that does not happen — and of
one deliberately not attempted — is in
[`architecture/engagement-containment.md`](engagement-containment.md).

## Failure behavior

**A delegation is refused at one of its gates.** The ACL, alias, spawn-cap and
plugin-withholding refusals, and what each payload discloses, are described in
[`architecture/delegation.md`](delegation.md).

**A driver fails to start after the record exists.** The engagement is marked errored, topic
cleanup is attempted, and the caller is told the start failed.

**A launch that aborts rolls back what it provisioned, and finishes doing so.** The
`claude_code` launch removes, in order, the s6 service directory (recompiling after it), the
workspace tree, the control directory that holds `.casa-meta.json`, the uid's passwd/group
identity and its private outbox. That the sequence runs to its end even when a cancellation lands inside it is
INV-ENG-014. Each attempt keeps the best-effort floor it already had, so that one
rollback failure cannot mask the launch failure underneath it — but the floor is not uniform,
and the difference is operationally visible. Removing the service directory, pruning the
identity and tearing down the outbox each catch their failure and log it. The workspace tree
and the control directory do not: both run under `shutil.rmtree(..., ignore_errors=True)`,
which discards the error inside `rmtree` so the enclosing handler never sees one, and a
permission or I/O failure there leaves the tree in place with **no diagnostic at all**. What
cannot happen is that an attempt is skipped; a `rmtree` that fails still fails, silently for
those two. A cancellation arriving inside the rollback, at one of its own awaits,
is recorded rather than propagated, no further rollback await is attempted after
it, and it is re-raised once the removals have run, carrying the launch failure it interrupted
as its context so a reader downstream holds both facts rather than one. The removals being
synchronous is what makes that cheap: after the last await there is no suspension point at
which a second cancellation could land, so no drain, shield or detached task is involved and
the launcher's compile lock is neither released early nor held across a new await. Until this
release those guards were `except Exception`, and `CancelledError` — a `BaseException` —
escaped the handler outright, leaving a workspace whose meta already read `UNDERGOING`; the
sweeper returns past `UNDERGOING` before it reads a retention deadline, and uids are never
reused (INV-CONT-001), so every such abort leaked a workspace tree, a control directory, an
identity and an outbox permanently. *Which* removals a rollback is entered to run now depends
on the cause, and only one cause changes it — see below. The *reporting* of a compound failure
is still not combined: a launch that failed and was then cancelled during its rollback reaches
the caller's cancellation arm and is announced as a cancellation, the failure surviving only as
that exception's context and reaching no operator.

These launch-failure arms — a missing driver, a clearance change during launch, a superseded
plugin, a missing prompt template, an API-level fault, and a start that raised — deliberately
do NOT take the launch-death path's bounded topic notice. The reason is not that the death
path has no caller left; its post-start arm answers one too, with `launch_turn_incomplete`,
unless another writer won the terminal first, and its cancellation arm answers nobody because
the cancellation is re-raised. The reason is that these arms carry a *named fault* and the
death path carries only an *absence*. Each of the six is a fact about why the launch did not
happen, returned synchronously to the turn that asked for it, in a kind string it can act on
— one of them carries retry advice only that caller can use. The topic is aborted silently
because the launch never reached the point where the engagement is handed over as live: the
tool call still owns it, and is the one answering for it. That is not a claim that nothing
ran — the API-fault arm is raised after the stream is drained, so text the turn had already
posted progressively can be standing in the topic and cannot be retracted — it is a claim
about who answers. A launch death has no such fact to hand back: the turn ran to its end and
left nothing, or the launch was cancelled, and an operator's topic that says nothing is what
the notice exists to explain.

The claim is bounded to what the tree provides, and it is a claim about the *caller*: the
failure kind reaches the live invoking turn. Whether it then reaches the operator is that
turn's own business, and nothing here relays it. The exemption is from the notice, and from
nothing else — it does not extend to a cancellation, nor to a launch turn that ended after
starting without its terminal artifact, both of which stay on the launch-death path
(INV-ENG-011). What it never extended to is the *strictness* of the mark behind the abort, and
that is now settled the other way by INV-ENG-019 above: the exemption buys these arms their
silence, never the authority to close a topic on a transition they did not win or did not
persist.

**A graceful stop cancels a launch.** This is a different event from a launch that failed, and
it is recorded as one (INV-ENG-015). Casa's graceful-stop cleanup writes `casa_shutdown`
against each launch it finds registered *before* it cancels that launch, so the cause is
carried on the row rather than inferred at the cancellation site from the shape of the
cancellation — the discriminator INV-JOB-009 settled for the job ledger, applied here rather
than folded into it. Three readers use it and no other carrier exists: the rollback, to decide
its removal set; the cancellation arm, to record a reason that says Casa was stopping instead
of blaming a tool call nothing cancelled; and the launch-death reporter, to decide whether the
retained workspace may be given terminal metadata.

What the rollback then does is *fewer* removals, not abandoned ones. The s6 service source is
still removed and the live database still recompiled — a service source is not the executor's
work, and leaving one planted with no workspace under it is its own defect. The workspace tree,
the control directory, the uid's identity and its private outbox are retained: an engagement's
workspace is the executor's only copy of what it produced, uids are never reused
(INV-CONT-001), and a stop Casa itself initiated is not a reason to destroy any of it.

Retention that does not end is not retention. A workspace kept with untouched metadata reads
`UNDERGOING`, and the sweeper returns on `UNDERGOING` before it reads any deadline, so it would
leak forever. The launch-death reporter therefore rewrites `.casa-meta.json` with a terminal
status and a retention deadline — but only once its own strict terminal transition has
committed, which is what makes the metadata safe: metadata saying *terminal, reap after seven
days* written while the record is still live would have boot replay resume the engagement into
that workspace, and one post-deadline sweep then delete it underneath the resumed CLI. Written
after the durable flip, "the metadata is terminal" implies "the record is terminal", and replay
never selects it. The operator's notice names the retention window if and only if that write
actually happened; when it did not, the notice says the launch was stopped and claims nothing
about expiry, because a promised window the sweep will not honour is a durable false statement
about state the operator will go looking for.

**What it does not cover**, and each exclusion is a place the change deliberately claims
nothing. A launch that becomes durable but is not yet enrolled when the stop latches carries
no cause: it is still enrolled, tagged and cancelled when it does enrol, but the cleanup has
already looked, so its death report is best-effort — which is what every death report is
today, so the window is a case this does not improve rather than one it makes worse. The
notice is *attempted*, never guaranteed: a wedged or failing transport still loses it,
bounded and logged. The stamp is *ordered*, not complete — a terminal write that commits
before the stop records the cause carries only the failure it was made for, which is the
primary fact and is not made false by the missing modifier. And the metadata clause is a
safety claim, not a liveness one: it says such metadata never precedes its durable record and
never authorizes a reap under a live one, not that every retained workspace is eventually
reaped. Three paths leave one `UNDERGOING` and therefore unreaped — the reporter loses the
terminal race to a `mark_error` inside the rollback, its strict transition fails to persist,
or it is destroyed after committing and before the write. All three are fail-safe, all three
are logged at `error`, and all three are strictly better than deleting the work outright. An
ordinary abort and a creator or barge-in cancellation are outside this entirely: both keep
the full five-removal set, unchanged.

**A restart interrupts an engagement.** Persisted records load with `active` rewritten to
`idle`. Replay is attempted only for the driver kind that supports it. A record whose
workspace or recorded plugin artifact is missing is *refused* with a warning — validated
before the intact-service fast path, so an ordinary restart cannot start a service whose run
script would exit-and-respawn forever — and a missing definition is skipped with a warning.
A failed stdin-FIFO recreation and a failed service start are refusals of the same kind:
the record is marked errored and no background spool/relay machinery attaches, rather than
accepting operator messages into an engagement with no consumer (or starting one that would
crash-loop under its supervisor). A record still owing a clearance-downgrade context
rebuild (INV-MEM-011) is never *resumed*: replay drops its session pointer and archive
cache and re-renders the workspace at the clamped floor first, refusing the same way if
that fails. Every one of those decisions sits downstream of preconditions this document does
not own: INV-CONT-004 and INV-CONT-005.

**A restart replays an outcome nobody was told.** A terminal record is not only state to be
tidied: if the finalization funnel committed it, somebody is still owed the news (INV-ENG-018,
in [`architecture/engagement-terminal-telling.md`](engagement-terminal-telling.md)). Startup walks the
records still carrying that obligation and enqueues one notice each, addressed from the
record's own persisted origin — the role that asked, on the channel it asked from, quoting the
request. It runs once the channels and the resident loops are up, because a notice enqueued
before a resident can consume it is not a telling; and it does not wait for delivery, because
a resident turn can take minutes and boot cannot. An unroutable role is logged and RETAINED
for the next boot rather than dropped.

What such a notice can say is bounded by what the tombstone keeps, which is less than the live
path has. There is no completion text, no artifacts and no next steps on the record — no such
fields exist — so the replay reports the FACT of the outcome and says plainly that the notice
does not carry the report. It does not say the report is GONE, which would be a claim about
storage this path cannot make: a finished engagement's structured summary may well have been
retained on the shared bank. It never reconstructs an answer from the task or from a stale
summary message id.

## Extension points

**A new launch-failure arm** decides one thing first: does it carry a named fault the calling
turn can act on, or only an absence? The first returns a kind string and aborts the topic
silently; the second belongs on the launch-death path, with its bounded notice.

**A new rollback removal** joins the synchronous tail and inherits both of that tail's
properties — it is attempted even when a cancellation has already been recorded, and it must
not await. It also owes an answer to the per-cause question: whether a stop-caused abort is
entered to run it, or whether it is one of the removals retention withholds.

**A new replay refusal** marks the record errored and attaches no background machinery, rather
than admitting operator messages into an engagement with no consumer.

**A new terminal writer** decides whether it is the one telling the engager. If it announces
the outcome, it arms the durable obligation in the same transition that commits the terminal;
if it does not — the launch-failure owner and the launch-death reporter both answer or notify
by other means — it leaves the default and owes nothing. The obligation follows the writer,
not the record, so there is no property of a record that can decide it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/casa_core.py::replay_undergoing_engagements`
- `casa/rootfs/opt/casa/tools.py::_abort_engagement_topic`

**Tests**
- `tests/test_claude_code_driver.py`
- `tests/test_boot_replay.py`
- `tests/test_engage_executor_tool.py`
- `tests/test_launch_failure_terminal_authority.py`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-containment.md`](../architecture/engagement-containment.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
<!-- END SOURCEMAP -->
