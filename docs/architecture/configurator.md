---
last_reviewed: 2026-11-08
---

# The configurator: a delegated run in the operator's DM

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What the configurator is and how a run of it starts, waits, ends and is told: the image-owned
agent and how it joins the delegation role map, the three origins of a run, the one lease
that admits one run at a time, the busy refusal, the interrupted-run notice at boot, the
Restart now button, the continuations an approval or a plugin erasure owes it, the install
time kept off its ceiling, the plugin-health hold while it installs, and its step line. How a
delegation is launched and bounded is [`delegation.md`](delegation.md); what its result
carries beside the answer is [`delegate-closing-message.md`](delegate-closing-message.md); the approval record, the card
and the run Casa carries on after Approve are [`configurator-approvals.md`](configurator-approvals.md); its lane is
[`agent-lane.md`](agent-lane.md); what its recipes do to the config tree is
[`configuration.md`](configuration.md) and [`tools-interface.md`](tools-interface.md).

## Mental model

**The configurator ships with Casa and is a delegate, not an engagement.** It is built from
the image alone (`agent_loader.py::load_configurator`): kind `executor`, working directory
`/config`, its prompt and doctrine under `defaults/configurator/`, its tools, hooks and
model in the role artifact `defaults/roles/executor/configurator/role.yaml`. Nothing under
`/config` can change it; a release does. `tools.py::_configurator_config` loads it once per
process, and `tools.py::build_agent_role_map`, the one builder of the delegation role map
at boot (`casa_core._build_role_registry`) and on every reload (`sync_agent_role_map`), adds
it unless a resident or specialist already holds the name — so from the first turn after boot
the assistant reaches it the way she reaches a specialist, through the delegate card in her `delegates.yaml` and
`delegate_to_agent`. Each run is a fresh, ephemeral session through the delegated builder;
its system prompt gains a world-state block (`Default vault:`, `Addon version:`, the live
config surface) at every options build.

**A run has two origins and one shape.** The assistant delegates the operator's request, or
Casa continues it on the assistant's behalf after an approval or a plugin erasure, as one
delegation of its own (`configurator_continue.start`,
[`configurator-approvals.md`](configurator-approvals.md)). A reply on one of the
configurator's posts in the resident's chat is the resident's turn, like any reply there that
does not quote a live task's post ([`telegram.md`](telegram.md)). A delegation to it always runs detached (`tools.py::_delegation_mode_for` turns
`sync` into `async`): a commit the run makes can reload the assistant, which would cancel a
run her turn was awaiting. Its answer comes back to her as a delegation result and she tells the
operator what it says, in her own words.

**One run at a time, and a waiter is told who holds it.** Every run takes the lease in
`configurator_run.py` before anything of it starts; the next one waits, and past the bound is
refused with the holder's task in the operator's words. A delegation refused this way comes
back to the assistant as a typed `busy` result carrying that text; a run Casa carries on
after an approval that is refused this way keeps the approval, and the operator is told it
could not be started.

**A restart it needs is the operator's tap.** Inside a run, `casa_restart_supervised`
restarts nothing and returns `deferred`; Casa follows the run's answer with a Restart now
button, so the restart happens when the operator chooses and never cuts the run that asked
for it.

**While it works, the DM shows a step line — unless its log does.** With no agent log on,
`tools.py::_configurator_step_line` posts one silent line at the run's first tool call, edits
it as steps start, and deletes it when the run ends, however it ends
(`drivers/turn_progress.py::TurnProgressLine` with a `delete`). Writes are coalesced, spaced
and bounded, and none is awaited by the run. With the agent log on, it posts no step line: the
run writes its start, words, steps and end to the configurator's own topic like any delegated
specialist run ([`agent-logs.md`](agent-logs.md)).

## Contracts & invariants

**INV-CFG-014**: A configurator run, from any origin, takes the one process-wide configurator lease before its ceiling clock starts and before anything of the run starts, waiting up to `LEASE_WAIT_S` in all, for its lane and then for the holder, for running plugin erasures and for specialist-bundle transactions still running after their handler was cancelled; a run that cannot take it by then is refused with the busy text naming the holder's task and nothing of it runs; the lease is released when the run ends, and a failure before the run starts releases it at once.

The lease is taken in `tools.py::_run_delegated_agent_bounded` (`_configurator_lease`), the
one door every origin goes through, so an approval continuation, an erasure continuation and
a delegation exclude one another alike, and waiting never counts toward the run's ceiling. It
is released by `configurator_run.release_when_done`, a done callback on the run's own task.
A run of any other agent takes no lease.

Every run of the configurator also waits for its lane before the lease — the one lane per
executing agent ([`agent-lane.md`](agent-lane.md)), taken through `tools._delegation_in_lane`
for a delegation and by the continuation's own task for a run Casa carries on — and the run
holding the lease usually holds that lane too. So
the bound starts when the request reaches the lane (`configurator_run.wait_deadline`), covers
the lane wait and the lease wait together, and a request still waiting for the lane at the
bound is refused with the same busy text, its queue place released; the lane only orders the
uses. That refusal is `configurator_run.ConfiguratorBusy`, a subclass of `agent_lane.LaneBusy`,
so every caller that treats a busy lane treats it alike.

The erasures waited for are the plugin erasers
`plugin_remove` and `specialist_uninstall` start (`tools._ERASE_TASKS`): an eraser runs
outside any run, and a new run must not reconfigure a plugin whose data it is still removing.
The bundle transactions waited for are the shielded ones `tools._run_bundle_transaction`
keeps running after a cancelled `specialist_install_commit`, `specialist_upgrade`,
`specialist_rollback` or `specialist_uninstall` (`tools._BUNDLE_MUTATION_TASKS`): a cancelled run's lease clears while
such a transaction still writes `/config`. With no holder, the busy text names whichever is
running, `removing a plugin's data` or `finishing a specialist change`.
While held, the lease is also written to `/data/configurator-lease.json` (mode 0600,
declared in `private_state.FILES`); a write that fails is logged and never refuses the run.

A run whose task is still unwinding when the bounded runner's teardown bound passes keeps the
lease until that task really ends, though the runner itself has returned.

What it does not cover: work a run left behind that outlives its task, such as a worker
thread a plugin tool does not wait for after its handler was cancelled (a tool does wait for
the thread hops it makes under the plugin-mutation lock, INV-TOOL-015 in
[`plugin-mutation-tools.md`](plugin-mutation-tools.md)); only plugin erasers and bundle
transactions are waited for.

**INV-CFG-015**: A configurator run that ends while Casa is stopping, whether cancelled or returning after it swallowed the cancellation, leaves its lease record on disk; the next boot removes that record and, once the channels are up, tells the operator once which task was interrupted, unless the job registry's restart recovery announces the same delegation; nothing of the run is replayed.

`configurator_run.claim_interrupted` runs in `casa_core.main` after the job registry's
recovery and removes the file before any run can start, so a stale record never blocks one;
`announce_interrupted` sends `Casa restarted while the Configurator was working on «task».
Nothing was replayed; ask again if it is still needed.` after the recovered-delegation
notices, and first deletes the run's step line, which the record names once the line is
posted (`configurator_run.record_step_line`). A delegation the assistant started is a job the registry recovers and announces
itself, which is why its id suppresses the second notice; a run Casa carries on after an
approval is a registered delegation too. A cancel outside a stop removes the file; inside one the file stays
however the run ended, since a runner that returns normally from the stop's cancellation was
interrupted all the same. An unreadable or malformed record
counts as one, announced with the task `a configuration task`.

**INV-CFG-016**: A `casa_restart_supervised` call inside a delegated run restarts nothing: it records the need and returns `deferred`; after that run's answer Casa posts one Restart now button, spent on its first tap, which asks the Supervisor for the restart, and a button that expires is edited to say so.

The need is keyed by the run's delegation id (`configurator_run.note_restart`) and consumed
by one `offer_restart`, after the assistant's turn that narrates the delegation's result
(`agent._offers_configurator_restart` around
`Agent.handle_message`) — after her own answer, whatever she told the operator of the
configurator's answer, and not while Casa is stopping. The button is Casa's own keyboard
(`operator_calls.post_question`, 24 h), claimed through the broker, so a second tap is
refused; a tap calls `supervisor_restart.request_app_restart` from a task detached from the
tap's handler, after the card's edit (that call returns only as Casa stops, and a shutdown
must not wait on the handler that asked for it), and a refused request is told in one line. A Casa restart before the tap edits the card as any retired question is
([`scheduled-asks.md`](scheduled-asks.md)). Outside a delegated run — an operator-driven
call — the tool still asks the Supervisor inline.

**INV-CFG-017**: `config_git_commit`, `casa_reload`, `casa_restart_supervised`, `config_trigger_upsert`, `config_trigger_delete` and `casa_reload_triggers` authorise on the executing role — the `execution_role` Casa stamps on a delegated run's origin, else a bound engagement's role, else the origin's role — and refuse every role but the configurator with `not_authorized`.

On a delegated run `origin["role"]` is the caller, the assistant, so a gate reading it would
admit any agent she delegates to and refuse the configurator; Casa stamps `execution_role`
from the config it runs (`tools.py::_run_delegated_turn`), never from anything a model wrote. The reader is
`tools.py::_effective_caller_role`, shared with the narrower in-tool checks of
[`tools-interface.md`](tools-interface.md).

**INV-CFG-018**: While a configurator run holds the lease, the operator DM neither names nor marks the plugin-health rows whose reason is an install phase (consent pending, a setup-provided value unwired, setup pending, not loaded yet, target pending) of the plugins that run is installing; every other row is sent as before; and when the lease is released in this process after such a run, a notify pass runs that can name those rows.

The plugin mutations and env wirings a run makes record their plugins on the held lease
(`tools.py::_record_plugin_installer`; bundle entries by their scoped names), and
`tools.py::plugins_under_live_install` reads them for the DM pass
([`plugin-health.md`](plugin-health.md)). Both halves fail silently. Deferring without the
ending pass leaves an install that stopped short unannounced until some later mutation or
boot, which may never come — so the release schedules the pass, however the run ended.
Deferring by plugin alone would withhold a real fault for as long as the install runs, which
is why the filter keys on the reason as well. A mutation made with no lease held records
nothing. The record is in-process: after a restart nothing is deferred (a spare DM, never a
lost one).

**Approvals and erasures continue the configurator.** A specialist or persona install
card's Approve has Casa start one run of the configurator on the assistant's behalf
(`configurator_continue.start`), whose task text names the next call and carries the
approved values, because that run is a fresh session; it commits exactly those values
([`configurator-approvals.md`](configurator-approvals.md)). Deny and expiry show on the card
only: no continuation runs. The plugin and specialist uninstall erase-choice tap and the
erasure's outcome continue it through the same launcher (`tools.py::_configurator_deliverer`);
when one cannot be started, the operator is told directly. Each continuation is a run like any
other, under the lane and the lease.

**Install time is off the ceiling.** `plugin_add`, `plugin_update`,
`specialist_install_inspect`, `specialist_install_commit`, `specialist_upgrade`,
`persona_install_inspect` and `persona_install_commit` are wrapped by
`tools.py::_off_the_ceiling`, which pauses the run's ceiling clock for the call, up to
`_OFF_THE_CEILING_MAX_S` (30 minutes) per call: past that the clock runs again while the call
goes on, so a call stuck behind a stalled mutation (waiting on the plugin lock) cannot keep
the run, and its lease, alive forever. The ceiling stays the backstop for a run that loops.

## Failure behavior

**The configurator does not load.** `_configurator_config` logs the error and returns
nothing; the role map has no configurator, so a delegation to it is refused as one to an
unknown agent, and nothing else stops. The next role-map sync tries the load again.

**The lease cannot be taken.** Nothing of the run starts; the delegation's result names what
is running. The operator or the assistant asks again.

**Casa stops mid-run.** The run is cancelled with the rest; INV-CFG-015 tells the operator
at the next boot. A half-made change stays as the run left it: nothing is rolled back or
replayed.

**The Restart now button cannot be posted.** The answer has already gone out; the failure is
logged and the restart is not asked for. The operator can ask again.

**An `executor` engagement record from an older install.** It cannot resume
([`engagement-failure-and-restart.md`](engagement-failure-and-restart.md)); the configurator
it named runs only as described here.

## Extension points

**A new origin of a configurator run** must reach `_run_delegated_agent_bounded` and stamp
`execution_role`, so it takes the lease (INV-CFG-014) and passes the privileged gates
(INV-CFG-017); a path that ran the configurator any other way would do both wrongly.

**A new install-like tool** whose call fetches or installs belongs under `_off_the_ceiling`;
a new plugin mutation records its plugins with `_record_plugin_installer` (INV-CFG-018).

**A new restart-needing tool** records the need with `note_restart` rather than restarting
inside the run (INV-CFG-016).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/configurator_run.py::acquire`
- `casa/rootfs/opt/casa/configurator_run.py::release_when_done`
- `casa/rootfs/opt/casa/configurator_run.py::claim_interrupted`
- `casa/rootfs/opt/casa/configurator_run.py::announce_interrupted`
- `casa/rootfs/opt/casa/configurator_run.py::note_restart`
- `casa/rootfs/opt/casa/configurator_run.py::offer_restart`
- `casa/rootfs/opt/casa/configurator_run.py::record_installing`
- `casa/rootfs/opt/casa/agent_loader.py::load_configurator`
- `casa/rootfs/opt/casa/defaults/roles/executor/configurator/role.yaml`
- `casa/rootfs/opt/casa/tools.py::_configurator_config`
- `casa/rootfs/opt/casa/tools.py::build_agent_role_map`
- `casa/rootfs/opt/casa/tools.py::_configurator_lease`
- `casa/rootfs/opt/casa/tools.py::_delegation_mode_for`
- `casa/rootfs/opt/casa/tools.py::_effective_caller_role`
- `casa/rootfs/opt/casa/tools.py::_off_the_ceiling`
- `casa/rootfs/opt/casa/tools.py::_configurator_step_line`
- `casa/rootfs/opt/casa/tools.py::_configurator_deliverer`
- `casa/rootfs/opt/casa/tools.py::_record_plugin_installer`
- `casa/rootfs/opt/casa/tools.py::plugins_under_live_install`
- `casa/rootfs/opt/casa/agent.py::_offers_configurator_restart`
- `casa/rootfs/opt/casa/drivers/turn_progress.py::TurnProgressLine`

**Tests**
- `tests/test_configurator_run.py`
- `tests/test_install_phase_health_dm.py`
- `tests/test_turn_progress.py`
- `tests/test_casa_restart_supervised.py::TestCasaRestartSupervised`

**Related**
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/delegate-closing-message.md`](../architecture/delegate-closing-message.md)
- [`architecture/configurator-approvals.md`](../architecture/configurator-approvals.md)
- [`architecture/agent-lane.md`](../architecture/agent-lane.md)
- [`architecture/plugin-health.md`](../architecture/plugin-health.md)
- [`architecture/hook-resolution.md`](../architecture/hook-resolution.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/configuration.md`](../architecture/configuration.md)
<!-- END SOURCEMAP -->
