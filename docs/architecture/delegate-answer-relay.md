---
last_reviewed: 2026-11-01
---

# A delegate's answer, posted under its own header

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a delegate's answer reaches the person in the Telegram DM: the assistant decides whether
to pass it on, and Casa posts it, in the delegate's own words, under the same `📊 <name>`
header the delegate's own posts carry. What Casa holds for that, when it refuses, and what a
failed send leaves behind. How a delegation is launched is
[`delegation.md`](delegation.md); what a finished delegation owes its creator is
[`delegation-announcements.md`](delegation-announcements.md).

## Mental model

**Two producers, one header.** A specialist's own posts (cards, links, files, desk replies)
are labelled by Casa with `📊 <name>`. A delegate's *answer* used to reach the person a second
way, retold by the assistant as "Alex: …". Now the assistant passes an answer on with the
`relay_answer` tool, and Casa posts it itself: the header line, then the delegate's answer
exactly as the delegate wrote it.

**The answer is the message the turn closed with.** A delegate's turn often writes text
before or between its tool calls ("Live view is done; observe the page.") and then ends with
its answer. Casa holds only that last text-bearing message, whole — never the narration before
it — so what is posted is what the delegate ended its turn with. When that message is silence
(`<silent/>`, as after a hand-over that already reached the person), nothing is held: this is
the #1075 closing-silence rule judged on the last message alone, and the earlier text is not
posted either. A sentinel beside the closing words is dropped and the words kept, as on a desk
reply (#1342). The assistant still reads the whole turn as the delegation result, and the
delegate's own memory retain still banks the whole turn: only what is posted under the
delegate's name is the closing message.

**A closing silence is told as silence.** When that closing message is silence, the
assistant would otherwise read only the narration before it, which can sound like progress
("Waiting for the run to get ready …") and draw a mechanical line of hers ("Fido is still on
it."). So the synchronous result (not a voice run's) and the later notice both end with
Casa's note that the delegate closed its turn with `<silent/>`, has nothing for the person,
and that she replies `<silent/>` unless she has something else for them. An empty closing
message is not a closing silence and carries no note.

**The model decides whether; code decides only mechanical facts.** The assistant still judges
whether an answer is for the person at all, and may write words of her own beside it (posted
as hers, after it). She never edits or selects the delegate's lines: the tool takes only a
delegation id, and Casa posts the whole held answer. Code checks that the id is held for this
conversation and not yet posted; it does not judge what the answer means.

**What is held.** When a resident's own Telegram turn gets a delegate's answer back — a
synchronous result, or a later notification (an async delegation, a sync one past its wait,
a browsing continuation's announced answer) — Casa holds the delegate's closing message,
bounded, and never Casa's post-echo lines, keyed by the full delegation id, with the
delegate's role and its label as they stand then (a later rename does not move the header),
the resident's role, the chat and the operator. A sync result then carries a
`relay` field naming the id; a notification turn gets one line naming the full id (its header
shows eight characters). Nothing is held for a voice run, a delegation made inside a delegated
run, an origin without a chat, or a turn whose closing message is only silence. An answer the run did not
finish (cut off before its end, which the memory retain also withholds) and a sync answer
whose run left an approval waiting are held but not offered: no relay field or notice line
names them, and a relay called anyway refuses, so the assistant passes the answer on herself
and can say it was cut off. Holding is memory-only and
bounded: only a never-posted answer is evicted (oldest first), so an id Casa no longer knows
is one whose answer the person has not seen; a posted entry drops its words but keeps its id
(bounded far higher), so a later call learns it was posted. A restart forgets it all, and a boot replay's notice is not relayable, so
the assistant then passes the answer on by hand, as the fallback below says.

**The post.** The labelled text is admitted on the assistant's turn as one discrete send, so
any line Casa's admission prepends to her output stands above the delegate's header, never
under it. It is sent through the channel's paged reply path, and each landed page is filed in
the post map under a `relayed_answer` record of the delegate: a swipe-reply on it reaches that
delegate's desk, as on its own posts (a scheduled turn's origin names no operator, so its
relay is filed under the configured operator when it lands in that operator's DM) (see [`specialist-desk.md`](specialist-desk.md)). The
tool's result tells the assistant the person now sees the answer under its name, and to reply
`<silent/>` unless she has something of her own to add. The tool is an accounted send, so a
buffered turn's closing silence after a delivered relay is earned (see
[`scheduled-prompt-endings.md`](scheduled-prompt-endings.md)).

**Memory is unchanged.** A delegate's answer reaches memory through the delegated run's own
capture, attributed to the delegate and gated on its memory switch
([`memory-capture.md`](memory-capture.md)), not through the assistant's retelling; a later
notice is captured as the opening of a turn as before. The relay posts the answer and
captures no copy of it.

**The fallback.** When `relay_answer` cannot post — nothing held, the channel unavailable, the
send failed — its result says so, and the doctrine has the assistant pass the answer on by
hand in the delegate's own words, introduced only by its name.

## Contracts & invariants

**INV-PASS-001**: A held answer is posted at most once, and only as the delegate's held words under that delegate's label as held; only a send proven unsent leaves it relayable.

The entry is claimed before the first await, so two concurrent calls post once; a second call
is told the answer is already posted and sends nothing. The held words are the delegate's
alone — taken before Casa's echo lines — and the label comes from `post_label` of the
delegate's role as held. A send that delivered nothing, or raised with no page landed, returns the
entry to held only when the failure proves nothing reached Telegram; any other failure is not
posted again.

**INV-PASS-003**: What is held for relay is the delegate's last text-bearing message alone, with no sentinel at its edges; a turn whose last such message strips to silence holds nothing, and narration written earlier in the turn is never held.

The runner records the turn's last text-bearing message (`DelegatedOutput.final_message`)
beside the joined text, whole, before any #1252 cut shortens `reply_messages` for the desk;
both holding seams — the synchronous result and the completion callback — hold
`_relay_words` of the output, bounded by the same cap. A producer that reports no messages is
taken as a single-message turn.

**INV-PASS-004**: A delegation result or notice whose delegate closed its turn with a message that is only silence ends with Casa's note that the delegate has nothing for the person; a closing message with words, or an empty one, carries no such note.

Both seams that hold for relay — the synchronous result (a voice run excepted) and the
completion callback — pass their text, after Casa's post-echo lines, through
`_with_silent_close_note`, which judges the same closing message `_relay_words` returns: silence
with at least one sentinel in it.

**INV-PASS-002**: Only the resident turn of the conversation that delegated may relay an answer, and never one whose synchronous run ended with an approval waiting or one the run did not finish.

The caller's chat and role must equal the entry's; another conversation is refused with
nothing sent. The approval fact is captured when the answer is held, from the child run's own
scope, because the resident's fold consumes the per-delegation approval record at that
result. A turn whose final reply is itself the delivery refuses the tool, as it refuses
`send_message`.

## Failure behavior

**Nothing held.** The id is unknown, evicted, from a boot replay or a voice run: refused; the
assistant passes the answer on by hand.

**Delivery.** A send proven unsent — Telegram refused it, or the request never connected (the
channel's own taxonomy), with no page landed: an error saying the answer did NOT reach the
person; the entry is still held, so one more call may post it. Anything less certain — a page
landed and the send then raised, a lost acknowledgement (a read timeout after the request went
out), a cancel mid-send — is an error saying delivery could not be confirmed, and nothing more
is ever posted for that id: Telegram may have shown it, and the person must not see it twice.

**The residuals this accepts, by design.** Each needs a Telegram failure or a very long run,
and each trades a possible loss for never a duplicate:
- an ambiguous send — a lost acknowledgement, or a cancel mid-send — that in fact showed
  nothing leaves the answer unseen, and it is not retried;
- a multi-page answer whose later page is refused after page 1 landed loses the rest: the
  pages are not resumed;
- posted ids are kept as a bounded record (`RELAY_SPENT_MAX`, 10,000); an id older than that
  reads as not held, and the hand-passed fallback could then repeat an answer already shown.

**Relay and retelling.** Nothing in code stops the assistant from relaying an answer and then
also retelling it; the doctrine and the tool's result steer her to silence, and the live
relay evals measure it.

## Extension points

**Another channel** would need its own labelled post and its own hold rule; today only a
Telegram origin is held. **A durable hold** (so a boot replay could relay) would store the
delegate's words apart from Casa's echo in the job row.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::relay_answer`
- `casa/rootfs/opt/casa/tools.py::_hold_answer`
- `casa/rootfs/opt/casa/tools.py::_relay_words`
- `casa/rootfs/opt/casa/tools.py::Relayable`
- `casa/rootfs/opt/casa/tools.py::_with_silent_close_note`

**Tests**
- `tests/test_relay_answer.py`

**Related**
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/delegation-announcements.md`](../architecture/delegation-announcements.md)
- [`architecture/specialist-desk.md`](../architecture/specialist-desk.md)
<!-- END SOURCEMAP -->
