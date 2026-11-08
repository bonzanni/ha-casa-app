---
last_reviewed: 2026-10-07
---

# A specialist desk turn's reply

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a specialist desk turn produces once it runs — the labelled reply of the specialist's
own words under the output boundary, the closing-silence and sentinel rules it is judged by,
the one notice a turn with no proven outcome ends in, the one emitter every line it records
goes through, the body-free line the chat's resident learns, and where an approval raised
inside a desk turn continues. The route, the post map, the desk's log, lock and queue, and the
note a reply the desk does not take carries are [`specialist-desk.md`](specialist-desk.md); the
approval challenge is [`plugin-authorization.md`](plugin-authorization.md); the
output-boundary rules the reply shares with the resident's own turn are
[`output-boundary.md`](output-boundary.md) and
[`output-scope-properties.md`](output-scope-properties.md).

## Mental model

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
`<silent/>` after earlier text is silent when the turn proved an outcome and no approval is
pending (#1433: the #1075 rule 1 on the desk, a ruled exception to #1036), else it posts the
earlier messages without it (rule 2). It is judged on the runner's own list of the run's text-bearing messages —
one entry per model message, cut where an approval cut cuts the words — against the words
shown, and a reply truncated at the cap is posted truncated. Whatever that leaves, a
marker at either end never reaches the operator (#1342): a run of sentinels at the start or
the end of the words shown, with the whitespace between it and the words, is removed and the
words are posted (`output_boundary.without_sentinels`); nothing else in the words is touched.
A sentinel between two runs of words, or written inside other text — a code span, a link, a
quoting sentence, an escaped `\<silent/>` — is the specialist's content and is posted as
written, unless it is itself the reply's first or last item: Casa reads text, not intent, so a
final blockquote line `> <silent/>` posts as `>`, and a code block cut off just after a
literal sentinel loses that sentinel. That rare residual is accepted
(telling them apart needs a Markdown parser). A turn with no proven operator-visible outcome at all —
an empty answer with no proven post and no delivered media, a refused permit, a full queue,
an abort, an exception, a failed reply send — ends in ONE labelled, body-free Casa notice.

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
origin's `user_text` stays raw; nothing is persisted.

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

**INV-DESK-002**: A desk turn's reply reaches the operator only as an admitted, labelled, paginated post of the specialist's own completed and bounded text — of a run that ended with a protected call waiting on the operator's approval, only the text written before that call; of a reply whose last text-bearing message is only `<silent/>` after earlier text, nothing after a proven outcome with no approval pending, else the earlier messages without it; and never with a `<silent/>` marker at the start or end of its words — whose messages join the post map; a turn that produces no proven operator-visible outcome, or whose reply is not proven, ends in one labelled Casa notice; the chat's resident learns of the turn only through a body-free line at its next turn, and no resident model turn is spent on it.

The desk turn's origin is the DM's own context with the three fields the provenance
classifier needs, the resident's role, the specialist as the executing role, depth one, a
fresh turn id as `send_media`'s quota key and the delivered-slot echo owner, the operator's
clearance (`private`), and the reserved `desk` marker; it classifies `dm`/`delegated`, so
capabilities, approvals, memory and posts in the turn work as in a sync delegation. The scope is
minted for the desk (no inherited obligations) and carried as `origin["turn_scope"]`.

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

**An approval continuation whose desk is no longer delegable, or whose approver is not the
operator.** One labelled notice (`… could not continue (not delegable).`), the same line in
the echo, no specialist run; the pending grant expires. The same check — the specialist is
still one the resident may delegate to, not merely loaded — runs under the lock before every
desk turn, a file's included.

## Extension points

The desk the reply is written from — its bounds, its queue, and the buttons and files that
share it — extends as [`specialist-desk.md`](specialist-desk.md) describes; the reply's
admission extends with the output boundary ([`output-boundary.md`](output-boundary.md)).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._dispatch_desk_continuation`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.deliver_desk_notice`
- `casa/rootfs/opt/casa/output_boundary.py::TurnScope.for_desk`
- `casa/rootfs/opt/casa/output_boundary.py::without_sentinels`
- `casa/rootfs/opt/casa/specialist_desk.py::bounded_line`
- `casa/rootfs/opt/casa/specialist_desk.py::turn_outcomes`
- `casa/rootfs/opt/casa/specialist_desk.py::record_echo`
- `casa/rootfs/opt/casa/specialist_desk.py::drain_echo_lines`

**Tests**
- `tests/test_desk_turn.py`
- `tests/test_desk_continuation.py`
- `tests/test_desk_echo_prompt.py`
- `tests/test_pending_approval_desk.py`
- `tests/test_redcase_1283.py`
- `tests/test_redcase_1342.py`
- `tests/test_desk_closing_silence.py`

**Related**
- [`architecture/specialist-desk.md`](../architecture/specialist-desk.md)
- [`architecture/output-boundary.md`](../architecture/output-boundary.md)
- [`architecture/output-scope-properties.md`](../architecture/output-scope-properties.md)
- [`architecture/plugin-authorization.md`](../architecture/plugin-authorization.md)
- [`architecture/plugin-delivered-slots.md`](../architecture/plugin-delivered-slots.md)
- [`architecture/memory-labelling.md`](../architecture/memory-labelling.md)
<!-- END SOURCEMAP -->
