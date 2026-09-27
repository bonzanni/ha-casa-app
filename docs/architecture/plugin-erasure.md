---
last_reviewed: 2026-09-27
---

# Plugin data erasure at uninstall

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an uninstall can erase a plugin's data first: the `casa.eraseTool` manifest field and
the result convention a plugin author implements, the one question Casa asks the operator,
the erase episode that runs the plugin's eraser, and the finishing `plugin_remove` or
`specialist_uninstall` call that removes the plugin only after a complete erasure. What a
removal discloses when nothing was erased is
[`plugin-mutation-tools.md`](plugin-mutation-tools.md)'s (INV-TOOL-007); the result broker
the capture lives in is [`plugin-result-contract.md`](plugin-result-contract.md)'s; the
grant identity of the Casa-dispatched turn is
[`plugin-setup-turn.md`](plugin-setup-turn.md)'s (INV-PLUG-027); the uninstall transaction
itself is [`specialist-bundle-transactions.md`](specialist-bundle-transactions.md)'s.

## Mental model

**Casa sequences the eraser; it never erases anything itself.** Only a plugin knows what
its data is and which consents it holds at its providers. So a plugin that wants its data
erased at uninstall declares a tool of its own that does it, and Casa's part is order and
authorization: ask the operator, run that tool while the plugin's MCP server still runs,
and remove the plugin only when the tool reported that the erasure is complete. Casa does
not delete the plugin's CLI-managed persistent data directory (`CLAUDE_PLUGIN_DATA`) on
any path, and it does not touch Home Assistant backups — a backup taken before the erasure
still holds the data, and every surface of this flow says so.

**The plugin-author contract is two things.** The manifest field, `casa.eraseTool`, names
the eraser: a lowercase ASCII tool name (`^[a-z][a-z0-9_]{0,63}$`), not the plugin's
`casa.setupTool`, and declared `"safe"` in the plugin's `casa.resultContract`. The last
condition is not decoration: the result broker refuses every non-setup tool of a plugin
that has not adopted the contract (INV-PLUG-017), so an eraser there could never run. The
tool takes no arguments, erases everything the plugin holds, revokes what it can at its
providers, and returns as its text result a JSON object:

```json
{"erasure": "complete", "report": "Deleted 3 feeds and withdrew the bank consent."}
```

`erasure` is `"complete"` or `"incomplete"`; `report` is prose for the operator, relayed
verbatim and cut at 4000 characters with a ` [truncated]` marker. Anything else — prose,
another value, a missing key, a non-string report, a tool error — is not a complete
erasure, and its raw text becomes the report. A plugin may also list its eraser in
`casa.protectedTools`; the question then shows the plugin's own summary for it, and the
operator's Erase tap is the approval of exactly the one call (below). A plugin that declares an eraser but no MCP
server to call it on is still treated as declaring one: the uninstall asks, and an Erase
choice can never complete (the episode is not dispatched), so it is never silently removed
as an ordinary plugin.

**One question, asked by Casa, and the tap is the authorization.** An uninstall that
includes a plugin whose resolved artifact declares a valid eraser — `plugin_remove` of a
plugin no specialist owns, or `specialist_uninstall` of a specialist whose owned plugins
include one — first posts a keyboard in the operator's DM, through the same challenge
coordinator the install-consent keyboards use: **Keep data / Erase data / Cancel**. The
question names each erasing plugin and its eraser and says that earlier Home Assistant
backups keep the data either way. It stays answerable for 600 s. The call that posted it
returns `erase_choice_pending` with nothing removed. An Erase tap records a single-use
choice bound to the operator, the uninstall subject (`plugin:<name>` or
`specialist:<slug>`) and the erasing artifacts as they were at question time, valid for
300 s; Keep and Cancel record nothing, because keeping the data is the ordinary removal.
Every tap continues the configurator engagement with the exact call to make next, and the
DM message is edited to say what happens — including, when the engagement could not be
resumed, that the operator has to ask the configurator to continue. The model can never
assert "erase" on the operator's behalf: an `erase_data=true` call that neither finishes a
complete erasure nor consumes that recorded choice returns `erase_not_confirmed`, and one on
an uninstall that no longer includes an erasing plugin (an update dropped the eraser, say)
returns `erase_unavailable`.

**The step and the removal see one state.** The erase step runs under the plugin mutation
lock the removal commits under — `plugin_remove` holds it across both, and a specialist's
step runs inside its bundle transaction — on the registry as it stands under that lock. So
an update cannot land between the decision and the removal: an eraser it adds is asked
about, one it drops refuses `erase_data=true`, and an erased artifact it replaced is not
the one a complete record names. Posting the question or starting the episode happens
under that lock too; the eraser itself runs afterwards, outside it.

**Every tap, run and record belongs to one question.** Posting the question opens a new
question id for the uninstall subject (`plugin_erasure.QUESTIONS`) and replaces any open
one; a Keep or Cancel tap, and a Keep removal (`erase_data=false`), close it. The Erase choice is
bound to that id, the episode it starts carries it, and every erasure record the episode
writes is stamped with it; only the open question's choice starts an erasure and only its
records finish an uninstall. So asking again voids everything an earlier question
produced — an Erase tap on it, a run still in flight when it was replaced, a record it
left, or one left by an earlier installation of the same artifact (erased, removed with
Keep, reinstalled): only an erasure run for THIS question can finish it.

**The erase episode runs in the background.** An `erase_data=true` call that consumes the
choice starts the episode and returns `erasure_running`, removing nothing. For each erasing
plugin in turn (`plugin_erasure.run_erase_episode`):

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
tapped artifact and stamped with the question id: `complete` only when the eraser said so,
otherwise not complete. The
episode stops at the first plugin whose erasure did not complete, because the uninstall
proceeds only when all of them did, and running the rest would erase data the operator
then keeps a plugin for. The outcome then continues the configurator engagement: a
complete erasure hands over the plugins' reports and tells the engager to call
`erase_data=true` again to finish; anything else hands over the report verbatim, says
nothing was removed, and tells the engager to ask the operator whether to try again later
or to uninstall anyway keeping what is left (`erase_data=false`).

**The finishing call removes.** A second `erase_data=true` call finds a complete record for
every erasing plugin at the artifact the registry resolves at that moment, consumes them,
and runs the ordinary removal, all under the mutation lock (above): an update that replaced
the erased artifact leaves no complete record for the installed one, so nothing is removed.
Its ok result carries `erasure: "complete"` and the reports
— `erase_report` for a `plugin_remove`, `erase_reports` (name and report per plugin) for a
specialist — and replaces the survival disclosure for the erased plugins with a
`plugin_data_note` saying that the plugin's own eraser reported its data erased and that
Home Assistant backups taken before still contain it. A specialist whose cascade also drops
owned plugins without an eraser keeps the ordinary disclosure for those, naming only them,
with the erased-plugins statement beside it as `erased_note`.

## Contracts & invariants

**INV-PLUG-034**: A present `casa.eraseTool` is accepted only as a lowercase ASCII tool name that is not the plugin's `casa.setupTool` and is declared `safe` in its `casa.resultContract`; any other value — an explicit null included — refuses the install or update with `erase_tool_invalid`, and a stored artifact carrying one gets the artifact verdict `erase_tool_invalid`.

Enforced by `plugin_store.manifest_erase_tool`, called from manifest validation on the
install and update paths and from `artifact_verdict` on the stored-artifact path, the same
upgrade-path posture `casa.setupTool` has. What it does not cover: whether the declared tool
actually erases anything, which only the plugin can know.

**INV-PLUG-035**: An eraser's outcome counts as a complete erasure only when its result is a JSON object whose `erasure` is exactly `"complete"` and whose `report` is a string; every other result, a tool error, a turn that ended without the call, an elapsed wait and an undispatched turn are recorded as not complete.

Enforced by `plugin_erasure.parse_erase_result` and by the episode folding every other
outcome to a non-complete record. Reading anything looser as success would let a plugin's
prose ("done, all erased") remove the one tool that could still finish the job.

**INV-PLUG-036**: An uninstall that includes a plugin whose resolved artifact declares an eraser removes nothing until the operator answers Casa's DM question; only an Erase tap on the currently open question authorizes an erasure, recorded as a single-use choice bound to the operator, the uninstall subject, the erasing artifacts and that question and expiring after 300 s; posting a new question replaces the open one and a Keep or Cancel tap or a Keep removal closes it, voiding every earlier choice; and an `erase_data=true` call that finds no complete erasure to finish and cannot consume the open question's choice starts nothing and removes nothing.

Enforced by `tools._erase_gate` in front of both removal tools and by
`plugin_erase_consent`, whose tap hook records the choice in the Telegram callback's commit
step. With no reachable operator DM the gate refuses (`consent_channel_unavailable`) rather
than removing. What it does not cover: a removal with `erase_data=false`, which is the
ordinary removal and asks nothing.

**INV-PLUG-037**: The erase step and the removal run under one hold of the mutation lock, on the registry as it stands there; an `erase_data=true` call removes only when a complete erasure record exists for every erasing plugin at the artifact the registry resolves under that lock, and it consumes those records there; a record counts only when it was written by an erasure run for the question that is still open; an incomplete record, a record for another artifact (including one an update replaced while the call waited for the lock), a record of an earlier or closed question (including one from a run that finished after a newer question was asked), a missing one, or an uninstall that includes no erasing plugin removes nothing.

Enforced in `tools._erase_gate`, which computes the erasing plugins from the registry at
the call and takes the records only when all are complete — a refused call spends none of
them. A complete erasure of one version therefore never removes another.

**INV-PLUG-038**: On an erase-marked turn, a plugin tool is refused before it runs unless the turn's own erase run is waiting for that exact tool, the uninstall question that run answers is still the open one, and the session's binding carries its plugin at the artifact the operator's tap named, and only the turn's own run is answered by its result or failure; a protected eraser that passes these checks is admitted there without the authorization challenge, no grant is ever minted for an erasure, and on any other turn a protected eraser meets the ordinary challenge.

Enforced by the result broker's admission, result and failure hooks. What it does not cover: an unmarked turn, which neither triggers the check nor
answers the episode.

**INV-PLUG-039**: An erase episode answers within its wait bound: an erase-marked turn that ends without its eraser's result resolves the episode as "no call" from the turn's `finally`, a turn that never reports is resolved as timed out after `ERASE_WAIT_S`, and several erasing plugins run one at a time, stopping at the first whose erasure did not complete.

Enforced by `plugin_erasure.turn_ended`, called from `Agent._process`'s `finally` for every
turn and inert on any other marker, and by `run_erase_episode`.

## Failure behavior

**Nothing is removed on any arm but a complete one.** A pending question, a running
erasure, a refused `erase_data=true`, an incomplete, unreadable, errored, uncalled or
timed-out erasure, and a turn that could not be dispatched — no target role, no
operator Telegram chat, or a bus that refused the turn — all leave the plugin installed. The operator decides what
happens next; the configurator's recipe forbids removing the plugin without that answer,
since removal would destroy the one tool that can finish the erasure.

**The engagement cannot take the continuation.** A tap whose continuation did not start
edits the DM to say the configurator was not resumed. An episode outcome the engagement
cannot take becomes an operator notice in Casa's own words, never the plugin's report, and
asks the operator to run the uninstall again, which asks again.

**A removal fails after a complete erasure.** The finishing call consumed the records
before the removal ran, so a removal that then fails — a bundle transaction refused, say —
leaves the plugin installed with its data as the eraser left it. Running the uninstall again
asks the question again; Keep removes it.

**The eraser reports after the wait.** The episode has disarmed its run's key, so an erase
turn that only now reaches the eraser is refused before it runs (the binding check), a
result that arrives now answers no other run, and the record stays not complete; the
operator runs the uninstall again.

**A restart mid-erasure.** The watch, the question ids, the choices and the records are all in process
memory. A restart loses them, which leaves the plugin installed — the safe side — and the
next uninstall asks again.

## Extension points

**A new removal path** that drops a plugin must pass the same gate before it removes
anything, and apply the erasure to its disclosure, or it removes an erasing plugin without
the question and reports its data as surviving when it was erased, or the reverse.

**A new kind of Casa-dispatched plugin turn** follows the marker table rule in
[`plugin-setup-turn.md`](plugin-setup-turn.md)'s extension points, as the erase turn did.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_erasure.py`
- `casa/rootfs/opt/casa/plugin_erase_consent.py`
- `casa/rootfs/opt/casa/plugin_store.py::manifest_erase_tool`
- `casa/rootfs/opt/casa/plugin_grants.py::plugin_tool_names`
- `casa/rootfs/opt/casa/tools.py::_erase_gate`
- `casa/rootfs/opt/casa/tools.py::_erase_specs_for`
- `casa/rootfs/opt/casa/tools.py::_deliver_erasure_outcome`
- `casa/rootfs/opt/casa/tools.py::_apply_erasure_to_disclosure`

**Tests**
- `tests/test_plugin_erase_manifest.py`
- `tests/test_plugin_erase_identity.py`
- `tests/test_plugin_erasure_capture.py`
- `tests/test_plugin_erasure_episode.py`
- `tests/test_plugin_erase_flow.py`

**Related**
- [`architecture/plugin-mutation-tools.md`](../architecture/plugin-mutation-tools.md)
- [`architecture/plugin-result-contract.md`](../architecture/plugin-result-contract.md)
- [`architecture/plugin-setup-turn.md`](../architecture/plugin-setup-turn.md)
- [`architecture/specialist-bundle-transactions.md`](../architecture/specialist-bundle-transactions.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
<!-- END SOURCEMAP -->
