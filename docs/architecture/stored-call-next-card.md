---
last_reviewed: 2026-10-08
---

# Stored-call buttons: the next card and the card replaced in place

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a stored-call tap may show besides its receipt: the next card a `safe` stored call
returns beside its receipt (#1302), and the same card shown in place of the tapped one
(#1339). The proposal, the tap's admission chain and re-checks, the pinned turn, the capture
and the receipt are [`stored-call-buttons.md`](stored-call-buttons.md); the delivered-slot
path a proposal rides on is [`plugin-delivered-slots.md`](plugin-delivered-slots.md).
Telegram only.

## Mental model

**A tap's result may bring the next card (#1302).** A `safe` stored call whose response is a
JSON object with a non-blank string `receipt` may also carry `next`: an object of exactly the
`operator_proposal` deposit shape (`text`, one to six `buttons`, optional `revision`). The
capture keeps it, encoded, beside the receipt; nothing is judged in the hook. Once the receipt
has landed, in the same desk use and under the same lock, `_post_next_card` judges it as a
deposit — `proposal_ok` against the stored tool's own contract entry (its plugin and its one
server) and the SAME captured maps the tap was re-checked against — and posts it through
`_post_proposal`: the live bound, the revision supersede, registered before the send. Its
record is the tapped proposal's own (chat, operator, role, artifact, plugin), so its buttons
are keyed like any card's and commit only from a tap, through the whole admission chain and
the re-checks. A walk is therefore one tap per card: the keyboard settles, the receipt lands,
then the next card. `next` absent or `null`, or not an object, is today's tap; `next` beside
no usable `receipt` is ignored and the text is the receipt verbatim; the `More` exception's
no-post shape never carries one. A receipt whose send is not proven, or a desk faulted by the
run, gets no card: the card follows a receipt the operator has, and a tap on it could only be
refused on a faulted desk. A plugin re-posting a card with the same `revision` replaces its
live predecessor (`↻ replaced`).

**A tap whose answer replaces its own card edits that card in place (#1339).** A tap that
only changes the card's own view — a page turn, a switch — would otherwise show three things:
the settled card, the receipt and the next card. Beside a usable `receipt` and a `next`
object, the response may carry `"in_place": true` (JSON `true` only; anything else, or no
`next` object, is ignored). The capture keeps the flag beside the encoded `next`. In the
same desk use, under the same lock, before any receipt is sent, `_post_next_card` judges the
card exactly as above and `_post_proposal` registers it as above, but with the tapped
message as its target: the synchronous block binds the new record to the tapped message id
and files that message in the post map under the new card's post record — before the edit,
whatever its outcome, so a supersede or the deadline marks the right message and a
swipe-reply on it reaches the specialist. The channel then edits the tapped message to the
new text and keyboard (`replace_operator_proposal`, rendered like a posted card). A landed
edit is the whole visible answer: no receipt is sent; the desk exchange keeps the receipt's
first line and the resident's echo says the tap was applied, as for any tap. A call an
installed hook rewrote carries its tell inside the edited card, or as one notice right after
it when the card would no longer fit one page. The tapped card shows `☑ <label>` from the
commit until the edit lands. Taps that act do not set the flag and are unchanged.

## Contracts & invariants

**INV-PROP-006**: A `safe` stored call's `next` card that is not shown in place (INV-PROP-008) is posted only beside its receipt, after that receipt's send is proven, within the tap's own desk use, and only if it passes the proposal deposit predicate against the stored tool's own plugin and server on the maps the tap was re-checked against; it is registered before it is sent and carries the tapped proposal's own chat, operator, role and artifact, so its buttons execute only from a tap; a card that does not land is one notice and the receipt stands; a response without `next` is today's tap.

**INV-PROP-008**: A `next` card returned with `"in_place": true` replaces the tapped card only by an edit of the tapped message, within the tap's own desk use, after it passed the same deposit predicate and the same registration as a next card, with its record bound to the tapped message id and that message filed under the card's post record before the edit is sent; a landed edit sends no receipt; any other outcome is today's sequence — the receipt, then the card as a new message.

What it does not cover: an edit whose landing is unconfirmed. Its record stays live until the
deadline, since the edited card may be on screen, and the fallback still posts the receipt and
the card, so the operator may see two working cards; a card carrying the same `revision`
replaces the edited one (`↻ replaced`).

## Failure behavior

**A next card does not land** — the deposit predicate refuses it, the chat already holds 32
live proposals, or its send is not proven. The receipt stands; one labelled notice
`📊 Finance could not show the next card (invalid | too many open | not delivered).`; no retry.

**A card cannot replace the tapped one** — the deposit predicate refuses it, the chat already
holds 32 live proposals, the tapped message's id is unknown, the desk was faulted by the run,
or Telegram refuses the edit (its record is then unregistered). Today's sequence follows,
visibly: the receipt, then the card as a new message, with the notice above if that fails too.
An edit whose landing is unconfirmed keeps its record and the same sequence follows.

**The new card is superseded or expires while its edit is in flight.** The finish hook marks
the message at once and records the terminal line; when the edit then lands, the poster
applies that line again, so the keyboard of a record that is gone is never left on screen.

## Extension points

**A tap that acts keeps its receipt.** `in_place` is for a tap whose answer is only a new view
of the same card; a tap that confirms, files or sends leaves it out, and its receipt stands.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/specialist_desk.py::_post_next_card`
- `casa/rootfs/opt/casa/result_broker.py::_receipt_of`
- `casa/rootfs/opt/casa/result_broker.py::_edit_operator_proposal`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.replace_operator_proposal`

**Tests**
- `tests/test_tap_next_card.py`
- `tests/test_tap_in_place_card.py`

**Related**
- [`architecture/stored-call-buttons.md`](../architecture/stored-call-buttons.md)
- [`architecture/plugin-delivered-slots.md`](../architecture/plugin-delivered-slots.md)
- [`architecture/specialist-desk.md`](../architecture/specialist-desk.md)
<!-- END SOURCEMAP -->
