---
last_reviewed: 2026-10-07
---

# Delivered files

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

A file a plugin's capability tool delivers to the operator: the `operator_file` slot, its
deposit, its claim from the plugin outbox, and the delivered name a deposit may carry. The
delivered-slot model every kind shares — the deposit, the result hook, the receipt and the post
echo — is [`plugin-delivered-slots.md`](plugin-delivered-slots.md).

## Mental model

**A file is a delivered slot whose value is a path in the plugin outbox.** The plugin stages
the file, deposits its path, and returns the reference; Casa claims the file, runs the kind's
media policy on its bytes and sends it, labelled, to the operator's chat. The file is consumed
whatever the outcome.

## Contracts & invariants

**INV-PLUG-046**: A capability slot a plugin declares as `operator_file` reaches the operator only as one media send Casa makes through the kind's media policy to the chat of the call's grant identity, captioned with the label Casa derives from the specialist's display name and the plugin cannot influence, with the plugin's caption beneath as the deposit validated it; proven delivery replaces the result with a receipt, and anything short withholds the result and drops the deposit, except that a keyed deposit whose key was already delivered is not sent again — its staged file is still consumed — and gets the original delivery's receipt (INV-PLUG-050).

The deposit requires the media `kind` — one of the kinds `send_media` has a policy for
(`bad_kind` otherwise) — and judges the optional caption as one printable line whose
COMPOSED form, the label line, a newline and the caption, fits the media caption cap
(`bad_caption`; overflow is refused, never truncated — the label's length is known at
deposit from the call's identity). The path is not read at deposit. At delivery the hook
claims it from the outbox the authenticated engagement record names — a uid-dropped
engagement's private outbox, else the shared one — exactly as `send_media` does, so a path
outside the outbox, a missing file or a symlink is not proven; the kind's policy (magic
gate, extension allowlist, size cap) runs on the claimed bytes; the channel sends them
through the kind's method with the composed caption as plain text, and the claim is removed
on every outcome — the file is consumed whether or not it was delivered, the outbox's
standing destructive-claim rule, so a plugin that wants to post and keep a file posts a
copy. Claim, name check, capture and removal are one synchronous unit off the loop, because
cancelling a thread does not stop it: a claim that lands after the hook's bound has ended
(the outbox lock was held) is consumed by that unit all the same. The bound is forty-five seconds, longer than a text send's and still under the CLI's
sixty-second matcher deadline, so the receipt path, not the deadline, decides. A file with
no plugin caption is still labelled.

What it does not cover: the slot as a `consumes` target — a delivered reference is spent by
the delivery, as a link's is — and the bytes' fate once Telegram has them.

**INV-PLUG-047**: An `operator_file` deposit may carry a delivered name only when its tool's result-contract entry declares `"filename": true`, a declaration accepted only on a tool delivering an `operator_file` slot; the name is judged at deposit by the same predicate `send_media` applies to its own `filename`, before any reference is minted, and it changes only the name the file is sent under — never which staged file is claimed, read or removed; a deposit with no name delivers under the staged file's basename as before.

A plugin stages each file under a name of its own, unique so that two sends never consume
each other's file, and the operator should still receive it under a name they chose. The
deposit's optional `filename` is that name. A tool entry that wants it declares
`"filename": true`; the extractor accepts the member only as the literal `true` and only
when the entry's `delivers` names an `operator_file` slot (`result_contract_invalid`
otherwise). A Casa that predates the member refuses it through the unknown-member check on
every artifact-verification path, so a plugin relying on it is never loaded there and never
silently delivers under its storage name, the same way `delivers` was introduced. At deposit
an absent, `null` or empty name changes nothing; a name on a tool that does not declare it is
refused `filename_not_declared`; a declared name must be a string that
`_validate_delivery_filename` accepts for the kind (a basename with no `/`, no NUL and no
character below U+0020, at most 255 bytes, an extension the kind allows), else
`bad_filename`. A refused name mints no reference. At delivery the staged path is claimed,
its own name checked and its bytes captured exactly as before; only the name the channel
sends under changes. The receipt and the echo line never name the file.

What it does not cover: `send_media`'s predicate does not refuse U+007F or the C1 controls,
and this slot uses that predicate unchanged. Choosing unique staging names is the plugin's.

## Failure behavior

**A file cannot be claimed, or fails its policy.** A path outside the outbox, a missing
file, a symlink, a wrong extension or magic, a size over the kind's cap: nothing is sent,
the result is withheld, and the file — once claimed — is removed either way.

## Extension points

**Another file-like kind** follows `operator_file`: the outbox claim and the media policy are
the delivery, never a path the plugin names directly.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/result_broker.py::compose_file_caption`
- `casa/rootfs/opt/casa/result_broker.py::_post_operator_file`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.deliver_operator_file`
- `casa/rootfs/opt/casa/tools.py::outbox_for_current_context`

**Tests**
- `tests/test_operator_file_delivery.py`
- `tests/test_operator_file_filename.py`

**Related**
- [`architecture/plugin-delivered-slots.md`](../architecture/plugin-delivered-slots.md)
- [`architecture/plugin-result-contract.md`](../architecture/plugin-result-contract.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
<!-- END SOURCEMAP -->
