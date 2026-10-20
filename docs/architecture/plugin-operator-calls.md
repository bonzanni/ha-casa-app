---
last_reviewed: 2026-10-18
---

# Plugin operator calls and run admission

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a plugin's own code may ask Casa while one of its declared tools runs: whom the call
serves (its binding), run admission — whether the operator's own words asked for the
browsing runs the plugin is about to start — and its confirmation's state, a continuation
turn for a run, and a delivered slot's outcome. It covers the `callsCasa` declaration, the
record the admission hook makes, the five internal routes, the binding store, the
confirmation card and the admission decision. The continuation turn those routes queue,
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
his, when his words ask for browsing at all. The kind of task is shown, not compared: in part 1 every run only reads, and what
a run may do on a logged-in site is bounded by that site's own approvals. Every run named
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
time, the measurement the design asks for.

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

## Failure behavior

**Telegram is down or the card fails to post.** Admission answers `refused` with
`not_delivered`, nothing is admitted, and a replay of the key answers the same. **The operator does not answer.** The card
expires after an hour, reads `expired`, and the run is continued so the plugin can say so.
**Casa restarts.** Call records and delivery outcomes are lost; bindings and admission
answers survive, and a card still waiting reads `expired`. **A malformed body.** A typed `error` (`bad_json`, `bad_client`, `bad_runs`,
`bad_mode`, `bad_digest`, `bad_task`, `bad_sites`, `bad_request_key`, `bad_run_ref`, `bad_reason`,
`unknown_binding`), with nothing posted or queued.

## Extension points

**A new kind of run** that is not a read — a change on the operator's account or a
commitment — is not admitted here; it needs its own exact, per-instance approval.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/operator_calls.py`
- `casa/rootfs/opt/casa/run_admission.py`
- `casa/rootfs/opt/casa/plugin_bindings.py`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.deliver_operator_question`
- `casa/rootfs/opt/casa/result_broker.py::DeliveryOutcomes`

**Tests**
- `tests/test_operator_calls.py`
- `tests/test_run_admission.py`

**Related**
- [`architecture/plugin-result-contract.md`](../architecture/plugin-result-contract.md)
- [`architecture/plugin-continuations.md`](../architecture/plugin-continuations.md)
- [`architecture/specialist-handoff.md`](../architecture/specialist-handoff.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
<!-- END SOURCEMAP -->
