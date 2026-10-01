---
last_reviewed: 2026-09-30
---

# The turn loop

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How one inbound message becomes one agent turn: what is assembled before the model is
called, how the call is retried, and what bounds the turn. It does not cover the life of
the warm client the turn runs on ([`architecture/sdk-client-pool.md`](sdk-client-pool.md)),
where the message came from (the channels), what the model is allowed to do inside the turn
(authorization), or how memory is stored and recalled — only the point at which memory is
read into a turn.

## Mental model

A turn is a single pass with a fixed shape: take a message, decide whether it continues an
existing conversation or starts a fresh one, assemble a system prompt and a memory block,
call the model with retry, stream the output, and record what happened.

The client the turn calls is usually a warm one the pool hands it rather than one this turn
created; what that reuse guarantees, and what a close or a reset owes it, is
[`architecture/sdk-client-pool.md`](sdk-client-pool.md).

**One scope per turn, minted before anything streams.** `handle_message` mints a
`TurnScope` after delegation synthesis has rebound the message and before the streaming
callback is obtained, under the reserved context key `_turn_scope`; `_process` mints one for
a direct caller that arrives without it, and puts the same object on the origin snapshot as
`turn_scope` — a live value like `speaker_provenance`, never persisted — where the tool
handlers and the read-evidence hooks find it, and hands `_make_on_message` the same scope.
Whether the turn streams at all is the scope's `streaming_allowed` — a scheduled turn or an
event wake is minted with `NoStream` and gets no token callback. The model's final text
passes through `scope.admit(FINAL_REPLY, …)`, which judges closing silence itself, on the
unannotated text, so a `<silent/>` turn is suppressed before any line could be added and
prose after a sentinel is delivered whole; a classified-error reply is Casa's own text; the
plugin-health notice is prepended outermost over the admitted value; and every streamed
cumulative `_emit` releases is admitted too, after the INV-TURN-009 hold has judged the
unannotated cumulative with the same predicates. Options assembly is where the
read-evidence matchers join every resident's hook bundle. What the scope does with the text
is [`output-boundary.md`](output-boundary.md) (INV-OUT-006 for the three decisions that
used to be inline checks here and in `send_message`).

## Contracts & invariants

**INV-TURN-004**: A memory read that raises does not fail the turn. It is logged and the turn proceeds with an empty memory block.

What it does not cover: nothing downstream is told. The unavailable-versus-empty distinction
is flattened to omission at this point — observable in logs and the recall breaker, not in
the prompt — and a successfully-read profile overlay may still be present beside the missing
recall block.

**INV-TURN-005**: Cancellation is never retried and is re-raised after a bounded interrupt-and-drain cleanup; retry covers exactly the three classified transient kinds — timeout, rate limit, and SDK error — with exponential backoff, honouring a server-supplied retry hint when present.

"Generic model errors" are *not* a retried category: a failure that classifies to none of the
three kinds is raised immediately.

**INV-TURN-007**: An API-level fault the CLI reports as an assistant message never becomes agent text; it ends the turn as a classified error, and a safety refusal is not retried.

The CLI does not raise for an API-level fault. It synthesizes an ordinary assistant message
whose single content block holds its own user-facing error string — an Anthropic request id
and terminal-UI advice among it — stamps the envelope with an `error` value, and for a
safety decline sets that message's stop reason to `refusal`. Both arrive through the SDK as a
normal assistant message, which is why the accumulator that folds assistant text into the
reply is where CLI prose would otherwise be spoken in persona position.

The gate is the *truthiness* of the envelope's error field, never membership in a known set:
the set of values the CLI can emit is open, and at least one it does emit is absent from the
SDK's own literal type for that field. A refusal is classified to its own kind and is
deliberately outside the retried set — the decline is deterministic, so further attempts buy
nothing but latency — while a fault the CLI names as transient maps back onto the retried
kinds.

A refused turn also gives up the conversation it was in. Dropping the pool entry unbinds the
*client*, not the *conversation*: the session registry would still name the session the turn
resumed, so the next turn on that key would resume the very conversation that was declined,
with the declined message still in it. The refusal therefore clears that registration —
under the same guards as the stale-resume recovery, so a concurrent turn's newer session
survives, and for a refusal only, since a transient fault is no reason to discard a
conversation the next turn could continue.

What it does not cover: a fault scoped to a sub-agent rather than the main loop is suppressed
without ending the turn, because the resident may still answer; the result message's stop
reason is read as a second carrier, so a refusal reported only there is still classified; text
already streamed to a channel before the fault stays delivered, since it cannot be recalled;
and this is a statement about the *turn* boundary. The other read loops that fold assistant
text apply the same suppression and then report *their own* failure rather than a turn error:
a delegated specialist run raises, so all four of its consumers fail the delegation with the
carried kind through the exception paths they already had; engager synthesis raises rather
than answering with an empty string that would read as "the engager remembers nothing"; and
an engagement turn raises too, so a fault on the launch turn reaches the engagement's terminal
record with its own kind instead of flattening into one generic driver-start failure, where a
refusal and a crash read identically. A fault on a *later* turn of a live engagement raises
the same way but deliberately does not end it: the topic is told the turn failed and the
engagement stays open, because one declined turn is not the engagement's verdict. That whole
half needed the engagement lifecycle to be ready for it rather than the suppression alone, so
it followed a release later. Its detection deliberately sits outside the
branch that streams text: an interactive specialist's output is capped, and once the cap
freezes the accumulator that branch stops running, so a fault arriving after the freeze would
otherwise end the turn as a truncated success.

**INV-TURN-008**: A turn carrying server-stamped trusted user ingress that consumes SDK retries and still ends in silence ends as a classified error instead of silence, unless it stopped at its turn limit (INV-TURN-014); and a conversation whose resumed trusted-ingress turns repeatedly evidence SDK faults is not resumed past a bounded streak — it starts fresh with the old session retained.

Two shapes of a doomed ask motivate this. Retries can exhaust and raise, which was always
visible; or the final attempt can return nothing but whitespace or the literal silence
sentinel, which the suppression gate would otherwise treat as chosen silence — consumed
retries and all. The second shape is reclassified before delivery into the mapped message
of the last retried kind, and rides the ordinary classified-error path end to end: plain
rendering, the voice error line, a non-empty response for request turns. The scope
predicate is the server-created trusted-ingress stamp — the fact that the turn has an
author — not humanity: a signed `/invoke` automation is in scope, while heartbeats, event
wakes, scheduled work and setup dispatches stay doctrinally silent through any upstream
congestion window.

Two things happen inside that gate that the rest of the loop never sees. A Casa-dispatched
plugin-setup turn re-checks its obligation the moment it holds the gate, before any client
or prompt, and returns without running when the obligation was settled meanwhile; and the
message handler hands every non-error plugin-tool result of an ordinary turn to the setup
store as it is observed, while the gate and the client lock are still held, so a setup turn
queued behind that turn finds the row settled rather than asking for the setup again. When
that handover clears a failed obligation, the handler also regenerates the plugin-health
report before it reads the next message, so the reply's health notice reads the new report.
These are [`architecture/plugin-setup-turn.md`](plugin-setup-turn.md)'s (INV-PLUG-023,
INV-PLUG-030); the loop only orders them. A third is a mark, not an action: the turn records that it completed only as
its last step, after the reply is produced and delivered, and the setup-outcome report the
loop makes in its `finally` carries that mark, so a turn that raised or was cancelled
reports itself as never having replied (INV-PLUG-012, INV-PLUG-024).

The streak half guards against a poisoned resume: each trusted turn that resumed its
conversation commits a health note — while still holding the per-key session write gate,
which is what orders notes against the decisions that read them — striking on a terminal
SDK-error raise or on silence whose consumed retries included an SDK error, resetting on
a real answer, a clean no-retry finish, or a turn that stopped at its turn limit whatever
retries preceded it (INV-TURN-014). A terminal rate-limit or timeout is
congestion-shaped and records no verdict either way. The note follows the session-id
chain (a resumed turn may publish a successor id), so the recorded fault id is always the
id the next ask would resume. At the streak bound the resume decision comes out fresh
*with retain*, exactly like an expired entry — continuity is saved, never cleared, and
the refusal-only registration clearing of INV-TURN-007 is untouched.

What it does not cover: the notice does not extend to delegation-completion synthesis
turns (no trusted ingress; a delegation fault surfaces through the delegation machinery's
own consumers), and text a channel streamed before classification stays streamed. The
silence sentinel is no longer part of that residue — it is never streamed at all
(INV-TURN-009), so the reclassified reply is one fresh send rather than a posted literal
and a superseding edit.

**INV-TURN-009**: The token stream never carries a cumulative that is nothing but `<silent/>` sentinels and whitespace, nor — on the partial-delta path — one that could still become such silence. With a token callback present, a canonical-fold cumulative that is not silence, and a partial-delta cumulative that cannot still become silence, is handed to the callback in full, including any literal sentinel, unless it equals the last cumulative already handed to that callback.

The sentinel is the model's way of saying "send nothing", and the gate above already
honours it — but only at the end of the turn, after the stream has run. So a turn that
chose silence used to post the literal to the operator first, and on that path nothing
ever took it back: the gate empties the text, delivery is skipped, and the teardown hook
only releases the typing indicator. The hold moves the decision to the emission itself.
It is stated per emission rather than per turn, because on the partial-delta path a
provisional sentence can be streamed and then replaced by a canonical fold of pure
silence; what has been spoken cannot be unspoken, and the guarantee is that the
correction adds nothing further. Release hands over the exact cumulative, the literal
included, so a turn that recants after the sentinel still delivers its whole text; and
because the fold uses the same predicate as the final gate, the stream never holds
something the gate would deliver. Nothing records a held cumulative, so a hold can never
suppress its own release. On a held turn the teardown hook is the only thing that stops
the typing indicator, since the first-token teardown never runs.

The fold also keeps each text-bearing message apart (`state["messages"]`, beside the joined
`state["text"]`). `_process` publishes the winning attempt's tuple as
`turn_report["reply_messages"]` and the number of attempts that ran as
`turn_report["attempts"]`; a stale-resume re-run consumes no retry but is still an attempt.
Its return value stays the joined text. Only a buffered turn's final-reply admission reads
them, for the #1075 closing-silence rule (INV-OUT-006 in
[`output-boundary.md`](output-boundary.md)); the stream and this hold are unchanged.

**INV-TURN-012**: A conversation is resumed only while the structural surface of its system prompt — the delegates, background jobs and executors the agent can reach, and, for a role whose Home Assistant tools the facade publishes, the names of the tools last published — still digests to what the session was registered with. A surface that differs, or a session that never recorded one, starts a fresh session with the old one retained. The surface is rendered once per turn and the same render is what the resume decision gates on, what the prompt carries, the published tool servers the session connects with, and what the registration stores.

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
surface digests exactly as before, so its conversations are not retired by this. The butler's
conversations restart once, retained, on the upgrade that introduces it.

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

**INV-TURN-014**: A resident turn whose terminal SDK result has subtype `error_max_turns` is a limit stop: it is detected from that subtype alone, returned rather than raised, its session published exactly as any returned turn's, never retried or continued, never reclassified as an error and never counted as a resume fault — a trusted turn records it healthy. Limit handling never rewrites or replaces the model's text: what admission delivers is delivered, and what it suppresses stays suppressed. Casa adds exactly one line of its own, attempted as a separate send — in the turn's Telegram chat; in the operator's Telegram chat, addressed explicitly, for a narration or a schedule that ran on no Telegram chat; at the end of a trusted `/invoke` response body; or spoken after the held tail on voice once something was spoken — except on an untrusted webhook turn, which sends nothing. Every limit stop logs one WARNING naming the role, the channel and the turn count. An untrusted webhook turn runs with a fixed limit of 20.

The CLI ends a turn that would need one model call more than its limit with an error result
of that subtype and no result text. The pool treats it like any non-retryable error result —
the entry is invalidated, the session id returned and published (INV-TURN-002) — so the next
message resumes the stopped conversation. Before #1121 nothing on the resident path read the
subtype: whatever progress text the turn had written went out as an ordinary reply, a
buffered turn that had written nothing was wholly silent, only an INFO line recorded it,
and a stop after a consumed SDK retry was turned into the generic error line and struck.

The fact is recorded in the result-message arm both attempt paths share and published on
the turn report from the winning attempt; a stop is never retried, so it can only be the
last. Every reader of "the turn ended silent" consults it: the INV-TURN-008 reclassification
skips it, the health note resets on it, and a narration's chosen-silence acknowledgement
skips it, because a cut narration did not choose its silence. The #1075 rule of INV-OUT-006
is untouched: the line is never merged into the admitted text, so admission decides the
model's text exactly as before and the line goes out whatever it decided.

Where the line goes is decided by the server-stamped origin route, never the message type —
an untrusted webhook turn dispatches as a scheduled one — and by whether the message that
arrived was a completion notice, captured before synthesis rebinds it. Its words must be
true where they land: "say 'continue'" only in a real chat whose next message resumes the
stopped session; a turn that ran in a session of its own (a schedule, a reminder, the
follow-up to a scheduled question, a narration whose session is not that chat's) names the
task by what the message carries and offers to redo it. A Casa-started turn in the
operator's chat is named by its kind. The WARNING is logged before any fallible delivery, and
a model delivery that raises still has the line attempted. The spoken line is
[`voice.md`](voice.md)'s.

The assistant's limit is `tools.max_turns` in its `runtime.yaml`, 80; the copy in its
`role.yaml` is kept in step, which moves the role checksum, so each open assistant
conversation restarts once, retained where its channel retains, on the release that
changes it. The restricted options every webhook-channel turn without the `invoke` route
takes — a webhook trigger, and a schedule declared on `channel: webhook` — pass the fixed
`_RESTRICTED_WEBHOOK_MAX_TURNS` instead. The butler (10) and the concierge (6) keep theirs.

What it does not cover: a delegated or job turn's limit, which stays the specialist's
`specialist_turn_limit` failure; the in-Casa engagement driver; a failure independent of the
stop — persistence, delivery, cancellation — which keeps its own handling, the line being
best-effort with its own failure logged; and a cut narration whose partial narration reached
the chat, which is acknowledged as today
([`delegation-announcements.md`](delegation-announcements.md)).

## Failure behavior

**The model call fails transiently.** Retried with exponential backoff up to a small attempt
limit. A retry hint from the server overrides the computed delay.

**Retries are consumed and the turn still ends silent.** On a trusted-ingress turn this is
surfaced as the mapped error, never as silence, and — when the turn was resuming — counted
against the conversation's fault streak; at the bound the next ask starts fresh with the
old session retained (INV-TURN-008).

**The session id is stale.** The stored session is cleared — but only while the registry
entry still carries the id that failed *and* no registration has landed since this
attempt's snapshot: each registration stamps a process-local generation the decision
captures, so even a concurrent turn that successfully re-resumed the *same* id keeps its
registration (and its save-time retention). The retry resumes whatever survives — and the
turn re-enters the normal fresh retry policy — up to the standard attempt limit, not a
single extra try.

**The pool cannot serve the turn.** It raises, and the turn creates its own client for this
turn only. The two failures compose: a stale session id hit on that per-turn fallback
re-enters the same stale-id recovery above, rather than surfacing raw.

**The CLI does not match its pin.** Boot verifies the effective Claude CLI against a pinned
path and exact pinned version, and a mismatch is *fatal at startup* — replacing or
upgrading the CLI without moving the pin prevents Casa from starting rather than merely
degrading turns.

**Memory is unavailable.** The turn proceeds without it, with a warning. An unavailable
memory is not an empty memory — but at this point the distinction lives only in the logs and
the recall breaker. The model and the turn's caller see the same thing either way: no recall
block (see INV-TURN-004).

What the loop does *not* do: it does not save long-term memory per turn. Saving happens at
session granularity, in the background, elsewhere.

## Extension points

Know where "every turn" actually runs: options assembly happens when a client generation is
*built* — a warm reuse skips it entirely, prompt, memory block and all. Anything that must
be true for literally every turn belongs on the message-processing path around the query,
not in the options assembly; anything that only needs to hold per client generation belongs
in the options assembly, which is the one place that sees the fully-resolved context.

Retry and the resume-fault streak are tuned by environment, alongside the pool's own
bounds and in the same place a new bound belongs; the knobs and their defaults are
listed in [`architecture/sdk-client-pool.md`](sdk-client-pool.md).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/agent.py::Agent._process`
- `casa/rootfs/opt/casa/agent.py::Agent._make_on_message`
- `casa/rootfs/opt/casa/agent.py::Agent._build_options`
- `casa/rootfs/opt/casa/retry.py::retry_sdk_call`
- `casa/rootfs/opt/casa/retry.py::compute_backoff_ms`

**Tests**
- `tests/test_agent_process.py::test_session_id_is_channel_plus_role`
- `tests/test_agent_process.py::test_telegram_channel_autorecalls_on_fresh_session`
- `tests/test_retry.py`
- `tests/test_agent_api_error_message.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/sdk-client-pool.md`](../architecture/sdk-client-pool.md)
<!-- END SOURCEMAP -->
