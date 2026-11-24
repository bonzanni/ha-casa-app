---
last_reviewed: 2026-11-24
---

# The phone line

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Casa answering a phone line: the signed call webhook, the call slots and the vault item,
how a caller is recognised and what a call's models are told, the live voice session that
follows a call (turns, the two triggers, the one action state, the clock), the three
abilities a call can use, the phone store and its transcripts, the record of an ended call
and its hand-off to the resident assistant, and the stop. The contacts store and the caller
lookup are [`contacts.md`](contacts.md); the restricted turn the record arrives as is
[`webhook-delivery.md`](webhook-delivery.md); the listeners that carry the route are
[`http-surface.md`](http-surface.md); `/data/phone` is in
[`persistent-state.md`](persistent-state.md); how the prompt files under `/config/phone/`
are kept in step with the image is [`config-reconciliation.md`](config-reconciliation.md);
the options are in [`../reference/operator-options.md`](../reference/operator-options.md).

## Mental model

**Off by default, and off means absent.** `phone_enabled` is exported as `PHONE_ENABLED`
only when the option is on, and `phone/settings.py` reads every phone option once. With the
line off, boot opens no phone store, registers no route, reads no secret, creates no client
session, schedules no job and starts no task; what remains is the import of the phone
modules and one `enabled()` check at each of those places and at the stop (INV-PHONE-007).

**The provider carries the audio; Casa follows the call.** A phone number on a SIP trunk
that points at an OpenAI project rings; OpenAI posts a signed `live.transport.incoming`
event (the older `live.call.incoming` is handled alike) to `POST /phone/incoming`. Casa
answers by REST — `accept`, carrying the instructions, voice, models and tools — then
attaches to the session's control channel (a WebSocket) and follows its events until the
session closes. `live_voice/api.py` is the client (`accept`, `reject`, `refer`, `hangup`,
`attach`, and the Responses calls of the decision step and the record); the key goes only
into the Authorization header and never into an error. `live_api_host` picks the API host
(empty means `api.openai.com`). Nothing in `live_voice/` names a caller, a number or a
transfer beyond a generic target URI: the phone package supplies a profile and reads the
result.

**The webhook decides before anything acts.** `phone/webhook.py` takes a request through
its own rate limit (30 a minute, then 429), a 64 KiB body cap (413), the signing secret
(none, empty or unreadable: 503), the Standard Webhooks signature over
`<id>.<timestamp>.<body>` — a 300-second timestamp window, any one of several `v1`
signatures, each of the three headers present exactly once, else 401 — then JSON (400), an
age check (an event whose `created_at` is more than 120 seconds old is answered 200 and
ignored), and two repeat checks, by `webhook-id` and by session id. Only then does an
`Incoming` (session id, caller and called number) reach the service; a verified event of
any other type is answered 200 and ignored. The caller number is the user part of the
`From` header's SIP or tel URI, never its display name, normalised to E.164 with
`contacts_default_region`; anything else is an empty, withheld number. The route produces
no agent turn.

**Two calls at a time, and the key is read off the loop.** `on_incoming()` is synchronous:
it reserves one of two call slots and starts one task. That task reads the API key; with
none, nothing at all is sent to the provider, so the caller falls to the carrier's
fallback, and the operator is told, at most once an hour. With both slots taken the call is
answered busy (`reject`, 486). The vault item is `OpenAI Voice` in the default vault, fields
`credential` (the API key) and `webhook_secret`, read through `vault_read.py` — the `op`
front both secrets backends answer — in a worker thread. A value read is kept for ten
minutes, a missing one is never kept, and concurrent readers of a field share one read.

**Who is calling is a mechanical fact.** `phone/controller.py` looks the number up with the
contacts lookup, in a thread. A known caller with priority is class `priority`, any other
known caller `known`, everyone else `unknown`. A call whose caller number equals the called
number is treated as a transfer that came back (`returned`): it is not looked up and is an
unidentified caller in English, because Casa cannot tell which earlier call came back and
a wrong guess would give one caller another's name. With no contacts store the language
comes from the number's calling code.

**Being put through is a grant per class.** Each class has a transfer level — `off`,
`emergency`, `urgent` or `open` — by default `open` for priority, `urgent` for known and
`emergency` for unknown callers; a grant stored in the phone store overrides its class's
default. A returned call, and a line without a usable `phone_transfer_number`, is `off`.
The level picks a paragraph of the prompt and decides whether the `transfer` ability
exists; whether a call is urgent is the models' judgement, never code's. With the phone
store unreadable every grant is `off`: calls are answered and messages taken, and nobody is
put through.

**What the models are told is files.** `phone/prompt.py` reads `agent.md` (the voice
model), `helper.md`, `decision.md`, `record.md` and `lines.yaml` from
`/config/phone/prompts/` when the operator's copy exists and is usable, else from the
shipped defaults; a broken copy is warned about once per version of the file and the
shipped one is used. `agent.md` is a template followed by `## who: <class>` and
`## transfer: <level>` sections, of which the controller picks one each; the helper and
decision prompts reuse those two paragraphs, so a rule is worded once. Placeholders are
filled in one pass, so a name holding braces is inserted as written. From the contacts
store the models get a known caller's name and first name and the language
(INV-PHONE-001). The owner is `phone_owner_name`, else "the person you called"; the
assistant's name comes from its character file. The files are read for every call, so a
commit that changes one reloads nothing. `lines.yaml` holds fixed lines per language:
`silence` and `too_long` are said to the caller, `transfer_failed` is told to the voice
model.

**Voices and models.** A call's voice is the stored voice of its language, else the
`live_voices` seed for it, else English's, else a stock voice; a value starting `voice_` is
sent as a custom voice id, anything else as a stock name. The four models (voice, helper,
decision, record) are the store's when set there, else the defaults in
`phone/settings.py`. Nothing in Casa writes the store's settings, voices or grants yet.

**Turns are read from the transcript.** The service has no event for the end of a turn, so
`LiveSession` (`live_voice/session.py`) builds turns from transcript deltas: consecutive
deltas from one side are one line, and when the caller starts to speak after an agent line
a new round begins (`ActionState.new_round`). Every request for an ability names the round
it was decided in.

**Two triggers, one action state.** The helper model the session delegates to holds the
abilities as function tools; its calls arrive as `response.event` and belong to the round
in which their delegation was created, and a call of a delegation the session never saw
created runs nothing. The decision step (`live_voice/decision.py`) is one model call made
each time the agent has been quiet for a second after saying something; caller speech
before that moment cancels it, and one already asked is judged by its round. Both go
through one `ActionState` (`live_voice/actions.py`), which holds one lock across a runner
(INV-PHONE-002, INV-PHONE-003). A refusal a runner wrote is told to the voice model once
per round; the action state's own short words never are. A failed decision is retried once.

**A call is never ended on the caller's unanswered words.** Before `end_call` runs, the
session waits, up to an eight-second grace, for the agent to have the last word and for
that line to have been played. If the caller says anything meanwhile, or the agent does not
answer, the request does not run, and a later request in the same turn may.

**The clock ends a call without any model** (`live_voice/clock.py`, INV-PHONE-004). The
agent has not spoken eight seconds after the attach (`first_speech`). Nobody speaks for 45
seconds: the `silence` line is said and the call ends unless the caller speaks within the
grace. The decision failed twice and nobody spoke for eight seconds (`undecided`). The call
reached ten minutes: the `too_long` line, the grace, then the end whatever is said (`max`).
The control channel dropped and could not be attached again within ten seconds (`lost`).
The clock ends the action state before it hangs up, so no ability starts meanwhile.

**Three abilities, and nothing else a call can cause** (`phone/abilities.py`). `end_call`
hangs up. `transfer`, present only when the level is not `off`, checks the round and the
phase, tells the operator "Putting through:" and who (the operator's phone shows the line,
not the caller), checks again, and asks the provider to refer the call to
`tel:<phone_transfer_number>`, with nothing awaited between the second check and the
request. A 4xx is a refusal the voice model hears. Success, no answer and a 5xx all mean
the provider may have accepted it: the transfer is marked pending and is never placed a
second time in that call. A session that closes while a transfer is pending was handed over;
a transfer still pending after 30 seconds is spent and the voice model is told it failed. A
returned call has no `transfer` but has `alert_operator`, which sends the operator a fixed
urgent line naming the caller and no word of the model's.

**The phone store.** `/data/phone/phone.db` (directory 0700) follows the contacts store's
discipline: one transaction per connection, the schema version in `PRAGMA user_version`,
WAL, a file from a newer Casa refused. `calls` holds a row per call — number, contact
name, class, language, returned flag, outcome, action log, record, delivered — and
`transcripts` a call's words as `Caller:` and `Assistant:` lines for 30 days, deleted by
the `phone_transcript_purge` job at 04:40. Audio is never kept.

**An ended call is recorded, then handed on.** The controller writes the outcome
(`caller_hung_up`; `ended_by_casa` for a clock ending, a lost channel or an `end_call` that
was done; `transferred`; `accept_failed`; `error`; `interrupted`) with the action log and
the transcript in one write. The record is then written in a task of its own, so the call's
slot is free meanwhile: `phone/record.py` sends the transcript to the record model under
`record.md` for four fields — `caller_name`, `reason`, `urgency`, `follow_up` — and code
bounds the caller's two to one line each (60 and 240 characters, control and format
characters removed) and coerces the other two to their enums. Casa adds what the row says:
the contact's name, class, language, the number's last four digits (none on a returned
call), outcome, abilities done, duration and local start time. The full number is never in
a record. The record is stored on the row, handed to the resident assistant
(INV-PHONE-006), and the call is marked delivered. Without a key the record has no model
part (`summary_failed`) and is handed on all the same.

**Every line to the operator is Casa's.** The missing-key line, "Putting through", the
urgent alert and the fallback line about an ended call (the contact's name or the number's
last four digits, the class, the outcome, the duration) are code-written and pass
`casa_text`. Logs carry no secret, no signature and no more of a number than its last four
digits.

## Contracts & invariants

**INV-PHONE-001**: What a call's models learn about the caller is the caller lookup's answer — a known caller's name and first name, and the language — and a call back from a transfer is not looked up and is given no name.

The lookup returns five fields (INV-CONTACTS-001), and its two flags only pick the class,
hence a paragraph. An unknown caller's text names nobody, and so does a returned call's,
even when its paragraph asks for `{name}`. What it does not cover: that the lookup is the
only read of the contacts store under `phone/` and `live_voice/` is kept by review, not by
a test.

**INV-PHONE-002**: An ability runs at most once per caller turn, whichever trigger asks for it — the helper's function call or the decision step — and a request that arrives while it runs waits and gets its outcome instead of running it again.

The lock is held across the runner, so the two triggers asking at nearly the same moment
are one run. A refusal stays the turn's outcome; the next turn may try again. A transfer the
provider may have accepted is spent for the whole call.

**INV-PHONE-003**: A request decided before the caller's latest turn began never runs: it is answered `superseded`, also when it waited for the lock while that turn began, and a transfer checks again just before it is placed.

A decision that was being made when the caller spoke, and a helper call whose delegation
was created before the caller spoke, are both stale. A request that waited behind a slow
runner is judged when it gets the lock, not when it was sent.

**INV-PHONE-004**: An accepted call is ended by Casa's clock, with no model involved, when the agent has not spoken in time after the attach, when nobody speaks for too long, when the decision step failed twice and nobody spoke, when the call reaches its maximum length, and when the control channel is lost and cannot be attached again in time.

Each ending hangs up and is recorded as `ended_by_casa`. A hang-up the provider refuses
still ends the session. What it does not cover: a maximum duration at the carrier, which
the person installing the line sets as a backstop to this one.

**INV-PHONE-005**: The call webhook acts only on a request whose Standard Webhooks signature verifies against the vault's secret within the timestamp window, and on each call session at most once; without a secret it refuses every request.

Repeat tables are written only after verification and the age check, so a refused request
cannot block a valid one. What it does not cover: the tables live in memory and hold the
last 500 ids each, so a restart forgets them; the age check is what keeps a late retry of
an old call from being answered.

**INV-PHONE-006**: An ended call's record reaches the assistant as a restricted webhook-trigger turn under the reserved name `casa-phone`, whose final reply is always delivered to the operator; the transcript never enters that turn.

The turn is built the way the webhook handler builds one: `_origin_route` `webhook_trigger`,
public clearance, `_webhook_deliver` `operator_always`, a fresh session per call, the
assistant's bot when Telegram routes one. The caller's words reach it only as the two
bounded fields. No resident trigger can take the name (`trigger_registry.py` refuses it at
registration), so no request to `/webhook/{name}` can reach a turn under it.

**INV-PHONE-007**: With `phone_enabled` off the call webhook's route is not registered; with it on, the route is served through the external listener and refused with a 404 on the ingress listener.

The provider must reach the route from outside, so it lives on the external API port, whose
reverse proxy the person installing the line sets up; Home Assistant's ingress never
carries it.

## Failure behavior

**The vault item or a field is missing.** Boot logs a warning naming `OpenAI Voice` and the
missing fields, or a warning that `public_url` is unset. Without the secret every webhook
request is answered 503; without the key no call is answered and the operator hears so at
most once an hour.

**The phone store cannot be opened.** Calls are answered with the seed voices and the
default models, and nobody is put through. A call gets no row and no transcript and nothing
is handed on; when it ends the operator gets the fallback line with "It was not recorded:
the phone store is unavailable." A store that fails during a call is treated the same from
then on.

**The answer fails.** A refused `accept` records `accept_failed` and runs no session. An
`accept` that got no answer is hung up first, since the provider may have taken it. A
shipped prompt that cannot be read stops the call before `accept`: it is logged and left to
the carrier's fallback. An unexpected error during a call hangs up and records `error`.

**The record cannot be handed on.** No bus, an assistant with no queue, or any error gives
the operator the fallback line and leaves the row undelivered. The `phone_boot_redeliver`
job, 90 seconds after a boot, hands on every undelivered call that ended within 24 hours: a
stored record as it is, an unwritten one by the whole path; so the operator may read the
fallback line first and the assistant's report later. A silent reply of the assistant's
gives the operator INV-TRIG-021's fallback, naming the webhook `casa-phone`.

**Casa stops during a call.** At the very start of the graceful stop the line is declared
stopping: from then on no call is admitted or accepted — one already looked up is recorded
`interrupted` and left to the carrier's fallback — and no record is put on the bus; such a
record stays undelivered, with no fallback line, for the next boot. Later, before the
channels go down, every running call is hung up, and the calls have up to five seconds
together to record their outcome and transcript, a call the stop hung up as `interrupted`
(also one whose transfer was still pending when it was hung up); a call whose hang-up the
provider refused is no longer counted as hung up by the stop once the refusal is back, and
keeps the outcome it ends with after that.
A call whose accept landed after that hang-up is hung up again; what is left (a call that did
not close, a record being written) is cancelled within five more seconds, and the line's
client session closed. A call with no outcome that the running process did not write — cut
off by the stop or by a crash — is ended as `interrupted` at the next boot and handed on by
the job above.

## Extension points

**A new ability** is an `Ability` in `phone/abilities.py`, `plain` or `final`; both
triggers see it through the profile and it runs only through the action state. A runner
that marks its work pending must do so as its last act and must not raise afterwards.

**Another transport** can reuse `live_voice/` unchanged: it accepts its own session,
supplies a profile, timing and fixed lines, and reads the result.

**A schema change** appends a version to `phone/schema.py`'s migrations; opening an older
file runs the missing ones in one transaction.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/phone/__init__.py`
- `casa/rootfs/opt/casa/phone/settings.py`
- `casa/rootfs/opt/casa/phone/schema.py`
- `casa/rootfs/opt/casa/phone/store.py`
- `casa/rootfs/opt/casa/phone/webhook.py`
- `casa/rootfs/opt/casa/phone/policy.py`
- `casa/rootfs/opt/casa/phone/prompt.py`
- `casa/rootfs/opt/casa/phone/abilities.py`
- `casa/rootfs/opt/casa/phone/controller.py`
- `casa/rootfs/opt/casa/phone/record.py`
- `casa/rootfs/opt/casa/phone/service.py`
- `casa/rootfs/opt/casa/live_voice/__init__.py`
- `casa/rootfs/opt/casa/live_voice/api.py`
- `casa/rootfs/opt/casa/live_voice/actions.py`
- `casa/rootfs/opt/casa/live_voice/session.py`
- `casa/rootfs/opt/casa/live_voice/clock.py`
- `casa/rootfs/opt/casa/live_voice/decision.py`
- `casa/rootfs/opt/casa/vault_read.py`
- `casa/rootfs/opt/casa/defaults/phone/prompts/agent.md`
- `casa/rootfs/opt/casa/defaults/phone/prompts/helper.md`
- `casa/rootfs/opt/casa/defaults/phone/prompts/decision.md`
- `casa/rootfs/opt/casa/defaults/phone/prompts/record.md`
- `casa/rootfs/opt/casa/defaults/phone/prompts/lines.yaml`
- `casa/rootfs/opt/casa/trigger_registry.py::TriggerRegistry._check_webhook_collision`

**Tests**
- `tests/test_phone_settings.py`
- `tests/test_phone_store.py`
- `tests/test_phone_webhook.py`
- `tests/test_phone_policy.py`
- `tests/test_phone_prompt.py`
- `tests/test_phone_abilities.py`
- `tests/test_phone_controller.py`
- `tests/test_phone_record.py`
- `tests/test_phone_service.py`
- `tests/test_live_voice_api.py`
- `tests/test_live_voice_actions.py`
- `tests/test_live_voice_session.py`
- `tests/test_live_voice_decision.py`
- `tests/test_vault_read.py`

**Related**
- [`architecture/contacts.md`](../architecture/contacts.md)
- [`architecture/webhook-delivery.md`](../architecture/webhook-delivery.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/config-reconciliation.md`](../architecture/config-reconciliation.md)
- [`reference/operator-options.md`](../reference/operator-options.md)
<!-- END SOURCEMAP -->
