---
last_reviewed: 2026-08-07
---

# Plugins

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a plugin becomes something an agent can use: the registry that assigns it, the store
that holds its bytes, what pins its identity, and what stands between a plugin's tools and
an operator's approval. It does not cover authoring a plugin, nor the MCP protocol itself.

## Mental model

Two things are easily conflated and do different jobs. **The registry is the authority on
what is assigned to whom.** **The store is content-addressed storage for the bytes.** A
plugin is usable at runtime only when a valid registry entry and a valid stored artifact
agree — and, for resident and specialist sessions, when the environment variables its
`.mcp.json` references are resolved in the effective environment (INV-PLUG-008): an
undefined `${VAR}` would otherwise reach the plugin's MCP server as the literal string,
which then runs "successfully" with placeholder credentials.

**The artifact id is not a content hash.** It is computed over source coordinates —
repository, resolved revision, subdirectory, and the registry name — and nothing else. Two
different byte trees fetched for the same coordinates produce the *same* artifact id. Bytes
are pinned separately, by a checksum recorded in the artifact's own metadata and verified
when the artifact is validated. Reasoning about integrity from the artifact id alone is the
most common way to be wrong here: identity answers "which plugin is this", and a separate
checksum answers "are these the expected bytes". Know the checksum's trust boundary, though:
its expected value lives in the artifact's own metadata file — which is excluded from the
hash and recorded nowhere else, not in the registry and not signed. It detects drift of the
tree relative to the artifact's internal metadata, not tampering: a writer able to change the
store can change bytes and recorded checksum together, and nothing recomputes a digest from
the source.

**Resolution is snapshot-based, but not everything is cached.** Registry parsing and deep
artifact validation happen when a snapshot is built; the checksum verdict is cached and never
recomputed by resolution. Resolution does, however, re-read the artifact's `plugin.json` on
every resolve, and the resolved paths hand the live artifact tree to the SDK without
revalidation. So a *registry* edit is invisible until a reload — deliberate, because
validation is expensive and a long-lived agent should not see a half-written registry — while
a mutation *inside a stored artifact* can become visible immediately and evades the cached
checksum until an explicit verification, the next snapshot reload — or an interactive
specialist resume, which deep-validates its recorded artifacts automatically. Executor
resume checks only that the recorded directories still exist.

**Plugin failures degrade; they do not stop the container.** The boot path writes health data
and exits successfully whatever it finds. One broken plugin costs that plugin. The health
report's operator-DM dedup (fingerprints already notified) is a read-merge-write over one
file from both the event loop and worker threads; a process-wide lock serializes it, and the
regeneration reads the previous report inside that critical section, so a regeneration
racing a just-delivered notification cannot erase its marker and re-alert. A held-back
plugin's unresolved variables reach the report and the DM by NAME (a bounded `detail` field
on the issue row — names only, never values, and never part of the dedup fingerprint, so a
detail change alone never re-alerts). Each reason draws its detail from the secret status
that produced it, so a variable a setup tool has yet to provision is named by the reason that
says so, and a setup episode that failed carries its own error into the row.

**Health speaks to the operator in what they can do, and does not repeat itself.** Both
operator-facing surfaces — the in-band notice a resident prepends to a reply, and the DM —
render through one translation that never emits a reason code: the codes are internal
identifiers, an open set that grows with every plugin feature. The translation therefore
classifies by families of code rather than enumerating them, so a code minted later still
reads as something actionable, and an unrecognized one degrades to a plain statement rather
than leaking. The shared renderer is now the whole sentence, not merely the per-issue clause:
the two surfaces had each written their own wrapper around the shared clause and those had
already drifted, so a set of stale bindings was announced as an incomplete update in-band
and as a generic fault by DM. What deliberately stays per-surface is how many issues each
names — the in-band line rides on top of a reply and names two, while the DM is a message of
its own and names five, because the operator has no other way to see those names in that
moment. The truncated tail is answerable now: the read-only plugin status tool the assistant
holds reports the full standing set on request.

A report is normalized as it is read, and normalization filters rather than rejects. External
corruption of the report file — a non-object document, a row that is not a mapping, an
unhashable target — used to raise out of the notice renderer, which runs on a resident's turn
and is not guarded there, so a hand-broken file cost the operator their reply and not merely
their notice. Rejecting the whole document on one bad row would have thrown away a valid
blocking issue sitting beside it, so the bad rows are dropped and the good ones still reach
both surfaces.

Repeats are suppressed
by a decaying, in-process memo of the exact line last put in front of each role: any change
to what the operator would read renders immediately, and a role whose issues have resolved
is re-armed at once.

The memo is released — the line offered again on the very next turn, without waiting out its
window — whenever the delivery that carried it can be shown to have displayed nothing. That
was once knowable only from a raise, so a delivery that returned normally after sending
nothing consumed the line silently; a reconnect makes that reachable mid-turn, and no
retrieval path exists for an operator to ask what they missed. Channels that carry these
notices now report delivery explicitly (see `INV-TG-006` in
[`architecture/telegram.md`](telegram.md)), so both a proven failure and a raise-with-nothing-shown
release the memo, while a delivery whose head reached the operator does not — repeating a
line they have already read is the failure in the other direction. A channel that does not
report keeps the old behavior, deliberately: "cannot report" must not read as "failed", or
the notice would repeat forever there.

A transport failure that cannot distinguish "never arrived" from "arrived, acknowledgement
lost" is *not* a proven negative and does not release the memo. Only an established one
does — the application being absent, so no call was made at all, or the API evaluating the
call and refusing it. Erring the other way would repeat the line on every turn that hits a
flaky connection.

The operator DM is gated the same way, and records its fingerprints only on a send the
channel did not report as undelivered — the same proven-negative rule, so a channel that
cannot report either way records rather than repeating forever. The out-of-band notices —
that DM and the reminders one — serialize through a single lock, so neither can be sent
twice by two paths that each read the state before either marked it.

That lock orders the out-of-band notices against each other. Between the DM and the in-band
notice there is no lock, only a shared field: **the DM records the rows it named, and the
notice skips rows already recorded.** That is a rule, not a guarantee of exactly-once, and
the difference is the whole of what follows. Each surface names a bounded prefix and counts
the rest — five for the DM, two for the notice. Nothing schedules a follow-up: further
naming happens when something next regenerates health and notifies, which is a boot, a
plugin mutation or a reload. And a row the DM named goes unrecorded whenever the report
moved during its send, so the notice may repeat it. The read-only status tool is what
answers completely and at once, reporting the whole standing set unfiltered.

Coordinating the two *stores* was tried first and does not work, which is why the rule is a
filter over one field rather than a second store. The surfaces select different rows: the DM
announces fingerprints not yet notified, across every target, while the notice states what
stands for one role — and setup-episode rows are targetless, never decaying while pending,
so they stand for every role during exactly the multi-step setup flow where the duplicate
appears. Two messages carrying the same warning therefore rarely carry the same text, which
defeats suppression keyed on the rendered line; and any separate record of "already
delivered" has a lifetime that must be cleared, which is how a recurrence goes unannounced.
Filtering per row needs neither: the field it reads is pruned to fingerprints still present
on every regeneration, so the report's own resolution pruning *is* the lifetime.

That filter is only as honest as the mark behind it, so the mark is narrowed on both axes.
It covers the rows the message actually **named** — a row behind the "and N more" count was
not something the operator was told, so it stays unrecorded and stays available to be named
later. And it applies only while the report the message described is still current, because
a row that resolves while its DM is in flight would otherwise have its fingerprint written
back into a report that no longer holds it, where the next regeneration's pruning preserves
it the moment the row recurs — a recurrence then reaching nobody. When the report has moved,
nothing is recorded, which can cost a repeat of a message already read. That direction is
chosen deliberately: a duplicate is recoverable and a silence is not.

What the notice can show is narrower than what stays unrecorded, and the difference is where
this is misread. It selects blocking **issues** addressed to its own role or to no target.
A warning, or an issue addressed to an executor, is therefore never in-band under any
condition; it waits to be named by a DM. So does a resident-addressed row sitting behind the
notice's own count. None of those is lost — they stay unrecorded, so a later notification can
name them, five at a time, and the status tool lists them on request — but nothing here
delivers them on a schedule.

Two consequences are deliberate. A `target=None` row is operator-global — one DM naming it
records it for every role. And a changed `detail` on an unchanged fingerprint no longer
re-shows in-band; the DM never re-announced it either, so this closes an undesigned channel
rather than removing a guarantee.

**Approval is per call, not per install, and it does not survive a restart.** A protected
tool call by a resident or specialist consumes a single-use grant bound to a specific
operator, chat, role, artifact, tool name and exact arguments. The grant store is in process
memory only.

**Tiers are not gated alike, and this is the asymmetry to carry away.** Residents and
specialists get plugin grants merged into their allowed tools, a fail-closed tool gate, and
the protected-tool approval hook. **Executors get none of those** — they receive plugin
paths, and their declared tool list is passed to the SDK as *auto-approved* tools, which is
a convenience rather than an enforcement boundary: sub-agent spawning bypasses an
allowed-tools list, and only the disallowed list is CLI-enforced. What actually constrains an
executor is the code-mandatory clamps merged into every options build — sub-agent spawn tools
are hard-denied, Bash is hard-denied unless the declaration allows it, and guard hooks
protect managed components and agent-home settings. A plugin declaring protected tools
protects resident and specialist calls; it creates no equivalent gate on the executor path.

## Contracts & invariants

**INV-PLUG-001**: A registry entry is usable only when its recorded artifact id equals the id computed from its own source coordinates.

Enforced during registry validation, which rejects a mismatching entry and excludes it from
the document rather than failing the whole registry.

What it does not cover: this is an identity check over coordinates. It attests nothing about
the bytes in the store.

**INV-PLUG-002**: A resolved artifact must match its recorded content checksum, and the artifact path and its parent must not be symlinks.

Enforced by the artifact verdict computed when a snapshot is built; a failing verdict means
the plugin is not resolved and the reason is recorded.

What it does not cover: the verdict is not recomputed after the snapshot is published.
Resolution meanwhile re-reads the artifact's manifest and hands out live paths, so a change
underneath a live snapshot is detected only by an explicit verification or the next snapshot
reload — nothing catches it on its own. The recorded checksum itself lives in the artifact's
metadata (see the mental model): it pins the tree against that metadata, not against an
independently authenticated content identity. Internal non-escaping symlinks inside an
artifact are permitted.

**INV-PLUG-003**: Archive extraction refuses traversal, absolute paths, links out of the tree, and special files.

Enforced by an explicit per-member loop. The standard library's extraction filter is applied
as well where the runtime supports it, but it is defence in depth — the per-member loop is
what actually carries the guarantee, and the fallback exists because the shipped interpreter
predates the filter.

**INV-PLUG-004**: A protected tool call from a resident or specialist proceeds only by consuming an exact, single-use grant.

Enforced by the authorization hook, with consumption made atomic in the grant store. The
grant binds operator, chat, tier-stripped role, artifact, full tool name, and a hash of the
canonical arguments — so an approval authorises one action with one argument set, not a
capability.

What it does not cover: unprotected plugin tools, and the executor path entirely. And
"proceeds by consuming" applies to direct resident calls, ephemerally delegated
specialists, and — since #400 — interactive specialist *engagements*: a protected call whose
provenance is an engagement routes through the same DM authorization challenge, but its grant
additionally binds the engagement id, so an approval minted inside one engagement can never
consume a matching call in another. Identity for the engagement path comes from the live
engagement record (its own operator DM), and on approval the challenge resumes that
engagement rather than a resident continuation. A non-authorizable engagement record (not an
active specialist with a topic and a reachable operator) still denies, fail-closed, before any
grant lookup.

Who may approve is a separate guarantee, INV-PLUG-007: read this invariant as "one
approval authorises one action" and that one as "the approver is the configured operator".

**INV-PLUG-005**: Grants exist only in process memory; a restart revokes every one of them.

There is no persistence path. Revocation additionally happens on consumption, on TTL expiry
plus a periodic sweep, on a chat reset, on role reload or removal, and on plugin update or
removal.

Trigger consent is the deliberate exception: a webhook-trigger acknowledgement is persisted
and re-validated from disk at startup, because it authorises a route rather than a call.

**INV-PLUG-006**: Executor options receive plugin paths without a grant merge and without a tool gate.

Stated as an invariant because it is a security-relevant asymmetry that reads like an
oversight and is not one — the executor path is constrained by its code-mandatory disallow
clamps, guard hooks and relay instead; its declared tool list is auto-approval, not a gate.
Verification will report an executor whose declaration lacks a needed authorisation, but
nothing merges it automatically.

**INV-PLUG-007**: An authorization challenge is posted, and its grant minted, only for a turn whose sender is the configured operator; any other sender's protected call is denied outright, before any grant lookup and with no challenge.

Enforced in the authorization hook through the Telegram channel's single operator rule —
the same sender-id match that decides attribution and clearance — so the person who taps
Approve is the person the deployment names as operator, not whoever asked. Denying without
a challenge is the point: posting one would hand the requester their own approval button.

What it does not cover: with `telegram_chat_id` empty ("accept all chats") there is no
configured operator, so protected tools are denied for every sender — deliberate, and
announced by a warning at channel construction. The in-engagement permission relay now
follows the same rule (its keyboard is answerable only by the configured operator, and
with none configured it denies immediately rather than posting one); in-engagement
*questions* remain answerable by the engagement's creator, since an answer is interaction,
not authorization. Sender identity itself is Telegram's authentication of its user ids,
not an additional Casa-side proof.

**INV-PLUG-013**: A plugin-health fingerprint is recorded as announced only for a row an operator message actually named, on a send the channel did not report as undelivered, and only while the report that message described is still current; a fingerprint that is not recorded is suppressed on neither operator surface.

Every failure it forbids is silent, which is why it is pinned rather than left to the
renderers. A mark that outruns what was named removes a row from the surface that would have
named it, permanently — it is filtered out of the notice and is no longer new to the next
message. A mark that lands on a report the message no longer describes is worse: the
fingerprint is written back onto a row that has resolved, where the next regeneration's
pruning keeps it as soon as the row recurs, so the recurrence reaches nobody. The negative
condition is deliberately "not reported as undelivered" rather than "confirmed": a channel
that cannot report either way must not be read as having failed, or its notices would repeat
forever. What holding this costs is a repeated message when a regeneration lands mid-send,
and a large incident being named five rows at a time.

## Failure behavior

**The registry document is malformed.** It loads as invalid, no plugins resolve, and the
condition is recorded as a health issue. Boot still succeeds.

**One entry is malformed, or two entries collide on name.** That entry is excluded — both, on
a collision — and the rest continue.

**An artifact is missing, corrupt, or fails its identity or checksum check.** That plugin is
not resolved and the reason is recorded. Other plugins are unaffected.

**An archive is unsafe.** Extraction raises, staging is cleaned up, and the failure is
reported. This happens *before* any registry mutation, so a refused install leaves no
half-state.

**A protected tool is called without an approval.** The hook denies the call and posts or
reuses an approval challenge to the operator. The retry must present identical canonical
arguments — a changed argument is a different grant.

**The authorization hook itself fails.** Any unexpected exception becomes an explicit deny;
only cancellation is re-raised. The hook fails closed.

**The plugin's MCP declaration is missing or malformed.** Grants degrade to none. A missing
declaration is not an error — a plugin with no tools is valid — but a malformed one is
reported.

**A required environment variable is unresolved.** The plugin is withheld from resident and
specialist session builds — excluded from the SDK plugin list, its server grants, and the
recorded binding, and surfaced as an `env_unresolved` resolution issue — and any pending
setup episode holds. Wiring the value and running the plugin-env reload makes the plugin
loadable; the agents that should carry it still need their own reload to rebuild sessions.

## Extension points

**Adding a plugin** means publishing it and registering it, then reloading. Editing the
registry file directly is not sufficient: nothing takes effect until a snapshot reload runs.

**Adding MCP tools** means shipping the declaration; grants are derived per server. If the
plugin targets an executor, that executor's own declared tool list must be updated separately
— verification will tell you it is missing, but no merge happens for you.

**Adding a protected tool** is a manifest declaration. Validation checks its shape and name
uniqueness, **not that the named tool exists**; a typo produces a declaration that protects
nothing. And it applies only to the resident and specialist paths.

**Declaring system requirements** is strict the same way the other manifest extensions
are: an absent declaration means none, but a present-but-malformed one (a non-list value,
a non-object member, or a requirement without a safe executable `verify_bin` name)
refuses the install or update rather than silently reading as "no requirements" — or
running an install that can never succeed — and activating a plugin whose binary was
never installed. Package-manager types keep their own dedicated refusal. On the
verification surface the same malformation reports as a visible missing-requirement
status instead of crashing the verify.

**Adding a webhook trigger** requires intrinsic validation plus a durable operator consent
bound to the exact trigger identity. This approval outlives a restart — one of the durable
approval ledgers (specialist and persona install acknowledgements are others), in contrast
to the in-memory tool-call grants above.

**Adding an authorization callback** is a sibling manifest extractor: `casa.callbacks` is a
peer of `casa.triggers`, parsed and intrinsically validated the same way, and gated by a
durable operator consent that is another of those restart-surviving ledgers. Its consent
is deliberately asymmetric to a trigger's, and the asymmetry is the thing to carry away: a
trigger ack binds the plugin *artifact*, so any update re-consents; a callback ack binds the
*declaration digest* (the declared name only) and excludes the artifact, so a routine
upgrade that leaves the declaration unchanged keeps consent — because a callback grants no
role turn and no memory access, only a spool deposit. The mechanism lives in
`architecture/callbacks.md`.

**Reloading** refreshes the snapshot before agents and executors are rebuilt, and purges
role-scoped grants and pending challenges before a role is replaced or removed.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_registry.py::compute_artifact_id`
- `casa/rootfs/opt/casa/plugin_registry.py::reload_snapshot`
- `casa/rootfs/opt/casa/plugin_store.py::safe_extract_tar`
- `casa/rootfs/opt/casa/plugin_store.py::artifact_verdict`
- `casa/rootfs/opt/casa/plugin_store.py::manifest_protected_tools`
- `casa/rootfs/opt/casa/plugin_grants.py::protected_map`
- `casa/rootfs/opt/casa/plugin_health.py::render_notice`
- `casa/rootfs/opt/casa/plugin_health.py::mark_notified`
- `casa/rootfs/opt/casa/casa_core.py::notify_plugin_health`
- `casa/rootfs/opt/casa/authz_grants.py::GrantKey`
- `casa/rootfs/opt/casa/authz_grants.py::GrantStore`
- `casa/rootfs/opt/casa/plugin_boot.py::main`

**Tests**
- `tests/test_plugin_registry.py`
- `tests/test_plugin_store_publish.py`
- `tests/test_plugin_grants.py`
- `tests/test_authz_grants.py`
- `tests/test_authz_hook.py`
- `tests/test_plugin_boot.py`
- `tests/test_plugin_health.py`
- `tests/test_plugin_health_notify.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/callbacks.md`](../architecture/callbacks.md)
<!-- END SOURCEMAP -->
