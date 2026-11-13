---
last_reviewed: 2026-11-10
---

# The Telegram channel

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How Telegram messages become turns and how answers come back: transport selection,
authentication, and interactive keyboards. How an answer is
rendered and paginated for the platform — the markdown grammar, the message budgets,
link-destination fallbacks and the UTF-16 measurement — is
[`architecture/telegram-rendering.md`](telegram-rendering.md); a reply shown as a draft while
the model writes it, and Stop on it, is [`telegram-reply-drafts.md`](telegram-reply-drafts.md).
It does not cover the Bot API
itself, nor what an agent does with a message once dispatched.

## Mental model

**One channel per bot, one transport in front.** Each Telegram bot is one endpoint (a
`TelegramChannel`) with its own state; the `"telegram"` channel is the transport that holds
them, and every stored message carries its bot. Which bot a turn or a send goes through, and
why a chat id and a message id are never used without the bot, is
[`telegram-bots.md`](telegram-bots.md) (INV-TG-017 to INV-TG-021).

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
turn on the bus. A message a person writes in the optional agent-log group reaches nothing: it
draws one pointer to the direct chat per person per boot (INV-TG-016, in
[`agent-logs.md`](agent-logs.md)). Both are *text*: the text
handler is one of two message handlers, and the other takes every new non-text message — a
file, a photo, a sticker, a pin. In the operator's chat it stores an accepted file and
acknowledges it, files sent together in one edited message (INV-FILE-005) — or, when the file carries a caption, runs one ordinary turn with the caption
as the operator's words, admitted through the text handler's own dispatch tail
(`_dispatch_dm_turn`, INV-FILE-004) — and answers everything else with a one-line refusal;
outside that chat it follows the text path, and in the log group it draws the same one
pointer ([`inbound-files.md`](inbound-files.md)). The two filters
are disjoint, so each update reaches exactly one handler. Both take new messages only: an
edited message, a channel post and a business message reach neither, so editing a message
already sent never starts a second turn. For a job,
the reply seam (`deliver_operator_reply`) and the system-continuation seam
(`deliver_system_turn`) each hand the message over in one synchronous step — journaled,
admitted as a ticket the completion gate reads, queued for the job's delivery worker — and
return without waiting on the session, and a message the job does not accept, or never reads, draws one
bounded notice ([`architecture/engagement-inbound-disclosure.md`](engagement-inbound-disclosure.md)
owns that notice, and
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md) what the gate
does with a ticket it finds).

A continuation dispatched from an operator's tap is admitted the same way, but its ticket
cannot be the first thing that exists for it: the tap commits before the seam runs. That
interval is covered by an ingress reservation taken at the tap and handed over to the ticket
in the seam's own synchronous entry. The seam returns whether the job accepted the message —
false when the record was terminal, the journal could not be written or shutdown had begun —
which is what the tap's operator-facing message is selected from. It is an acceptance and not
a delivery receipt; the delivery worker asks the registry again immediately before it writes
the message ([`architecture/engagement-turn-admission.md`](engagement-turn-admission.md)).

**Update dispatch is concurrent; ordering is re-imposed per scope, not globally.** Handlers
run non-blocking, so nothing about arrival order survives dispatch on its own. Direct
messages re-serialize per chat: `/new` holds its chat's lock across the whole reset (the pool close, then the pointer
removal, INV-MEM-013),
so once the reset holds the lock, no same-chat follow-up can be enqueued — let alone resume
the dying session — until it finished. Distinct chats never contend. Two boundaries to keep
in view: the lock serializes in *acquisition* order, which matches arrival order except in
the brief window between handler dispatch and the acquire, where near-simultaneous updates
can in principle swap; and a message dispatched *before* the `/new` is not ordered by the
lock at all — an already-in-flight turn races the reset and is handled by the session
registry's session-id checks instead: the reset removes only the session it snapshotted.

**The agent log has one writer of its own.** A background agent's words and steps go to
its topic in the optional log group through a synchronous sink and one writer task with its
own request budget; nothing on the direct-chat path waits on it
([`agent-logs.md`](agent-logs.md), INV-TG-011).

**Text leaves for the Bot API through one request seam, and a character the platform cannot
carry is replaced there.** The client library sends every request as UTF-8 form data, so a
string parameter still carrying a lone surrogate — a JSON `\ud800` escape in a model or tool
result decodes to one — would raise before any request left the process: the whole reply,
and the loss classed as unknown so the health notice is not re-offered. The channel therefore installs its own request class on the
ordinary Bot API pool, below every sender — plain and rich, the draft send, narration, media
captions — and it replaces each surrogate code point with one U+FFFD, one code point for
one, so entity offsets computed upstream stay valid. Nothing is normalised at the send
sites, so a new sender cannot forget it. A value nested inside a JSON-encoded parameter (an
entity address, a button label) is already an ASCII escape by the time it is serialised and
is left for the platform to judge; a bot injected for a test is outside the seam.

**Model text enters the six model-text methods only as admitted text.** `send`,
`send_response`, the draft send (`_send_draft`), `post_dm_keyboard`, `send_media`'s caption
and `guest_finish` accept an `Admitted` — the value the authoring turn's scope mints, or
Casa's own `casa_text` — and refuse a bare string with an `ERROR` log (the draft send takes
the empty string, the draft before the first word, as it is).
The first five then make zero Bot API calls and return the method's own failure value:
`NOT_DELIVERED`, `None`, or `UnadmittedText` from `send_media`; `guest_finish` still owes the
group its one edit and makes it Casa's failure line ([`guest-summons.md`](guest-summons.md)). `edit_dm_message` stays string-typed, because it re-renders a body admitted
when it was posted or replaces it with a Casa status; the arrival replies and the
delivered-link message are outside the contract, and so is the agent log, which is no channel
method and posts plain text. The contract is INV-OUT-001 in
[`output-boundary.md`](output-boundary.md), which also records every call site.

**Casa's bot also hears of the bots it manages; an agent's bot passes its lifecycle first.**
Each endpoint states the update types it wants on every `setWebhook` and `getUpdates`
(`allowed_updates`): `message` and `callback_query`, plus `managed_bot` on Casa's bot alone
and `guest_message` on an agent's own bots alone, and on both `stopped_message_generation`, a
press of Stop on a reply's draft ([`telegram-reply-drafts.md`](telegram-reply-drafts.md)).
On Casa's bot a `managed_bot_created` service message is caught by its own handler, registered
ahead of the non-text one (which would otherwise answer it as an unreadable message): from the
operator in his chat it is handed to the own-chat lifecycle and is never a turn; anything else
is logged and ignored. The `managed_bot` update is handed over the same way. Both run the
lifecycle in a task the channel owns (`_lifecycle_task`), not in the handler's own: the
lifecycle may ask for a restart, which the Supervisor answers only once Casa has stopped, and
`Application.stop()` waits for its handler tasks; the channel's `stop()` cancels these first and
waits for them only briefly. An agent's own bot
queues every update through `_IntakeQueue`, which asks `own_voice.intake` before the update is
queued — a guest summons aside, which goes straight to its own handler
([`guest-summons.md`](guest-summons.md)): an update held or answered there reaches no handler; the lifecycle later replays held
ones through `release_update`. On such a bot `/start` is an ordinary turn; the operator's first
turn there, whatever it says, carries the reserved `_chat_opening` key that has the agent
say hello (`"moved"` in an agent's new chat after a move to a new bot), and marks the chat open once its queue takes it (`TelegramChannel._send_turn`,
a retry after a hand-back included). Once the bot is bound, the agent also greets the operator
by itself (`TelegramChannel.speak_first`, a turn Casa starts with no operator identity), which
opens the chat only once Telegram took its reply, and a bare `/start` sent while that greeting is
on its way is held for it (replayed if the greeting does not reach him); `/new` resets that agent's own
conversation, and Casa's lines name the agent plainly, without the `📊` label. The bot's
lifecycle is [`own-voice-lifecycle.md`](own-voice-lifecycle.md), the rest
[`own-voice.md`](own-voice.md).

**A tap is authorised against the request it answers.** Callback data is versioned and
carries the namespace and request id; resolution is bound to the operator the request was
posted for. A tap from someone else is refused. Note that the parser still accepts a legacy
permission format — but actionability is bounded by process memory: a callback of a broker
namespace resolves only while its request lives in the current process's broker, so buttons
from before a restart are rejected as expired however well their format parses. Ahead of
every namespace's handler, a tap on Casa's bot whose card records an agent that now declares
its own chat is answered with one line naming that chat (`_refuse_moved_card`, INV-OWN-009). The
namespaces are `_CALLBACK_NAMESPACES`; `proposal` is
[`stored-call-buttons.md`](stored-call-buttons.md). `run_stop`, an inline Stop on a browsing
request's card, is no broker request: it reads the durable admission, so it still acts after
a restart ([`plugin-stop.md`](plugin-stop.md)). Stoppable work is otherwise stopped by its red
key at the reply keyboard, whose press arrives as a text message (below). A background job's
buttons — its questions' options and its Cancel — are no broker requests either: their data (`v1|job_q|…`,
`v1|job_cancel|…`) names the engagement and is resolved against its record, by the operator
whose job it is, and is matched before the generic parser, whose four-field shape it does not
have ([`background-job-direct-chat.md`](background-job-direct-chat.md)).

## Contracts & invariants

**INV-TG-001**: A webhook update is accepted only when a secret is configured and the request's secret-token header matches, exactly, the secret derived from it for the bot its route names.

Enforced in the update route (`POST /telegram/update/{bot_id}`, one per bot) before the
payload is parsed or enqueued: a bot with no endpoint is a 404, and the header is compared
with that bot's own secret (`webhook_secret_for`, an HMAC of the configured secret over the
bot id, the value the endpoint registered with `setWebhook`), so an update signed for one bot
cannot enter another bot's route. The comparison is constant-time over the encoded bytes of
both header and secret, so a non-ASCII header is refused without an error.

What it does not cover: polling updates do not pass through this route at all, and the header
establishes only that the sender knows the shared secret.

**INV-TG-002**: A callback can resolve a request only from the operator that request is bound to.

Enforced twice, deliberately. When the callback arrives, before the broker claim, a missing
or mismatched user is refused with a best-effort acknowledgement. And inside the broker
claim itself, which rejects any actor that does not match the identity the request was bound
to at registration — fail-closed on both sides, so a request registered without a bound
identity is claimable by nobody, and a future delivery path cannot forget the check. There
is no other verdict writer: the internal socket carries no verdict route.

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

**INV-TG-006**: A one-shot operator notice is released for re-offer if and only if no part of it reached the operator.

Every delivery method reports a `DeliveryOutcome`. The distinction it exists to draw is
that a **normal return is not proof of delivery**: each method has an availability guard
that returns after zero Bot API calls when the application is absent, which a reconnect can
make true mid-turn. A caller that suppresses a one-shot notice on a normal return therefore
consumes notices that were never displayed.

Because such a notice is prepended at the *head* of the text, the outcome keys on the first
unit of output rather than on total success: the first chunk of a split message or page 1 of
a paginated response — a streamed reply included, since it ends through the same send. A
later page failing does not un-show what page 1 displayed. One consequence worth stating,
because it inverts the naive reading: a streamed reply whose final send never landed is
**not delivered**, even though the operator watched its draft arrive. The notice is
prepended after streaming, so it was never in what they saw.

A raise carries the same question and cannot answer it through a return value, so the head
fact is stamped on the turn context and consulted on the exception path.

What it does not cover: channels that have not adopted the contract. They return nothing,
which reads as `UNKNOWN` and preserves their existing behavior. That case is deliberately
distinct from a negative — treating "cannot report" as "failed" would re-offer a notice on
every turn forever.

**INV-TG-007**: A settled DM question drops its buttons along with its text; an edit keeps only the buttons it names, and the browsing start card's answered Start names none.

The DM settle edit sends an *explicit empty* keyboard rather than omitting the markup.
Omitting it is not neutral: the client library drops the absent parameter, so the platform's
edit call never touches the markup and the buttons outlive the text that retired them. The
operator is then holding a live-looking control for a question that is already closed —
which is the single-attention-lane discipline broken in the one place it is most visible.

The clear is the default because every caller of the DM settle edit is a broker finish
hook, and a finish hook runs only at a terminal outcome: an answered question, an expiry, a
supersession, a shutdown. There is no mid-question repaint on this path, and an opt-in flag
is a thing the next call site forgets. An edit that names its buttons
(`edit_dm_message(buttons=...)`) carries exactly those; the start card of a browsing request,
answered Start, settles to none, and the work it started is stopped by its key at the reply
keyboard ([`plugin-stop.md`](plugin-stop.md)).

A button is posted in Telegram's red style only where the caller names its position
(`post_dm_keyboard(danger=...)`, `edit_dm_message(danger=...)`). Three callers do: the memory
wipe question for its approving button, the uninstall question for its erasing choices, and
the browsing start card for the Stop it carries when no key could be raised. Red marks a
choice that deletes something for good, or stops running work; every other button, Approve
and Deny included, keeps the app's default look, so that a red button stays worth a second
look.

An edit is idempotent, so a DM settle edit that fails transiently is tried once more: after
flood control's stated wait (at most ten seconds), or a second after a network error. Any
other refusal, and a second failure, is logged and the edit reports that it did not land.

What it does not cover: it does not make the edit *succeed*. A settlement whose edit fails
in transport leaves the keyboard standing, and the failure is logged rather than retried
(INV-TG-003's caveat applies here too). A specialist's picture card is the exception: when
its caption edit does not land, its keyboard is still removed by a markup-only edit
(`_edit_proposal_message` calling `clear_dm_keyboard`, best effort, logged when it fails), so
only its text stays as posted. A tap on such a keyboard is still refused by the
broker as stale, so what survives is a misleading display, never a wrong outcome.

**INV-TG-009**: The Telegram channel's configured request replaces every surrogate code point in a serialized form-parameter value with one U+FFFD before HTTP encoding, preserving every other character and all multipart file data.

Enforced in the request class the channel installs when it builds its application, which
hands the client library's transport a view of each request whose string values are
normalised. It is pinned against the real client library over a mock transport, asserting
the requests that reach it per sender, and its installation on every rebuild is pinned
in-process.

What it does not cover: a bot injected from outside the channel; values nested inside
JSON-encoded parameters, which the library escapes and the platform judges; where the
surrogate came from; and the polling pool, which carries no Casa-authored text.

**A Stop key's press is no message to the agent.** The operator's text equal to a Stop key
Casa issued in this chat (`stop_keys`, exact equality) is taken first, before a reply into a
live task and everything below (`_press_stop_key`): it stops that work, his message is deleted,
and no turn starts ([`plugin-stop.md`](plugin-stop.md)).

**A plain DM message retires the questions this conversation asked, and only those.** The
order in the DM path is fixed and each step is there for a measured reason: `/new` is
intercepted first and takes its own reset, which retires both halves of the operator's
attention lane; the rate limiter returns next, so a dropped message never expires a question
it will never answer; then the typed-answer retirement runs, synchronously, before the text
is dispatched as a normal turn, so the stale keyboard is edited to say the typed reply closed
it — not that it expired, which it did not — while the same text still reaches the agent. That retirement is bound twice over — to this chat's plain-ask
scope, and to the absence of the marker a machine-timed question carries. The scope binding
is not decoration: the broker's predicate cancel is namespace-wide, so a selection that
forgot it would retire another operator's live question. Authorization challenges live in a
separate scope and are untouched here, as they always were.

**Three outbound messages are composed by Casa, not by an agent: a plugin's delivered
link, message and file.** The channel's part — one link send (a message with its URL button,
the labelled link only when the platform refuses the button), the whole-plan
judgement and multi-page loop for a message, the kind's media method for a file — is in
[`plugin-delivered-slots.md`](plugin-delivered-slots.md) (INV-PLUG-025, -045, -046); all
three are Casa-composed notices under the output boundary.

**A reply to a live task's post** — a job's or a delegation's, on any bot — is routed first in
the serialised DM handler, into that task ([`own-voice.md`](own-voice.md)). Every other reply is
the chat's own agent's turn: in the resident's chat a reply on a specialist's post is the
resident's, and a `/new` sent as a reply resets the resident's conversation as any `/new` there
does.

**INV-DESK-004**: A Telegram DM text message that replies to another message and does not go into a live task reaches the chat's own agent with its text unchanged and one Casa-composed note on the reserved `_reply_note` context key, naming who posted the quoted message — decided from Telegram's sender ids and the task indexes, never from text — when, and what it read, clipped to 600 characters.

The key is in `provenance.RESERVED_CONTEXT_KEYS`, so no external ingress can set it, and the
channel stamps it after sanitization; the agent's turn reads it only on a Telegram turn. A
message from Casa's bot that belongs to no task — the resident's own reply, a Casa card or
notice, a specialist's post from before an upgrade — is named as one of the chat's earlier
messages, never by an author Casa cannot know; a reply to an ended task's post names whose task it
was and that it has ended. What it does not cover: a file sent as a reply without a caption starts no
turn and so carries no note.

## Failure behavior

Moved to [`telegram-failures.md`](telegram-failures.md) — the same contracts, the same handlers; split when this document reached the corpus ceiling (#1206).

## Extension points

**Changing transport** touches the manifest schema, the environment-driven selection, the
route's authentication, and the channel rebuild.

**A new callback namespace** needs the namespace list, the parser and formatter, the dispatch
and authorization metadata, the broker registration, and the settlement hook — six places,
none of which will tell you the others were missed.


## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel`
- `casa/rootfs/opt/casa/channels/telegram.py::_SurrogateSafeRequest`
- `casa/rootfs/opt/casa/channels/telegram.py::_parse_callback_data`
- `casa/rootfs/opt/casa/casa_core.py::_make_telegram_update_handler`
- `casa/rootfs/opt/casa/verdict_broker.py::VerdictBroker`
- `casa/rootfs/opt/casa/channels/telegram.py::_InboundReservation`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.engagement_inbound_reservation`
- `casa/rootfs/opt/casa/channels/telegram.py::_text_filter`
- `casa/rootfs/opt/casa/result_broker.py::PostRecord`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._record_post`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._reply_note_for`

**Tests**
- `tests/test_telegram_update_handler.py`
- `tests/test_reply_note.py`
- `tests/test_ellen_chat_replies.py`
- `tests/test_c1_continuation_admission.py`
- `tests/test_telegram_dm_settle.py`
- `tests/test_verdict_broker.py`
- `tests/test_telegram_new_reset.py`
- `tests/test_telegram_supervisor.py`
- `tests/test_telegram_delivery_outcome.py`
- `tests/test_telegram_reconnect.py`
- `tests/test_telegram_surrogate_boundary.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/telegram-rendering.md`](../architecture/telegram-rendering.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
<!-- END SOURCEMAP -->
