---
last_reviewed: 2026-11-02
---

# Memory scoping

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Which stored facts a given reader gets back: the read-clearance model — per channel, per
Telegram sender via the stamped origin, failing closed — the clearance an engagement
inherits from its creating turn and the monotonic clamp steering applies to it, the
procedural-epoch scoping on the executor archive, and the mental-model overlay, which is
shown only at the highest clearance, together with the mental models Casa declares for it.
How a recalled fact is rendered, what a
caller is told when the store cannot answer, and what a caller may claim from what comes
back are [`architecture/memory.md`](memory.md); the write side — write trust and
retention — is [`architecture/memory-lifecycle.md`](memory-lifecycle.md), and tier
labelling is [`architecture/memory-labelling.md`](memory-labelling.md).

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
record's own launch materials (task, brief, context, world-state, a fresh job's recorded
launch context and the text of its pending completion, INV-BGJOB-007 — every later render re-derives from the record, and evicting the session
while the record still carried them would re-import them), tears the live session down, and drops the resume pointer — recording the evicted session on the record in that same write, so the terminal transcript pass (INV-ENG-022) deletes it once the engagement ends. Every
resume path — the steering turn itself, a system continuation, boot replay — refuses to
resume while the rebuild is pending and establishes a fresh session at the clamped floor
instead; while it is pending, the old process's tool calls are refused at both dispatch
choke points (the internal socket and the in-process fence), so it cannot read or launch
children carrying its pre-clamp task. A read already in flight when the clamp lands is
re-filtered at the new clearance after it returns, and a launch caught mid-start aborts
rather than deliver a prompt rendered from pre-clamp materials.

The teardown never cuts an `in_casa` turn that is already running: it waits for that turn to
end. The wait does not run under the per-topic handler lock, so a `/cancel` still interrupts
the turn. When a turn is running, the ingress skips the eager teardown and the steering
message's delivery task waits for the turn, then runs the rebuild itself. That rebuild drops
the session pointer again, since the finished turn can have written it back. A turn that was
waiting for the turn lock when the clamp landed is refused before it is accepted and is sent
into the fresh session instead. A batch of a background job is therefore never ended by a
downgrade ([`background-jobs.md`](background-jobs.md)).

What the eviction deliberately does not do: content already posted to the topic stays
posted, and a turn already running at clamp time completes into the topic, its tool calls
refused while the rebuild is pending —
engagement topics are readable by every supergroup member regardless, so both are
disclosures the topic already carried. A nested engagement spawned before the clamp keeps
its own record's clearance (steering its topic clamps it the same way). And it only moves a
record that carries a stamped clearance: an engagement running before per-sender markers
existed has none, so it keeps channel-keyed clearance — on Telegram, private — until it
finishes.

**The mental-model overlay is shown only at `private`, because it cannot be tier-filtered.**
A *mental model* is a standing question the memory backend re-answers from the bank on its
own schedule; its answer is a bank-wide summary, not individually tagged facts, so there is no
way to redact part of one. On a fresh session at `private` origin clearance the turn reads the
bank's models (with their content — the backend's list returns metadata only unless asked)
and pushes the ones that have content into the prompt. Each renders under its name with
`refreshed <weekday> <day> <month> <year>` — the date of its last refresh in the operator's
timezone, the same form as a recalled fact's recorded date — and, when its automatic refreshes
are paused because the last one failed (the backend's own rule: a failure later than the last
success, or a failure with no success at all), says that it may be out of date. A model with no
content yet (its first refresh still running) or no readable refresh date is left out: the
overlay never shows a summary whose age it cannot state. A non-empty overlay opens with one
line framing it as memory-derived leads: not live state, and not a complete list, so nothing
missing from it is evidence of absence. A missing bank (after a wipe, before the first save) is
simply no overlay; any other failure to read it is logged and the turn runs without it.

**Casa declares two of those models and keeps them reconciled.** `casa-open-commitments` asks
for the operator's open commitments and follow-ups with their stated dates and deadlines,
leaving out the assistant's own reminders and briefings; it refreshes on the backend's daily
schedule at 05:00 UTC. `casa-operator-profile` asks for a profile of the operator and their
household — who they are, the people around them, how they like to be addressed, standing
preferences and routines with their cadence, each item with the date it was last stated —
leaving out health, money and other people's private matters; it refreshes after the backend
consolidates new memories, at most once a day. Both are untagged, so each reads the whole
bank. The exclusions are written into the question, not enforced: they shape what the backend
writes and filter nothing. A reconcile pass makes the backend match the declarations: it
creates a missing model, rewrites a definition whose declared fields drifted (fields Casa does
not declare are left as they are), and deletes any model under the **reserved `casa-`
prefix** that Casa no longer declares — so a model the operator creates under that prefix is
deleted at the next pass, while every other id is never touched. The pass reads one page of
up to 1,000 models, and when the backend reports more than it returned, it deletes nothing. A
model whose definition was rewritten, or whose automatic refreshes are paused, gets one
explicit refresh in that pass, because rewriting does not refresh and only an explicit refresh
resumes a paused model. A pass runs in the background at boot and after every completed wipe,
one at a time in arrival order; boot and the wipe never wait for it, and shutdown cancels it
before the memory client closes.

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
tool fence refusing a flagged record; by the `in_casa` turn lock, under which a waiting turn
re-checks the flag and the live session before it is accepted; and by the post-await re-filter and the drivers'
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

**INV-MEM-020**: The mental-model overlay is pushed only on a fresh session at `private` origin clearance, and every model it renders carries its name and the date of its last refresh in the operator's timezone.

Enforced by the turn's load plan, which pushes the overlay on a fresh session only, behind the
`private`-only overlay gate; by the backend's overlay read, which asks the list for content;
and by the overlay render, which emits a model only together with its name and its local
refresh date and leaves out a model whose refresh date it cannot read.

What it does not cover: what the summaries *say*. They are written by the backend's own model
from the whole bank; their dates are the last refresh, not when any fact in them was stated,
and the declared exclusions are part of the question rather than a filter. Nor does it cover
the declared models' reconcile, which is best-effort by design and pinned by ordinary tests:
a pass that cannot reach the backend leaves the models as they were until the next boot or
wipe.

## Failure behavior

**A mental-model reconcile pass fails.** If the backend's model list cannot be read —
unreachable, timing out, a server error, an unrecognisable answer — the pass writes nothing
and logs it. A missing bank is not a failure: its list answers 404 and the pass creates both
models, which recreates the bank. One model's create, rewrite, refresh or delete failing is
logged and the pass moves on; a rewrite that failed is not followed by a refresh. A refresh
whose connection drops is not resent — the pass cannot tell whether the backend accepted it.
Nothing retries inside the process: if the backend comes up after Casa, the declared models
wait for the next boot or wipe.

**A clamp fails to persist.** The clamp is applied in memory first, so a failed persist
leaves this process correctly clamped while a restart would restore the higher tier
(logged as security-relevant).

Recall failures — an unavailable backend, a malformed envelope, a tripped breaker — are
[`architecture/memory.md`](memory.md)'s; failures on the write side are
[`architecture/memory-lifecycle.md`](memory-lifecycle.md)'s.

## Extension points

**A new declared mental model** is one more entry in the declarations, under the reserved
`casa-` prefix. Its first reconcile creates it; removing the entry later deletes it from the
backend at the next pass.

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
- `casa/rootfs/opt/casa/delegated_memory.py::delegated_recall`
- `casa/rootfs/opt/casa/tools.py::_origin_clearance_markers`
- `casa/rootfs/opt/casa/tools.py::inherit_origin_markers`
- `casa/rootfs/opt/casa/tools.py::_engagement_rebuild_fence`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.lower_origin_clearance`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.clear_context_rebuild_pending`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.handle_update`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._resume_and_ready`
- `casa/rootfs/opt/casa/internal_handlers.py::_make_internal_tools_call_handler.handler`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.start`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.start`
- `casa/rootfs/opt/casa/casa_core.py::replay_undergoing_engagements`
- `casa/rootfs/opt/casa/semantic_memory.py::render_mental_models`
- `casa/rootfs/opt/casa/semantic_memory.py::mental_model_refresh_paused`
- `casa/rootfs/opt/casa/hindsight_memory.py::HindsightSemanticMemory.profile`
- `casa/rootfs/opt/casa/hindsight_memory.py::HindsightSemanticMemory.reconcile_mental_models`
- `casa/rootfs/opt/casa/mental_models.py::reconcile`
- `casa/rootfs/opt/casa/mental_models.py::schedule_reconcile`
- `casa/rootfs/opt/casa/mental_models.py::drain_reconcile_tasks`

**Tests**
- `tests/test_sensitivity.py::test_readable_tiers_is_clearance_and_below`
- `tests/test_sensitivity.py::test_clearance_docstring_states_the_fail_closed_direction`
- `tests/test_executor_epoch.py`
- `tests/test_mental_models_1126.py`

**Related**
- [`architecture/memory.md`](../architecture/memory.md)
- [`architecture/memory-lifecycle.md`](../architecture/memory-lifecycle.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/memory-wipe.md`](../architecture/memory-wipe.md)
<!-- END SOURCEMAP -->
