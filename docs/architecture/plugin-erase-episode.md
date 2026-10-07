---
last_reviewed: 2026-10-07
---

# The plugin erase episode

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How Casa runs a plugin's chosen eraser once an `erase_data=true` call has consumed the
operator's Erase choice: the Casa-dispatched erase turn and its `plugin_erase` marker, the
result broker's admission, result and failure hooks that bind that turn to its own run, the
fence on the erasing plugin's other tools, the wait bound, and the erasure record the episode
writes for each plugin. The plugin-author contract and its result convention, the question
and its choice, the finishing call that removes, and what a restart leaves are
[`plugin-erasure.md`](plugin-erasure.md)'s; the grant identity of the Casa-dispatched turn is
[`plugin-setup-turn.md`](plugin-setup-turn.md)'s (INV-PLUG-027); the result broker the capture
lives in is [`plugin-result-contract.md`](plugin-result-contract.md)'s.

## Mental model

**The erase episode runs in the background.** An `erase_data=true` call that consumes the
choice starts the episode and returns `erasure_running`, removing nothing. Each erasing
plugin is projected onto the tapped kind (`EraseSpec.for_kind`), and in turn
(`plugin_erasure.run_erase_episode`):

- *Dispatch.* Each run gets its own run id, and the eraser's full tool name is armed in a
  watch under each MCP server the plugin declares, keyed by that run id. Casa then
  dispatches a Casa-authored turn
  addressed to the configured operator, through the same seam the setup dispatch uses,
  to the role the plugin's targets select — a resident directly, or the assistant as a
  courier that delegates to the specialist by name. The turn is told to call the eraser
  once, with no arguments, and to call nothing else.
- *Marker.* The turn carries Casa's `plugin_erase` marker beside
  `plugin_erase_target` (the role that runs the eraser), `plugin_erase_artifact` (the
  artifact the tap named), `plugin_erase_episode` (the run id), and `plugin_erase_subject` /
  `plugin_erase_question` (the uninstall question the run answers). All are reserved context
  keys no ingress can supply, the agent copies the stamps onto the turn's origin, and the
  marker admits the `setup`
  transport, so the turn's grant identity is gated exactly like a setup turn's, from one
  table of Casa plugin-turn markers (INV-PLUG-027), and it can delegate only in `sync`
  mode.
- *Approval.* When the plugin declared its eraser protected, the Erase tap is the
  operator's approval of exactly this call, so no second challenge is posted: the broker's
  admission hook admits the eraser itself on its own run's turn once the checks below
  pass, instead of consulting the authorization hook. No grant is minted for it, so
  nothing an ordinary turn could consume is ever left in the grant store; on any other
  turn the protected eraser meets the ordinary challenge (INV-PLUG-004).
- *Binding check.* An erase-marked turn exists to run one eraser. On such a turn the result
  broker's admission hook refuses every plugin tool before it runs unless the turn's own run
  is waiting for that exact tool, the question the run answers is still the open one, and
  the session's binding carries the plugin at the tapped artifact. So asking again, Keep or
  Cancel also stops an approved erase whose turn has not reached the eraser yet (an
  operator ruling). Without these checks an update published between the tap and the
  session build would run another version's eraser, a turn that runs after its run stopped
  waiting — or a turn of another run — would run an eraser nobody is waiting for, and an
  unprotected eraser meets no grant check that could catch any of these.
- *Capture.* The result hook hands the eraser's result to the turn's own run, before the
  early return a `safe` tool takes, and passes the result itself on unchanged; the failure
  hook answers it as an error. A late result of one run can never answer another, even
  for the same artifact and tool.
- *Turn end.* Every turn's `finally` reports to the erasure module; on an erase-marked turn
  every key still unanswered resolves at once as "no call", so a turn that ended without
  calling the eraser does not leave the episode waiting.
- *Wait bound.* Otherwise the episode waits at most 900 s (`ERASE_WAIT_S`) for the first
  answer, preferring a real result over a "no call" on another server, and then disarms.

Each plugin's outcome is recorded as an erasure record keyed by `plugin:<name>` and the
tapped artifact and stamped with the question id and the kind: `complete` only when the
eraser said so, otherwise not complete. The
episode stops at the first plugin whose erasure did not complete, because the uninstall
proceeds only when all of them did, and running the rest would erase data the operator
then keeps a plugin for. The outcome then continues the configurator engagement: a
complete erasure hands over the plugins' reports and tells the engager to call
`erase_data=true` again to finish; anything else hands over the report verbatim, says
nothing was removed, and tells the engager to ask the operator whether to try again later
or to uninstall anyway keeping what is left (`erase_data=false`).

## Contracts & invariants

**INV-PLUG-038**: On an erase-marked turn, a plugin tool is refused before it runs unless the turn's own erase run is waiting for that exact tool, the uninstall question that run answers is still the open one, and the session's binding carries its plugin at the artifact the operator's tap named, and only the turn's own run is answered by its result or failure; a protected eraser that passes these checks is admitted there without the authorization challenge, no grant is ever minted for an erasure, and on any other turn a protected eraser meets the ordinary challenge.

Enforced by the result broker's admission, result and failure hooks. What it does not cover: an unmarked turn, which neither triggers the check nor
answers the episode.

**INV-PLUG-039**: An erase episode answers within its wait bound: an erase-marked turn that ends without its eraser's result resolves the episode as "no call" from the turn's `finally`, a turn that never reports is resolved as timed out after `ERASE_WAIT_S`, and several erasing plugins run one at a time, stopping at the first whose erasure did not complete.

Enforced by `plugin_erasure.turn_ended`, called from `Agent._process`'s `finally` for every
turn and inert on any other marker, and by `run_erase_episode`.

**INV-PLUG-042**: From an erase episode's dispatch, any turn's call of an erasing plugin's tools but its own eraser call is refused before it runs until the episode ends incomplete, its question stops being open, or an `erase_data=true` call can no longer finish its erasure; a finishing call's fence is settled only by that call (a specialist's by its transaction), then stays while the registry file lacks it.

Enforced by `plugin_erasure.EraseFence` (by name) and the broker's admission hook. Not
covered: a call admitted before it rose; executors (bundled only).

## Failure behavior

**The eraser reports after the wait.** The episode has disarmed its run's key, so an erase
turn that only now reaches the eraser is refused before it runs (the binding check), a
result that arrives now answers no other run, and the record stays not complete; the
operator runs the uninstall again.

**A restart during the episode** loses its watch and its fence with the rest of the
uninstall's in-memory state; what that leaves installed is
[`plugin-erasure.md`](plugin-erasure.md)'s.

## Extension points

**A new kind of Casa-dispatched plugin turn** follows the marker table rule in
[`plugin-setup-turn.md`](plugin-setup-turn.md)'s extension points, as the erase turn did.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_erasure.py::run_erase_episode`
- `casa/rootfs/opt/casa/plugin_erasure.py::turn_ended`
- `casa/rootfs/opt/casa/plugin_erasure.py::EraseWatch`
- `casa/rootfs/opt/casa/plugin_erasure.py::EraseFence`
- `casa/rootfs/opt/casa/plugin_erasure.py::erase_turn`
- `casa/rootfs/opt/casa/plugin_erasure.py::erase_turn_question_open`

**Tests**
- `tests/test_plugin_erasure_episode.py`
- `tests/test_plugin_erasure_capture.py`
- `tests/test_plugin_erase_fence.py`

**Related**
- [`architecture/plugin-erasure.md`](../architecture/plugin-erasure.md)
- [`architecture/plugin-result-contract.md`](../architecture/plugin-result-contract.md)
- [`architecture/plugin-setup-turn.md`](../architecture/plugin-setup-turn.md)
- [`architecture/plugin-authorization.md`](../architecture/plugin-authorization.md)
<!-- END SOURCEMAP -->
