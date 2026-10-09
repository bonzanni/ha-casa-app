---
last_reviewed: 2026-09-26
---

# What a finished delegation owes its creator

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The obligation a finished delegation leaves behind on the durable job ledger: the
announcement Casa owes its creator and when that is discharged, the answer retained so the
announcement survives a restart, what the boot replay tells the narrating resident, and how
long the finished row is kept. The ledger itself — snapshots, restart reconciliation, what a
graceful stop does to a live row — is
[`architecture/jobs-and-delivery.md`](jobs-and-delivery.md); the device delivery protocol
for a voice answer is [`architecture/voice-delivery.md`](voice-delivery.md).

## Mental model

**An announcement is owed until it is delivered, not until it is enqueued.** A durable
marker on the row says a creator is still owed a notice, and only the consuming resident's
channel reporting that the notice's turn reached the transport clears it; a process lost
before that announces again at the next boot.

**The answer lives exactly as long as the obligation.** A non-voice answer is written onto
the row in the snapshot that arms the obligation and removed in the snapshot that clears
it, so a replay can quote it — and the replay says that it is a replay.

**The notice also says what a plugin posted on the work's behalf.** When a plugin tool
running under the delegation, engagement or job delivered a message or a file straight to
the operator ([`plugin-result-contract.md`](plugin-result-contract.md)), the text the
terminal notice carries ends with one body-free Casa line per proven post — the
specialist's label, the kind, the page count or media kind — the same lines a synchronous
delegation's returned text ends with — on the error arm, after the message. The lines are
appended to the bounded answer before it is retained, so a replay carries them too; they
never quote the body, the caption or the file name.

**An engagement's live completion says the engagement has ended.** When a successful
completion notice belongs to an engagement (its context carries `engagement_id`), the
synthesized prompt adds, after the result text, that the engagement has ended and the person
can no longer talk with the specialist in its topic, and that where the summary — the
specialist's own words — says the topic or the conversation is still open, that is not passed
on as fact (#1411: a specialist's summary said its topic "will stay open" in the very call
that ended it, and the resident relayed it). It claims nothing about the topic's close, which
is best-effort. A delegation's notice carries no such line, and a replayed engagement outcome takes the
answerless arm, so INV-JOB-016's live/replay pairs are unchanged.

## Contracts & invariants

**INV-JOB-010**: An announcement Casa owes a creator is durably owed until it has been DELIVERED or answered by a clean chosen silence — the row's pending marker is cleared only once the consuming resident's channel reports that its turn reached the transport, or once that turn ended in a clean chosen silence (a final text of nothing but one or more `<silent/>` sentinels, with no error, no consumed SDK retry, a channel present, and every piece of model-authored content the turn or a synchronous delegate committed to the operator through Casa's own send paths confirmed delivered), never when the bus accepted the notice for enqueue — so an announcement lost with the process is announced again at the next boot. A turn that ends with no text and no sentinel (an answer given only through tool use) is not a chosen silence and stays owed, and the effect of any other tool on the turn is not observed.

Two markers carry it, and a row can only ever hold one. `orphan_notification_pending` is
written where it always was, by the conversion of a *live* row at boot.
`terminal_notification_pending` is new, and is written **in the same snapshot as the
terminal itself** by the arm that actually posts a notification — the async and
degraded-to-pending delegation callback, in all three of its shapes (a CLI abort, an
answer, an exception). A synchronous delegation arms nothing: its answer went back to the
caller in-band and no notice is owed, so arming it would re-announce every sync delegation
of the last result-TTL window at the next boot. A cancelled terminal arms nothing either,
for the reason INV-JOB-009 gives for a row its creator had already
cancelled ([`jobs-and-delivery.md`](jobs-and-delivery.md)).

What "delivered" means here is what the channel already says it means (INV-TG-006): the
first unit of output was accepted by the transport. A *normal return* is deliberately not
enough — every delivery method returns normally when the Telegram application is absent,
having made zero Bot API calls — and neither is a generic turn-failure reply, which tells
the operator that something broke rather than what their delegation did. Every ambiguous
answer keeps the obligation, because the cost of keeping it is one duplicate announcement
and the cost of dropping it is silence.

A **clean chosen silence** discharges it too (#1079). A resident that reads the notice and
decides there is nothing to tell answers with `<silent/>`, and before this that answer was
never acknowledged: the notice replayed at every boot, one resident turn each time, with the
answer kept on the row. The discharge is narrow on purpose. The sentinel must be there — the
admission records whether a suppressed final reply carried one, and an empty text is not a
choice. The turn must not have failed, and a narration turn has no authored origin, so the
retry-tainted-silence rule that turns such a turn into an error elsewhere never runs here: the
turn's own report of consumed retries is read directly, and a turn that did not report retains.
And anything the turn committed to the operator itself must have arrived. Every admission of
model text for a message, a media caption or a question keyboard opens a record on the turn's
scope — a caption-less media send opens one too, and a synchronous delegate's scope shares the
list — which only the sender's own positive evidence confirms: `DELIVERED` from the channel, a
media send that returned, a keyboard post that returned a message id. A send that failed or
whose outcome is unknown keeps the notice owed, so a turn that sent the news, saw it fail and
then fell silent is announced again. The decision is taken once, from what the record holds
when the silent turn ends: a delegate launched synchronously counts even if its wait timed out
and it went on as pending, up to that moment; an async delegate never counts, because its
result is told by its own announcement; and nothing added or confirmed later revises the
decision. A replayed orphan notice discharges the same way: a
resident that stays silent about a job it lost track of leaves that loss untold, which is the
cost of not replaying the notice forever.

A narration cut at its **turn limit** did not choose its silence (#1121, INV-TURN-014 in
[`turn-limits.md`](turn-limits.md)). When its only delivered output is Casa's own step-limit
line — it wrote nothing, or nothing but the sentinel — the notice stays owed: that line is a
send of its own, never the turn's delivery, so it acknowledges nothing, and the chosen-silence
discharge skips a limit-stopped turn. A cut narration whose partial narration did reach the
chat is acknowledged exactly as any delivered narration is; the line follows it.

What it does **not** claim is that the resident's words describe the delegation. The
acknowledgement is discharged by Casa's own output for that notification reaching the
transport, and nothing inspects the narration's content — a resident that answers something
else entirely still discharges it. That limit is deliberate and is the same one the LIVE
completion path has always had: a delegation that finishes while Casa is up is narrated by
a resident in its own words, with no verification either. Holding the recovery path to a
stricter standard would mean the announcement could no longer be a resident's narration at
all, which is a different product, not a stricter guard.

That duplicate is the accepted trade, stated plainly: a process lost between the delivery
and the durable acknowledgement announces the same outcome again at the next boot, as does
a terminal whose snapshot write failed and was landed afterwards by the registry-owned
retry. Boot never waits for any of it — a resident's turn can take minutes — so recovery
enqueues its notices and returns.

A terminal replayed at boot carries the answer the row kept for it, when it kept one. A
delegation whose answer was in hand when it completed retained that answer for exactly this
moment (INV-JOB-015 below), and the replay quotes it just as the live announcement would
have. A row that kept none — one written before the field existed, or by an arm that owed no
announcement — is still announced truthfully as having completed with its answer
unavailable, never as an empty answer and never laundered into a failure. Which of the two a
notice is, is the row's own durable fact and never "is the stored text empty": a specialist
that legitimately answered with nothing is not a row that kept nothing. A delegation that
*failed* replays its own durable typed kind, exactly as the live path would have reported it.

That changes what the accepted duplicate above costs. A process lost between the delivery
and the durable acknowledgement now announces the same outcome again **with its answer** —
there is no dedupe on the resident's side, and none was added, because hiding the duplicate
would mean tracking delivery of content Casa deliberately does not inspect. What the replay
does instead is say what it is. The resident is told that this is a post-restart
re-announcement whose full delivery was not confirmed — the interrupted relay may have shown
the operator nothing, a partial streamed draft, or the whole answer with only its
acknowledgement lost — and that the complete result is still owed; a live completion is never
so marked, and that statement is the only difference the replay introduces.

Every other terminal outcome a boot replays says what it is too. A failure, or a success whose
answer the row did not keep, is announced as an earlier outcome reached before the restart whose
announcement was never confirmed, with the instruction not to present it as something that just
happened; without that, a failure replayed at boot read exactly like one that had just occurred.
A converted orphan gets its own statement, because it reached no outcome before the restart: the
boot that found it still running recorded the loss, so its notice says the delegation was already
lost when Casa came back up. Each statement also gives the time Casa recorded the outcome — the
row's terminal time, rendered in the zone and form of the turn's `<current_time>` block, with a
rough age — and for an orphan that is when the loss was recorded, stated as such, since the work
may have stopped earlier. A row with no recorded time says its age is unknown rather than borrowing
another clock. The statement, time included, is one block inserted after the notice's header, so
on every arm the live and replayed prompts still differ in exactly that block. The replay mark is
the notice's own flag, set only by a boot replay, never the presence of a time. An engagement
outcome replayed at boot carries the same flag with its record's completion time, and so gets
the same outcome statement and time on the answerless-success and failure arms; that is stated
with INV-ENG-018 in [`engagement-terminal-telling.md`](engagement-terminal-telling.md), since an
engagement replay differs from its live notice in more than the statement. The boot
log names the same distinction: only a converted live row is logged as an orphan, and a row that
went terminal before the restart is logged as an unannounced outcome being replayed. A live prompt can
carry one more instruction that a replay cannot: the note telling the resident not to retry an
action that stopped at an operator approval
([`plugin-authorization.md`](plugin-authorization.md), under INV-PLUG-004) is read from an in-process record, which no
boot replay inherits. Repeating an answer the
operator may already have read is the price of never losing one, and a resident that read
the replay as a duplicate and narrated a fragment discharged the whole answer by the rule
above, which is why it is now told not to.

**INV-JOB-016**: A delegation outcome replayed at boot is handed to the consuming resident with one replay statement after the notice's header — for a retained answer, a post-restart re-announcement whose full delivery was not confirmed, with the instruction to relay the whole answer; for a failure or an answerless success, a post-restart re-announcement of an earlier outcome; for a converted orphan, a notice that the delegation was already lost when Casa came back up — and that statement carries the time Casa recorded the outcome (for an orphan, when the loss was recorded), or says that time is unknown. A completion announced live is never so marked, and that statement is the only difference the replay introduces: for the same completion the two synthesized prompts differ in exactly that statement, except where the live prompt also carries an instruction that depends on in-process state.

**INV-JOB-015**: A non-voice delegated answer is retained on the durable row exactly while its announcement is owed — it is written in the same snapshot that arms the obligation and only when the obligation is armed, and it is removed in the same snapshot that clears the obligation, on DELIVERY or on a clean chosen silence (INV-JOB-010) — so an answer that was in hand when a delegation completed reaches its creator across a restart, and stops being retained once the announcement has been acknowledged.

One predicate decides both ends, and it is the one that already decides whether the
announcement is owed at all: a terminal that owes no notice cannot store an answer, whatever
its caller passes, so the synchronous arm and any non-Telegram creator keep exactly the
posture they had. One method drops it, `ack_terminal_notification`, which is where the LIVE
announcement's acknowledgement and the boot replay's already both arrive — so the two paths
cannot drift into two rules, and a drop that fails leaves the answer owed rather than gone.
The voice arm is outside this: its result has its own lifecycle and its own TTL, and an
acknowledged voice delivery still keeps its answer for the continuations that replay it.

Two limits are stated rather than designed away. A narration whose HEAD reached the transport
discharges the obligation even if its tail then raised, by the rule above, and the answer is
dropped with it — Casa does not re-narrate a turn the operator has already begun reading. That
head is the one the channel's finalize reports; a streamed draft stamps nothing, which is why a
replay cannot say what, if anything, was seen. And
a delegation still executing when a stop begins boots as a lost row, which never held an
answer: what this retains is an answer that had already been written, not one that never was.

Every notice ends by quoting "the original user question", read from the completion's origin,
and that origin is the snapshot of the turn that launched the work. The turn that narrates a
completion is itself a turn, and a resident often delegates again from it. Its origin
therefore records the question its completion carried, and not the notice it was handed.
Without that, each hop's notice quoted the whole previous notice, result text included, so the
prompt grew with every hop of the chain, and a job row that takes its request from that origin
stored the nested text as its request.
The narration turn's own prompt and its options are still the full notice; only the recorded
question differs. Its recall, when it opens a fresh session, searches with Casa's fixed query
for turns no person opened, not with the notice ([`memory.md`](memory.md)). A completion that carried no question passes on an empty
one, never the notice. The question reaches `Agent._process` as an argument that
`Agent.handle_message` passes on the narration branch alone, never as a context key, so no
ingress can set it and no later turn can inherit it. Rows written before this rule keep the text
they stored, and their replays quote it.

**INV-JOB-017**: A turn synthesized from a delegation completion records, as its origin's question, the question that completion's origin carried — empty when it carried none — never the synthesized notice; so a delegation launched by `delegate_to_agent` from that turn records the chain's root question in its origin — and persists it as its job row's request wherever that row takes its request from the origin — and the next notice quotes that root rather than re-embedding earlier notices or their result texts. Every other turn records its own text. Rows persisted before this rule are not repaired.

## What Casa keeps about a finished delegation

The durable row holds the *caller's* prose — the request as it was made — the specialist's
role, the origin, the note the brief owes (`output_note`), the terminal state and its typed
failure envelope. The file is written
0600.

**It is kept until its deadline, and then it is deleted.** Every terminal write stamps the
row with a deadline: 24 h for an ordinary delegation, the shorter voice TTL for a trusted
voice-delivery row, which can be as little as thirty seconds. The first expiry pass at or
after that deadline REMOVES the row from the file — the request, its context and, on the
voice arm, the answer go with it, out of the file's bytes rather than out of a field. That
happens whatever state the delivery reached, a delivered or cancelled one included; there
is no marked-and-retained audit record, and a row with no deadline is a live job and is
never touched. This is the one statement of the retention rule; the delivery side points
here rather than restating it.

**One record is exempt: one that still owes its creator an announcement.** A terminal that
Casa has not yet been able to tell its creator about carries a durable marker until the
notice reaches the transport (INV-JOB-010), and such a row is kept — with its content —
past its deadline, so the boot replay still finds it. Its delivery still expires on time;
only the record survives. The first pass after the notice is acknowledged deletes it. This
is deliberately the same shape as INV-ENG-018's engagement-record exemption.

**Deletion is opportunistic, and this is a stated limit rather than an oversight.** The
passes are a delegation launch, the three voice job tools, and the delivery coordinator's
reconciliation — its one-second sweep, a route connecting, and each inbound job frame.
Nothing sweeps at boot or on a wall clock, so on an install where no delegation is launched
and no voice channel is running, a due row waits in the file until something runs a pass.

**It also holds the specialist's answer, for as long as that answer is owed.** The decision
was taken explicitly (#688) and then reversed explicitly: Casa used to write an empty result
on the Telegram and synchronous arms, so a delegation that finished while Casa was restarting
could only ever be announced as "it completed, the answer is gone, shall I run it again?" —
which is a worse outcome than the live path's for the same work. What that posture was
protecting against was a widened retention cost, and the cost is now bounded and stated on
both ends: an answer is written only onto a row that owes its creator an announcement, and it
is removed at the moment that announcement is acknowledged as delivered — before, and
independently of, the deadline that deletes the row itself (INV-JOB-015).

The window is therefore the delivery window, not a TTL: the answer is on the row from the
terminal write until the operator has been told, and a stop in the middle is precisely the
case it exists for. The synchronous arm still keeps nothing, because its answer went back to
the caller in-band and no notice is owed. The file is written 0600 under `/data`; the earlier
description of it here as "a file that backups and config-git snapshots reach" was never true
of it, because `config_git` versions `/config`.

**What Casa does not claim is that dropping the row's copy is forgetting.** The answer that
was delivered reached the operator's channel and the narrating resident's own turn, exactly
as a live delegation's does, and whatever those are retained in is governed by memory's rules
rather than by this one. This rule is about the durable delivery record.

The voice arm is the stated exception, and the asymmetry is deliberate rather than
accidental: a voice answer is persisted because the device delivery protocol and its
continuations replay it, which is a capability that must survive a restart and therefore
has to be in the row.

## Failure behavior

**A process is lost between the delivery and the acknowledgement.** The same outcome is
announced again at the next boot, with its answer when the row kept one — the accepted
duplicate INV-JOB-010 states. **The terminal snapshot write fails.** The registry-owned
retry lands it afterwards, carrying the same answer and the same marker. **Dropping the
answer fails.** The answer stays owed rather than gone (INV-JOB-015).

## Extension points

**A new terminal arm that posts a notification** must arm `terminal_notification_pending`
in the same snapshot as the terminal and attach the delivery acknowledgement to its notice;
an answer passed on an arm that owes no notice is not stored.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/casa_core.py::_notify_recovered_delegations`
- `casa/rootfs/opt/casa/agent.py::Agent._synthesize_delegation_turn`
- `casa/rootfs/opt/casa/job_registry.py::JobRegistry.ack_terminal_notification`

**Tests**
- `tests/test_delivery_acked_announcements.py`
- `tests/test_engagement_stays_open.py`
- `tests/test_delegation_chain_root_1160.py`
- `tests/test_delegation_chain_root_1160_regressions.py`

**Related**
- [`architecture/jobs-and-delivery.md`](../architecture/jobs-and-delivery.md)
- [`architecture/voice-delivery.md`](../architecture/voice-delivery.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
<!-- END SOURCEMAP -->
