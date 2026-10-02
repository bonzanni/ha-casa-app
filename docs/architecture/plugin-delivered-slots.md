---
last_reviewed: 2026-10-02
---

# Plugin delivered slots

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

A slot a plugin's capability tool declares it delivers — a link the operator must open, or
a message or a file a specialist's plugin shows the operator — and how Casa posts it: the
declaration, each deposit's validation, the hook that posts and replaces the result with a
receipt, the channel's part, and the body-free echo the resident's conversation sees. The
contract and broker the delivery rides on — adoption, refusal before execution, references,
arming and redemption — are [`plugin-result-contract.md`](plugin-result-contract.md); the
chat a post lands in is the call's grant identity, derived as
[`plugin-authorization.md`](plugin-authorization.md) describes; a file's claim is the
plugin outbox's ([`plugin-runtime.md`](plugin-runtime.md)).

## Mental model

**A link the operator must open is delivered by Casa, into the chat the operator asked
in.** A capability tool may declare one of its slots as `delivers: {slot: "operator_link"}`.
The producer deposits an `https` URL — with an optional caption and label, validated
before any reference is minted — and returns the reference exactly as for any capability.
After the result's structural check passes, the same PostToolUse hook takes the deposit
once and posts ONE message to the chat of the call's grant identity: a link whose text is
the label and the destination host, the host printed by Casa from the URL and never
supplied by the plugin, with the caption beneath. That chat is the operator's chat with
the resident on a direct turn, a sync delegation from it, or a Casa-dispatched setup turn;
for a specialist engagement it is the chat the operator asked in, never the engagement's
topic — an engagement's topic is closed by finalisation and can never show what came of
the link. Proven delivery replaces the result with a receipt (`casa_delivery.status` is
`delivered`, every producer field and the reference intact); anything short of proven
delivery — the channel down, a refused send, an exception, a bound of twenty seconds —
withholds the result and drops the deposit. The producer's own result is
**delivery-neutral**: it says a link for the operator was minted and that Casa reports
whether it arrived, never that it did. That neutrality is load-bearing, because the CLI's
matcher deadline runs independently of this process: a hook abandoned past it is
cancelled and the original result reaches the model, so the receipt is the only carrier of
the positive claim and a result without one claims nothing. A delivered reference is spent
by the delivery — no tool of the plugin may name the slot in `consumes`.

**A specialist's output the operator should see is posted by Casa, labelled, verbatim.**
Two more delivered kinds, `operator_message` and `operator_file`, let a plugin running
inside a specialist — on a sync delegation, in a specialist engagement, or in a
specialist-hosted job — show the operator its output with no model retelling it. The
producer deposits the body (a message) or an outbox path with its media kind and an
optional caption (a file), and returns the reference as for any capability. After the
structural check the same hook posts it to the chat of the call's grant identity, headed by
a label Casa derives — a fixed glyph and the persona display name the `<delegates>` block
advertises for the call's execution role — which the plugin cannot set, prefix or suppress.
A message is the label line and the body, rendered in Casa's own dialect and paginated as
ONE text, so page 1 carries the header and later pages are bare like a long reply's; its
whole physical plan — every page, every plain chunk a refused page would be resent as, every
link destination — is judged before the first send, and a plan that cannot be sent complete
is refused with zero sends. A file is claimed from the plugin outbox exactly as `send_media`
claims it, passed through the kind's media policy, sent once with the label line and the
plugin's caption beneath, and consumed on every outcome. Proven delivery replaces the result
with the receipt (`pages` for a message, `kind` for a file); anything short withholds the
result and drops the deposit, exactly as for a link, and the producer's result stays
delivery-neutral. The resident's conversation learns of a proven post as one body-free Casa
line — the label, the kind, the page count or media kind — appended to the vehicle it already
receives: a sync delegation's returned text, or a finished delegation's, engagement's or
job's terminal notice. The line never quotes the body, the caption or the file name; the
operator has the post, the resident has the fact of it.

**Every physical message of a post is filed as it lands.** The hook hands the channel a
record of the poster (the call's enforcement role and operator, the slot, the tool-use id,
the echo owner) and the channel files each page, each fallback chunk, the media message and
the link message under it in the post map the moment its send returns — so the operator's
swipe-reply on any of them reaches that specialist's desk
([`specialist-desk.md`](specialist-desk.md)), a page that landed before a later page failed
included. The map is memory-only and count-bounded; a reply on a forgotten message is a
plain message to the resident.

**The channel's part for a link.**
When a plugin tool's result carries a slot declared `operator_link`, the result broker
composes a single labelled-link message — the label and the destination host as the link
text, one `text_link` entity, the caption beneath — and asks the channel to post it to the
chat of the call's grant identity. The channel sends it through the same one-retry
primitive a rendered response uses (an entity the platform refuses is resent as the plain
text, which spells the URL out), reports it delivered on any normal return, and lets an
exception propagate: the broker reads anything but a normal return as not proven
(INV-PLUG-025). Nothing here goes through the markdown renderer or pagination; the
message is bounded by the broker's own validation of the three parts.

**The channel's part for a message and a file.** For a slot declared
`operator_message` the broker composes the specialist's label line and the plugin's body as
ONE text, and the channel judges the whole physical plan before the first send — every page
the paginator gives, each page's plain fallback chunks, and every `text_link` destination
read from the rendered entities and looked for whole in those chunks, since the fallback
helper filters an oversized destination silently and its splitter can cut a fitting one
across two messages — refusing with zero sends when the plan has more than six pages or any
element would not reach the operator complete. It then sends every page the way the multi-page
response loop does (the single-page authored-text retry is never used) and reports
delivered only when every page and every fallback chunk returned normally; a refused plan
is not delivered, and an exception on any page or chunk propagates (INV-PLUG-045). For
`operator_file` the channel sends bytes the broker has already claimed from the plugin
outbox and passed through the kind's media policy, through the kind's method with the
composed caption as plain text — no rendering, no entities, no thread id (INV-PLUG-046).
Both are Casa-composed notices under the output boundary, like the delivered link.

## Contracts & invariants

**INV-PLUG-025**: A capability slot a plugin declares as `operator_link` reaches the operator only as one message Casa posts to the chat of the call's grant identity, after the result's structural check, with the destination host printed by Casa from the URL; the only model-visible statement that it was delivered is the hook's replacement receipt (`casa_delivery.status` equal to `delivered`); when delivery is not proven the result is withheld and the deposit dropped; and the slot's value never appears in the model-visible result, the transcript or the structured-output sidecar.

The delivery is what the deposit's validation exists for: the value must be an `https`
URL with a hostname, at most 2048 bytes, printable and free of whitespace; the caption at
most 200 printable characters on one line; the label at most 40, and neither may contain a
scheme separator, `www.`, or (for the label) anything that reads as a domain — the host
is Casa's to print. A deposit failing any of these mints no reference, so the result fails
the structural check and is withheld as any bad capability result is. The message never
goes through the markdown renderer: label and caption are sent as the bytes they are,
beside one `text_link` entity whose length is measured in UTF-16 code units. Taking the
deposit is atomic and once — a taken reference is used, so `arm`, `redeem` and a second
take all refuse it — and the broker's three matchers carry an explicit sixty-second timeout.

What it does not cover: Casa's process logs. The boundary is the model — keeping the link
out of the model's context is what stops it being echoed into whatever chat or topic the
model is in — not the log. A sign-in or approval link is not a bearer credential: opening
it still requires the account holder's own login at the provider, it expires, and it
authorises once. So the client library's own loggers (request parameters at debug level,
an error body that quotes the request) are not policed; Casa's own two warnings on the
send path name an error's type rather than its text, which is housekeeping, not a
guarantee. Nor does it cover the hook's own cancellation by the CLI's deadline. Cancellation
can land inside the channel's send or after the hook returned, so the message may or may
not have left; the hook drops the deposit and re-raises with no replacement, the model
holds the delivery-neutral original and reports the link as unconfirmed, and the operator
may hold a valid link the notice's wording already covers — an unconfirmed delivery, never
a false positive. Nor does it cover a producer that copies the URL into a field it did
not declare: the plugin-author trust boundary [`plugin-result-contract.md`](plugin-result-contract.md)
describes.

**INV-PLUG-026**: `delivers` is accepted only on a `capability` tool, names only that tool's own provided slots, at most one, with the closed kind `operator_link`, and a delivered slot is consumed by no tool of the plugin; a setup tool declared as a capability delivers every slot it provides and consumes nothing, and any other capability declaration on a setup tool is malformed.

An older Casa refuses a manifest carrying `delivers` as malformed through the extractor's
unknown-member check, on every artifact-verification path — the plugin is excluded from
resolution there, never half-loaded — so a plugin release adopting it requires the Casa
release that ships it, and says so in its own changelog. The map keeps a setup tool's
capability entry; only the safe or absent declaration is skipped as exempt.

**INV-PLUG-028**: A result from a call that delivers a slot, with no deposit bound to it and with every slot the call provides present as JSON `null`, passes to the model unchanged, with no receipt and no message; every other result of such a call that fails the structural check is withheld and its deposits dropped, exactly as for any capability result.

A tool that delivers a link does not always make one. A sign-in tool finds the account
already connected, or a link still outstanding, and must say so. Without this rule
that answer could reach the model only as a tool error, and a Casa-dispatched setup run
whose only result is an error counts as not run, so it is retried and then failed. The
producer states "no link" explicitly: every provided slot present and `null`, and nothing
deposited. A missing member, an empty string or any other falsy value is not that
statement, and neither is `null` after a deposit, so a producer that crashed half-way
or forgot its reference is still withheld. The passed result is an ordinary non-error
result, so the setup run counts. It adds no exposure a `safe` declaration does not
already allow: Casa has never scanned a producer's prose.

**INV-PLUG-045**: A capability slot a plugin declares as `operator_message` reaches the operator only as pages Casa posts to the chat of the call's grant identity, headed by a label Casa derives from the specialist's display name and the plugin cannot influence, rendered from the deposited text with no model between; proven delivery of every page replaces the result with a receipt, and anything short withholds the result and drops the deposit.

The deposit is judged in characters: a non-blank body of at most 12,000, with no control
character (Unicode category Cc) but newline and tab — spaces of every width, joiners and
marks are text — within the broker's byte cap — a body over the cap is refused
(`bad_message`), never truncated, because truncation would be Casa retelling. Caption,
label and kind are ignored for a message slot: the label is Casa's. The character cap does
not bound the page count (a body of emoji is two UTF-16 units per character; a body of
markers is bounded by the entity budget), so the plan is capped separately at delivery,
before the first send: the channel renders the composed text through the same paginator a
long reply uses and refuses — zero sends, the result withheld — when it has more than six
pages, or when any element of it would not reach the operator complete: a page over the
platform's budget, a plain fallback chunk over it, a `text_link` destination longer than
one message, or a destination the fallback chunks would carry in pieces. The destination
checks read the rendered entities directly, because the shared fallback helper filters an
oversized destination silently and returns no sign that it did, and its splitter cuts the
joined overflow form at a newline or hard — so two destinations that each fit one message
can leave the second split across two; every destination must occur whole in some chunk.
Every page then goes the way the multi-page response loop sends — the rendered page, then
its plain chunks if the platform refused the entities — and the single-page retry with the
authored text is never used, since it is not bounded by the page budget. The receipt counts
the pages of the plan that was judged, so nothing the plan would have had to drop, shorten
or skip can sit under a positive receipt.

What it does not cover: a multi-page post whose later page fails is withheld though its
first page may have left — the same honest asymmetry the link accepts, and the receipt
remains the only positive claim. Nothing is retried, persisted or edited; a post is one
shot. The hook's cancellation by the CLI's deadline, and Casa's own logs, are outside it as
they are for a link.

**INV-PLUG-046**: A capability slot a plugin declares as `operator_file` reaches the operator only as one media send Casa makes through the kind's media policy to the chat of the call's grant identity, captioned with the label Casa derives from the specialist's display name and the plugin cannot influence, with the plugin's caption beneath as the deposit validated it; proven delivery replaces the result with a receipt, and anything short withholds the result and drops the deposit.

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

**What the resident's conversation sees of a post.** On proven delivery of a message or
file the hook appends one event — the call's tool-use id, plugin, slot, label, and page
count or media kind — to an in-memory ledger keyed by the call's owner: the engagement id
the grant identity carries, or for a sync delegation the delegation id the child origin
carries, threaded onto the identity as advisory metadata that takes no part in identity
equality or binding. The ledger is a list, never a set — two tools of one plugin may each
deliver the same slot name, and two proven posts are two lines — and the vehicle that echoes
it drains the owner's list atomically, so a post is echoed once: a sync delegation appends
the lines to its returned text after the delegate's answer; a finished delegation's,
engagement's or job's terminal notice carries them after its text. The echo follows the
post, not the work's outcome: a delegation that fails or is aborted after a proven post
carries the lines on its error result or error notice, after the message. Each line is at most 120
characters, at most five lines then a count, and carries only Casa-derived metadata. The
ledger dies with the process, like the broker's references: a restart between the post and
its echo loses the echo, not the post.

## Failure behavior

**A delivered link's deposit is malformed.** No reference is minted (`bad_link`,
`bad_caption` or `bad_label`, none echoing the value); the producer's result cannot carry
one and is withheld. For a slot the tool does not deliver, caption and label are ignored
rather than refused, so a producer library can send them uniformly.

**A delivered message's or file's deposit is malformed.** `bad_message` (blank, a
control character, over the character cap), `bad_kind` (a file without a media kind Casa
has a policy for) or `bad_caption` (not one printable line, or a composed caption over the
media cap): no reference is minted and the result is withheld as any bad capability result
is. A message slot ignores caption, label and kind rather than refusing them.

**A message plan cannot be sent complete.** More pages than the cap, a page or a fallback
chunk over the platform's budget, or a link destination no chunk carries whole (longer than
one message, or split by the plain fallback): the channel refuses with zero sends, the
result is withheld and the deposit dropped.

**A file cannot be claimed, or fails its policy.** A path outside the outbox, a missing
file, a symlink, a wrong extension or magic, a size over the kind's cap: nothing is sent,
the result is withheld, and the file — once claimed — is removed either way.

**Delivery is not proven.** The channel is not started, the send returns without proof,
raises, or exceeds the bound: the result is replaced by a notice telling the model the
operator may or may not hold a link — if one arrived just now it is valid, otherwise ask
for a fresh one — and the deposit is dropped. Telegram accepting the message and the
client raising afterwards reads the same way; that is the one double-telling the design
accepts, and why the notice promises nothing about what did not arrive. A provider-side
pending authorisation is orphaned and the next request mints a fresh one.

**A delivered slot is presented to a consuming parameter.** The manifest forbids naming it;
a reference that reaches arming anyway is used and refused.

**Casa restarts between deposit and hook, or the CLI abandons the hook.** The call is gone
and the result withheld; on abandonment the neutral original passes through and claims
nothing (INV-PLUG-025's not-covered clause).

## Extension points

**A new delivered-slot kind** joins the closed vocabulary in the extractor, the deposit's
validation, the hook's dispatch and the channel's notice method together, as
`operator_message` and `operator_file` did beside `operator_link`; a kind Casa cannot
compose a post for is a kind the extractor must refuse.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/result_broker.py::_deliver_and_replace`
- `casa/rootfs/opt/casa/result_broker.py::compose_operator_link`
- `casa/rootfs/opt/casa/result_broker.py::compose_operator_message`
- `casa/rootfs/opt/casa/result_broker.py::compose_file_caption`
- `casa/rootfs/opt/casa/result_broker.py::post_label`
- `casa/rootfs/opt/casa/result_broker.py::_post_operator_message`
- `casa/rootfs/opt/casa/result_broker.py::_post_operator_file`
- `casa/rootfs/opt/casa/result_broker.py::PostLedger`
- `casa/rootfs/opt/casa/result_broker.py::echo_lines`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.deliver_operator_link`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.deliver_operator_message`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.deliver_operator_file`
- `casa/rootfs/opt/casa/tools.py::_with_post_echo`
- `casa/rootfs/opt/casa/tools.py::outbox_for_current_context`

**Tests**
- `tests/test_operator_link_delivery.py`
- `tests/test_operator_link_no_link_outcome.py`
- `tests/test_operator_message_delivery.py`
- `tests/test_operator_file_delivery.py`
- `tests/test_operator_post_echo.py`
- `tests/test_result_contract.py`

**Related**
- [`architecture/plugin-result-contract.md`](../architecture/plugin-result-contract.md)
- [`architecture/plugin-authorization.md`](../architecture/plugin-authorization.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/output-boundary.md`](../architecture/output-boundary.md)
- [`architecture/plugin-runtime.md`](../architecture/plugin-runtime.md)
- [`architecture/delegation-announcements.md`](../architecture/delegation-announcements.md)
<!-- END SOURCEMAP -->
