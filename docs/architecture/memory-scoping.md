---
last_reviewed: 2026-08-18
---

# Memory scoping

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Which stored facts a given reader gets back: the read-clearance model — per channel, per
Telegram sender via the stamped origin, failing closed — the clearance an engagement
inherits from its creating turn and the monotonic clamp steering applies to it, and the
procedural-epoch scoping on the executor archive. How a recalled fact is rendered, what a
caller is told when the store cannot answer, and what a caller may claim from what comes
back are [`architecture/memory.md`](memory.md); the write side — write trust, tier
labelling, retention — is [`architecture/memory-lifecycle.md`](memory-lifecycle.md).

## Mental model

**Read clearance is per channel and fails closed** — and on Telegram, per *sender*. Known
channels are mapped explicitly; an unrecognised one gets the *least* sensitive clearance.
The fail direction is the security control here: an unknown surface must read less, never
more, and a test pins the function's docstring to that direction so prose and behaviour
cannot drift apart again. The Telegram channel's mapped clearance (private) is not
route-wide: the ingress stamps a per-sender origin clearance — private only for the sender
whose id matches the configured operator chat id, public for anyone else the accept-all
mode lets in — and origin-aware resolution honors that stamp, failing closed to public
when a telegram-marked turn carries a missing or malformed one. Both ways into a Telegram
turn stamp it: a message and a button tap.

**An engagement reads at the clearance of the turn that created it.** This needs stating
because the mechanism is not the obvious one: an engagement's own tool calls arrive over the
internal socket, which binds the engagement record but no ambient turn origin — so a
clearance resolved from the ambient origin alone would find nothing and fall through to the
channel default, handing an engagement started by a low-clearance sender the operator's
private tier. The engagement's recorded origin carries the markers its creating turn was
stamped with, and that is what its reads resolve against: its own recall, the prior-
engagement archive injected into an executor's prompt at launch, the engager-side context a
specialist can query, and a nested engagement it spawns, which inherits rather than
re-deriving. An engagement whose record predates this (or came from an origin that stamps no
route) keeps the channel-keyed behaviour.

Inheritance at creation is only half the rule, because an engagement is *steerable*: anyone
in the engagement supergroup can direct it by messaging its topic. So the recorded clearance
is also a **monotonic floor** — a steering turn clamps it down to that sender's clearance and
never raises it back, which makes the property "an engagement never reads above the
least-privileged person who has taken part in it". The clamp is deliberately one-way: two
people steering concurrently can only drive it further down, and the operator returning to
their own engagement does not restore what a lower-clearance participant has already seen.

A clamp that moves the record does not stop at future reads — it evicts what the session
already holds. The transcript and the launch-injected archive were built at the old tier, and
a lower-clearance steerer could simply ask the engagement to restate them, so the same locked
ingress pass that lowers the record also: durably marks the context for rebuild (the flag and
the clamp persist in one write, so a crash between them cannot happen), withholds the
record's own launch materials (task, brief, context, world-state — every later render
re-derives from the record, and evicting the session while the record still carried them
would re-import them), tears the live session down, and drops the resume pointer. Every
resume path — the steering turn itself, a system continuation, boot replay — refuses to
resume while the rebuild is pending and establishes a fresh session at the clamped floor
instead; while it is pending, the old process's tool calls are refused at both dispatch
choke points (the internal socket and the in-process fence), so it cannot read or launch
children carrying its pre-clamp task. A read already in flight when the clamp lands is
re-filtered at the new clearance after it returns, and a launch caught mid-start aborts
rather than deliver a prompt rendered from pre-clamp materials.

What the eviction deliberately does not do: content already posted to the topic stays
posted, and a turn already running at clamp time may still complete into the topic —
engagement topics are readable by every supergroup member regardless, so both are
disclosures the topic already carried. A nested engagement spawned before the clamp keeps
its own record's clearance (steering its topic clamps it the same way). And it only moves a
record that carries a stamped clearance: an engagement running before per-sender markers
existed has none, so it keeps channel-keyed clearance — on Telegram, private — until it
finishes.

## Contracts & invariants

**INV-MEM-003**: An unrecognised channel reads at the least-sensitive tier.

Enforced by the channel-clearance lookup's default. The direction matters: an unknown surface
sees less, not more.

What it does not cover: origin-aware resolution is narrower than it sounds. Resident
auto-recall and the recall tool resolve clearance from the stamped origin
(`clearance_for_origin()`), so a webhook turn's clearance there depends on its declared
origin. Delegated recall honours a route its caller passes, but falls back to channel-keyed
clearance for every caller that passes none — check the call site before assuming a
delegated read is origin-filtered.

**INV-MEM-008**: An engagement's reads resolve clearance from the origin markers its own record carries, and a steering turn from a lower-clearance sender clamps those markers down permanently.

Enforced in two places that have to agree: where the markers are resolved for an
access-control decision — the ambient origin wins when it has a route of its own (an
in-process delegated turn), otherwise the bound engagement record supplies them — and at the
engagement-topic ingress, which lowers the record's stamped clearance to each steering
sender's before their turn is delivered. Every read an engagement can perform resolves
through the first: its own recall, the executor archive injected at launch, the engager-side
context, and a nested engagement it spawns.

What it does not cover: a record carrying no markers falls back to channel-keyed clearance,
which on Telegram is private — so this tightens what a *newly* stamped origin can reach, and
does not retroactively downgrade engagements created before the markers existed. The clamp
also cannot un-share what was already disclosed before a lower-clearance sender arrived.

**INV-MEM-011**: A clearance downgrade durably invalidates the engagement's session context in the same write that lowers the record — until a fresh session is established at the clamped floor, no path resumes the old session and the record's tool calls are refused at both dispatch choke points.

Enforced by the clamp setting the rebuild flag and withholding the record's launch materials
under one strict persist; by the resume core and boot replay routing a flagged record to a
fresh-session rebuild instead of a resume; by the internal-socket handler and the in-process
tool fence refusing a flagged record; and by the post-await re-filter and the drivers'
last-instant launch gates, which close the calls already in flight when the clamp lands.

What it does not cover: output of a turn already running at clamp time, content already
posted to the (group-readable) topic, and a pre-clamp nested child's own record — the
deliberate residuals listed above. The completion tool stays reachable while the rebuild is
pending, its output being in-flight-turn residual.

**INV-MEM-015**: The lessons block injected into an executor's prompt at launch carries only summaries stamped with that launch's own procedural epoch; a summary is stamped at finalize with the epoch persisted on its engagement's record at create, never with the finalize-time definition.

The epoch is a digest over the procedural bytes *as read at launch* — the role-artifact
checksum, the resolved prompt template's path and just-read bytes, every doctrine file,
and (for workspace-driven executors) the *source* workspace instruction file,
pre-interpolation, whichever of the template or plain-copy forms will be consumed.
Launch-specific substitutions are never inputs: rendered bytes embed the task and the
recalled memory itself, which would make every epoch unique and the filter circular.
Workspace provisioning re-reads those sources, so after provisioning the driver recomputes
the digest and aborts the launch on a mismatch rather than stamp an engagement with an
epoch it did not run under. Filtering is client-side between recall and render (the tag
filter cannot narrow), drops foreign-epoch *and* unstamped hits alike — a lesson that
cannot prove it was learned under the current doctrine is exactly what the filter exists
for — and the injected block subordinates itself to the doctrine files in as many words.

What it does not cover: `query_engager` is deliberately unfiltered (an answer to "what
happened last time" is not injected procedure); a same-epoch lesson that is merely wrong
survives, tempered only by the subordination line; epochs never expire lessons — a
doctrine rollback to byte-identical state resurrects that epoch's lessons, by
construction; and the recheck reads the current sources, not the bytes provisioning
consumed, so an edit that lands *and reverts* inside the provisioning window can still
mislabel — accepted: it needs two opposing edits within milliseconds, and closing it
would thread an immutable byte snapshot through the whole provisioning path.

## Failure behavior

**A clamp fails to persist.** The clamp is applied in memory first, so a failed persist
leaves this process correctly clamped while a restart would restore the higher tier
(logged as security-relevant).

Recall failures — an unavailable backend, a malformed envelope, a tripped breaker — are
[`architecture/memory.md`](memory.md)'s; failures on the write side are
[`architecture/memory-lifecycle.md`](memory-lifecycle.md)'s.

## Extension points

**A new channel** needs an explicit clearance entry, or it reads at the least-sensitive tier
by default. Write access is a separate, deliberate addition, and the retention list is a
third ([`architecture/memory-lifecycle.md`](memory-lifecycle.md)).

**Scoping what a recall surfaces** is a caller-side decision: the backend's only content
filter is the sensitivity tags, so a caller that must narrow further (the executor archive
drops lessons from another doctrine epoch — INV-MEM-015) filters the typed hits between
recall and render, never by adding tags to the request (an added tag *broadens* an
any-match filter).

**One archive recall per launch.** A recall is relevance-ranked and bounded, so two are not
two copies of one answer: an executor handed both is handed two accounts of what happened
last time. A memory-enabled launch recalls once — into the first turn for an in-process
executor, into the workspace instructions for one with a workspace, never both. Nothing
after the launch recalls again ([`engagements.md`](engagements.md)).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/sensitivity.py::clearance_for_channel`
- `casa/rootfs/opt/casa/sensitivity.py::clearance_for_origin`
- `casa/rootfs/opt/casa/executor_epoch.py::compute_procedural_epoch`
- `casa/rootfs/opt/casa/executor_epoch.py::epoch_application_tag`
- `casa/rootfs/opt/casa/executor_epoch.py::make_archive_epoch_filter`

**Tests**
- `tests/test_sensitivity.py::test_readable_tiers_is_clearance_and_below`
- `tests/test_sensitivity.py::test_clearance_docstring_states_the_fail_closed_direction`
- `tests/test_executor_epoch.py`

**Related**
- [`architecture/memory.md`](../architecture/memory.md)
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
<!-- END SOURCEMAP -->
