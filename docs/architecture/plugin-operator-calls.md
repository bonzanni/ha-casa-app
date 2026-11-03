---
last_reviewed: 2026-10-31
---

# Plugin operator calls and run admission

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a plugin's own code may ask Casa while one of its declared tools runs: whom the call
serves (its binding), run admission — whether the operator's own words asked for the
browsing runs the plugin is about to start — and its confirmation's state, a continuation
turn for a run, a delivered slot's outcome, and the check of one browsing action. It covers
the `callsCasa` declaration, the record the admission hook makes, the six internal routes,
the binding store, the confirmation card and the Stop it keeps, the admission decision and the action check. The continuation turn those routes queue,
and the driver sweep, are [`plugin-continuations.md`](plugin-continuations.md); the result
contract and the hooks this extends are
[`plugin-result-contract.md`](plugin-result-contract.md); the requester chain and the
operator's words a call inherits are [`specialist-handoff.md`](specialist-handoff.md).

## Mental model

**The plugin's code asks; the model only calls the tool.** A tool whose
`casa.resultContract` entry carries `"callsCasa": true` is recorded when the admission
hook admits the call (`operator_calls.CALLS.open`), with the call's grant identity — the
operator and chat, derived exactly as an approval's is — the operator's own words for the
turn, the chain of requesters, the turn's key and, on a continuation turn, its binding.
The chain always ends with the agent making the call (`handoff.child_chain` over the turn's
origin and the call's role): a delegated run's origin already carries it, but an engagement
turn runs under the ambient origin of the resident's turn that launched it, which does not.
During the call the plugin's code presents its session's broker client id
(`CASA_BROKER_CLIENT`) on the internal socket; the call is found by that id alone and must
be the client's only recorded call, the deposit route's rule. The record is forgotten at
the call's PostToolUse or PostToolUseFailure, or an hour after it opened. A call is
recorded only for the operator as configured now (`operator_calls.is_configured_operator`,
the Telegram channel's one operator rule): a turn with no grant identity — voice, a
webhook, a schedule — or a sender the accept-all mode lets in has the call denied before
it runs, so the routes never serve a call with no operator behind it.

**A binding names whom the work serves, for as long as it takes** (`plugin_bindings`).
`POST /internal/broker/requester` answers the call's binding: an opaque `casa-bind-…`
reference, the requester (the chain's element before the plugin's role), its kind
(`operator_resident` when the resident asked directly, else `specialist`) and the chain.
One binding is minted per plugin and turn; a continuation turn answers the binding it was
queued for. Bindings are durable (`/data/plugin-bindings.json`, `0600`) because a run can
outlive a restart, keep only plain origin markers — never the operator's words — and age
out thirty days after their last use. A binding is honoured only for the plugin and
operator it was minted for.

**Admission reads his words and nothing else.** `POST /internal/broker/run-admission`
takes the runs the plugin wants to start — each with its spec digest, mode, task line and
the sites it opens, each site with its grant and the display name the plugin's own
registry gives it. With operator words on the call, one clean-context model call
(`run_admission._ask_model`: Haiku, no tools, no settings sources, no session) reads only
those words and returns the sites they name. The comparison is mechanical
(`run_admission.run_matches`): an account run is named when each of its account-grant
sites is a site he named — the same name once case, spacing and punctuation are ignored
(`site_named`: "Run Pod" names "RunPod"; "Chase Center" does not name "Chase", "Booking.com"
does not name "ING", "GitHub" does not name "GitHub Enterprise"; the reading is asked for
the bare name, "UniFi" for "my UniFi gateway") — and an open-web run, holding nothing of
his, when his words ask for browsing at all. The kind of task is shown, not compared:
admission stays site-based because every change a run would make is asked per action by
the action check below. Every run named
answers `admitted`, with one `origin_ref` per spec digest. Each request carries a
`request_key` the plugin chooses; a key this plugin and operator used before gets its first
answer again — no second reading and no second card — and requests with one key are decided
one at a time, so a retry that overlaps the first waits for it and gets its answer. Otherwise — no words (a
continuation, a narration, a tap), a reading that names nothing ("collect them"), a
failed reading, or one run he did not name — Casa posts ONE question listing every run,
one line each (`TelegramChannel.deliver_operator_question`, the resident-ask keyboard in
the `ask:<chat>` scope) and answers `confirmation_pending` with its id. His tap settles it
— "Start" admits all of them, anything else none — and so does the hour's timeout; either
way Casa queues a continuation of `admission:<id>`, whose plugin call reads the state with
`POST /internal/broker/run-admission-status`. Every answer — by his words or by his tap —
is kept in `/data/plugin-admissions.json` (`0600`) for thirty days after it was given and
returned identically on every status read and key replay, across restarts; a card a
restart left behind reads `expired`. Each decision logs its model time and total
time, the measurement the design asks for. When the call carries his words, admission keeps
them for each requested run's spec digest — admitted by them or put to the card — in
`action_judgement.WORDS`: memory-only, at most 500 for a day each, keyed by plugin,
operator and digest, because nothing durable keeps his words.

**An admitted request's card carries its Stop.** When his tap on Start admits the runs, the
settled card keeps one button, Stop (`run_admission.stop_button`, callback data
`v1|run_stop|<cid>|0`): `operator_calls.post_question`'s `after_choice` keeps that keyboard
for the Start choice (`edit_dm_message`'s `buttons`), and that choice's `on_settle` runs
before the edit that shows it, so the admission is already `admitted` — with the card's
message id and its settled text (`Confirmation.card_message_id`, `card_text`) — when the
button appears. When his words admitted the runs there is no start card, so one status card
(`compose_status`: `Browsing now:` over the same run lines, with the same Stop) is posted
beside the answer in its own task, never awaited by it (`_spawn_status_card`,
`TelegramChannel.deliver_run_card`); when it lands its message and text are recorded with
the admission (`Confirmations.set_card`), and a failed post is only logged. The Stop's tap
reads the admission by its id alone (`Confirmations.peek`) and compares the tapper itself;
what it does is [`plugin-stop.md`](plugin-stop.md).

**The action check judges one browsing action, bound to the bytes it judged.**
`POST /internal/broker/action-judgement` takes `{"kind", "packet", "spec_digest"}`: the
kind (`action`, or `rejudge` at a tapped question; `login_item`, `signin` and `sign_in` for the
judgements Casa Browser starts itself, each answered from its packet alone; `sign_in`: whether
the page is the site's own sign-in, `{"verdict": "sign_in" | "not_sign_in", "fields", "reason"}`,
`fields` naming the planned field ids the username and the password go into), the app's packet as its own JSON text
relayed as a string, and the run's spec digest or null. Casa hashes exactly the UTF-8
bytes it received and runs one clean-context call (`action_judgement._ask_check`: Opus
5.5 at effort high, fixed whatever the model map says; the measured rulebook for the kind,
`rulebooks/*.txt`, byte for byte, as the system prompt; no tools, no settings sources, no
session). For `action` the model reads `{"context": {"operator_words": …}, "packet": …}`,
the words being the ones kept for this plugin, operator and digest; for `rejudge` the
packet alone. It answers `{"verdict", "packet_sha256", "latency_ms"}`, so the caller can
bind the verdict to the exact packet it froze. The log line carries the kind, the
verdict, the category, the latency and the digest's first twelve characters — never the
packet, the words or the reason, because a packet holds masked page text of his accounts; the
SDK's own payload diagnostics are dropped for the call (`sdk_logging`'s context-scoped filter).

**A continuation is queued, a delivery is looked up.** `POST /internal/broker/continue`
queues one continuation turn for a run of a binding the plugin owns (reason
`operator_answer`, `next_run` or `sweep`). `POST /internal/broker/delivery-status`
answers what became of a delivered slot's deposit by its reference — `delivered` or
`withheld` once the result hook decided, `pending` while it is escrowed, else `unknown` —
for the plugin artifact and operator it was posted for (`result_broker.DELIVERY_OUTCOMES`), so the
plugin records Casa's outcome rather than its model's report.

## Contracts & invariants

**INV-PLUG-051**: Every operator-call route serves only the unique in-flight `callsCasa` call of the presenting broker client, recorded at that call's admission with the call's grant identity, and acts only on bindings, confirmations and delivery outcomes minted for that call's plugin and operator.

Enforced by `OperatorCalls.find` (zero calls `no_call_in_flight`, two `ambiguous_call`),
by the admission hook deriving the identity through `authz_grants.resolve_grant_identity`
and denying the call when there is none, by `BindingStore.get`,
`Confirmations.get` and `DeliveryOutcomes.status` comparing plugin and operator, and by
the resident-ask tap handler's actor check on the confirmation card. No request body names
an identity, a requester or a chain; Casa reads them from the record.

What it does not cover: a plugin whose own code misuses what it is told — the plugin is
the trust boundary the result contract already accepts; and a call whose PostToolUse
never arrives, which stays answerable until the hour's cap.

**INV-PLUG-052**: Run admission sends the clean-context reading only the operator's own current words, admits with no prompt only when every requested run is named by what those words ask for, and otherwise admits nothing unless the operator taps Start on one question listing every requested run.

Enforced in `run_admission.request_admission`: `_ask_model` is called with
`OperatorCall.operator_words` and nothing else, and not at all when there are none; a
reading that cannot be parsed or a model failure reads as nothing named; the question's
text is `compose_question` over the validated fields; only the tap's first option settles
`admitted`.

What it does not cover: whether the site names a plugin presents are the sites its run
then opens — that is the plugin's code checking its own registry; two registered sites
whose labels are the same words; meaning, which the model judges and which can vary
between calls (a miss costs a tap, measured by `test-local/eval/run_admission_probe.py`);
and a card a restart left behind, which reads `expired`.

**INV-PLUG-055**: The judgement route returns a verdict only for the packet bytes it received, with their SHA-256, and any failure to obtain a parsed verdict is check_unavailable, never a verdict.

Enforced in `action_judgement.judge`: the digest is taken over the received string's
UTF-8 bytes before anything parses them, and a string with a repeated key is refused, so
the object the model reads is the one hashed; a timeout (90 s), an exception, an error or
refusal message from the CLI, a turn that did not finish its answer (no terminal result,
or one outside the allow-lists of a successful, `end_turn`, `completed` turn — a cancelled
or cut turn), a reply with no JSON object, a verdict outside the kind's
set, an `ask` without a well-formed question, or any field past its bound (the question's
text 600 characters, the confirm label 32, at most three extra options of 60 and six
highlights, the reason 600) answers `check_unavailable`. Bounds are checked, never clipped,
since a clipped question could lose a material fact; they sit above every measured reply, so
a well-formed check never costs a retry.

What it does not cover: whether the verdict is right — the model judges; whether the packet describes the page — the app builds it; and the
operator's words after a restart, which the check then lacks, as a continuation's does.

## Failure behavior

**Telegram is down or the card fails to post.** Admission answers `refused` with
`not_delivered`, nothing is admitted, and a replay of the key answers the same. **The operator does not answer.** The card
expires after an hour, reads `expired`, and the run is continued so the plugin can say so.
**Casa restarts.** Call records, delivery outcomes and the words kept for the action check
are lost; bindings and admission answers survive, and a card still waiting reads `expired`.
**The check cannot answer.** The route answers `check_unavailable`, never a verdict in its
place. **A malformed body.** A typed `error` (`bad_json`, `bad_client`, `bad_runs`,
`bad_mode`, `bad_digest`, `bad_task`, `bad_sites`, `bad_request_key`, `bad_run_ref`, `bad_reason`,
`unknown_binding`, `bad_kind`, `bad_packet`), with nothing posted, queued or judged.

## Extension points

**A new kind of run** is admitted here by its sites alone; what it may change on the
operator's account is decided per action by the action check, never at admission.
**A new kind of check** is a new `kind`, a new measured rulebook file under `rulebooks/`
and its own verdict set in `action_judgement.parse_reply`.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/operator_calls.py`
- `casa/rootfs/opt/casa/action_judgement.py`
- `casa/rootfs/opt/casa/run_admission.py`
- `casa/rootfs/opt/casa/plugin_bindings.py`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.deliver_operator_question`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.deliver_run_card`
- `casa/rootfs/opt/casa/result_broker.py::DeliveryOutcomes`

**Tests**
- `tests/test_operator_calls.py`
- `tests/test_run_admission.py`
- `tests/test_action_judgement.py`

**Related**
- [`architecture/plugin-result-contract.md`](../architecture/plugin-result-contract.md)
- [`architecture/plugin-continuations.md`](../architecture/plugin-continuations.md)
- [`architecture/specialist-handoff.md`](../architecture/specialist-handoff.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
<!-- END SOURCEMAP -->
