---
last_reviewed: 2026-08-23
---

# Engagement inbound disclosure

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a terminal outcome tells about inbound messages that died with the engagement: which spool
populations it quotes, when an evicted message is quoted because its notice never sent, how a
held ingress reservation carries its message's text, and what the disclosure's count may claim.
What a *successful* completion is refused over and what each driver counts are in
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md), which also holds
the driver-lifetime rule both halves' reads answer under. The terminal
transition itself and the finalization side effects behind it are in
[`architecture/engagement-finalization.md`](engagement-finalization.md); the outcome mark and the
notify obligation are in
[`architecture/engagement-terminal-telling.md`](engagement-terminal-telling.md).

## Mental model

**A message that dies with the engagement is disclosed, not swallowed.** Every terminal
outcome posts the messages no turn ever took up into the topic — all three spool text
populations, at any age, excerpted and counted, plus a count of pending ingress reservations. The claim it
makes is what the system can evidence: that no turn start was *recorded* for them before the
engagement ended. It does not claim they were never read, because that is not provable for a
message already handed to the CLI — the CLI can read the line and emit its init frame before
the relay processes it, and a cancellation landing in that interval would otherwise assert
something false about a message the agent did see.

## Contracts & invariants

**INV-ENG-017**: An ingress reservation taken for an operator message carries that message's text from the moment the message is accepted until that reservation is released or the engagement is terminally cancelled, on a ledger that survives a session teardown and never on the spool; the reservation-ledger contribution to a terminal disclosure is deduplicated by message id, limited by the same disclosure-count clamp as text-less reservations, then excludes ids whose printable spool envelope — queued, in flight, or evicted and awaiting its notice — is in that disclosure, so, while that exclusion read succeeds, it quotes at most one text per id; it does not deduplicate the unread or in-flight spool populations; and while the reservation count reads, the count and its "up to" hedge stay exactly as a text-less reservation would have produced them — when that read raises, every reservation text that was read still reaches the disclosure's bullets under the same excerpt budget and overflow line as every other population, and the disclosure states no number in place of one it cannot evidence, so a terminal notice never quotes more messages than it counts and never drops a text it read for want of a count.

**An evicted message is quoted by the terminal when its notice never sent.** A redirect at
the ordinary lane's cap evicts the newest ordinary message and owes its sender a threaded
notice; the row stays on disk until that notice is delivered, and it is in neither of the two
populations above, because both are keyed on an envelope owing no notice. The notice was that
message's whole disclosure, and it is best-effort — so when it never sent, the message was
durable on disk and quoted by nobody: its ingress reservation had been released the moment
its own enqueue resolved, long before the redirect arrived. The claude-code driver therefore
answers a third, disclosure-only population — queued, awaiting its notice, not the initial
task — and both terminal renderers quote it after the other two, as an exact unit of the
count. It reads through the same view as the others, so the file tier answers it too; it
joins the exclusion set below, so a reservation still held for the same message is not quoted
twice; and it feeds no veto, because nothing an agent can do clears an evicted envelope — a
completion refused on it could never be satisfied, and would never reach the disclosure it
was refused for. The capacity-drop notices the spool also retains are not in it: they are
notices about messages that were never spooled, and carry no text. A notice that finally
sends retires the row, and the next boot's reconcile keeps retrying it; that later notice is
a second telling of a message the terminal already quoted, never a first.

**A reservation carries its message's text, and its count stays an upper bound.** On the
claude-code side the handler is holding the operator's exact words when it reserves, so the
reservation carries them — keyed by the Telegram message id, on the same engagement-lifetime
ledger as the counter, never on the spool — and a terminal quotes what was accepted and never
reached the spool rather than only counting it. Each reservation records its own occurrence,
so two deliveries of one redelivered message are two entries and one release consumes one of
them; a successful enqueue removes nothing. Which occurrence a terminal actually prints is
decided when it prints, not when the message was persisted: the occurrences are collapsed to
one per message id, clamped to the same disclosure count a text-less reservation would have
produced, and then suppressed while an envelope that same disclosure is already printing
carries the same message id — so
one message is quoted once, from wherever it currently lives, and the words come back the
moment that envelope is consumed or pruned — an evicted envelope keeps printing, from the
spool, until its notice sends. Deciding at persist time instead was the
defect this rule replaced — a persist is evidence that expires, and it also cannot tell one
delivery of a message from another, so it removed a text that a second, still-held
reservation was the only remaining carrier of. Absence decides nothing: where no spool can be
read, or reading one fails, nothing is suppressed, because a duplicate bullet is a far
smaller harm than a silent one. Two occurrences of one message id still print one text,
because two identical bullets would claim two messages were lost; the spool's own
populations are not deduplicated, so two genuine envelopes sharing an id still print twice.
A message that arrives with no id, and every in-casa reservation, is
counted and not quoted: the in-casa reservation is taken for a system continuation whose text
does not exist anywhere yet, which is the contrast that makes the claude-code case the
achievable one. The count itself is unchanged, and so is its hedge. Because a reservation is
still anonymous to the count, it can alias a text the disclosure already excerpts: a message
is durably spooled before its reservation is released, and a terminal landing inside that
window sees the same message in both populations. A total that includes reservations
therefore reads "up to N", which is true in that window; a text-only total keeps the exact
claim. When the reservation count could not be read at all, the texts of the reservations it
would have counted are still quoted and the sentence above them names no number: "up to" would
be false whenever a text-less reservation the failed read would have counted exists, and "at
least" would be false when the exclusion read also failed and a held text sits beside its own
spool envelope — the disclosure gives up the number rather than risk a false one. One reservation is excluded
by construction: the one a recognized command (`/cancel`, `/complete`, `/silent`) holds for
itself while the handler processes it — classified at the reservation's birth, so the
exclusion holds under *every* terminal winner, not only the command's own finalize — still
counts toward the completion veto but is never disclosed as lost, because a command is
consumed by the handler and never delivered to the model. The operator's ungated complete
command finalizes past unread input deliberately (above), and it does so *disclosing* —
the topic post counts what it committed past, including foreign reservations.
The launch-death reporter folds the same projection into its own disclosure.

## Failure behavior

**An eviction notice never sends.** The evicted message is quoted by the terminal disclosure
from the spool, at any age, and the notice keeps retrying — through the pre-close drain and
the next boot's reconcile — until it is delivered or the topic is gone.

**A disclosure accessor raises or is absent.** These populations are read by the same terminal
hook, under the same per-accessor isolation rule, as the reads the completion gate refuses on:
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md) (INV-ENG-003) has
what one failing read costs — its own contribution and nothing else.

## Extension points

**A new driver that owns inbound state** should implement the text accessors, so a terminal
outcome can quote what died with its engagements. Which read feeds which population, and what
the count over them may claim, is stated above and in INV-ENG-017; this section adds no rule of
its own and states no second account of the renderer. What no terminal ever does is assert that
nothing was lost — the disclosure, like the refusal, is scoped to what a driver can evidence.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::_finalize_engagement`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.inbound_unread_texts`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.inbound_in_flight_texts`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.inbound_message_reservations`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.inbound_reservation_texts`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver._disclosed_spool_message_ids`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::_InboundSpool.disclosed_message_ids`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::_InboundSpool.evicted_pending_texts`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.inbound_evicted_pending_texts`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_unread_texts`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_in_flight_texts`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_message_reservations`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_reservation_texts`

**Tests**
- `tests/test_emit_completion_tool.py`
- `tests/test_claude_code_driver.py`
- `tests/test_launch_death_reporter.py`
- `tests/test_evicted_inbound_disclosure.py`
- `tests/test_evicted_inbound_regressions.py`

**Related**
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/engagement-terminal-telling.md`](../architecture/engagement-terminal-telling.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
<!-- END SOURCEMAP -->
