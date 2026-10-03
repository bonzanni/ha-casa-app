---
last_reviewed: 2026-10-03
---

# Stored-call buttons

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a specialist's plugin proposes a decision to the operator as buttons, and how a tap
executes exactly the call each button stood for: the `operator_proposal` delivered kind and
its deposit rule, the broker registration and the keyboard, the tap's admission chain, the
re-checks under the desk lock, the pinned one-call turn that executes the stored call, the
receipt, the bounded termination of that turn's processes and the faulted desk, and what the
resident learns. The delivered-slot path a proposal rides on is
[`plugin-delivered-slots.md`](plugin-delivered-slots.md); the desk a tap uses is
[`specialist-desk.md`](specialist-desk.md); the result contract and the hooks are
[`plugin-result-contract.md`](plugin-result-contract.md); the callback transport is
[`telegram.md`](telegram.md). Telegram only.

## Mental model

**The plugin proposes, the operator decides, Casa executes; the model is only the hands.**
A capability tool of a specialist's plugin declares a slot delivering `operator_proposal`
and deposits a JSON object: a text (one page, with Casa's label line) and one to six
buttons, each a label and a *stored call* — a bare tool name of the same plugin, resolved
through the depositing call's own server, with fixed JSON arguments. Casa posts the text
labelled with the buttons as an inline keyboard, filed in the post map like any post. When
the operator taps, no ordinary model turn runs and no model decides anything: Casa runs one
short *pinned* specialist turn whose prompt names the one call and whose hooks allow exactly
that call — the stored tool with the stored arguments, byte for byte in canonical JSON —
and deny every other call and any second call. The plugin's own response is the receipt,
posted labelled; a plugin refusal (its revision guard) is a receipt too. Nothing the model
writes reaches the operator or the resident; the pinned turn retains nothing into memory.

**Which calls a button may store.** At deposit, every button's tool must be a declared tool
of the same plugin on the same (single, stdio) server — a `safe` tool that consumes no
reference, or a `capability` tool whose only provided slot is its own `operator_proposal`
(the `More` exception: a button whose call renders the next page, with its own buttons). A
setup tool, a protected tool, a tool declared on an ambiguous or HTTP server, and any
tool that consumes a reference are refused; so is a runtime name in place of a bare one.
Arguments are a JSON object with identifier keys (no `_` prefix), values that are strings
up to 2,000 characters without `<` or `>`, booleans, null, integers within ±(2⁵³−1), lists
or objects of the same, at most 4,096 bytes, round-tripping through canonical JSON. The
grammar is narrow because the CLI decodes a call's arguments in JavaScript and its MCP
wrapper rewrites some values; a value a transformation could touch is refused at deposit
rather than repaired after posting, and the pin never normalises.

**Register, then post; bound; supersede.** The composed post — label line and text — must
render to one page with room left for the longest line Casa appends when the keyboard
settles (`☑ <label>` with a label of 32 characters that may each take two UTF-16 units;
`✖ <reason>`, `⌛ expired` and `↻ replaced` are shorter), so a maximal proposal can always be
settled. Before the message is sent the proposal is
registered with the verdict broker under the `proposal` namespace with a one-hour deadline
and a live record (chat, operator, role, artifact, plugin segment, the stored calls, the
text) — so a tap can never find a keyboard the broker does not know. At most 32 live
proposals per chat; the 33rd deposit is withheld. A non-empty `revision` supersedes the
earlier live proposals of the same chat, plugin and role carrying the same revision (their
keyboards are edited to `↻ replaced`). A post that is not proven unregisters at once and
withholds the result. A Casa restart empties the broker: a tap on an older keyboard answers
"expired".

**The tap's admission is a chain, fail closed, nothing claimed before every check passes.**
The callback's message and sender are present; the record is live; its chat is the
callback's chat; the message id is the message the keyboard was posted as; the index names a
button; the tapper is the operator the proposal was posted for and is still the configured
operator; the deadline has not passed. Then one claim and one commit — a second tap is
"already answered". The handler edits nothing and dispatches nothing: the finish hook the
post installed edits the keyboard away first (`☑ <label>`), then reserves the desk.

**A tap is one desk use, re-checked under the lock on one captured build input.** Under the
specialist's desk lock, immediately before the session build, Casa captures ONCE exactly the
derivations that decide which artifacts and tools the session admits — the role's plugin
resolution, the environment withholding, the protected map, the contract map and the
profile plan — and judges the stored call against them: the resident still declares the
specialist; the plugin is still assigned to the role; the artifact is the one the proposal
ran on; the profile still allows the tool; the plugin's erasure is not running; the stored
call still passes the deposit predicate against the live maps; the deadline has not passed.
Each refusal is a named reason (`✖ profile`, `… could not apply your tap (profile).`), never
a silent `no_call`, and a refused tap is not a use. The pinned session is then built from
that same captured input, unchanged — no second resolve, no rebuild — so a replacement
artifact carrying the same tool name cannot be reached after the check. The permit is taken
after the lock, as a desk reply takes it.

**The pin and the capture.** The pin is one predicate applied in two places, because the CLI
runs matching PreToolUse hooks concurrently and one hook's deny does not stop its siblings:
inside the plugin admission hook as its first check, and as a `matcher=None` matcher first
in the list for every non-plugin tool. The capture resolves at the END of the result hook
with the hook's own effective result: a `safe` tool's response text; for `More`, the
delivery receipt of the proposal it just posted (the landed proposal is then the sole
visible receipt), the withheld replacement, or the no-post pass whose own text is the
receipt. A denied call never reaches a post-hook, so a denied call is never a receipt. A
validated capture is authoritative over the turn's own ending — a model that then hit its
turn limit, stalled or aborted still produced the receipt. The CLI's reported post-hook
input is compared with the stored canonical: on a difference the receipt is still posted,
with one Casa line above it (`⚠ the CLI reported this call's arguments changed by an
installed hook`), the echo says the same, and an ERROR names the run, the tool and both
forms. Nothing is prevented or retried: installed hooks are trusted (as every plugin is).

**The controller owns the run's processes.** The pinned turn is not driven by the bounded
delegation wrapper. A `PinnedRun` enters the SDK client, pins a pidfd on the CLI and on
every descendant it finds at start, and owns the client to the end; the runner never exits
it. On the turn's normal end the controller starts the SDK's close as a detached task and
confirms every pinned process's exit by pidfd readability under one deadline — nothing
inside the SDK's teardown is awaited. On the desk turn's ceiling, or when a process is still
alive after that, the operator is told at once (`✖ failed`), and the controller cancels the
execution task (5 s), signals the CLI through its pidfd (SIGTERM, 5 s, SIGKILL) and the
descendants (SIGKILL), seals its own hook callbacks — a callback entering after the seal has
no effect; one already inside is drained — waits for every pidfd under 10 s, discards the
confirmed-dead CLI from the SDK's reaper set, and only then releases the desk and the
permit. A receipt captured during the hold is still posted.

**One release path.** Every end of a pinned run — the normal end, the ceiling and a
cancellation (a Casa stop cancels the turn's task) — goes through one settle function, run
shielded from any cancellation, whose only input is the confirmation predicate: every pinned
fd's own pidfd readable, every callback drained, every identity established. On it, together
and in order, the function decides the fault (unconfirmed — a teardown itself cancelled by a
loop shutdown included — faults the desk, tells, refreshes the health report), deletes the
pinned run's transcript — at once when the exit is confirmed, otherwise through a detached
waiter that runs after the pinned fds report exit, since an unconfirmed writer may still
flush it — closes the fds and releases the permit; nothing else in the tap performs any of
these. A pinned fd is dropped as extinct only on its own pidfd's evidence,
never on a `/proc` read's outcome: a descendant that is unreadable or whose descent from the
CLI cannot be proven is kept, awaited, never signalled, and leaves the run unconfirmed. The
controller owns the client from before its entry is awaited, so a ceiling that fires while
the CLI's initialisation hangs still finds the process; an entry cancelled with no process
to pin is unconfirmed. A tap cancelled in-process after its call ran is never silent: an ERROR
names the run, the exchange is logged and the resident's line records the applied tap. The
process's own exit — the event loop's shutdown sweep cancelling every task at once — is outside
this: in-memory state and pidfds do not outlive the process, a restart starts clean, and a CLI
child or an in-flight reply lost at that moment is what every delegation and resident turn
already accepts today.

**Bounded best effort, told.** If a pinned process's exit stayed unconfirmed at the
deadline, or its identity could not be established while pinned, or a callback was still
inside, the desk is *faulted*: every later use of it — reply, tap, delegation — is refused
at once with a labelled notice (`📊 Finance's desk is faulted; a Casa restart clears it.`),
the permit is released, an ERROR is logged, and the standing plugin-health report carries a
`desk_faulted` row against the plugin and the specialist until a restart. A worker a plugin
daemonises or spawns after the snapshot is outside the pin: that is the plugin's own
responsibility, an accepted risk rather than a Casa guarantee.

**The desk and the echo.** A committed tap that reaches execution appends one exchange —
`[tapped: <label>]` and the receipt's first line, `[posted a proposal]` for a landed `More`,
or `[no receipt]` — and the resident's echo gets one body-free line (`📊 Finance applied your
tap (Yes).`, `📊 Finance refused your tap (Yes): no_call.`). A typed verdict still takes the
desk: a reply on the proposal message routes to the specialist by the post map.

## Contracts & invariants

**INV-PROP-001**: A tap on a proposal button admits for execution exactly the stored call bound to that button when the proposal was posted — the same tool, the same arguments by value as Casa's pin compares them, with any rewrite by an installed hook told on the receipt and logged, never prevented — at most once per proposal, on a desk that is not released to a later use until the CLI and the plugin servers it started are terminated or the desk is faulted, only when tapped by the operator the proposal was posted for, on the message it was posted as, in the chat it was posted in, and only while the specialist that posted it is still assigned the same plugin artifact with a profile that allows that tool; every other callback is answered and executes nothing.

What it does not cover: a worker a plugin daemonises or spawns after the controller's
snapshot (the plugin's responsibility); the time between the tap and the desk lock, during
which the world may move — the re-checks run after the wait, not before it; the process's own
exit sweep, which ends every task at once (Casa-wide behaviour, not S5's).

**INV-PROP-002**: No ordinary model turn runs on a tap; between the tap and the receipt exactly one pinned specialist turn may run, and in it exactly one tool call can execute — the stored tool with the stored arguments, canonical JSON for canonical JSON — on a specialist session the existing builder built from the captured input, under the desk lock; every other call, and any second call, is denied before it runs; nothing the model writes reaches the operator or is retained; the receipt is the executed call's own response, posted labelled and bounded, the plugin's refusal included; a turn with no executed call is a refusal notice with no retry; the resident learns of it only by a body-free echo line.

**INV-PROP-003**: A keyboard with stored calls exists only for a deposit from a tool declaring the `operator_proposal` slot, whose calls name the same plugin's declared tools with fixed reference-free arguments within the bounds, and whose message was proven delivered; a proposal that did not land holds no stored call, a chat holds at most 32 live proposals, and a proposal expires after one hour.

## Failure behavior

**A callback by someone else, in the wrong chat, on another message, with a bad index, after
expiry, after a restart, or a second tap.** A toast only — `expired`, `invalid`, `not for
you`, `already answered`; the keyboard is untouched, nothing runs.

**A re-check fails** (not delegable, plugin unassigned, plugin changed, profile, plugin
erasing, undeclared, protected, transport). `✖ <reason>` on the keyboard, one labelled
notice, the refused echo line; no turn, no exchange.

**The desk queue is full, or the permit is refused.** `✖ busy`, `📊 Finance is busy; the
specialist will propose again, or type your verdict.`, the refused echo line.

**The turn ends with no executed call, aborts, raises, or hits the ceiling.** `✖ failed`,
`📊 Finance could not apply your tap (<kind>).`, the exchange with `[no receipt]`, the
refused echo line. On the ceiling the notice goes out at once and the desk stays held
through the bounded termination path; a receipt that lands during the hold is posted after
it, with the applied echo line.

**A `More` proposal was withheld or not proven.** The refusal notice with the reason; a
`More` that posted nothing (the contract's no-post shape) is a success whose own text is the
receipt.

**The receipt's send fails.** `📊 Finance applied your tap; the receipt did not go out.` —
the call ran; the exchange carries the receipt; the applied echo line.

**Termination unconfirmed at the deadline.** The desk is faulted (above); the plugin-health
report says so until restart.

**A Casa stop while a tap runs.** The channel's stop cancels the turn's task in-process: the
settle function runs before the cancellation propagates — on the run, or on the receipt's send; an unconfirmed exit faults the desk for the
rest of the process's life; a receipt captured meanwhile cannot be posted: an ERROR names the
run, the exchange and the applied echo line are still recorded.

**The keyboard edit itself fails after a commit.** The operator must still see which button won
before any effect: one labelled notice names it and says the buttons could not be cleared, then
the tap is applied as usual — the commit already excludes a second execution, so the stale
buttons only answer a toast.

**A proposal superseded while its own send is in flight.** The finish hook finds no message
id yet and leaves the terminal line for the poster, which applies `↻ replaced` the moment the
message lands; no live record remains for it.

**Casa restarts.** The broker, the post map, the desks and their faults are gone; a tap on
an older keyboard answers "expired".

## Extension points

**A file sent as a reply** (a later slice) routes through the same post map into the
specialist's inbox.

**Reusing the controller** for the desk-reply early-release overlap (#1197) is possible but
not done here; the bounded termination path stays simple.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/stored_calls.py`
- `casa/rootfs/opt/casa/pinned_run.py`
- `casa/rootfs/opt/casa/result_broker.py::proposal_ok`
- `casa/rootfs/opt/casa/result_broker.py::_post_proposal`
- `casa/rootfs/opt/casa/result_broker.py::_capture_of`
- `casa/rootfs/opt/casa/specialist_desk.py::handle_tap`
- `casa/rootfs/opt/casa/specialist_desk.py::faulted_desk_issues`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._on_proposal_callback`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.proposal_finish_hook`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._dispatch_proposal_tap`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.deliver_operator_proposal`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.mark_proposal`
- `casa/rootfs/opt/casa/tools.py::_capture_build_input`

**Tests**
- `tests/test_stored_calls.py`
- `tests/test_proposal_slot.py`
- `tests/test_proposal_tap.py`
- `tests/test_desk_tap.py`
- `tests/test_pinned_run.py`
- `tests/test_pinned_wiring.py`

**Related**
- [`architecture/specialist-desk.md`](../architecture/specialist-desk.md)
- [`architecture/plugin-delivered-slots.md`](../architecture/plugin-delivered-slots.md)
- [`architecture/plugin-result-contract.md`](../architecture/plugin-result-contract.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/plugin-health.md`](../architecture/plugin-health.md)
- [`architecture/output-boundary.md`](../architecture/output-boundary.md)
<!-- END SOURCEMAP -->
