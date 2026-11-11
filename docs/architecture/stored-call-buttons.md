---
last_reviewed: 2026-11-11
---

# Stored-call buttons

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a specialist's plugin proposes a decision to the operator as buttons: the
`operator_proposal` delivered kind and its deposit rule, which calls a button may store, the
`📎`, Close and link buttons, and the broker registration and the keyboard. What a tap does —
its admission chain, the re-checks on the agent's lane, the pinned one-call turn that executes
the stored call, the receipt and the note the chat's agent gets — is
[`stored-call-tap.md`](stored-call-tap.md); the bounded termination of that turn's processes
and the faulted agent are [`stored-call-termination.md`](stored-call-termination.md). The
delivered-slot path a proposal rides on is
[`plugin-delivered-slots.md`](plugin-delivered-slots.md); the result contract and the hooks
are [`plugin-result-contract.md`](plugin-result-contract.md); the callback transport is
[`telegram.md`](telegram.md). Telegram only.

## Mental model

**The plugin proposes, the operator decides, Casa executes; the model is only the hands.**
A capability tool of a specialist's plugin declares a slot delivering `operator_proposal`
and deposits a JSON object: a text (one page, with Casa's label line) and one to six
buttons, each a label and a *stored call* — a bare tool name of the same plugin, resolved
through the depositing call's own server, with fixed JSON arguments (optionally with plain
`pages` before it — [`stored-call-next-card.md`](stored-call-next-card.md)). Casa posts the text
labelled with the buttons as an inline keyboard, filed like any post. When
the operator taps, no ordinary model turn runs and no model is asked to decide anything: Casa
runs one short *pinned* specialist turn whose prompt names the one call, to be made whatever
the tap looks like, and whose hooks allow exactly that call — the stored tool with the stored arguments, byte for byte in canonical JSON —
and deny every other call and any second call. The plugin's own response is the receipt,
posted labelled — its `receipt` sentence when the response's text is a JSON object carrying
a non-blank string `receipt`, otherwise the text verbatim; a plugin refusal (its revision
guard) is a receipt too. A `safe` call's response that also carries `"on_card": true` (and no
`next`) asks for its receipt as the tapped card's last line (`☑ <receipt>`) instead of a
message; when that edit does not land, the receipt is posted as a message. Nothing the model
writes reaches the operator or the chat's agent; the pinned turn stores nothing of its own in
memory (a receipt or card a specialist that keeps no memory posts leaves only Casa's
body-free line that it posted, INV-MEM-040, [`memory-capture.md`](memory-capture.md)).

**Which calls a button may store.** At deposit, every button's tool must be a declared tool
of the same plugin on the same (single, stdio) server — a `safe` tool that consumes no
reference, or a `capability` tool whose only provided slot is its own `operator_proposal`
(the `More` exception: a button whose call renders the next page, with its own buttons), or
one whose only provided slot delivers an `operator_file` (#1303: the landed file is the
receipt; the file rides the delivered-slot path unchanged; such a button may leave its card
live, #1362, [`stored-call-next-card.md`](stored-call-next-card.md)). A
setup tool, a protected tool, a tool declared on an ambiguous or HTTP server, and any
tool that consumes a reference are refused; so is a runtime name in place of a bare one.
Arguments are a JSON object with identifier keys (no `_` prefix), values that are strings
up to 2,000 characters without `<` or `>`, booleans, null, integers within ±(2⁵³−1), lists
or objects of the same, at most 4,096 bytes, round-tripping through canonical JSON. The
grammar is narrow because the CLI decodes a call's arguments in JavaScript and its MCP
wrapper rewrites some values; a value a transformation could touch is refused at deposit
rather than repaired after posting, and the pin never normalises.

**A `📎` button stores no call.** A button may instead be `{"label", "arm_file": true}`, never
both keys, at most one per proposal, counting toward the six. Its tap runs the same admission
chain and commit, then writes an *arming* for that chat, synchronously and before any await: the
next non-text message the same operator sends there within ten minutes goes into the task
whose post the card is, while that task is live, as if it were a reply to its post; for a card
of no live task it is the chat's own agent's, as any file is
([`inbound-files.md`](inbound-files.md)). A later arming replaces it, a text message leaves
it, the first file consumes it — a reply file too, which goes where the reply points — and a
restart forgets it. The finish hook only edits the keyboard to `☑ <label>`: no turn runs, nothing is
dispatched, and by the operator's ruling no message about the arming is ever sent. A Close
button (`{"label", "close": true}`, #1375) stores no call either; its tap only removes the
card's buttons — [`stored-call-next-card.md`](stored-call-next-card.md). A link button
(`{"label", "url"}`) stores no call and sends no tap: Telegram opens its address. It
is accepted only under the `operator_link` rule (`https`, a host, printable, no whitespace),
only from a plugin that declares an `operator_link` slot — so it posts nothing that plugin
could not already post as a link — and at most one per card; a callback naming its index is
void.

**Register, then post; bound; supersede.** The composed post — label line and text — must
render to one page with room left for the longest line Casa appends when the keyboard
settles (`☑ <label>` or `⏳ <label>`, a label of 32 characters that may each take two units;
`✖ <reason>`, `⌛ expired` and `↻ replaced` are shorter), so a maximal proposal can always be
settled. Before the message is sent the proposal is
registered with the verdict broker under the `proposal` namespace with a one-hour deadline
and a live record (chat, operator, role, artifact, plugin segment, the stored calls, the
text) — so a tap can never find a keyboard the broker does not know. At most 32 live
proposals per bot and chat; the 33rd deposit is withheld. A non-empty `revision` supersedes the
earlier live proposals of the same chat, plugin and role carrying the same revision (their
keyboards are edited to `↻ replaced`, and a tap on one answers "replaced"). A post not proven is withheld and
unregistered, unless its send broke off (a timeout, a lost link): its card may be on screen and stays. A Casa restart empties the broker: a tap on an older keyboard answers
"expired".

**A tap's result may bring the next card (#1302), or replace the tapped card in place
(#1339)** — [`stored-call-next-card.md`](stored-call-next-card.md).

## Contracts & invariants

**INV-PROP-003**: A keyboard with stored calls exists only for a deposit from a tool declaring the `operator_proposal` slot, or for the `next` card a `safe` stored call returned beside its receipt (INV-PROP-006) judged as such a deposit of the tapped call's own plugin, whose calls name the same plugin's declared tools with fixed reference-free arguments within the bounds, and whose message was proven delivered or whose send broke off; a proposal Telegram refused holds no stored call, a chat holds at most 32 live proposals, and a proposal expires after one hour.

**INV-PROP-007**: A button may store a `capability` tool only when its one provided slot delivers `operator_proposal` or `operator_file`; a stored file is sent only through the delivered-slot path and, landed, is the tap's receipt; a stored call that ran with arguments an installed hook changed is told to the operator whatever the outcome.

**INV-FILE-007**: A `📎` tap arms the next file in that chat, for the operator who tapped — into the live task whose post the card is, or for a card of no live task the chat's own agent's path — for at most ten minutes; the arming exists from the moment the tap is committed, before the keyboard is edited or anything is sent; exactly one file consumes it; a later arming replaces it; a restart forgets it; and Casa sends no message about an arming — the edited keyboard is the whole acknowledgement, and the only text a tap can produce is the past-fact line that the buttons could not be cleared.

## Failure behavior

**A proposal superseded while its own send is in flight.** The finish hook finds no message
id yet and leaves the terminal line for the poster, which applies `↻ replaced` the moment the
message lands; no live record remains for it, and a tap on it answers "replaced".

**Casa restarts.** The broker and the settled-proposal memory are gone; a tap on an older
keyboard answers "expired", and a `📎` arming is forgotten.

## Extension points

**A new kind of button** is judged at deposit, in `result_broker.proposal_ok`, beside the
stored-call, `📎`, Close and link buttons; what its tap does is
[`stored-call-tap.md`](stored-call-tap.md)'s.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/stored_calls.py`
- `casa/rootfs/opt/casa/result_broker.py::proposal_ok`
- `casa/rootfs/opt/casa/result_broker.py::_post_proposal`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.deliver_operator_proposal`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.mark_proposal`

**Tests**
- `tests/test_stored_calls.py`
- `tests/test_proposal_slot.py`
- `tests/test_file_handoff_arm.py`
- `tests/test_tap_delivers_file.py`

**Related**
- [`architecture/stored-call-tap.md`](../architecture/stored-call-tap.md)
- [`architecture/stored-call-termination.md`](../architecture/stored-call-termination.md)
- [`architecture/stored-call-next-card.md`](../architecture/stored-call-next-card.md)
- [`architecture/inbound-files.md`](../architecture/inbound-files.md)
- [`architecture/plugin-delivered-slots.md`](../architecture/plugin-delivered-slots.md)
- [`architecture/plugin-result-contract.md`](../architecture/plugin-result-contract.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
<!-- END SOURCEMAP -->
