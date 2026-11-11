---
last_reviewed: 2026-11-11
---

# Approvals the configurator carries on

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a specialist install, a specialist upgrade or a persona install is approved and carried
out: the approval record that freezes what the operator approved, the card that asks, the run
Casa starts for the configurator after Approve, the rule that one tap is one attempt, the
request that changes nothing and asks nothing, and the continuations an erasure owes the
configurator. The configurator itself, its lease and its other origins are
[`configurator.md`](configurator.md); what the card says about the component and its digests,
and the consent ack the library checks, are
[`specialist-lifecycle.md`](specialist-lifecycle.md) and
[`persona-lifecycle.md`](persona-lifecycle.md); the lane a run waits for is
[`agent-lane.md`](agent-lane.md).

## Mental model

**An approval is for one action and its effective settings.** After inspecting a specialist,
the configurator calls `specialist_install_request_approval` with the staged facts, the
mode, the config it agreed with the operator and the secret names provided.
`persona_install_inspect` asks for a persona install the same way, with no settings. Casa
computes the configuration the library will actually stage
(`specialist_install.preview_effective_config`: for an upgrade, the active install's settings
and a pending snapshot beneath the call's own) and stores a record
(`approval_records.request`) with the inspected identity and those values, frozen. Applying a
persona override asks for no approval.

**The card shows what the approval binds.** Casa posts the card in the operator's chat on its
own bot: each effective setting with its value, the ones the request did not submit marked
`(kept from the current settings)`, and the secret names provided, never their values
(`specialist_install_consent.render_settings_lines`). A newer request for the same component
replaces the record, and the older card is edited to say "Replaced by a newer request; nothing
was installed." (or "updated"). Deny and an unanswered card close the record; nothing runs.

**Approve carries the configurator on.** The tap records the consent ack, moves the record to
`approved` and calls `configurator_continue.start`, which never awaits: it takes the
approval's launch slot and spawns one task. That task takes the configurator's permit, a
place on its lane and its lease, in that order, registers one durable delegation on the
resident's behalf with the operator's words as its task, and runs the configurator with a
task text that names the call to make and ends with the approved values
(`configurator_continue.continuation_text`). The card is edited when the task tells whether
the run started: "✅ Approved — the Configurator is carrying on with …", or "Approved and saved
— but the configurator could not be started. Ask Ellen again; the approval is kept." The run's
result reaches the resident as any delegation's does.

**One tap is one attempt.** The governed tools — `specialist_install_commit`,
`specialist_upgrade` and `persona_install_commit` — run only under their run's own approval
(`approval_required` otherwise). Each uses it up (`approval_records.attempt`) immediately
before its first library call, under the plugin-tools lock, whatever the call then does:
success, failure, compensation, cancellation, a crash. A second call under the same approval
is refused `approval_used`. So a failed attempt, or wanting the same upgrade again after a
rollback, asks again with a new card. An approval whose run made no governed call — busy past
the deadline, a failed start, a refusal, a restart before the call — is still `approved`, and
asking again starts it without a new card (`already_approved`).

**A request that would change nothing asks nothing.** An install of what is already
installed is refused at inspection as an occupied slug. An upgrade to the active version
whose effective settings equal the active install's, with no secret names on either side, is
`not_needed`: no record, no card, no run, and the configurator tells the operator it is
already done. Anything else posts a card: Casa keeps no record of the secret names an install
got, so it cannot show that they match.

**The library checks the values too.** `commit_specialist_install` and `upgrade_specialist`
take the approval's values, check the ack for that identity, and once they hold the config
they would stage refuse `approval_values_differ` unless it equals the approved values —
before any staging or journal write. Settings changed between the approval and the run
therefore stage nothing.

**An erasure continues the configurator through the same launcher.** The erase choice and
the eraser's outcome carry no approval: their tap is a one-shot grant of its own
([`plugin-removal.md`](plugin-removal.md)). Each is keyed by its question; a second
continuation while one runs waits as its single follower and starts when the first ends.

## Contracts & invariants

**INV-CFG-019**: The configurator commits exactly the values the operator approved on the card — the effective settings, the ones kept from the current install included — and an approval that a newer request superseded, or that is no longer its component's current record, writes nothing.

Enforced twice: the governed tool's approval must be current and of its own kind and identity
at entry and again at its attempt, and the library compares the config it would stage with the
approved values before it stages anything. A newer request supersedes the record under the
same plugin-tools lock the governed tool holds from its attempt through the library write, so
no record changes between its use and the write.

**INV-CFG-020**: A run Casa starts for an approval starts only while that approval is approved and its component's current record; the approval is used only by a governed call of that run, of the call's own kind and identity, immediately before its first library call and whatever follows, and every later call under it is refused.

The start check is a read at the configurator's lease (`approval_records.startable`); it
writes nothing, so a run that waited past the deadline or refused before any governed call
leaves the approval for the next. Two calls of one run, one after the other or at once, are
serialised by the plugin-tools lock and the second finds the approval used.

## Failure behavior

**The card cannot be posted.** The request tool returns the structured failure
(`consent_channel_unavailable`, `consent_prompt_failed`, …); the record stays pending and
nothing runs.

**The configurator cannot be started after Approve.** The card says so and the approval is
kept; asking again starts it without a new card. When the start came from asking again with no
card to edit, the operator is told once in a notice.

**Casa restarts between Approve and the attempt.** The approval is still `approved`; asking
again starts it without a card. **After the attempt** it is `used`, nothing is replayed, and
asking again posts a new card. The records live in `/data/approval-records.json`, declared in
`private_state.FILES`; a terminal record older than thirty days is dropped at load, the
component's current record kept.

## Extension points

**A new governed mutation** checks its run's approval at entry (`approval_records.fence`),
uses it with `attempt` under the plugin-tools lock immediately before its library call, and
holds the lock through the write.

**A new continuation of the configurator** goes through `configurator_continue.start`, so it
takes the permit, the lane and the lease in the one order every configurator run uses.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/approval_records.py`
- `casa/rootfs/opt/casa/configurator_continue.py`
- `casa/rootfs/opt/casa/tools.py::specialist_install_request_approval`

**Tests**
- `tests/test_approval_records.py`
- `tests/test_approval_claim_under_lease.py`
- `tests/test_approval_required.py`
- `tests/test_approval_restart.py`
- `tests/test_configurator_approval.py`
- `tests/test_configurator_continue.py`
- `tests/test_consent_identity.py`
- `tests/test_no_change_requests.py`

**Related**
- [`architecture/configurator.md`](../architecture/configurator.md)
- [`architecture/specialist-lifecycle.md`](../architecture/specialist-lifecycle.md)
- [`architecture/persona-lifecycle.md`](../architecture/persona-lifecycle.md)
- [`architecture/agent-lane.md`](../architecture/agent-lane.md)
- [`architecture/plugin-removal.md`](../architecture/plugin-removal.md)
<!-- END SOURCEMAP -->
