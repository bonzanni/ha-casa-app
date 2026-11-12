---
last_reviewed: 2026-11-10
---

# Telegram bots: endpoints, routes and addresses

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How Casa names a Telegram bot, and the messages it sends or receives, once more than one bot
can be configured: the per-bot endpoint, the one transport in front of them, the route a turn
carries, the address a stored message carries, Casa's persisted bot id, and the per-bot
webhook route. What a single bot does with an update (handlers, keyboards, the request seam)
is [`telegram.md`](telegram.md); its failure paths are
[`telegram-failures.md`](telegram-failures.md).

## Mental model

**Telegram numbers everything per bot.** `update_id` and `message_id` count up from 1 on each
bot, and a private chat's id is the user's id, so two bots' private chats with the operator
share a chat id. A chat id and a message id therefore name a message only together with the
bot. Everything that outlives a turn names the bot as well.

**One endpoint per bot, one transport in front.** Each bot is a `TelegramChannel` with its
own Application, update stream, dedupe window, typing state and readiness. A
`TelegramTransport` holds the endpoints by bot id and by agent role. It is the only object
registered as the `"telegram"` channel, and `telegram_transport()` is the one place Casa looks
it up by name. The transport resolves every outbound call to exactly one endpoint, by one of
three rules, and then calls that endpoint's own method:

- **A, address-bound** (`TelegramTransport.endpoint`): the bot of a stored address, for an
  edit, a delete or a reply to an existing message.
- **B, route-bound** (`TelegramTransport.endpoint_for_route`): the route in the delivery
  context, for a turn's reply, its stream, a media send, and a summoned turn's answer in the
  group's inline message (`guest_finish`, [`guest-summons.md`](guest-summons.md)).
- **C, Casa-bound** (`TelegramTransport.casa_endpoint`): Casa's own cards and notices (consent,
  install and erase cards, Casa's operator notices, the limit line of a turn that did not come
  from a chat), which always go through Casa's bot whatever bot the triggering turn came from.

An agent's own bot ([`own-voice-lifecycle.md`](own-voice-lifecycle.md)) is an endpoint the own-chat lifecycle adds
and removes while Casa runs: `add_agent_endpoint` registers it by bot id before its role is
known, `set_endpoint_role` binds or releases the role (Casa's endpoint keeps its own), and
`remove_endpoint` forgets a stopped one. A job's posts are address-bound to the job's
delivery (`_job_endpoint`), decided at its birth; a job with no delivery posts nothing. Each
endpoint states the update types it asks Telegram for on every `setWebhook` and `getUpdates`:
Casa's bot adds `managed_bot` to `message` and `callback_query`, an agent's own bot
`guest_message` (`ALLOWED_UPDATES_CASA`, `ALLOWED_UPDATES_AGENT`).

Resolution never falls back. A bot with no endpoint is `EndpointUnavailable`, never answered
by another endpoint. The transport adds no outcome of its own: an endpoint that is not ready
reports `NOT_DELIVERED` from `send`, raises its availability error from `send_media`, and
returns its usual callable from `create_on_token`, exactly as a lone channel does.

**A route is not an address.** The two values are kept apart in `channels/tg_address.py`:

- A **route** (the reserved context key `_tg_bot`) is what a turn or a post is *going to*
  deliver through: a verified bot id, or the reserved value `CASA` (`"casa"`), meaning Casa's
  own endpoint whatever id it verifies. `None` is never a route.
- An **address** (`TgAddress`, stored as `[bot_id, chat_id, message_id]`) is a message that was
  actually sent or received. Its bot is always the integer id of the endpoint that sent or
  received it, read from that endpoint (`TelegramChannel.bot_id`) at that moment. `CASA`
  cannot reach an address, nor a lane scope (`scope(kind, bot, chat)`, `"<kind>:<bot>:<chat>"`),
  nor a memory key; each refuses a bot that is not an int with `AddressError`.

**A route is decided once, where the turn is born.** An inbound update gets the id of the
endpoint it arrived on. A turn born with no parent (a trigger, a reminder, `/invoke`, a voice
turn, a webhook turn, a scheduled job's launch) asks `TelegramTransport.route_for(role)`: the
bot of the endpoint bound to that role, else Casa's route. A plugin-setup turn is Casa's own
conversation and is born on Casa's bot. From then on the route is copied, never re-derived:
`provenance.carry_route` spreads it into every derived origin, context and record, and it is
persisted with engagement origins, plugin bindings and the delegation row's `route`. A
derivation whose source had no route has none either, and fails at delivery with
`RouteMissing` rather than being given a guessed bot. Binding a role to another bot later does
not move a turn already born.

**Casa's bot id is known before the bot reconnects.** `CasaBotIdStore` keeps the id Casa's
endpoint last verified, with a short digest of the token it was verified for, in
`/data/telegram-casa-bot.json` (private, 0600). It is read before the HTTP server starts and
trusted only when the digest matches the configured token, so on an ordinary restart every
turn is born with the integer route even before `getMe` has run again. With nothing trusted (a
first boot, a replaced token) Casa's route is `CASA` until the new token's `getMe` succeeds.
Each successful `getMe` reports the id through `TelegramTransport.observe_endpoint_identity`
before the endpoint marks itself ready, in one synchronous step, and the file is rewritten only
when the id or the digest changed. A write failure is logged and only costs the next boot that
head start.

**Identities that two bots could produce equal carry the bot.** A chat lane of the verdict
broker (`dm`, `authz`, `ask`, `proposal`), a chat's notes, the job post indexes, a
scheduled question's record, a plugin request's dedupe key, a stop in flight, a delivery key, a
run reference, the operator's words recorded for a plugin, the run card and the journal's
notice address, and the memory key of an operator message (`occurrence.message_key(channel,
bot, chat, message)`). So `/new` on one bot cancels only that bot's lanes, a typed reply
retires only that bot's pending question, and a reply to message 5 on one bot never reaches a
job that posted message 5 on another.

**Persisted shapes are strict.** A stored record or key that should carry its bot and does not
(a post written as a bare message id, a scheduled question without `bot_id`, a delivery key
without a bot, a Telegram engagement origin or delegation row without a valid route) is refused
at load and logged, never assigned to some bot. Earlier state written without the bot is not
read; there is no conversion inside Casa.

**One webhook route per bot.** In webhook mode each endpoint registers
`<public_url>/telegram/update/<bot_id>` once its id is verified, signed with its own secret,
`webhook_secret_for(root, bot_id)`: an HMAC-SHA256 of the one configured webhook secret. The
route `POST /telegram/update/{bot_id}` looks the endpoint up by the path's bot id (404 for a
bot with no endpoint) and compares the header with that bot's derived secret (INV-TG-001), so
an update signed for one bot cannot enter another bot's route. An endpoint known by its
persisted id takes updates on its route even before it is ready.

## Contracts & invariants

**INV-TG-017**: Every outbound Telegram call through the transport resolves to exactly one endpoint — the bot of the address it names, the route its context carries, or Casa's endpoint for Casa's own cards — and a context with no route raises `RouteMissing`, a malformed route or address raises `AddressError`, and a bot with no endpoint raises `EndpointUnavailable`, with no call on any other endpoint.

Enforced in `TelegramTransport.endpoint`, `endpoint_for_route` and `casa_endpoint`, which every
route-bound reply method and every job post method goes through, and in the keyboard
retirement, which edits each post through the bot that posted it and leaves the keyboards of a
bot with no endpoint as they are.

What it does not cover: a send made with a raw bot outside the transport, of which one is
deliberate: the pre-boot maintenance line (`memory/maintenance.send_operator_line`) runs before
any channel exists and posts with Casa's token.

**INV-TG-018**: A turn's route is set where the turn is born — the receiving endpoint's bot for an inbound update, `route_for(role)` for a turn with no parent — and every derived origin, context or stored record carries that same value, so a later change of which bot serves the role does not move it.

Enforced by the stamping at each ingress, by `carry_route` at each derivation, and by the
persisted `_tg_bot` on engagement origins and binding origins and the `route` of a delegation
row, which `specialist_registry.origin_from_job` returns unchanged at the boot replay.

What it does not cover: a derivation that never copied its source's route. It has none and
fails at delivery with `RouteMissing`; nothing finds that before the send.
Nor is the route where a task's posts go: that is the task's delivery, decided once at its birth
([`own-voice.md`](own-voice.md)). A job of an agent behind the resident carries its route and has
no delivery, so its lines are not posted anywhere.

**INV-TG-019**: A stored Telegram message is addressed by bot, chat and message, and an address, a lane scope or a memory key refuses a bot that is not an integer bot id; a stored record or key that lacks its bot is refused at load and logged, and the rest of the file still loads.

Enforced in `TgAddress`, `scope`, `occurrence.message_key` and `engagement_key`, and in the
strict loaders of the engagement registry, the delegation registry, the scheduled questions,
the plugin delivery keys, the stored confirmations, the bindings and the job journal.

What it does not cover: state written by an earlier version without the bot. It is refused
like any other malformed entry, not converted.

**INV-TG-020**: Two bots never share an identity they could produce equal — equal update ids, message ids, chat lanes, chat notes, plugin request or delivery keys on two bots are two distinct things, and an action on one bot touches only that bot's.

Enforced by keying each such identity by the endpoint's `bot_id`, on both its write and its
read path: a key that gained the bot on one side only would pass every single-bot test.

What it does not cover: the conversation's session key, which is built from the channel kind,
the agent and the chat, without the bot, so replacing an agent's bot token keeps its
conversation.

**INV-TG-021**: Casa's endpoint knows a bot id before its first `getMe` only when `/data/telegram-casa-bot.json` holds one under the digest of the configured token; otherwise Casa's route is `CASA` until a `getMe` succeeds, and that success is reported to the transport, and persisted, before the endpoint becomes ready.

Enforced by `CasaBotIdStore.known_id`, which returns nothing for another token's digest or a
malformed file, and by `TelegramTransport.observe_endpoint_identity`, which the endpoint calls
synchronously after each successful `getMe`. An id already served by another endpoint is
refused with `DuplicateEndpoint`, and that endpoint stays stopped.

What it does not cover: whether the operator's chat is configured; a route says which bot,
never which chat.

**INV-TG-022**: A turn born before Casa's endpoint is ready, and every record persisted from it, delivers its routed messages — its reply and a delegation's completion notice — once through that endpoint after its `getMe` succeeds; a task born in that window has no delivery and posts nothing, even after `getMe`, while its result still reaches its requester; until then each method behaves as the unready endpoint does, and an identity that must name Casa's bot before its post is not formed, so no broker, grant, challenge or question store changes.

Enforced by the route a birth gets (Casa's persisted id, else `CASA`), which resolves to Casa's
endpoint at delivery whatever id it then verifies; by the transport passing each call to that
endpoint unchanged (`send` reports not delivered, `send_media` is `channel_unavailable` and the
file stays owed, a streamed reply's callable is harmless); and by every writer that forms an
identity before it posts reading the endpoint's `bot_id`, which raises `EndpointNotReady`, and
answering as when its post fails before it touches a store.

What it does not cover: a message sent in that window. It is reported not delivered, as it was
with one channel, and is not queued for later.
A task's own posts — its start line, progress, job lines — follow its delivery, decided once at
its birth ([`own-voice.md`](own-voice.md), INV-OWN-002); a resident task born before Casa's bot
has an id gets none, so they are never posted.

## Failure behavior

- **No route in a delivery context**: `RouteMissing`, naming the call site. No endpoint is
  called.
- **A route or address naming a bot with no endpoint**: `EndpointUnavailable`. A job keyboard
  on such a bot keeps its buttons (a tap on one reads the terminal record); the boot pass logs
  it and goes on.
- **An endpoint read for its id before it has one** (`EndpointNotReady`): only a writer that
  must form an identity before it posts meets it — a broker registration, a grant identity, a
  scheduled question's record, a plugin key — and each returns the outcome the site gives when
  its post fails, before changing any store.
- **A malformed or foreign `/data/telegram-casa-bot.json`**: ignored with a warning; the route
  is `CASA` until `getMe`.
- **Another bot failing to start**: logged; Casa's endpoint and the others start regardless.

## Extension points

**A new place that stores a Telegram message** stores a `TgAddress` (or its
`[bot, chat, message]` row) read from the endpoint that sent or received it, and its loader
refuses a row without the bot.

**A new turn born with no parent** stamps `route_for(role)` at birth; **a new derivation**
spreads `carry_route(source)`. Neither reads the role's bot at delivery.

**A new identity a second bot could produce equal** takes the bot from the endpoint object on
both its write and its read path.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/channels/telegram_transport.py::TelegramTransport`
- `casa/rootfs/opt/casa/channels/telegram_transport.py::CasaBotIdStore`
- `casa/rootfs/opt/casa/channels/telegram_transport.py::webhook_secret_for`
- `casa/rootfs/opt/casa/channels/telegram_transport.py::telegram_transport`
- `casa/rootfs/opt/casa/channels/tg_address.py::TgAddress`
- `casa/rootfs/opt/casa/channels/tg_address.py::scope`
- `casa/rootfs/opt/casa/provenance.py::carry_route`
- `casa/rootfs/opt/casa/casa_core.py::_build_telegram_transport`

**Tests**
- `tests/test_tg_address.py`
- `tests/test_telegram_transport.py`
- `tests/test_endpoint_isolation.py`
- `tests/test_bot_webhook_routes.py`
- `tests/test_fresh_routes.py`
- `tests/test_job_addresses.py`
- `tests/test_ask_scopes.py`
- `tests/test_plugin_identities.py`
- `tests/test_bot_boundary_invariants.py`
- `tests/test_bot_boundary_sites.py`

**Related**
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/telegram-failures.md`](../architecture/telegram-failures.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
