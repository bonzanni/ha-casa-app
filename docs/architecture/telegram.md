---
last_reviewed: 2026-08-01
---

# The Telegram channel

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How Telegram messages become turns and how answers come back: transport selection,
authentication, the per-topic causal log, interactive keyboards, and message rendering. It
does not cover the Bot API itself, nor what an agent does with a message once dispatched.

## Mental model

**Two transports, chosen by configuration.** Polling is the default. Webhook mode is selected
explicitly and additionally requires a public URL — without one, the system logs and falls
back to polling rather than failing.

**Webhook mode with no secret is not "unauthenticated but working" — it is dead.** The
receiving route refuses every request when no secret is configured. Registration with the Bot
API can still succeed and report itself as started, so the failure looks like Telegram not
delivering rather than like a configuration error. This is deliberate fail-closed behaviour:
the route would otherwise accept forged updates from anyone who found the URL. Polling is
unaffected, because it does not use that route.

**Two kinds of inbound message go to different places.** A direct message becomes an ordinary
turn on the bus. A message in an engagement topic is delivered to that engagement's driver
instead — it is input to running work, not a new conversation. For an in-casa engagement,
the topic handler and the system-continuation seam (`deliver_system_turn`) both admit the
turn synchronously, before any await a completion could race, as an admission ticket the
completion gate reads; recognized commands are consumed by the handler itself and are never
ticketed, and a ticket its delivery task failed to consume is discharged only after one
bounded failure notice (the engagements document owns the ticket lifecycle, and
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md) owns what the
gate does with a ticket it finds).

**Update dispatch is concurrent; ordering is re-imposed per scope, not globally.** Handlers
run non-blocking, so nothing about arrival order survives dispatch on its own. Engagement
topics re-serialize under a per-topic handler lock, and direct messages re-serialize per
chat: `/new` holds its chat's lock across the whole reset (retain, then pointer removal),
so once the reset holds the lock, no same-chat follow-up can be enqueued — let alone resume
the dying session — until it finished. Distinct chats never contend. Two boundaries to keep
in view: the lock serializes in *acquisition* order, which matches arrival order except in
the brief window between handler dispatch and the acquire, where near-simultaneous updates
can in principle swap; and a message dispatched *before* the `/new` is not ordered by the
lock at all — an already-in-flight turn races the reset and is handled by the session
registry's session-id checks instead (INV-MEM-006).

**Ordering in a topic is a property of the sequencer, not of Telegram sends.** An engagement
topic is a causal log: a single serialized writer keeps narration, discrete posts and edits
in an order that matches what actually happened. That guarantee belongs to the sequencer
seam, and **only claude-code engagement topics have one** — platform notices for other
engagements post directly to the topic, so an in-casa specialist topic sits outside the
ordering guarantee entirely. **A direct send bypasses it, and nothing mechanically prevents
one** — fallback paths exist for when no driver seam is present, and they are outside the
ordering guarantee.

**Narration seals on what landed, not on what was attempted.** Posting anything below the
open narration message seals it — nothing edits it again — so the seal follows a
*confirmed* send. A discrete send that fails outright leaves the narration open and
editable, because no message went below it. Platform and completion notices follow the same
rule: a notice whose send definitely failed posts nothing, changes no state, and leaves the
narration open. A send that merely times out is treated as ambiguous and seals
anyway: the platform may have accepted the message before the response was lost, and a later
edit landing above a message that does exist is the worse outcome. The same rule governs the
relay-mediated path, where a discrete post runs through a caller-supplied poster: it seals
when that poster confirms a message id, when the poster reports a compensated physical send,
or when it is cancelled mid-flight (ambiguous, like a timeout) — but not when it fails.

**The turn's first output threads to the message that triggered it.** The inbound envelope
records a reply target that whichever output posts first this turn consumes — narration, a
deferred reply, or an ask keyboard. It is one-shot, so later output in the same turn is not a
reply. Consumption is tied to success: a send that fails or is cancelled restores the target
so the next successful output still threads, and restoring never overwrites a newer target
set by a later envelope.

**A tap is authorised against the request it answers.** Callback data is versioned and
carries the namespace and request id; resolution is bound to the operator the request was
posted for. A tap from someone else is refused. Note that the parser still accepts a legacy
permission format — but actionability is bounded by process memory: a callback resolves only
while its request lives in the current process's broker, so buttons from before a restart
are rejected as expired however well their format parses.

**Rendering recognizes a small, deliberate grammar, and fails literal.** Agent output is
markdown-ish; the rich renderer recognizes fenced code, inline code, asterisk bold/italic,
ATX headings, labelled `[text](url)` links (http/https only — any other scheme leaves its
whole line untouched), and confident markdown tables — nothing else. Block structure
(fences, table runs) is segmented by casa's own line-based scanner, so an unclosed fence
keeps its remainder byte-for-byte; inline semantics run through a CommonMark engine one
line at a time, with the deliberate exception that underscores are never emphasis (tool
and identifier names carry them). A confident table is re-emitted from its parsed cells
in one of three forms — a padded monospace box when narrow and link-free, per-record
`Header: value` stanzas when wide or link-bearing under a real header, or plain rows with
their formatting intact otherwise — chosen so cell content and link destinations are never
silently dropped. Anything ambiguous stays literal rather than rendering wrongly.

**Both rendering paths measure length in the unit Telegram counts.** The platform's limits
are counted in UTF-16 code units — an astral character (most emoji) counts as two. The rich
paginator, the plain splitter and streaming edits, and the authorization-challenge size gate
all resolve to one shared measurement helper, so the paths cannot drift apart again. The
plain splitter prefers newline boundaries, consumes exactly the one newline it splits at
(blank-line separation survives into the next chunk), and drops a whitespace-only chunk
rather than sending a message the platform would reject.

## Contracts & invariants

**INV-TG-001**: A webhook update is accepted only when a secret is configured and the request's secret-token header matches it exactly.

Enforced in the update route before the payload is parsed or enqueued, using a constant-time
comparison over the encoded bytes of both header and secret — so a non-ASCII value is
handled without an error, and a matching non-ASCII secret is accepted.

What it does not cover: polling updates do not pass through this route at all, and the header
establishes only that the sender knows the shared secret.

**INV-TG-002**: A callback can resolve a request only from the operator that request is bound to.

Enforced twice, deliberately. When the callback arrives, before the broker claim, a missing
or mismatched user is refused with a best-effort acknowledgement. And inside the broker
claim itself, which rejects any actor that does not match the identity the request was bound
to at registration — fail-closed on both sides, so a request registered without a bound
identity is claimable by nobody, and a future delivery path cannot forget the check. There
is no other verdict writer: the internal socket carries no verdict route.

What it does not cover: topic commands are authorised separately and to different rules —
some are originator-only and at least one is open to topic participants. Do not generalise
this invariant to commands.

**INV-TG-003**: A live request is resolved exactly once.

"Once" has a short afterlife: a finished request keeps a sixty-second tombstone, and a
same-key registration inside that window *reattaches to the prior outcome* instead of
creating a fresh request — so an HTTP retry sees the original verdict rather than posting
a duplicate keyboard. Transport failures are handled separately by a reconnect supervisor
(a periodic probe with bounded timeout, then unbounded jittered-backoff rebuilds), which is
why a Telegram outage recovers without operator action. Rebuild triggers coalesce: one
arriving while a rebuild is already running is satisfied by that rebuild rather than
tearing the fresh transport down again.

Enforced by a claim-then-commit protocol in the broker: claiming marks the request, commit
validates the claim token, and finishing removes it and resolves its waiter.

What it does not cover: it does not make the keyboard edit succeed. Settlement hooks run
asynchronously, and their failures are logged rather than reversing the resolution.

**INV-TG-004**: Writer operations on a sequenced topic are serialized under one lock.

Enforced by the sequencer for narration, discrete posts and edits, notices, and inbound
high-water advances.

What it does not cover, and it is the thing to check before assuming ordering: direct sends
that bypass the sequencer, including the fallback used when no driver seam is present.

**INV-TG-005**: A rich response is paginated to Telegram's message-length and entity budgets.

Enforced in the rich renderer's pagination, which measures in UTF-16 units as the platform
does.

What it does not cover: the plain streaming and splitting path. That path now measures the
same UTF-16 units through the shared helper, but it splits plain text only — it carries no
entities and makes no entity-budget promise.

**INV-TG-006**: A one-shot operator notice is released for re-offer if and only if no part of it reached the operator.

Every delivery method reports a `DeliveryOutcome`. The distinction it exists to draw is
that a **normal return is not proof of delivery**: each method has an availability guard
that returns after zero Bot API calls when the application is absent, which a reconnect can
make true mid-turn. A caller that suppresses a one-shot notice on a normal return therefore
consumes notices that were never displayed.

Because such a notice is prepended at the *head* of the text, the outcome keys on the first
unit of output rather than on total success: the first chunk of a split message, page 1 of a
paginated response, or the final edit of a streamed one. A later page failing does not
un-show what page 1 displayed. Two consequences worth stating, because both invert the
naive reading:

- An edit refused as "not modified" is **delivered** — the message already displays exactly
  this text, notice included.
- A streamed message whose final edit never ran is **not delivered**, even though the
  operator watched prose arrive. The notice is prepended after streaming, so it was never
  in what they saw.

A raise carries the same question and cannot answer it through a return value, so the head
fact is stamped on the turn context and consulted on the exception path.

The engagement-topic stream handle joins the contract at finalize, with the head keyed
differently — and the difference is the rule's own rationale applied, not an exception to
it. No one-shot notice rides the topic stream, so the question its consumer (the `in_casa`
driver) asks is whether the turn was *wholly invisible*, not whether the final edit landed:
the first successful topic output latches `delivered`, and a failed final edit or overflow
page cannot un-show it (those failures stay warnings). `not delivered` remains a claim —
reported only when every attempted output was positively refused, or when no Bot API call
was possible before any output was attempted. An earlier attempt that failed *ambiguously*
(a lost acknowledgement) weakens the claim to `unknown` — its text may be on screen, so a
later refusal cannot establish that the turn was wholly invisible. One imprecision is deliberate: a finalize with no prior
emit sends through the shared paginating helper, whose multi-page raise cannot say whether
page 1 landed, so that raise reads as the ambiguity it is rather than as a claim that would
hand a false death to a partially visible turn.

What it does not cover: channels that have not adopted the contract. They return nothing,
which reads as `UNKNOWN` and preserves their existing behavior. That case is deliberately
distinct from a negative — treating "cannot report" as "failed" would re-offer a notice on
every turn forever.

**INV-TG-007**: A settled DM question drops its buttons along with its text.

The DM settle edit sends an *explicit empty* keyboard rather than omitting the markup.
Omitting it is not neutral: the client library drops the absent parameter, so the platform's
edit call never touches the markup and the buttons outlive the text that retired them. The
operator is then holding a live-looking control for a question that is already closed —
which is the single-attention-lane discipline broken in the one place it is most visible.

The clear is unconditional because every caller of the DM settle edit is a broker finish
hook, and a finish hook runs only at a terminal outcome: an answered question, an expiry, a
supersession, a shutdown. There is no mid-question repaint on this path for the flag to
protect, and an opt-in flag is a thing the next call site forgets. The topic path is
different — it has genuine mid-question edits — and keeps its explicit opt-in.

What it does not cover: it does not make the edit *succeed*. A settlement whose edit fails
in transport leaves the keyboard standing, and the failure is logged rather than retried
(INV-TG-003's caveat applies here too). A tap on such a keyboard is still refused by the
broker as stale, so what survives is a misleading display, never a wrong outcome.

**A plain DM message retires the questions this conversation asked, and only those.** The
order in the DM path is fixed and each step is there for a measured reason: `/new` is
intercepted first and takes its own reset, which retires both halves of the operator's
attention lane; the rate limiter returns next, so a dropped message never expires a question
it will never answer; then the typed-answer retirement runs, synchronously, before the text
is dispatched as a normal turn, so the stale keyboard is edited to expired while the same
text still reaches the agent. That retirement is bound twice over — to this chat's plain-ask
scope, and to the absence of the marker a machine-timed question carries. The scope binding
is not decoration: the broker's predicate cancel is namespace-wide, so a selection that
forgot it would retire another operator's live question. Authorization challenges live in a
separate scope and are untouched here, as they always were.

## Failure behavior

**No secret, or a wrong one, in webhook mode.** The route refuses before parsing. Nothing
reaches the channel.

**Webhook transport selected without a public URL.** Boot does not fail; the system logs and
uses polling.

**A duplicate update arrives.** A webhook redelivery is absorbed by a bounded, process-local
recent-update cache consulted on the webhook path only. Polling updates never pass that
cache, and no equivalent deduplication is established for them here. The route still
answers 200 with an empty body either way — Telegram's contract — but an `X-Casa-Update`
response header distinguishes the outcomes for programmatic callers: `accepted` (queued),
`duplicate` (absorbed), or `ignored` (channel not started, or the payload did not parse as
an update).

**A message arrives from an unconfigured chat.** Logged and dropped. Note that leaving the
chat id empty accepts other chats — the check is only as narrow as the configuration.

**A tap is stale, expired, for the wrong topic, or from the wrong user.** Absorbed with a
single best-effort acknowledgement; failures answering are themselves absorbed.

**Posting a keyboard fails.** The request is unregistered and its waiter resolves with a
delivery-failure outcome rather than hanging.

**Delivering a turn into a topic raises.** Logged, with a best-effort failure notice posted
to the topic. Cancellation is quiet by design.

**Delivering a turn into a topic RETURNS without the turn having finished.** A different
shape from the one above, and it used to be silent because of that. The failure notice
described there lives on the exception path, and an `in_casa` turn cut off mid-tool-loop
raises nothing — the response iterator simply ends without the turn's result frame — so the
operator's message was consumed and answered with nothing. The delivery task therefore also
asks, on its success path, whether the turn it just ran left that artifact, and posts one
bounded notice when it did not. It stays quiet only when the engagement's settled record is
terminal AND that terminal path confirmed a telling into this topic — a terminal status on
its own is not proof anything was said, and where it is not known that the topic was told,
the notice is posted. What it says depends on that answer, and the two are not
interchangeable: over a live record the turn really was cut off, while over a terminal one
it ended because it completed the engagement — whose summary may in fact already be on the
operator's screen, since a lost acknowledgement is indistinguishable from a failed send
from here. That notice is a single attempt and is not ordered against a concurrent
finalization's topic operations; the contract is INV-ENG-012 in
[`architecture/engagements.md`](engagements.md).

## Extension points

**Changing transport** touches the manifest schema, the environment-driven selection, the
route's authentication, and the channel rebuild.

**A new callback namespace** needs the namespace list, the parser and formatter, the dispatch
and authorization metadata, the broker registration, and the settlement hook — six places,
none of which will tell you the others were missed.

**A new topic output** should go through the sequencer seam if causal order matters. A direct
send is available and is not prevented.

**A new rich response** belongs on the paginating path. The plain splitter measures the same
units but sends unformatted text only.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel`
- `casa/rootfs/opt/casa/channels/telegram.py::_parse_callback_data`
- `casa/rootfs/opt/casa/channels/telegram.py::_split_message`
- `casa/rootfs/opt/casa/casa_core.py::_make_telegram_update_handler`
- `casa/rootfs/opt/casa/channels/output_sequencer.py::OutputSequencer`
- `casa/rootfs/opt/casa/channels/tg_richtext.py::render_paged`
- `casa/rootfs/opt/casa/verdict_broker.py::VerdictBroker`

**Tests**
- `tests/test_telegram_update_handler.py`
- `tests/test_telegram_dm_settle.py`
- `tests/test_tg_richtext_remnants.py`
- `tests/test_verdict_broker.py`
- `tests/test_telegram_new_reset.py`
- `tests/test_telegram_supervisor.py`
- `tests/test_telegram_split.py`
- `tests/test_telegram_topic_stream.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
<!-- END SOURCEMAP -->
