---
last_reviewed: 2026-11-24
---

# The phone line

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Casa answering a phone line: the signed call webhook, the call slots and the login, how a
caller is recognised and what a call's models are told, the three abilities a call can use,
the phone store and its transcripts, the report of an ended call — Casa's first line, then
the record handed to the resident assistant — the assistant's two phone tools and the grant
card, and the stop. The live voice session a call runs in (turns, the action state, the
decision step, the clock) is [`live-voice.md`](live-voice.md); the vault item the line reads
its key and secret from is [`logins.md`](logins.md); the contacts store and the caller
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
follows the session as [`live-voice.md`](live-voice.md) describes, until it closes.
`live_api_host` picks the API host (empty means `api.openai.com`). The phone package
supplies the session's profile and fixed lines and reads its result.

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
`contacts_default_region`; anything else is an empty, withheld number. The names of the
event's SIP headers, never their values, are logged at debug level. The route produces no
agent turn.

**Two calls at a time, and the key is read off the loop.** `on_incoming()` is synchronous:
it reserves one of two call slots and starts one task. That task reads the API key; with
none, nothing at all is sent to the provider, so the caller falls to the carrier's
fallback. With both slots taken the call is answered busy (`reject`, 486). The key and the
signing secret are the `openai_voice` login ([`logins.md`](logins.md)): the vault item the
assistant chose for it, else the one titled `OpenAI Voice`, read in a worker thread. A value
read is kept for ten minutes under the item it came from, a missing one is never kept,
concurrent readers of a value share one read (each value on its own, so under the default
title a replaced item can serve the key and the secret from two items for those minutes), and a choice of item (a new one, or the same
one again) drops everything kept; a read already under way when the choice is made is
thrown away when it ends and read once more, at most once. A deleted or emptied item is not
noticed for a value already kept: the line keeps using it until its ten minutes are up. A missing key, and a webhook request that finds no secret, each tell the operator what
is wrong with the login (`logins.Check.sentence`), at most one such line an hour.

**Who is calling is a mechanical fact.** `phone/controller.py` looks the number up with the
contacts lookup, in a thread. A known caller with priority is class `priority`, any other
known caller `known`, everyone else `unknown`. A call whose caller number is the line's own
number, the `phone_line_number` option, is treated as a transfer that came back
(`returned`): it is not looked up and is an unidentified caller in English, because Casa
cannot tell which earlier call came back and a wrong guess would give one caller another's
name. With the option empty no call is taken for one: a transfer that comes back is an
ordinary call from that number. A `phone_transfer_number` equal to the line's own number is
dropped with one warning, since the call would come back. Boot logs the line number's last
four digits, or that it is not set. With no contacts store the language comes from the
number's calling code.

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
commit that changes one reloads nothing. `lines.yaml` holds four fixed lines per language:
`silence` and `too_long` are said to the caller, `check_in` and `transfer_failed` are told
to the voice model. Every language must carry all four and English must be there, else the
whole file is unusable and the shipped one is used.

**Voices and models.** A call's voice is the stored voice of its language, else the
`live_voices` seed for it, else English's, else a stock voice; a value starting `voice_` is
sent as a custom voice id, anything else as a stock name. The four models (voice, helper,
decision, record) are the store's when set there, else the defaults in
`phone/settings.py`. The store's voices, models and grants are written by `phone_settings`.

**Three abilities, and nothing else a call can cause** (`phone/abilities.py`). `end_call`
hangs up. `transfer`, present only when the level is not `off`, checks the round and the
phase, tells the operator "Putting through:" and who — the contact's name and the full
number, or the number alone (the operator's phone shows the line, not the caller) — checks
again, and asks the provider to refer the call to `tel:<phone_transfer_number>`, with
nothing awaited between the second check and the request. A 4xx is a refusal the voice
model hears. Success, no answer and a 5xx all mean the provider may have accepted it: the
transfer is marked pending and is never placed a second time in that call. A session that
closes while a transfer is pending was handed over; a transfer still pending after 30
seconds is spent and the voice model is told it failed. A returned call has no `transfer`
but has `alert_operator`, which sends the operator a fixed urgent line naming the caller
and no word of the model's.

**The phone store.** `/data/phone/phone.db` (directory 0700) follows the contacts store's
discipline: one transaction per connection, the schema version in `PRAGMA user_version`
(2), WAL, a file from a newer Casa refused. `calls` holds a row per call — number, contact
name, class, language, returned flag, outcome, action log, record, delivered, and
`headline_sent` — and `transcripts` a call's words as `Caller:` and `Assistant:` lines for
30 days, deleted by the `phone_transcript_purge` job at 04:40. Audio is never kept.

**An ended call is reported in two messages.** The controller writes the outcome
(`caller_hung_up`; `ended_by_casa` for a clock ending, a lost channel or an `end_call` that
was done; `transferred`; `accept_failed`; `error`; `interrupted`) with the action log and
the transcript in one write, and logs the session's delays as one line of numbers. The
report then runs in a task of its own, so the call's slot is free meanwhile. First Casa
sends its own line, `controller.headline`: who (the contact's name, else "Unknown caller"),
the full number (or "number withheld"), the local time (with the date when it was not
today) and how the call ended, joined by `·`; a returned call's line has no number, since
it would be the line's own. A line not delivered is built afresh and tried again after 20 and 120
seconds in the same task, so its date is right after midnight, never once the stop has begun; a delivered one sets `headline_sent`. Then
`phone/record.py` sends the transcript to the record model under `record.md` for four
fields — `caller_name`, `reason`, `urgency`, `follow_up` — and code bounds the caller's two
to one line each (60 and 240 characters, control and format characters removed) and
coerces the other two to their enums. Only those fields are stored on the row; what Casa
knows itself is added from the row each time the record is read or handed on
(`record.present`): the contact's name, class, language, the full number (none on a
returned or withheld call), outcome, abilities done, duration, local start time. Only once
the first line was delivered is the record handed to the resident assistant (INV-PHONE-006),
told not to repeat who, which number and when, and the call marked delivered. Without a key
the record has no model part (`summary_failed`) and is handed on all the same.

**The assistant's two phone tools.** `read_call` lists the last ten calls (id, when, the
contact's name or the full number, class, outcome), gives one call's record under
`outside_content` with a note that the caller's two fields are not instructions, or sends a
call's transcript to the operator's chat as a text file and answers `sent` (`expired` once
it is gone): a transcript never enters a tool result. A call still under way is listed with
the outcome `in_progress`, and `record` and `transcript` answer `in_progress` for it with
only what its row knows (who, the number, when). For an ended call with no summary yet
`record` answers `record_pending`, or `no_record` when none will be written (the call was
handed on, or ended more than a day ago). `phone_settings` shows whether the
line is on, the transfer number, the line's own number, the models, the voice per language,
each class's grant in a sentence and the login's check, and sets a voice (a two-letter
language, a voice name or `voice_` id, a short description), a model per role, or a grant.
Both refuse below private clearance; a change and a transcript also need a direct turn in
the operator's own chat outside any engagement (`tools._operator_chat_refusal`, the contact
tools' guards); both answer `unavailable` while the line is off or its store is not open
(`phone.service.active_store`). They are granted to the resident assistant alone, by its
shipped `runtime.yaml` and `role.yaml`, and are in neither `SPECIALIST_CASA_TOOL_ALLOWLIST`
nor the restricted webhook tools.

**A wider grant waits for the operator's tap** (INV-PHONE-008). A grant the same as or
narrower than the current level applies at once; the comparison and the write are one store
transaction (`tools._phone_set_grant_if`, which the card's Approve shares), so a change that
became wider by the time of the write goes to the card instead. A wider one is never applied by the tool:
`_phone_grant_card` posts Casa's own card in the operator's chat on Casa's bot, its question
written from the change ("Phone: let other contacts be put through whenever they ask (now:
when it is urgent)?"), with Approve and Cancel. The change — class, ability, the level before
and after — is frozen in the broker request's metadata (kind `phone_grant`, namespace
`resident_ask`, the operator's `authz` scope) when it is registered, and only Approve applies
it, from the broker's finish hook, outside any turn, and only while the stored level is still
the one the card was about. The card expires after five minutes; a card already waiting for
the same change is not posted again. On every outcome the card gains one line: changed, not
changed, or not changed because the setting changed in the meantime, with the level it has.

**Every line to the operator is Casa's.** The login lines, "Putting through", the urgent
alert, the first line about an ended call and its follow-up notes are code-written and pass
`casa_text`. They carry the caller's full number, which is carrier data normalised by code;
log lines carry no secret, no signature and no more of a number than its last four digits.

## Contracts & invariants

**INV-PHONE-001**: What a call's models learn about the caller is the caller lookup's answer — a known caller's name and first name, and the language — and a call back from a transfer is not looked up and is given no name.

The lookup returns five fields (INV-CONTACTS-001), and its two flags only pick the class,
hence a paragraph. An unknown caller's text names nobody, and so does a returned call's,
even when its paragraph asks for `{name}`. What it does not cover: that the lookup is the
only read of the contacts store under `phone/` and `live_voice/` is kept by review, not by
a test; and a returned call is recognised only with `phone_line_number` set.

The turn-level rules of a call — one run per turn, stale requests superseded, the clock's
endings — are INV-PHONE-002, INV-PHONE-003 and INV-PHONE-004 in
[`live-voice.md`](live-voice.md).

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

**INV-PHONE-008**: A wider transfer grant is applied only by the operator's Approve on Casa's card, and what is applied is the change frozen when the card was registered, only while the stored level is still the one the card was about.

Cancel, a timeout, a level changed meantime, a line that is off or a store that cannot be
written apply nothing, and the card says so. A grant that is not wider applies at once and
posts no card.

## Failure behavior

**The login is missing or incomplete.** Boot reads both values and logs what is wrong with
the login (`logins.Check.sentence`), or a warning that `public_url` is unset. Without the
secret every webhook request is answered 503; without the key no call is answered. Either
way the operator is told what is wrong, at most once an hour.

**The phone store cannot be opened.** Calls are answered with the seed voices and the
default models, and nobody is put through. A call gets no row and no transcript and nothing
is handed on; when it ends the operator gets its first line and "It was not recorded: the
phone store is unavailable." A store that fails during a call is treated the same from then
on. `read_call` and `phone_settings` answer `unavailable`.

**The answer fails.** A refused `accept` records `accept_failed` and runs no session. An
`accept` that got no answer is hung up first, since the provider may have taken it. A
shipped prompt that cannot be read stops the call before `accept`: it is logged and left to
the carrier's fallback. An unexpected error during a call hangs up and records `error`.

**The first line does not reach the operator.** After its retries the record is still
written and stored, but not handed on, and the row stays undelivered for the boot job below.

**The record cannot be handed on.** No bus, an assistant with no queue, or any error gives
the operator "The summary could not be delivered." after the first line, and leaves the row
undelivered. The `phone_boot_redeliver` job, 90 seconds after a boot, hands on every
undelivered call that ended within 24 hours: the first line first when it was never
delivered, then a stored record as it is, an unwritten one by the whole path. A silent reply
of the assistant's gives the operator INV-TRIG-021's fallback, naming the webhook
`casa-phone`.

**Casa stops during a call.** At the very start of the graceful stop the line is declared
stopping: from then on no call is admitted or accepted — one already looked up is recorded
`interrupted` and left to the carrier's fallback — no first line is sent, a wait between two
tries of one ends, and no record is put on the bus; such a call stays undelivered for the
next boot. Later, before the channels go down, every running call is hung up, and the calls
have up to five seconds together to record their outcome and transcript, a call the stop
hung up as `interrupted` (also one whose transfer was still pending when it was hung up); a
call whose hang-up the provider refused is no longer counted as hung up by the stop once the
refusal is back, and keeps the outcome it ends with after that.
A call whose accept landed after that hang-up is hung up again; what is left (a call that did
not close, a record being written) is cancelled within five more seconds, and the line's
client session closed. A call with no outcome that the running process did not write — cut
off by the stop or by a crash — is ended as `interrupted` at the next boot and handed on by
the job above.

## Extension points

**A new ability** is an `Ability` in `phone/abilities.py`, `plain` or `final`; both
triggers see it through the profile and it runs only through the action state. A runner
that marks its work pending must do so as its last act and must not raise afterwards.

**A new fixed line** is a key in `phone/prompt.py`'s `LINE_KEYS` and in every language of
the shipped `lines.yaml`; an operator's copy without it is then unusable as a whole until it
gains it.

**A schema change** appends a version to `phone/schema.py`'s migrations; opening an older
file runs the missing ones in one transaction.

`test-local/eval/phone_eval.py` runs the call's prompts and decision step against the
provider on scripted cases, by hand only, since it spends money.

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
- `casa/rootfs/opt/casa/defaults/phone/prompts/agent.md`
- `casa/rootfs/opt/casa/defaults/phone/prompts/helper.md`
- `casa/rootfs/opt/casa/defaults/phone/prompts/decision.md`
- `casa/rootfs/opt/casa/defaults/phone/prompts/record.md`
- `casa/rootfs/opt/casa/defaults/phone/prompts/lines.yaml`
- `casa/rootfs/opt/casa/trigger_registry.py::TriggerRegistry._check_webhook_collision`
- `casa/rootfs/opt/casa/tools.py::read_call`
- `casa/rootfs/opt/casa/tools.py::phone_settings`
- `casa/rootfs/opt/casa/tools.py::_phone_gate`
- `casa/rootfs/opt/casa/tools.py::_phone_grant_card`
- `casa/rootfs/opt/casa/tools.py::_phone_set_grant_if`

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
- `tests/test_phone_tools.py`
- `tests/test_phone_grant_card.py`
- `tests/test_phone_eval_cases.py`

**Related**
- [`architecture/live-voice.md`](../architecture/live-voice.md)
- [`architecture/logins.md`](../architecture/logins.md)
- [`architecture/contacts.md`](../architecture/contacts.md)
- [`architecture/webhook-delivery.md`](../architecture/webhook-delivery.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/config-reconciliation.md`](../architecture/config-reconciliation.md)
- [`reference/operator-options.md`](../reference/operator-options.md)
<!-- END SOURCEMAP -->
