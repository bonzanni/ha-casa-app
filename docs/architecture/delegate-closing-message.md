---
last_reviewed: 2026-11-10
---

# A delegate's closing message

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a delegation result or notice carries about the message the delegate closed its turn
with: Casa's note when that message is only silence, and the closing message itself when the
length cap cut it from the result text. How a delegation is launched is
[`delegation.md`](delegation.md); what a finished delegation owes its creator, and what the
boot replay tells the resident, is [`delegation-announcements.md`](delegation-announcements.md).

## Mental model

**The resident tells the person the answer itself.** A delegate's answer comes back to the
resident as the delegation result — in-band for a `sync` delegation, in a notice for an
`async` one or a `sync` wait that went `pending` — and she passes it on in her own words.
Casa posts nothing of the answer under the delegate's name in the resident's chat; the
delegate's own posts there (cards, links, files, desk replies) still carry its `📊 <name>` label
([`specialist-desk.md`](specialist-desk.md)). A delegate with its own open chat also gets its
closing message, whole and before any cut, posted in that chat (nothing when it is silence),
while the resident still receives the bounded result described here
([`own-voice.md`](own-voice.md)).

**The result text is the whole turn, cut from the end.** Every text-bearing message of the
delegate's turn, joined (`DelegatedOutput.text`), truncated at the result cap
(`specialist_limits.truncate_output`, 20,000 characters), with `output_truncated` saying
whether it was cut. Casa's post-echo lines follow it.

**The closing message is the turn's last text-bearing message.** A delegate's turn often
writes text before or between its tool calls and then ends with its answer. The runner
records that last message (`DelegatedOutput.final_message`) beside the joined text, whole,
before any cut shortens `reply_messages` for the desk. `_closing_text` reads it whole, and
`_closing_words` bounds that at the same cap from its start; a producer that reports no
messages is a single-message turn, whose closing message is its whole text. The bounded
words are read for the two things below; the whole message is what a task in the delegate's
own chat posts there, and what a plugin continuation's turn must be exactly to declare
progress only ([`plugin-continuations.md`](plugin-continuations.md)).

**A closing silence is told as silence.** When the closing message is `<silent/>` — as after a
hand-over that already reached the person — the result or notice ends with Casa's note that
the delegate has nothing for the person (`_with_silent_close_note`), so the resident does not
retell the narration that came before it. For a task shown in the delegate's own chat the note
says the same without steering the resident to silence, since the person asked her
(`OWN_CHAT_SILENT_CLOSE_NOTE`, [`own-voice.md`](own-voice.md)). This is the #1075 closing-silence rule judged on
the last message alone.

**A closing message the cap cut away travels beside the result.** When the result text was
truncated and the kept prefix does not already end with the closing message,
`_closing_beyond_cut` returns that message with its sentinels dropped, and it travels as its
own field, `closing_message`, never concatenated into the capped text: a key of the
non-voice `sync` result; a field of `DelegationComplete`, which
`Agent._synthesize_delegation_turn` renders after the result text under one Casa line
(`CLOSING_MARKER`: the result above was cut at the length limit and the delegate's closing
message follows in full); and a field of the durable completion record (`VoiceJob`), written
beside the retained answer, so a boot replay renders it the same way. A turn under the cap, a
prefix that already ends with the closing message, and a closing silence carry none, and the
key is then absent from the `sync` result. A record written before the field existed loads
with it empty.

## Contracts & invariants

**INV-PASS-004**: A delegation result or notice whose delegate closed its turn with a message that is only silence ends with Casa's note that the delegate has nothing for the person; a closing message with words, or an empty one, carries no such note.

Both seams — the `sync` result (a voice run excepted) and the completion callback — pass their
text, after Casa's post-echo lines, through `_with_silent_close_note`, which judges the
closing message `_closing_words` returns: silence with at least one sentinel in it.

## Failure behavior

**An output Casa cannot read.** `_closing_words` and `_closing_beyond_cut` never raise: an
output they cannot read yields no closing message, so the result carries neither the note
nor the field, and the delegation's outcome is unchanged.

**What is still lost.** Text that is in neither the kept prefix nor the closing message — the
middle of a long turn whose answer was not its last message — is not carried, and a closing
message longer than the cap is kept from its start.

**The voice path.** A voice run's `sync` result carries no `closing_message`: its answer
keeps its own spoken-text bound ([`voice-delivery.md`](voice-delivery.md)).

## Extension points

**Another seam that returns a delegate's result** — a new result shape, a new notice arm —
passes its text through `_with_silent_close_note` and attaches `_closing_beyond_cut` beside
it, so a closing silence and a cut answer are told the same way on every path.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::_closing_words`
- `casa/rootfs/opt/casa/tools.py::_closing_text`
- `casa/rootfs/opt/casa/tools.py::_closing_beyond_cut`
- `casa/rootfs/opt/casa/tools.py::_with_silent_close_note`

**Tests**
- `tests/test_delegate_closing_words.py`

**Related**
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/delegation-announcements.md`](../architecture/delegation-announcements.md)
- [`architecture/specialist-desk.md`](../architecture/specialist-desk.md)
<!-- END SOURCEMAP -->
