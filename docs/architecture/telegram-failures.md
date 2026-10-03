# Telegram — failure behavior

What the Telegram channel does when its inputs are wrong, late, duplicated or unavailable. Split out of [`telegram.md`](telegram.md) (which owns the transports, authentication, the per-topic log and the keyboards) when that document reached the corpus ceiling (#1206); the contracts it refers to are defined there.

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

**A message arrives from an unconfigured chat.** A message is logged and dropped, text or not.
Note that leaving the chat id empty accepts other chats for text — the check is only as narrow
as the configuration. A non-text message from a sender who is not the operator, in the
configured chat or in any chat when none is configured, draws a refusal saying files are
accepted only from the operator, and nothing is downloaded; an empty chat id therefore accepts
no chat's files.

**A tap is stale, expired, for the wrong topic, or from the wrong user.** Absorbed with a
single best-effort acknowledgement; failures answering are themselves absorbed.

**Posting a keyboard fails.** The request is unregistered and its waiter resolves with a
delivery-failure outcome rather than hanging.

**Delivering a turn into a topic raises.** Logged, with a best-effort failure notice posted
to the topic. Cancellation is quiet by design. When the record is a live background job, the
raise also fails the job ([`background-jobs.md`](background-jobs.md)).

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
[`architecture/engagements.md`](engagements.md). A background job whose record is still live
fails when one of its turns draws that notice; any other returned turn hands the job to its
batch loop.

**A turn RETURNS having stopped at its turn limit.** Its result frame says so, and the
delivery task does not treat it as cut off. On every turn but a batch turn — one that
`deliver_system_turn`, which marks its turns explicitly, delivered into a background job's
engagement — it posts Casa's step-limit line once after whatever the turn posted, even when
it posted nothing, posts nothing over a settled terminal record, and logs one WARNING; the
limit takes precedence over the undelivered-text notice, so an operator's turn in a job topic
hands the job to its batch loop. A batch turn keeps the handling above. The contract is
INV-ENG-020 in [`architecture/engagements.md`](engagements.md).

