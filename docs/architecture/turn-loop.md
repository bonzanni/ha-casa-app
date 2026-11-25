---
last_reviewed: 2026-11-23
---

# The turn loop

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How one inbound message becomes one agent turn: what is assembled before the model is
called, the token stream and resume; how the call is retried, and when a fault ends the
turn, is [`turn-faults.md`](turn-faults.md). What bounds the turn — a resident turn's stop at its
turn limit, the one line Casa adds about it, and the limits the residents run with — is
[`turn-limits.md`](turn-limits.md). It does not cover the life of
the warm client the turn runs on ([`sdk-client-pool.md`](sdk-client-pool.md)),
where the message came from (the channels), what the model is allowed to do inside the turn
(authorization), or how memory is stored and recalled — only the point at which memory is
read into a turn.

## Mental model

A turn is a single pass with a fixed shape: take a message, decide whether it continues an
existing conversation or starts a fresh one, assemble a system prompt and a memory block,
call the model with retry, stream the output, and record what happened. On a write-trusted
channel the turn's opening text and each message the resident writes are also captured into
the memory store as they happen ([`memory-capture.md`](memory-capture.md)).

The client the turn calls is usually a warm one the pool hands it rather than one this turn
created; what that reuse guarantees, and what a close or a reset owes it, is
[`sdk-client-pool.md`](sdk-client-pool.md).

**One scope per turn, minted before anything streams.** `handle_message` mints a
`TurnScope` after delegation synthesis has rebound the message and before the streaming
callback is obtained, under the reserved context key `_turn_scope`; `_process` mints one for
a direct caller that arrives without it, and puts the same object on the origin snapshot as
`turn_scope` — a live value like `speaker_provenance`, never persisted — where the tool
handlers and the read-evidence hooks find it, and hands `_make_on_message` the same scope.
Whether the turn streams is the scope's `streaming_allowed` — a scheduled turn, event
wake or own-chat greeting is minted with `NoStream`: no token callback. On Telegram the
callback writes a draft, which `handle_message` ends on every way out of the model's turn
before the reply goes out as one ordinary send, and a press of the draft's Stop ends the turn
([`telegram-reply-drafts.md`](telegram-reply-drafts.md)). The model's final text
passes through `scope.admit(FINAL_REPLY, …)`, which judges closing silence itself, on the
unannotated text, so a `<silent/>` turn is suppressed before any line could be added and
prose after a sentinel is delivered whole; a classified-error reply is Casa's own text; the
plugin-health notice is prepended outermost over the admitted value, except on a
`deliver: operator` or `operator_always` webhook turn, whose reply reaches the operator
through one fresh delivery context on every path (INV-TRIG-018 in
[`webhook-delivery.md`](webhook-delivery.md)), and on a summoned turn, whose notice is its
own message to the operator ([`guest-summons.md`](guest-summons.md)); and every streamed
cumulative `_emit` releases is admitted too, after the INV-TURN-009 hold has judged the
unannotated cumulative with the same predicates. Options assembly is where the
read-evidence matchers join every resident's hook bundle. What the scope does with the text
is [`output-boundary.md`](output-boundary.md) (its three decisions: INV-OUT-006 in
[`output-scope-properties.md`](output-scope-properties.md)).

**The turn's question and Casa's notes.** What the query carries ahead of the message's
text — the time envelope and Casa's per-turn notes, summons notes among them — what the
origin records as the turn's question, and how a specialist's own conversation runs this
loop are [`turn-notes.md`](turn-notes.md).

**A friend's summons leaves the loop early.** A message on the guest channel
(`"telegram_guest"`) — a friend summoning a specialist's bot in a group the operator allowed —
branches off in `_process` once the origin snapshot and scope exist, before the turn's
identity, the session gate, the memory view, the correction note, chat notes, capture or the
pool, into `Agent._guest_turn`: one bounded run on a client of its own, built by
`tools._build_guest_options`, retried whole and cut at its timeout
([`guest-summons.md`](guest-summons.md), INV-GUEST-002, INV-GUEST-003).

Three things the loop only orders are [`plugin-setup-turn.md`](plugin-setup-turn.md)'s: a
dispatched setup turn's re-check of its obligation under the session gate, the handover of
an ordinary turn's plugin-tool results to the setup store (INV-PLUG-023, INV-PLUG-030), and
the setup-outcome report its `finally` makes before the reply is admitted or delivered
(INV-PLUG-012, INV-PLUG-024).

## Contracts & invariants

**INV-TURN-004**: A memory read that raises does not fail the turn. It is logged and the turn proceeds with an empty memory block.

What it does not cover: nothing downstream is told. The unavailable-versus-empty distinction
is flattened to omission at this point — observable in logs, not in the prompt — and the
memory view and the correction note fail independently, so either may be present beside the
other's absence.

**INV-TURN-009**: The token stream never carries a cumulative that is nothing but `<silent/>` sentinels and whitespace, nor — on the partial-delta path — one that could still become such silence. With a token callback present, a canonical-fold cumulative that is not silence, and a partial-delta cumulative that cannot still become silence — less an unfinished trailing sentinel — is handed to the callback as admitted (INV-OUT-006), unless it equals, or after a dropped sentinel run is a prefix of, the last cumulative already handed to that callback, or the attempt has folded the result of a call left waiting on the operator's approval — or of a synchronous `delegate_to_agent` call whose child's run left one waiting (#1207) — with no later result of a call that consumed the same grant, directly or in such a delegate's child — then nothing is handed over (#1252).

The sentinel is the model's way of saying "send nothing", and the gate above already
honours it — but only at the end of the turn, after the stream has run. So a turn that
chose silence used to post the literal to the operator first, and on that path nothing
ever took it back: the gate empties the text, delivery is skipped, and the teardown hook
only releases the typing indicator. The hold moves the decision to the emission itself.
It is stated per emission rather than per turn, because on the partial-delta path a
provisional sentence can be streamed and then replaced by a canonical fold of pure
silence; what has been spoken cannot be unspoken, and the guarantee is that the
correction adds nothing further. Release hands over the cumulative as admitted (#1512)
and never takes back words already handed over; and because the fold uses the same
predicate as the final gate, the stream never holds something the gate would deliver.
Nothing records a held cumulative, so a hold can never suppress its own release. On a
held turn the teardown hook is the only thing that stops the typing indicator, since the
first-token teardown never runs.

The fold also keeps each text-bearing message apart (`state["messages"]`, beside the joined
`state["text"]`). `_process` publishes the winning attempt's tuple as
`turn_report["reply_messages"]` and the number of attempts that ran as
`turn_report["attempts"]`; a stale-resume re-run consumes no retry but is still an attempt.
Its return value stays the joined text. A buffered turn's final-reply admission reads them
for the #1075 closing-silence rule (INV-OUT-006 in
[`output-scope-properties.md`](output-scope-properties.md)), which leaves the stream and this
hold unchanged. The winning attempt's `turn_report["approval_cut"]` — how many messages came
before a call still waiting on an approval — is read with them on any turn, for the #1252 rule
there, which also stops the stream at the cut (INV-TURN-009).

**INV-TURN-012**: A conversation is resumed only while the structural surface of its system prompt — the delegates and background jobs the agent can reach; for a role whose Home Assistant tools the facade publishes, the names of the tools last published; and the clearance the turn reads at — still digests to what the session was registered with. A surface that differs, or a session that never recorded one, starts a fresh session with the old one retained. The surface is rendered once per turn and the same render is what the resume decision gates on, what the prompt carries, the published tool servers the session connects with, and what the registration stores.

The CLI pins a session's system prompt when the session is created. Rebuilding options on a
cold connect therefore does not reach a conversation that is being resumed: the prompt is
re-rendered, handed over, and the resumed session keeps the one it already had. So a plugin
that declares a new background job, or a delegate that gains one, became reachable for every
*new* conversation and for none of the open ones — silently, because the model simply does
not name a capability its prompt never listed, and the request falls back to an ordinary
delegation that looks like a plausible answer. There is no error to see. The pool's own
freshness machinery cannot close this: dropping a warm client only forces a cold connect,
and the next connect resumes the same session and restores the same prompt.

Only the *structural* blocks are digested. The memory and channel-context blocks change per
turn and per sender, and digesting them would retire the conversation on nearly every turn —
costing both the thread and the cached prompt prefix that makes a long conversation
affordable. A session that never recorded a digest is treated as a mismatch rather than as
consent: its prompt was never observed, so it cannot be certified as current, and recording
the present digest for it would assert a match that does not exist and mask the staleness
permanently. The one-off cost is that each conversation open across the upgrade restarts
once; the benefit is that a conversation already stuck on a stale surface recovers by
itself.

The single render is load-bearing rather than an optimisation. The decision and the prompt
assembly are separated by an awaited memory load, and a reload landing in that window moves
the surface: rendering at both points lets a session be created carrying one prompt while a
digest describing a different one is stored against it, which retires it again on the next
turn. Rendering once and carrying that result through the decision, the prompt and the
registration removes the second read rather than trying to order the two.

The Home Assistant tool names are the one part of the surface that changes under a running
Casa: an upgrade of Home Assistant renamed every tool (#1091). They are not in the prompt, but
a resumed conversation's own history keeps calling the old names, and the CLI refuses them.
So the facade's publication carries a digest of the published names — sorted, so a different
upstream order moves nothing — and the turn captures each published server together with
that digest when it arms its surface. The digest joins the surface's, and the options build
connects with exactly the captured servers, so a publication landing mid-turn never pairs
new tools with the old identity; the next turn picks it up. A role with no published tool
surface digests exactly as before, so its conversations are not retired by this.

The turn's read clearance joins the surface the same way, because the session's prompt — its
memory view, its channel context — was written for one reader. A turn read at another
clearance than its chat's session starts a session at its own, retained like any surface
change; `private` adds nothing, so the operator's sessions keep their digest. In a chat whose
turns alternate between the operator and another sender, each switch starts a new session.

**INV-TURN-013**: Every in-process SDK client Casa builds raises the SDK's per-message stream limit to one shared value, `SDK_MAX_BUFFER_SIZE` in `claude_runtime.py`, sized so that a built-in `Read` of the largest PDF the pinned CLI inlines whole still arrives as one message instead of ending the turn.

The SDK reads the CLI's output one JSON line at a time and fails the whole query when a
line exceeds `max_buffer_size`, which is 1 MiB when left unset. A `Read` of a PDF puts the
file's base64 on one line twice — in the tool result's document block and again in
`tool_use_result` — so a PDF of about 400 KB was enough to cross the default, and a
delegated specialist that read one lost its whole turn (#1111). The pinned CLI reads a
whole PDF of up to 20 MiB as one inline document and refuses a larger one; above 3 MiB it
first tries to render pages through `pdftoppm`, and when that is unavailable, as it is in
the Casa image, it falls back to the inline document. Two base64 copies of a 20 MiB PDF come
to about 53 MiB, so the shared value is 64 MiB. It bounds one line; nothing is allocated up
front. A `Read` that names a page range is a different path: it accepts PDFs up to 100 MiB
and always renders pages through `pdftoppm`, so in the Casa image it ends in a tool error.
Were `pdftoppm` ever added to the image, that path could return up to 20 page images of up
to 5 MiB of base64 each, and 64 MiB would no longer cover it. The value is passed by every
`ClaudeAgentOptions` construction rather than applied by a wrapper, because not every
client goes through the same wrapper: a construction that omits it silently falls back to
the 1 MiB default, and a source sweep refuses one.

**INV-TURN-015**: Every SDK exec Casa makes hands the CLI a non-empty string system prompt as a file (`--system-prompt-file`), never as an argument, so a prompt of any size starts the CLI.

Linux refuses an exec whose one argument reaches 128 KiB; a resident's prompt carries its
memory block and outgrew that (#1570). `with_system_prompt_file` (`claude_runtime.py`)
converts at the exec site only; the CLI reads the file once, at startup, and a source sweep
refuses an unconverted SDK client or query.

## Failure behavior

A model call that fails, a stale session id and a pool that cannot serve the turn are
[`turn-faults.md`](turn-faults.md).

**The CLI does not match its pin.** Boot verifies the effective Claude CLI against a pinned
path and exact pinned version, and a mismatch is *fatal at startup* — replacing or
upgrading the CLI without moving the pin prevents Casa from starting rather than merely
degrading turns.

**Memory is unavailable.** The turn proceeds without it, with a warning; only the logs tell
an unavailable memory from an empty one (INV-TURN-004).

What the loop does *not* do: it does not save long-term memory per turn. Saving happens at
session granularity, in the background, elsewhere.

## Extension points

Know where "every turn" actually runs: options assembly happens when a client generation is
*built* — a warm reuse skips it entirely, prompt, memory block and all. Anything that must
be true for literally every turn belongs on the message-processing path around the query,
not in the options assembly; anything that only needs to hold per client generation belongs
in the options assembly, which is the one place that sees the fully-resolved context.

The retry and resume-fault knobs are [`turn-faults.md`](turn-faults.md)'s.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/agent.py::Agent._process`
- `casa/rootfs/opt/casa/agent.py::Agent._make_on_message`
- `casa/rootfs/opt/casa/agent.py::Agent._build_options`

**Tests**
- `tests/test_agent_process.py::test_session_id_is_channel_plus_role`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/sdk-client-pool.md`](../architecture/sdk-client-pool.md)
- [`architecture/turn-faults.md`](../architecture/turn-faults.md)
- [`architecture/turn-limits.md`](../architecture/turn-limits.md)
<!-- END SOURCEMAP -->
