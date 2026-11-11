---
last_reviewed: 2026-11-11
---

# A tap on a stored-call button

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What happens when the operator taps a stored-call button: the tap's admission chain, the
re-checks on the executing agent's lane on one captured build input, the pinned one-call turn
that executes the stored call, the pin and the capture, the receipt, the direct call of a
plugin's stop tool, and the note the chat's agent gets. How a plugin proposes the buttons, which
calls a button may store and the `📎` arming are [`stored-call-buttons.md`](stored-call-buttons.md);
the bounded termination of the pinned turn's processes and the faulted agent are
[`stored-call-termination.md`](stored-call-termination.md); the next card a tap may bring is
[`stored-call-next-card.md`](stored-call-next-card.md); the lane is
[`agent-lane.md`](agent-lane.md). Telegram only.

## Mental model

**The tap's admission is a chain, fail closed, nothing claimed before every check passes.**
The callback's message and sender are present; the record is known (live, or settled and
remembered — below); its bot and chat are the callback's; the message id is the message the
keyboard was posted as (if none, the request id proves it and a live one is bound to it); the index names a button; the tapper is the operator the proposal was posted for and is still the configured
operator; the deadline has not passed. Then one claim and one commit — a second tap is
"already answered". The handler edits nothing and dispatches nothing: the finish hook the
post installed edits the keyboard away first (`⏳ <label>`; `☑ <label>` for `📎`), then reserves a place on the agent's lane —
except a stored call of the declared stop tool, which it calls at once, before any edit
([`plugin-stop.md`](plugin-stop.md)).
A settled proposal's answer does not end with the broker's short-lived record of it: at the
moment of settlement the channel remembers the proposal's own record and what happened —
answered, replaced, or ended otherwise (the deadline, a shutdown) — until the deadline, so
a tap that passes the same chain answers "already answered", "replaced" or "expired" for
the proposal's whole hour; only after every check has passed, so someone else is still
told "not for you". The memory lives in the process and keeps a chat's latest 256 settled
proposals (`_PROPOSAL_SETTLED_PER_CHAT`). A tap on one it has dropped from that bound is answered
from the broker's record, as before the memory existed: "already answered" while an answered
proposal's record lasts (`_RETIRE_S`, 60 seconds from its settlement), "expired" otherwise.

**A tap is one use of the agent's lane, re-checked under its lock on one captured build
input.** Under the executing agent's lane lock ([`agent-lane.md`](agent-lane.md)), immediately before the session build, Casa captures ONCE exactly the
derivations that decide which artifacts and tools the session admits — the role's plugin
resolution, the environment withholding, the protected map, the contract map and the
profile plan — and judges the stored call against them: the card's agent is loaded and
this chat is its own; the plugin is still assigned to the role; the artifact is the one the proposal
ran on; the profile still allows the tool; the plugin's erasure is not running; the stored
call still passes the deposit predicate against the live maps; the deadline has not passed.
Each refusal is a named reason (`✖ profile`, `… could not apply your tap (profile).`), never
a silent `no_call`, and a refused tap is not a use. The pinned session is then built from
that same captured input, unchanged — no second resolve, no rebuild — so a replacement
artifact carrying the same tool name cannot be reached after the check. The agent's
concurrency permit is taken after the lock and never waited for.

**What the pinned turn is given.** Its system prompt is the specialist's own role prompt
followed by a stored-call section: the call is a tool of a plugin assigned to it, the
operator's tap is the authorisation, and the call is not a delegation to judge against the
role's scope or remit. Its one prompt opens with a `<stored_call_context>` block naming the
caller as the operator, by a tap on the specialist's own proposal, in place of the
`<delegation_context>` lines a delegated turn opens with: a role prompt that limits which
delegations it answers refused the call when the tap read as the resident's delegation
(#1308). It has no [scratchpad](scratchpad.md) and is not told of one (INV-PAD-005). Then the task: Casa's note that the operator tapped the
button, the stored tool's runtime name and its canonical arguments, the instruction to make
that call even if it looks out of date or already done because the plugin decides whether the
tap still applies and its answer is what the operator sees, and to write nothing to the
operator. It carries no earlier exchange, no recalled memory and no frame calling the task the
operator's message (#1282): those are what a model would judge the tap by, and the judgement
is the plugin's (its revision guard). That lowers the chance the model ends without the call;
it cannot remove it — nothing in the session forces a tool call — and such a turn is the
refusal notice, with no retry.

**The pin and the capture.** The pin is one predicate applied in two places, because the CLI
runs matching PreToolUse hooks concurrently and one hook's deny does not stop its siblings:
inside the plugin admission hook as its first check, and as a `matcher=None` matcher first
in the list for every non-plugin tool. The capture resolves at the END of the result hook
with the hook's own effective result: a `safe` tool's response text; for `More` or its file sibling, the
delivery receipt of the post it just made, with the delivered kind from the tool's own
contract (the landed post is then the sole visible receipt), the withheld replacement, or the no-post pass whose own text is the
receipt. A passed-through text (the `safe` response, the no-post) that parses as a JSON
object with a non-blank string `receipt` is captured as that string, as given; anything else
— plain text, another JSON shape, a text over the broker's parse ceiling or nested too deep
to parse — is captured verbatim. The member is read only here: it never changes which kind
a `More` result is. A denied call never reaches a post-hook, so a denied call is never a receipt. A
validated capture is authoritative over the turn's own ending — a model that then hit its
turn limit, stalled or aborted still produced the receipt. The CLI's reported post-hook
input is compared with the stored canonical: on a difference the receipt is still posted,
with one Casa line above it (`⚠ the CLI reported this call's arguments changed by an
installed hook`), the chat's note says the same, and an ERROR names the run, the tool and both
forms. Nothing is prevented or retried: installed hooks are trusted (as every plugin is).

**A stored call of the plugin's declared stop tool takes no lane.** After the commit, a tap
whose stored call is the `casa.stopTool` of the proposal's own plugin and artifact, with
arguments exactly `{"ref": <string>}`, runs no pinned turn and none of the re-checks above:
Casa calls the tool directly (`_stop_tap`, [`plugin-stop.md`](plugin-stop.md)).

**The stored tool is in the turn's first request.** The pinned turn has one prompt, and the
pin admits no call but the stored one, so the stored tool must already be callable when that
prompt is sent (#1220). Two things would hide it. The CLI defers every MCP tool that is not
`alwaysLoad` while `ToolSearch` is on the surface — whatever the number of tools — and the
pin denies the `ToolSearch` that would load it, so the pinned build disallows `ToolSearch`,
once; without it the CLI loads every tool up front. Ordinary specialist turns keep it
(`tools.py`, the ruling above `_SUBAGENT_SPAWN_TOOLS`). And the CLI starts plugin MCP servers in
the background: a server still connecting or listing its tools contributes none to the
request being built. So after the controller enters the client, the pinned runner asks the
CLI for its MCP status (`_await_mcp_servers_settled`) and sends the prompt only once no server
is `pending`. `pending` lasts through the connect and the tool listing, each bounded by the
CLI's own MCP timeout; a server that failed is settled too, and the turn then has no stored
tool to call — a refusal notice. The tap's ceiling bounds the whole turn, the wait included.
Measured on the pinned CLI version: these are its behaviours, not Casa's, and a CLI bump
re-measures them.

**The pinned turn's processes, its one release path and the faulted agent** are
[`stored-call-termination.md`](stored-call-termination.md).

**The chat's note.** A committed tap that reaches execution leaves one body-free note for the
agent of the chat the card is in (`chat_notes`, [`own-voice.md`](own-voice.md)), which it reads
at its next turn: `📊 Finance applied your tap (Yes).`, `📊 Finance refused your tap (Yes):
no_call.`. A typed verdict is an ordinary message: a reply on the card is the chat's agent's
turn, or goes into the task the card belongs to while that task lives
([`telegram.md`](telegram.md)).

## Contracts & invariants

**INV-PROP-001**: A tap on a proposal button admits for execution exactly the stored call bound to that button when the proposal was posted — the same tool, the same arguments by value as Casa's pin compares them, with any rewrite by an installed hook told on the receipt and logged, never prevented — at most once per proposal (a `keep_card` button: once per admitted tap, settling nothing — INV-PROP-010), on the executing agent's lane, which is not released to a later use until the CLI, the plugin servers it started and the ordinary children those servers started by termination time are terminated or the agent is faulted, only when tapped by the operator the proposal was posted for, on the message it was posted as, in the chat it was posted in, and only while the specialist that posted it is still assigned the same plugin artifact with a profile that allows that tool — a stored call of the plugin's declared stop tool instead takes no lane and no profile check and is called directly while the role's resolution still carries that artifact declaring it (INV-PLUG-056); every other callback is answered and executes nothing.

What it does not cover: a worker a plugin deliberately detaches — `setsid`, a double fork —
so that it is no descendant of the CLI at termination time (the plugin's responsibility); the time between the tap and the lane lock, during
which the world may move — the re-checks run after the wait, not before it; the process's own
exit sweep, which ends every task at once (Casa-wide behaviour, not S5's).

**INV-PROP-002**: No ordinary model turn runs on a tap; a stored call of the plugin's declared stop tool runs no turn at all and is called directly (INV-PLUG-056); for every other stored call, between the tap and the receipt exactly one pinned specialist turn may run, and in it exactly one tool call can execute — the stored tool with the stored arguments, canonical JSON for canonical JSON — on a specialist session the existing builder built from the captured input, under the lane lock; every other call, and any second call, is denied before it runs; nothing the model writes reaches the operator or is retained; the receipt is the executed call's own response — its `receipt` sentence when the response's text is a JSON object carrying a non-blank string `receipt`, otherwise the text verbatim — posted labelled and bounded, the plugin's refusal included; a turn with no executed call is a refusal notice with no retry; the chat's agent learns of it only by a body-free note.

**INV-PROP-004**: A pinned stored-call turn is built with `ToolSearch` disallowed, and its one prompt is sent only after the CLI's MCP status lists no server of the session as pending; an ordinary specialist turn's build adds no `ToolSearch` denial and its prompt is sent without asking for MCP status.

**INV-PROP-005**: A pinned stored-call turn's prompt carries no earlier exchange of the agent, no memory recalled for the turn, and no frame presenting its task as a message from the operator.

## Failure behavior

**A callback by someone else, in the wrong chat, on another message, with a bad index, after
expiry, after a restart, a second tap, or a tap on a replaced proposal.** A toast only —
`expired`, `invalid`, `not for you`, `already answered`, `replaced`; the keyboard is
untouched, nothing runs.

**A re-check fails** (agent unavailable, not this chat's card, plugin unassigned, plugin changed, profile, plugin
erasing, undeclared, protected, transport). `✖ <reason>` on the keyboard, one labelled
notice, the refused note; no turn.

**The lane's queue is full, or the permit is refused.** `✖ busy`, `📊 Finance is busy; the
specialist will propose again, or type your verdict.`, the refused note.

**The turn ends with no executed call, aborts, raises, or hits the ceiling.** `✖ failed`,
`📊 Finance could not apply your tap (<kind>).`, the refused
note. On the ceiling the notice goes out at once and the lane stays held
through the bounded termination path; a receipt that lands during the hold is posted after
it, with the applied note.

**A `More` proposal or a stored file was withheld or not proven.** The refusal notice with the
reason; one that posted nothing (the contract's no-post shape) is a success whose own text — its
`receipt` sentence when it carries one — is the receipt.

**A stored call ran with arguments an installed hook changed** (both post-hooks compare): a
proposal carries the tell in its own message; a landed file, or a refused call, gets one
`📊 Finance ⚠ …` notice — after the file, before the refusal.

**The receipt's send fails.** `📊 Finance applied your tap; the receipt did not go out.` —
the call ran; the applied note.

**The keyboard edit itself fails after a commit.** The operator must still see which button won
before any effect: one labelled notice names it and says the buttons could not be cleared, then
the tap is applied as usual — the commit already excludes a second execution, so the stale
buttons only answer a toast. For a `📎` tap the notice is only `📊 Finance ☑ 📎 Add a document — the buttons could not
be cleared.`: it names no destination and nothing is applied.

**Casa restarts.** The broker, the settled-proposal memory, the lanes and the agents'
faults are gone; a tap on an older keyboard answers "expired".

## Extension points

**A tap never starts a job.** The pinned turn's session is a specialist's delegated build, so it
is offered `start_job` when the specialist's plugins declare a job, but the pin refuses every
call except the stored one, and a stored call is a plugin tool. A plugin that wants a tap to
lead to work posts a proposal or a line, and the operator's next typed ask starts
it ([`specialist-job-start.md`](specialist-job-start.md)).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/pinned_run.py`
- `casa/rootfs/opt/casa/result_broker.py::_capture_of`
- `casa/rootfs/opt/casa/stored_call_settler.py::handle_tap`
- `casa/rootfs/opt/casa/agent_lane.py::FaultRegistry.issues`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._on_proposal_callback`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.proposal_finish_hook`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._dispatch_proposal_tap`
- `casa/rootfs/opt/casa/tools.py::_capture_build_input`
- `casa/rootfs/opt/casa/tools.py::_await_mcp_servers_settled`

**Tests**
- `tests/test_proposal_tap.py`
- `tests/test_stored_call_tap.py`
- `tests/test_pinned_run.py`
- `tests/test_pinned_wiring.py`
- `tests/test_redcase_1282.py`
- `tests/test_pinned_prompt_controls.py`

**Related**
- [`architecture/stored-call-buttons.md`](../architecture/stored-call-buttons.md)
- [`architecture/stored-call-termination.md`](../architecture/stored-call-termination.md)
- [`architecture/stored-call-next-card.md`](../architecture/stored-call-next-card.md)
- [`architecture/agent-lane.md`](../architecture/agent-lane.md)
- [`architecture/plugin-stop.md`](../architecture/plugin-stop.md)
- [`architecture/own-voice.md`](../architecture/own-voice.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
<!-- END SOURCEMAP -->
