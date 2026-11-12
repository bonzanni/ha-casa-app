---
last_reviewed: 2026-11-09
---

# Memory capture: what is kept as it happens, and what is kept out

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a conversation's words become durable memory at the moment they happen — Casa's
per-turn capture into the memory store, with its spool fallback — and which text is
eligible at all: write-trust, completeness, and the specialists whose output is kept out.
Capture is the only source of memory. How each captured item is identified and given its
tier floor is [`architecture/memory-labelling.md`](memory-labelling.md); the session
lifecycle — freshness windows, the reset, transcript retention — is
[`architecture/memory-lifecycle.md`](memory-lifecycle.md); the store itself and the registry
of everything that holds memory are [`architecture/memory-store.md`](memory-store.md).

## Mental model

**Capture happens where the words happen.** Each seam appends what it sees, once, and
awaits the append, so every capture a turn made has returned by the time the turn ends:

- **A resident turn's opening**, in the admitted block of `Agent._process`, before the model
  is called. The text is stored raw: the time envelope and Casa's notes are composed into
  the prompt separately, so neither reaches the event (INV-MEM-022). That covers an
  operator's DM and every other turn on a write-trusted channel — scheduled, reminder and
  trigger turns, and Casa's own turns such as a delegation's completion notice. An
  operator's message is keyed with the bot that received it, read from the route its
  endpoint stamped (`occurrence.message_key`); a context whose route is not a bot id is
  logged as a failed opening and captures nothing, never a key without its bot. Once an
  operator's message is durable, its occurrence key goes on the turn's origin, where
  `correct_memory` finds the message to mark ([`memory-tools.md`](memory-tools.md)).
- **Each text-bearing message the resident writes**, in the fold that streams it, before it
  is emitted. Every attempt of the turn folds its own messages, so a retry's output and a
  regenerated answer are captured as well; an API fault's prose is never folded.
- **A delegated run's request**, raw (the task, not the assembled prompt), when its prompt is
  assembled and before its client starts; and **its answer**, the whole live fold, when the
  run completes.
- **An engagement turn of a memory-writing specialist**: the operator's message handed to
  the job — a reply in their direct chat to one of its posts — as it was admitted (never the
  line Casa prefixes when it redelivers the message after a restart), once it is written into
  the job's session, and each message the specialist writes. Plugin jobs are not
  memory-writing specialists.
- **Casa's line about how a change of an agent's own chat turned out**, from the own-chat
  lifecycle (`Lifecycle._remember` → `TelegramHost.remember`). The resident starts a chat's
  setup or a move with its card, but what finishes it (the operator's Create, a handle typed
  differently in Telegram's form, Casa's question about which agent a new bot is for, the
  restart) happens on Casa's cards, never in its turns. A completed binding writes "Alex's chat
  is set up: @…." or "Alex's chat moved from @old to @new.", the handle read from the bot
  itself; a move the boot holds back writes that it waits and why (INV-MEM-047).
- **Casa's line that a specialist which keeps no memory posted to the operator's chat**, in
  the Telegram channel's `_record_post`, where every message Casa sends on a specialist's
  behalf lands: a plugin's link, message, page, proposal or file, a file sent with
  `send_media`, a run admission's question or status card, a task's answer and a stored-call
  receipt (the Stop's line is filed as the card it answers: the card's one event). The line is Casa's own words with the time, never anything of the post (INV-MEM-040): "Fido posted his answer to your chat."
  when the path that sent the post carries his answer — the message his own-chat task closed
  with (`result_broker.ANSWER_KINDS`, read from the post record's `kind`) — and "Fido posted to
  your chat." for every other post. A plugin's message, page, file, link or card and a
  stored-call receipt go out the same way mid-run and at the end, so nothing mechanical says
  one of them is the answer, and the post's text is never read to guess it; Fido's answer
  posted with its sources through his `show_sources` message slot is such a post. A task's
  start line and its answer in the agent's own chat share their owner and tool call, so
  each is posted under a slot of its own (`task-start`, `task-answer`) and is its own event:
  "posted to your chat." for the start line, "posted his answer" for the answer. It is
  not awaited at its seam: the landing is synchronous on the send path, so the write is
  detached, and a send never waits for or fails on it.
- **The operator's reply to a job of a specialist that keeps no memory**, in
  `TelegramChannel.deliver_operator_reply`, where it is handed to the job as a turn. Such an
  engagement captures nothing of its own, so his words were lost; they are captured as his
  message, the text he typed and nothing else (INV-MEM-044). A reply the job does not accept
  captures nothing here. Detached, like the post line, and for the same reason. Nothing
  anyone writes in the agent-log group reaches memory (INV-TG-016).

**A receipt means durable, and the receipt is all a turn waits for.**
`memory.capture.append` makes one item durable and returns a receipt: committed in one
short store transaction, or — when the store is unavailable or held past a 50 ms bound —
written to `/data/memory/spool.jsonl` and fsynced. That write is the only part of a capture
a seam awaits. Everything else is detached and never awaited by a turn: the first append
that commits while a spool waits starts a drain (one at a time); an old spool's notice is
sent under its own bound.

**A drain takes the spool whole, then stores it outside the lock.** Spool appends and the
drain share one lock, and the drain holds it for one step only: renaming `spool.jsonl` to
`spool.draining.jsonl` and fsyncing the directory. A line appended before the rename is in
that snapshot; a line appended after it starts a fresh spool, so a capture that has to spool
never waits for a drain. The drain then stores the immutable snapshot in batches, leaving
the store free between them for the captures of running turns, and removes it only once
every line is committed. At most one snapshot exists: a drain finishes a leftover one before
it takes the spool again. There is no model call on this path and no comparison of text:
the same words said twice are two events.

**Writing is narrower than reading.** Only write-trusted channels capture. A channel that
can read memory is not thereby able to store.

**A turn that did not finish is not a fact.** Write-trust decides *whether* a channel may
store; it does not decide whether what is being stored actually happened. A delegated
specialist run that the CLI ends without completing the turn has produced text, but not an
answer, and the store has no way to say "partial". The exclusion is therefore made where
the terminal verdict is known: the incomplete answer is never captured.

The rest of that exchange is admitted turn by turn. The caller's request is captured before
the run whenever it is non-blank — the caller did genuinely make it — regardless of whether
the specialist produces any answer text. The answer is captured only when it is non-empty and
the terminal evidence says the model actually finished it; a withheld answer is logged where
the run's evidence is read, and a blank request beside a withheld answer captures nothing.

**A specialist's own conversation is captured as the specialist's.** The conversation turn's
capture (`_TurnCapture`) labels the agent's messages with its own kind: `resident` for a
resident, `specialist` for a specialist talking in its own chat ([`own-voice.md`](own-voice.md)).

## Contracts & invariants

**INV-MEM-005**: Only write-trusted channels capture into memory.

Enforced by the channel write-policy check (`channel_policy.writes_to_bank`), read by every
capture seam: a turn or a delegation whose channel is not write-trusted captures
nothing (`memory.eligibility.admit`).

What it does not cover: `memory.capture.record` itself enforces nothing. A future seam that
skips `admit` would bypass it.

**INV-MEM-016**: A delegated specialist answer the terminal evidence does not certify complete — an aborted run, a terminal fault, or an answer the model did not finish — is never captured; the caller's own non-blank task is captured before the run, independently of the answer.

The answer is captured only when the output's own `answer_incomplete` is false.

A stored-call pinned turn ([`stored-call-buttons.md`](stored-call-buttons.md)) reads
nothing — its prompt carries no memory block — and captures nothing at all —
neither the Casa-composed task nor the model's text — since its only operator-visible
outcome is the plugin's receipt; the read and the write are skipped on the same
`stored_call` marker. The runner records the whole terminal shape once, beside the returned
text, and captures the answer only when a single completeness predicate on that
evidence says the model finished. The predicate is deliberately wider than the run's own
reported verdict, in three ways. A result carrying an error flag while still naming itself
a success is a failed API call, and a result naming any reason for stopping other than a
completed one — cancellation among them — is a turn that ended some other way; both now end
the run as a typed caller failure, raised before the answer is captured, so only the
request of those exchanges is kept. An otherwise completed result whose *stop reason* says
the model ran out of output tokens — or says anything else not on the short list of reasons
that mean *finished* — is a caller-visible success whose unfinished answer is withheld from
memory, audibly. Both vocabularies belong to the CLI and can grow, so each is tested
against an allow-list of completed values, never a list of known-bad ones: a reason this
release has never heard of withholds the answer rather than keeping it. A value that is not
even a string is normalized at capture to a sentinel that fails every such list — malformed
evidence never reads as the absent-field legacy case, which counts as completed. The
reported verdict itself is left alone — it has consumers outside memory, and widening it
there would change what they see. "Did not complete" covers every non-success terminal
subtype — including one this release has never seen — and a stream that ends with no
terminal message at all, which certifies nothing. The subtype is a *required* field on a
parsed terminal message, so a null there is malformed evidence and reads as an abort too;
the completed-without-subtype reading survives only for directly constructed outputs, which
is where the legacy and test shapes live.

The caller's request is kept deliberately: it is a true utterance. The two are independent
events rather than one paired record, so withholding the answer leaves nothing dangling.
Admission is per turn, and a whitespace-only request is blank.

What it does not cover: the rule for the delegation's *delivery* copy: a job row keeps its
answer only while that answer is owed to its creator, and drops it on delivery
(INV-JOB-015). One rule, two purposes — memory keeps what happened, the row keeps what has
not yet been said.

**INV-MEM-032**: No row of the memory store's `events` holds output authored by a specialist that writes no memory — a loaded specialist whose `memory.token_budget` is 0, or a role no longer loaded — whether it would arrive as its delegated answer, Casa's notice carrying it, a hand-off or relay task it wrote, or its job's turns; the resident's own words relaying it are captured.

The switch is the specialist's own declaration: a role that reads no memory writes none,
and a role Casa no longer has loaded — whose switch it cannot read — writes none either
(`delegated_memory.role_writes_memory`). It is read at the capture seams through
`memory.eligibility.admit`, with `delegated_memory.notice_from_excluded_role` for Casa's
notice header: the opening of a resident turn that is Casa's notice naming such
a role is not captured, while the resident's reply in that turn is; a delegated run of such
a role captures neither side; a request is captured only with the chain's resident shape;
an engagement of such a specialist captures no turn of its own, and the requester is
still told; the operator's own replies into its job are his
(INV-MEM-044). A delegated run's task is captured only as a resident's own words:
a chain of at most the resident and one specialist (`len(_handoff_chain) <= 2`) on a turn
Casa did not compose (`synthetic != "continuation"`).

What it does not cover: what the resident chooses to say about the result, which is the
resident's; Casa's own body-free line that such a specialist posted (INV-MEM-040); a specialist that writes memory quoting the excluded specialist's answer in its
own reply, which is that specialist's own output; and a Casa whose tools were never
initialised (an empty role map), where nothing is gated. It is the capture half of the rule; what reads the store later (ledgers, search, export, restore) is held to it by
the mechanisms that read.

**INV-MEM-040**: When a message Casa sends on behalf of a specialist that writes no memory lands in the configured operator chat, exactly one event is captured per post — Casa's words "<name> posted his answer to your chat." when the post's kind is a task's answer — the message an own-chat task closed its turn with — and "<name> posted to your chat." for every other kind, decided from the post record and never from its text, with the time, speaker `casa`, origin `system`, the chat's resident as its role, the specialist as its speaker id, a `private` floor — holding nothing of the post's body, caption, file name or buttons, and keyed by the post's identity (its owner, tool call and slot) so every page of it and a re-landing are one event; nothing is captured for a specialist that writes memory, another chat, or a channel with no operator chat.

The specialist's answer stays out of memory (INV-MEM-032), and so does Casa's notice carrying
it, so without this line the writer saw a request handed to the specialist and then nothing,
and kept the errand open. The seam is where every such message lands, `_record_post`, read
with the same `role_writes_memory` switch; the name is the persona name the post's own label
carries. Pinned by real channel sends over a fake bot against a real store: a canary in the
post's body reaches no row, pages and a re-landing are one event, a task's answer leaves one
answer line beside its start line's neutral one, and a failing capture still delivers the send.

What it does not cover: whether the post answered what was asked; the writer judges that from
the line and what it already holds. Casa's own cards under a specialist's label — the start
card of a browsing run, its status card, the line answering a Stop — are
Casa's status text, not the specialist's post, and leave no line. The write is detached: a
crash between the landing and the write loses the line, never the post.

**INV-MEM-047**: A binding of an agent's own bot that completes captures exactly one event in the resident's memory — Casa's words naming the bot's own handle as Telegram reported it at creation: that the agent's chat is set up there, or, for a move, that it moved there from the old handle — with speaker `casa`, origin `system`, the resident as its role, the agent as its speaker id, a `private` floor and the rule `own_chat_line`, handed to capture (detached, like Casa's line for a specialist's post) before the bound phase is saved; a later boot of the bound bot captures none, a pending switch the boot holds back captures the line that the move waits, and a capture that fails never stops the binding.

**INV-MEM-044**: When the operator's reply in their direct chat to one of a job's posts, the job being the engagement of a specialist that writes no memory, is handed to that engagement as a turn, exactly their typed words are captured once as their message — speaker `operator`, origin `operator`, no floor, the chat's resident as its role, keyed by their message with the key a memory-writing specialist's engagement gives it (`memory.occurrence.engagement_key`) — never the specialist's turn; nothing is captured for a specialist that writes memory.

The specialist's engagement captures nothing (INV-MEM-032), and the operator's words went down
with it: what he told Fido's job left no trace for the writer. They are his, so they are
classified like any message of his; the eligibility rule is stored as `topic_message`, the
name it was first stored under (`Source.JOB_REPLY`). One key per message, whichever seam
captures it: a redelivery, or a specialist whose memory switch changed between the hand-off
and its turn, adds nothing twice. Pinned through the real `_handle_serialized` over a real
channel and a real `in_casa` driver whose specialist answers with a canary, against a real
store: the canary reaches no row.

A reply into a job carries Casa's `[replying to «…»]` line for the job; the captured words are
theirs alone, the text their admission ticket holds. The capture (`capture_job_reply`) takes
a specialist's engagement only and never raises: a failure is logged and the reply still
reaches the job.

What it does not cover: a plugin job's replies, and the rest of the engagement: the
specialist's turns, Casa's cards and the taps answering them stay out. A job reply is not
marked for `correct_memory`. The write is detached: a crash between the hand-off and the write
loses his words, never the turn.

## Failure behavior

**The store is unavailable, or busy past the capture's bound.** The item is spooled and
fsynced, with a receipt, and the turn goes on; a locked store costs a turn at most the
50 ms bound per captured item, and never waits for a drain that is running. The next append
that commits starts a detached drain, and the memory writer drains a waiting spool on each of
its passes once the store is available, which covers a store that only came back after boot
([`memory-writer.md`](memory-writer.md)); `correct_memory` asks for the same detached drain
when the message it would mark is still spooled ([`memory-tools.md`](memory-tools.md)). The
drain inserts every line with an `INSERT` that
ignores a `source_key` already stored, so a drain cut off at any point and run again adds
nothing twice.

**The spool cannot be written either.** No receipt; the failure is logged at ERROR with the
item's key, and the turn goes on without that item in memory.

**The spool grows old.** When a spooled append finds the spool's oldest line more than an
hour old, the operator gets one private notice, sent detached so no turn waits on its
delivery; it is sent once per spool, and a send that fails or exceeds its own bound is
logged and retried at the next spooled append.

**A drain fails, or the process dies during one.** Whatever the drain had taken stays in
`spool.draining.jsonl` (a drain waits on the store no longer than a capture does, so one
blocked by another writer gives up at its next batch). The next drain, started by the next
healthy append, stores that snapshot first — its committed batches are ignored — and only
then takes the spool. Both files are registered memory: the export drains them, the wipe
deletes them. A line a
crash tore part-way is skipped by the drain — it was never fsynced, so it never had a
receipt — and the next spool write starts on a fresh line so it is never glued to one.

**A seam's capture raises.** It is logged and swallowed: capture never fails a turn or a
delegation. A cancellation is the caller's own and propagates.

## Extension points

**A new capture seam** builds its item with `memory.capture.event`, decides
it with `memory.eligibility.admit` from the predicates Casa already has, keys it only
through `memory.occurrence`, and awaits `memory.capture.record` at the seam — before
anything relies on the words being remembered.

**A new eligibility fact** is a mechanical fact the seam computes (a channel, a marker, a
role's switch, a chain's shape, a terminal verdict); what the text means is never decided
in code.

**A new file under `/data/memory`** is a registry row, and the store pin runs the producer
that writes it ([`memory-store.md`](memory-store.md)).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/memory/capture.py`
- `casa/rootfs/opt/casa/memory/eligibility.py`
- `casa/rootfs/opt/casa/agent.py::_TurnCapture`
- `casa/rootfs/opt/casa/tools.py::_run_delegated_agent`
- `casa/rootfs/opt/casa/tools.py::_capture_delegated`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::_EngagementCapture`
- `casa/rootfs/opt/casa/channel_policy.py::writes_to_bank`
- `casa/rootfs/opt/casa/delegated_memory.py::role_writes_memory`
- `casa/rootfs/opt/casa/delegated_memory.py::notice_from_excluded_role`
- `casa/rootfs/opt/casa/delegated_memory.py::capture_post_line`
- `casa/rootfs/opt/casa/delegated_memory.py::capture_job_reply`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._record_post`

**Tests**
- `tests/test_memory_capture.py`
- `tests/test_memory_specialist_post.py`
- `tests/test_own_chat_memory_line.py`
- `tests/test_memory_eligibility.py`
- `tests/test_memory_excluded_specialist.py`
- `tests/test_no_retain_specialist.py`
- `tests/test_channel_policy.py`
- `tests/test_specialist_memory_tiers.py`

**Related**
- [`architecture/memory-store.md`](../architecture/memory-store.md)
- [`architecture/memory-labelling.md`](../architecture/memory-labelling.md)
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/memory-scoping.md`](../architecture/memory-scoping.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
<!-- END SOURCEMAP -->
