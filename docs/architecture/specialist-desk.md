---
last_reviewed: 2026-10-07
---

# The specialist desk

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How the operator's swipe-reply on a message Casa posted for a specialist reaches that
specialist directly — the post map every delivered message is filed in, the route in the
Telegram DM handler, the desk a specialist keeps with a chat (a bounded dialogue log, reset
after idle, one use at a time), the desk turn and its reply, what the chat's resident learns
of it, and where an approval raised inside a desk turn continues. The posts themselves — how
a plugin's output is deposited and delivered — are
[`plugin-delivered-slots.md`](plugin-delivered-slots.md); the delegation a resident launches,
its ACL and its limits are [`delegation.md`](delegation.md); the approval challenge is
[`plugin-authorization.md`](plugin-authorization.md). Button taps and the stored calls they
execute are [`stored-call-buttons.md`](stored-call-buttons.md); files sent as a reply are a
later slice; the desk is Telegram-only.

## Mental model

**A reply on a specialist's post is that specialist's turn, not the resident's.** When the
operator swipe-replies on a message Casa posted for a specialist — a plugin's delivered
message, file or link, or the specialist's own earlier desk reply — Casa hands the operator's
exact words to that specialist as one desk turn. No resident model turn is spent, nothing is
retold: the specialist answers in the operator's chat under its own label (`📊 Finance`), and
the resident learns of the exchange as one body-free line at its next turn. A reply that does
not meet the route's four conditions — the sender is the authenticated operator, the quoted
message is retained in the post map for this chat and was posted for this operator, the
poster is not the chat's own resident, and the poster is a specialist (`is_specialist`: the
loaded role's `kind`, the same predicate the delegation gate reads) the resident may
delegate to now — takes the resident's path as before, its text unchanged (a reply carrying
Casa's note of what it answered, below). The route runs first in
the DM handler, ahead of the `/new` interception (so `/new, start over` on a Finance post is
Finance's turn, as text: it resets nothing, neither the resident nor the desk), and after the
chat's rate decision, taken exactly as today's path takes it.

**The post map is filed as messages land.** The channel files every physical message a post
produces — each page, each plain fallback chunk, the media message, the link message, a desk
reply's pages — under a record of its poster (the call's enforcement role and operator, the
slot, the tool-use id, the echo owner), keyed by the canonical int chat id and the Telegram
message id, the moment that message's send returns. A page the operator holds is therefore
routable even when a later page failed and the plugin's result was withheld. The map is
memory-only and count-bounded (4,096 entries, FIFO): a restart forgets it, and an entry
older than 4,096 newer posted messages is evicted. Both are stated behaviour, not a gap — a
reply on such a message reaches the resident with Casa's note of what it answered, and the
resident routes it by judgement.

**A reply the desk does not take still says what it answered.** A text message in the DM
that replies to another and does not route — on the resident's own post, a forgotten one, a
specialist's the resident may not delegate to now, anyone else's — reaches the resident with
its words unchanged and one Casa-composed note on the reserved `_reply_note` context key,
stamped after sanitization (`TelegramChannel._reply_note_for`, composed by `reply_note`): who
posted the quoted message, when (in Casa's zone), and what it read — its text or caption,
clipped to 600 characters (`REPLY_QUOTE_CHARS`), or `(no text)`. Who posted it is decided
from the post map and Telegram's sender ids, never from text, the first match winning: a
retained post names the specialist's label, or "on your behalf" for the chat's resident; a
message sent on behalf of a chat (`sender_chat` on either message) names no person; the
bot's own id with no record is "an earlier message from Casa with no record of who wrote it":
the map never holds the resident's own replies, so the note says it is most likely one of
the resident's, without claiming a lost record or a certain author — a specialist's file, Casa's
notices and a forgotten post have no record either; the sender's own id is their own
earlier message;
anything else is someone else's message. The resident's turn carries the note in its Casa
notes block, after the desk lines (below); a reply the desk takes carries none.

**A desk is a window on a dialogue, not a session.** Each (chat, specialist) pair has a desk:
an ordered log of exchanges — the operator's words, the resident's brief when it delegated,
the specialist's delivered text — injected into every desk turn's context as a plain
`<desk>` block, oldest first. It is not an SDK session resume: the specialist runner runs
every delegated turn in a fresh CLI session and gives the specialist its memory by prompt
injection, and the plugin's own store is the state a specialist works from. The desk keeps
at most twelve exchanges (the oldest dropped), each side clipped to 400 characters with a
visible `[…]`, the rendered block budgeted to 10,000 characters by dropping whole oldest
exchanges; a desk idle for an hour starts its next use empty. Every completed turn is an
exchange, a post-only or silent one included — its specialist side is then a body-free
marker, never the view.

**The block is written from the specialist's side.** It opens with a fixed frame line
(`DESK_FRAME`) saying these are the specialist's own recent exchanges in this chat, not a
transcript of other parties, and each side is labelled by party as the specialist sees it:
`[14:02] the operator: …`, `[14:02] you: …`, and a resident's brief as `[14:02] Ellen, on
the operator's behalf: …` (the caller's display name, clipped to 40 characters). A desk
turn's context opens with `turn_frame`: the task is the operator's own message to the
specialist, now, from the same operator as in the exchanges shown, and the resident did not
write or relay it — or, for an approval continuation, Casa's note of the operator's decision.
The frame and the labels are counted in the block's budget like any other text, so the
bounds above are unchanged; a budget too small to hold the tags and the frame renders no
block rather than overrunning. Only the rendering names the parties: the log stores what it
stored. When the operator's reply starts with `/` (leading spaces aside), `turn_frame` is
followed by one more line (`SLASH_TASK_LINE`): Casa did not run it as a command and it reset
nothing. The task itself stays the operator's exact words, and a tap, a continuation or a
file's turn (whose task is Casa's note, opening `[casa file]`, even when the caption starts
with `/`) never carries the line.

**One desk, one use at a time, the lock covering the whole use.** Every use of a desk — a
swipe-reply turn, an approval continuation, and the resident's delegations to that
specialist from that chat whether sync, degraded or async — runs alone under the desk's lock
from the idle check and the log read through the run, the classification, the delivery
bookkeeping and the exchange's commit, in arrival order. The specialist's concurrency permit
is taken after the lock (it never waits; a refusal is a notice) and released inside the
lock, before the lock, so the next queued use never wakes to a permit a finished run still
holds. At most three uses may wait, reserved before any task exists: a fourth swipe-reply or
continuation gets the busy notice at once, a fourth delegation gets the delegation tool's
typed busy result, and no task is created for either. A run cut off — at the ceiling, or by a
cancellation of the use — whose unwind outlives the runner's teardown bound still ends its use
(the permit released inside the lock, the ceiling's notice and `[no reply]` exchange as below),
but the desk then refuses every later use, as a faulted desk does, until that run has ended:
it is not a fault (no health row, no restart needed), and the next use after the run ends runs.

**The reply is the specialist's own words, labelled, under the output boundary.** The runner
joins the specialist's successive text-bearing messages with one blank line, as the resident's
own turn does (a message with no text, such as one that only calls a tool, adds none), so what
it wrote before and after a tool call stays two paragraphs. The runner's
output is classified exactly as a sync delegation classifies it — a CLI-aborted run yields
no text — then bounded by the same 20,000-character cap a sync answer gets, admitted under
the desk's own scope, prefixed with the label line, and sent through the resident reply path
(`send_response`): rendered, paginated, INV-OUT-001. Its pages join the post map under the
specialist, so the operator can reply to the reply. A plugin view, package or file the
specialist produces during the turn reaches the operator through the delivered-slot path,
verbatim — the desk never retells a view; a turn whose outcome was such a post and whose
final text is silent posts no reply. A reply whose last text-bearing message is nothing but
`<silent/>` after earlier text posts the earlier messages without it, whether or not the turn
posted anything (the operator's #1075 rule 2; its rule 1, silence after a proven outcome, is
not the desk's). It is judged on the runner's own list of the run's text-bearing messages —
one entry per model message, cut where an approval cut cuts the words — against the words
shown, and a reply truncated at the cap is posted truncated. Whatever that leaves, a
marker at either end never reaches the operator (#1342): a run of sentinels at the start or
the end of the words shown, with the whitespace between it and the words, is removed and the
words are posted (`output_boundary.without_sentinels`); nothing else in the words is touched.
A sentinel between two runs of words, or written inside other text — a code span, a link, a
quoting sentence, an escaped `\<silent/>` — is the specialist's content and is posted as
written, unless it is itself the reply's first or last item: Casa reads text, not intent, so a
final blockquote line `> <silent/>` posts as `>`, and a code block cut off just after a
literal sentinel loses that sentinel. That residual is accepted: telling the
two apart would take a Markdown parser, for a case a specialist hardly ever writes. A turn with no proven operator-visible outcome at all —
an empty answer with no proven post and no delivered media, a refused permit, a full queue,
an abort, an exception, a failed reply send — ends in ONE labelled, body-free Casa notice.

**A desk turn may start its specialist's own job.** A desk or delegated turn whose session
loads a job-declaring plugin holds `start_job` for the jobs the specialist's own plugins
declare, hosted on itself; the start takes the job's own permit scope, never the desk's lock or
permit, and the job's end notice goes to the chat's resident
([`specialist-job-start.md`](specialist-job-start.md)).

**A file can start a desk turn too.** A file the operator sends as a swipe-reply on a
specialist's post, or after a `📎` tap on its proposal, is stored in that specialist's own
inbox and starts one desk turn whose task Casa composes from the file's name and the caption,
framed as Casa's note of the file rather than as the operator's own reply
([`inbound-files.md`](inbound-files.md)). It is the same use of the same desk; only its lines
differ: every line it records carries the receipt first, such as `📊 Finance received your
file statement.pdf; answered (1 page).`, and a failed run says `… could not handle your file
statement.pdf (<kind>).` The turn's scope is armed over that one file before the run starts,
so its "answered without opening" line names only that file and clears when the specialist
opens it or shares it with a plugin, whatever else the inbox holds
([`output-boundary.md`](output-boundary.md)).

**Every line a desk turn records goes through one emitter.** The notice to the operator and
the resident's echo are the same string, composed once by `bounded_line`, which keeps the
label and the outcome whole and clips only the display fields (a file name, a job title) to
fit the 120-character echo line. Nothing else in the desk turn sends a notice or writes the
echo. Every such line states a past or standing fact: what happened, never what will.

**The resident learns, without a turn.** Each desk turn leaves one Casa line on the chat's
echo ledger (`📊 Finance answered your reply (2 pages).`, `… could not handle your reply
(specialist_turn_limit).`), with the delivered-slot echo lines of any post the turn made; the
resident's next turn in that chat drains them (read-and-clear), each marked `(front desk)`,
at most five then a count, each within 120 characters, as the first note of its Casa notes
block: one `<casa_notes>` block directly after the turn's `<current_time>` envelope
(`timekeeping.compose_turn_preamble`), which the transcript readback strips with the
envelope, so no Casa line is retained as the operator's words (INV-MEM-022 in
[`memory-labelling.md`](memory-labelling.md)). No narration turn is synthesised; the
origin's `user_text` and the recall query stay raw; nothing is persisted.

**An approval raised inside a desk turn continues the desk.** The desk turn's grant identity
carries an advisory `desk_role` beside its advisory `target_role` (which stays the resident,
with its slot-wait semantics); the challenge record carries the destination, and the finish
hook reads it at settle time, so a pending challenge the resident's delegation raised and a
desk then raised again with the identical call is promoted to that desk — the operator's one
approval continues the desk that last asked. The continuation is admitted by the route's own
conditions at dispatch: the approver is the authenticated operator and the specialist is
still one the resident may delegate to; otherwise one labelled notice and no specialist run,
the pending grant left to expire. The grant key and its single-use binding are unchanged.

## Contracts & invariants

**INV-DESK-001**: An operator's Telegram message that quotes a message Casa posted for a specialist's slot in that chat, for that operator, and still retained in its post map, reaches that specialist as one desk turn carrying the operator's exact words — never a resident turn — when the specialist is one the chat's resident may delegate to; every other message, quoting or not, takes the path that existed before.

The four conditions are positive and read from Casa's own state — the authenticated
operator check the ingress already makes, the post map the channel filed, the chat's default
resident, the live delegate map the ACL reads — never from text. The route runs first in the
serialised DM handler and after the chat's rate decision; the desk task is tracked like an
engagement turn's so the per-chat serial lock is released at once and a stop can drain it.
The map's retention is the invariant's edge: a restart or eviction makes the reply a
resident message carrying Casa's note (INV-DESK-004), by design.

**INV-DESK-002**: A desk turn's reply reaches the operator only as an admitted, labelled, paginated post of the specialist's own completed and bounded text — of a run that ended with a protected call waiting on the operator's approval, only the text written before that call; of a reply whose last text-bearing message is only `<silent/>` after earlier text, the earlier messages without it; and never with a `<silent/>` marker at the start or end of its words — whose messages join the post map; a turn that produces no proven operator-visible outcome, or whose reply is not proven, ends in one labelled Casa notice; the chat's resident learns of the turn only through a body-free line at its next turn, and no resident model turn is spent on it.

The desk turn's origin is the DM's own context with the three fields the provenance
classifier needs, the resident's role, the specialist as the executing role, depth one, a
fresh turn id as `send_media`'s quota key and the delivered-slot echo owner, and the reserved
`desk` marker; it classifies `dm`/`delegated`, so plugin capabilities, approvals and
delivered posts inside the turn work exactly as inside a sync delegation. The scope is
minted for the desk (no inherited obligations) and carried as `origin["turn_scope"]`.

**INV-DESK-003**: A desk's log holds at most twelve bounded exchanges, in arrival order, shared by swipe-replies and the resident's delegations to that specialist from that chat from the first of them — every completed desk turn an exchange — and is empty on the first use after the idle bound or a restart; every use of a desk — a swipe-reply, a continuation, a sync, degraded or async delegation — runs alone under its lock from the log read to the exchange's commit, in arrival order, and no later use starts a run while an earlier use's run, cut off at the ceiling or by a cancellation, is still unwinding — the desk refuses it until that run has ended.

A resident's delegation from an operator DM (`desk_for_delegation`: the operator's own Telegram
turn with no `synthetic` marker — a button continuation or a setup turn delegates as before) to
a specialist gets or creates the desk and
runs as one use of it: the resident's arguments are validated exactly as today (task 4,000,
context 8,000), then the desk block is fitted into what remains of the desk's 12,500-character
context budget — newest exchanges kept, possibly none — after the resident's own context;
an async launch or a degraded sync still returns `pending` to the resident at once while its
task waits for the desk. A delegation launched elsewhere (an engagement, a scheduled or
webhook turn, another chat), a job batch, and a voice turn touch no desk.

**INV-DESK-004**: A Telegram DM text message that replies to another message and that the desk does not take reaches the chat's resident with its text unchanged and one Casa-composed note on the reserved `_reply_note` context key, naming who posted the quoted message — decided from Casa's post map and Telegram's sender ids, never from text — when, and what it read, clipped to 600 characters.

The key is in `provenance.RESERVED_CONTEXT_KEYS`, so no external ingress can set it, and the
channel stamps it after sanitization; the resident's turn reads it only on a Telegram turn.
What it does not cover: a Casa message with no record — the resident's own reply, a file a
specialist sent, a Casa notice, a post the map has forgotten — is named by no author; a file
sent as a reply, and a message in an engagement topic, carry no note.

## Failure behavior

**The queue is full, the permit is refused, the run raises, is aborted by the CLI, or
exceeds the ceiling.** No reply post; one labelled, body-free notice (`📊 Finance could not
handle your reply (<kind>).`, `📊 Finance's desk was full when the place was requested.`,
`📊 Finance was at its concurrent-work limit when the turn was attempted.`); the echo line says
the same; a failed run is still logged as an exchange with the `[no reply]` marker.

**The turn ends with no proven operator-visible outcome.** The same notice shape (`📊 Finance
had nothing to add.`). A proven outcome is read from the two records that exist by design
(`turn_outcomes`): every message Casa posted for the turn, whatever the slot's kind — the post
map by owner (`PostMap.owned`, each record stamped with its delivery `kind`) — and the sends
the specialist's turn scope confirmed delivered (one suffices; a later send that failed does not
erase it — the resident's every-send closing-silence rule is not the desk's). Deliberate silence
— a reply that is nothing but sentinels — after a proven outcome stays silent; every
outcome with no S3 echo line of its own (a link post, a send the specialist made itself) adds
one Casa-composed line per kind, named from the records (`OUTCOME_ECHO`: `📊 Finance posted a
link to your chat.`, `… sent you a file.`, `… asked you a question.`, `… sent you a
message.`), never from a body; a message or file post is echoed once, by its S3 line.

**The run ends with a protected call waiting on the operator's approval** (#1252). The runner
reads the authorization hook's records at each call's result, as the resident's turn does
([`output-scope-properties.md`](output-scope-properties.md)), and when a call's `pending`
record — a POSTED deny, or a PENDING deny whose reused keyboard was posted — is still unanswered by a consume of the same grant at the end of the run it publishes
the messages written before that call (`TurnScope.approval_kept`). Only those are posted
under the label; when they are nothing, there is no labelled post, and the pending approval
counts as an outcome (`turn_outcomes(…, approval=True)`), so the echo is `… asked you a
question.` — also for a PENDING deny, whose keyboard may have been posted under an earlier turn. The
exchange logs the specialist's whole text.

**The reply post is not proven.** The exchange is logged (the specialist did answer; a retry
would re-run it), the notice says `📊 Finance answered; complete delivery could not be
confirmed.` (some pages may have landed), no retry;
if the notice itself fails, nothing more is attempted.

**A reply whose quoted message is not retained, by a non-operator, on the resident's own post,
or in a chat whose id does not normalise.** The resident's path, the text untouched, with
Casa's reply note (INV-DESK-004).

**An approval continuation whose desk is no longer delegable, or whose approver is not the
operator.** One labelled notice (`… could not continue (not delegable).`), the same line in
the echo, no specialist run; the pending grant expires. The same check — the specialist is
still one the resident may delegate to, not merely loaded — runs under the lock before every
desk turn, a file's included.

**Casa restarts.** The map, the desks and the echo ledger are gone; the next reply on an
older post reaches the resident with a note naming an earlier message from Casa, which can no
longer say who posted it; the next desk turn starts a fresh log.

## Extension points

**Buttons on a specialist's post** land in the same desk: a tap is a use of the desk under its
lock and queue, the stored call its task, and a desk whose pinned run could not be confirmed
terminated is *faulted* — every later use refused until restart
([`stored-call-termination.md`](stored-call-termination.md)).

**The idle bound** is a module constant until tuning has an evidence base; an app option
follows.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/specialist_desk.py`
- `casa/rootfs/opt/casa/result_broker.py::PostMap`
- `casa/rootfs/opt/casa/result_broker.py::PostRecord`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._maybe_route_desk_reply`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._spawn_desk_turn`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._dispatch_desk_continuation`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.deliver_desk_notice`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._record_post`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._reply_note_for`
- `casa/rootfs/opt/casa/output_boundary.py::TurnScope.for_desk`
- `casa/rootfs/opt/casa/output_boundary.py::without_sentinels`

**Tests**
- `tests/test_specialist_desk.py`
- `tests/test_desk_post_map.py`
- `tests/test_desk_route.py`
- `tests/test_desk_turn.py`
- `tests/test_desk_delegation.py`
- `tests/test_desk_continuation.py`
- `tests/test_desk_echo_prompt.py`
- `tests/test_file_handoff_turn.py`
- `tests/test_file_handoff_lines.py`
- `tests/test_pending_approval_desk.py`
- `tests/test_redcase_1283.py`
- `tests/test_redcase_1342.py`
- `tests/test_desk_closing_silence.py`
- `tests/test_reply_note.py`

**Related**
- [`architecture/plugin-delivered-slots.md`](../architecture/plugin-delivered-slots.md)
- [`architecture/inbound-files.md`](../architecture/inbound-files.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/plugin-authorization.md`](../architecture/plugin-authorization.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/output-boundary.md`](../architecture/output-boundary.md)
- [`architecture/memory-labelling.md`](../architecture/memory-labelling.md)
<!-- END SOURCEMAP -->
