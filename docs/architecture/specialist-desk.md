---
last_reviewed: 2026-11-08
---

# The specialist desk

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How the operator's swipe-reply on a message Casa posted for a specialist reaches that
specialist directly — the post map every delivered message is filed in, the route in the
Telegram DM handler, the desk a specialist keeps with a chat (a bounded dialogue log, reset
after idle, one use at a time), and the note a reply the desk does not take carries. The desk
turn's reply, the notices and lines it records, what the chat's resident learns of it, and
where an approval raised inside a desk turn continues are
[`specialist-desk-reply.md`](specialist-desk-reply.md). The posts themselves — how
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
poster is not the chat's own resident, and the poster holds a desk (`has_desk`: the loaded
role's `kind` is `specialist`, or `executor` for the configurator; the same predicate the
delegation gate reads) and is one the resident may delegate to now — takes the resident's
path, its text unchanged (a reply carrying Casa's note of what it answered, below). The route
runs first in the DM handler, ahead of the `/new` interception (so `/new, start over` on a Finance post is
Finance's turn, as text: it resets nothing, neither the resident nor the desk), and after the
chat's rate decision, taken exactly as today's path takes it.

**The configurator holds a desk as a specialist does.** A reply on a post Casa made for it,
its own earlier desk reply included, is the configurator's desk
turn, and the resident's DM delegation to it uses the same desk; each such run also takes the
configurator lease ([`configurator.md`](configurator.md)). Everything below about a
specialist's desk holds for it alike.

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
notes block, after the desk lines ([`specialist-desk-reply.md`](specialist-desk-reply.md)); a reply the desk takes carries none.

**A desk is a window on a dialogue, not a session.** Each (chat, specialist) pair has a desk:
an ordered log of exchanges — the operator's words, the resident's brief when it delegated,
the specialist's delivered text — injected into every desk turn's context as a plain
`<desk>` block, oldest first. It is not an SDK session resume: the specialist runner runs
every delegated turn in a fresh CLI session, a plugin's continuation turn alone resuming its
request's ([`plugin-continuations.md`](plugin-continuations.md)), and gives the specialist its memory by prompt
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
(the permit released inside the lock, the ceiling's notice and `[no reply]` exchange as in [`specialist-desk-reply.md`](specialist-desk-reply.md)),
but the desk then refuses every later use, as a faulted desk does, until that run has ended:
it is not a fault (no health row, no restart needed), and the next use after the run ends runs.

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

## Contracts & invariants

**INV-DESK-005**: An operator's Telegram message that quotes a message Casa posted for an agent that holds a desk (a specialist or the configurator) in that chat, for that operator, and still retained in its post map, reaches that agent as one desk turn carrying the operator's exact words — never a resident turn — when it is one the chat's resident may delegate to, unless the quoted message is a post of a background job — one whose record still counts as live goes into that job instead (INV-BGJOB-012), and one of an ended job this operator started in this chat goes to the resident with the job's end named (INV-BGJOB-024); every other message, quoting or not, takes the resident's path.

The four conditions are positive and read from Casa's own state — the authenticated
operator check the ingress already makes, the post map the channel filed, the chat's default
resident, the live delegate map the ACL reads — never from text. The route runs in the
serialised DM handler right after a live job's reply route
([`background-job-direct-chat.md`](background-job-direct-chat.md)) and after the chat's rate
decision; a job's plugin post the desk would otherwise take reaches the job while it lives, and
the resident, told how the job ended, once it has ended — the ended-job check runs ahead of
this route, since the desk cannot see the job; the desk task is tracked like an
engagement turn's so the per-chat serial lock is released at once and a stop can drain it.
The map's retention is the invariant's edge: a restart or eviction makes the reply a
resident message carrying Casa's note (INV-DESK-004), by design.

**INV-DESK-003**: A desk's log holds at most twelve bounded exchanges, in arrival order, shared by swipe-replies and the resident's delegations to that specialist from that chat from the first of them — every completed desk turn an exchange — and is empty on the first use after the idle bound or a restart; every use of a desk — a swipe-reply, a continuation, a sync, degraded or async delegation — runs alone under its lock from the log read to the exchange's commit, in arrival order, and no later use starts a run while an earlier use's run, cut off at the ceiling or by a cancellation, is still unwinding — the desk refuses it until that run has ended.

A resident's delegation from an operator DM (`desk_for_delegation`: the operator's own Telegram
turn with no `synthetic` marker — a button continuation or a setup turn delegates as before) to
a specialist gets or creates the desk and
runs as one use of it: the resident's arguments are validated exactly as today (task 4,000,
context 8,000), then the desk block is fitted into what remains of the desk's 12,500-character
context budget — newest exchanges kept, possibly none — after the resident's own context;
an async launch or a degraded sync still returns `pending` to the resident at once while its
task waits for the desk. A delegation launched elsewhere (an engagement, a scheduled or
webhook turn, another chat), a job's session, and a voice turn touch no desk.

**INV-DESK-004**: A Telegram DM text message that replies to another message and that the desk does not take reaches the chat's resident with its text unchanged and one Casa-composed note on the reserved `_reply_note` context key, naming who posted the quoted message — decided from Casa's post map and Telegram's sender ids, never from text — when, and what it read, clipped to 600 characters.

The key is in `provenance.RESERVED_CONTEXT_KEYS`, so no external ingress can set it, and the
channel stamps it after sanitization; the resident's turn reads it only on a Telegram turn.
What it does not cover: a Casa message with no record — the resident's own reply, a file a
specialist sent, a Casa notice, a post the map has forgotten — is named by no author; a file
sent as a reply without a caption starts no turn and so carries no note.

## Failure behavior

**A reply whose quoted message is not retained, by a non-operator, on the resident's own post,
or in a chat whose id does not normalise.** The resident's path, the text untouched, with
Casa's reply note (INV-DESK-004).

**Casa restarts.** The map, the desks and the echo ledger are gone; the next reply on an
older post reaches the resident with a note naming an earlier message from Casa, which can no
longer say who posted it; the next desk turn starts a fresh log.

## Extension points

**Buttons on a specialist's post** land in the same desk: a tap is a use of the desk under its
lock and queue, the stored call its task, and a desk whose pinned run could not be confirmed
terminated is *faulted* — every later use refused until restart
([`stored-call-termination.md`](stored-call-termination.md)). The one exception is a stored
call of the plugin's declared stop tool, which takes no desk and is called directly
([`plugin-stop.md`](plugin-stop.md)).

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
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._record_post`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._reply_note_for`

**Tests**
- `tests/test_specialist_desk.py`
- `tests/test_desk_post_map.py`
- `tests/test_desk_route.py`
- `tests/test_desk_turn.py`
- `tests/test_desk_delegation.py`
- `tests/test_desk_echo_prompt.py`
- `tests/test_file_handoff_turn.py`
- `tests/test_file_handoff_lines.py`
- `tests/test_reply_note.py`

**Related**
- [`architecture/plugin-delivered-slots.md`](../architecture/plugin-delivered-slots.md)
- [`architecture/inbound-files.md`](../architecture/inbound-files.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/plugin-authorization.md`](../architecture/plugin-authorization.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/output-boundary.md`](../architecture/output-boundary.md)
- [`architecture/memory-labelling.md`](../architecture/memory-labelling.md)
- [`architecture/specialist-desk-reply.md`](../architecture/specialist-desk-reply.md)
<!-- END SOURCEMAP -->
