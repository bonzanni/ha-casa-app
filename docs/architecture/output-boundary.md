---
last_reviewed: 2026-09-22
---

# The output boundary

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

One place for what the operator sees from a turn: the scope every dispatched turn is minted
with, the admission of model-authored text at the moment it is committed to the Telegram
transport, the one obligation that admission enforces today — a turn that was shown inbound
files and opened none says so at the head of everything it emits — and the note a payload
stored for a later turn carries to the turn that finally sends it. It does not cover how a
file arrives or who may read it ([`inbound-files.md`](inbound-files.md)), how a reply is
rendered and paginated ([`telegram-rendering.md`](telegram-rendering.md)), the turn loop
itself ([`turn-loop.md`](turn-loop.md)), or the tools' other result contracts
([`tools-interface.md`](tools-interface.md)).

## Mental model

**One scope per turn, and the channel accepts nothing that did not pass through one.**
`Agent.handle_message` mints a `TurnScope` for every dispatched turn — after delegation
synthesis has rebound the message, so a narration turn is its own turn, and before the
streaming callback exists — and carries it under the reserved context key `_turn_scope`;
`_process` mints one for a direct caller that arrives without it and places the same object
on the origin snapshot as `turn_scope`, a live value beside `speaker_provenance` that is
never persisted (`engagement_registry._NON_PERSISTABLE_ORIGIN_KEYS`). From there the pooled
client's holder, the tool handlers' entry snapshots and the read-evidence hooks all reach
the one object. The Telegram channel's model-text methods accept only an `Admitted` — a
`str` subclass, so rendering, splitting and logging keep working, while
`isinstance(x, Admitted)` is what the channel checks. The only minter of an `Admitted` with
`source="model"` is `TurnScope.admit`; Casa's own templated notices enter through
`casa_text`, whose call sites are a recorded list. Any string operation on an `Admitted`
yields a plain `str`, which is right: derived text is not admitted text, and the one Casa
prepend that happens after admission — the plugin-health notice — re-wraps through
`Admitted.with_text`.

**Admission happens when text is committed, against the evidence gathered so far.**
`TurnScope.admit(kind, text)` applies the scope's obligations at that moment and returns
what the operator will see plus `annotations`, the exact lines Casa added. It is not
re-evaluated later: a `send_message` before a `Read` is annotated, and a `Read` afterwards
does not retract what was already sent, only what is committed next. Empty or whitespace
text is never annotated. Inherited notes come first, then today's line, joined by blank
lines above the model's text. For `IntentKind.STORED` the text is returned unchanged and
`.note` carries the resolved line the emitting turn will prepend — a stored payload keeps
its words and gains a note beside them.

**Obligations are a closed, Casa-owned set; the model can never add one, and nothing is
ever held.** `ReadBeforeDescribe` is armed by `list_inbound_files`, from the records it
lists rather than from the rendered text, and by a `Read` attempt on an inbox path the turn
never listed (a path copied from an earlier listing makes this a file turn too). It is
discharged by a successful `Read` of any one listed file — per-file disclosure is not built
— and its remedy is one Casa line at the head of every emission: `Casa: <persona> answered
without opening “<file>” in this turn.`, or with several unread, `…without opening any of
the N files you sent…`. `InheritedNote` is the resolved note of a payload an earlier turn
authored — a reminder's `output_note`, a delegation or engagement brief's launch note —
re-registered on the turn that sends it so that turn's model cannot paraphrase it away; it
is never discharged. The model's words are never suppressed or withheld by any of this; the
`<silent/>` convention is judged inside final-reply admission, on the unannotated text and
before any line is added, so a silent turn is never turned into a visible line.

**Evidence is the runtime's own call, not an inference from the model's text.**
`hooks.read_evidence_matchers(role)` adds `PostToolUse` and `PostToolUseFailure` matchers on
`Read` to every resident's hook bundle; for a path under `agent_inbox.readable_prefixes(role)`,
normalised as the `path_scope` guard normalises, they record `read_ok` or `read_failed` into
the running turn's scope. A read the `PreToolUse` guard denied produces no post event, so
it is never evidence; a failed read is recorded as failed, not as ok. A role with no inbox
records nothing, and voice turns cannot be file turns because only the Telegram default
agent has an inbox ([`inbound-files.md`](inbound-files.md)).

**A stored payload resolves its obligation now and carries the result as data.** The turn
that sets a reminder, or launches a delegation or an engagement, may not be the turn that
speaks. `set_reminder` stores the resolved line as the entry's `output_note` with the prompt
unchanged; every route that builds the entry's `TriggerSpec` goes through the one constructor
`reminders.spec_from_entry`, and both firing sites stamp it into the firing turn's context as
the reserved `_inherited_note` marker through `provenance.scheduled_delivery_markers`, where
`TurnScope.mint` registers it. `_launch_note` puts the same kind of note on a delegation
record's origin and on an engagement's persisted origin as a plain string, which survives
persistence where the live scope does not; the registration adapter writes it to the job
row's `output_note`, boot recovery restores it onto the replay origin, and the synthesized
completion turn copies it into its context. An engagement's own DM sends mint their scope
from the persisted record (`TurnScope.for_engagement`), which carries the persisted note and
nothing else, so launch, resume and boot recovery all see the same.

**A child never runs under its parent's live scope.** A parent `Read` after launch would
discharge the parent's obligation although the child's brief was written unread. The
delegation launch therefore binds the task's context to a frozen copy of the launcher's
entry snapshot carrying `TurnScope.for_child(parent, note)` — the launcher's identity and
markers plus the launch-time note, never the parent's live obligations — around
`create_task`, the same pattern the delegation quota key uses. For the same reason the
emitting tools resolve the scope they commit under from the snapshot they copied at entry
(`_current_scope(origin)`: a bound engagement's scope, else the entry snapshot's turn scope,
else none), never from the pooled holder the next turn rewrites in place; a tool that
resolves none refuses, so the absence is loud rather than a permissive default.

## Contracts & invariants

**INV-OUT-001**: Model-authored text reaches the Telegram transport only as an `Admitted` minted by the authoring turn's scope — `send`, `send_response`, `finalize_stream`, `finalize_response_stream`, `post_dm_keyboard` and `send_media`'s caption refuse a bare string with an error log, zero Bot API calls and the method's own failure value.

The failure value is each method's own contract, so every existing consumer already
handles it: `NOT_DELIVERED` from the four senders, `None` from the keyboard post, and
`UnadmittedText` — distinct from the channel-unavailable `RuntimeError` — from
`send_media`, which the tool's send classifier reports as `refused` rather than as a down
channel. `edit_dm_message` stays string-typed on purpose: every caller re-renders a body that
was admitted when it was posted, replaces it with a Casa status, or edits a plugin-authored
consent body, and each caller is recorded and classified in
`tests/test_output_boundary_sites.py` — as is every `casa_text` call site, every raw
`bot.send_message`, `edit_message_text` or media call by enclosing function, and every
public channel coroutine with a text parameter. A new site fails those tests until someone
records it and, in doing so, answers who authored the words.

What it does not cover: the engagement-topic methods, which the output sequencer owns and
which carry no scope; the arrival replies and the plugin delivered-link message, which the
recorded lists class as notices; and the voice channel, which has not adopted the contract. The recorded lists
catch an accidental bypass; they are not a sandbox against Casa deliberately fabricating an
admission.

**INV-OUT-002**: In a turn where inbound files were listed and none read, every model-text emission committed after the listing carries the disclosure line at its head — streamed cumulatives included — and when a streamed reply's page-1 unit did not land, the line goes out once as its own message before the overflow pages; after a successful `Read` of any listed file none does; and a silent turn stays silent.

The final reply passes through `scope.admit(FINAL_REPLY, …)` in `handle_message`, which
judges closing silence itself before annotating; a classified-error reply is Casa's text and is never annotated; the
health notice is prepended outermost over the admitted value. Every streamed cumulative
`_emit` releases is admitted as `STREAM_UPDATE` after the INV-TURN-009 hold has judged the
unannotated cumulative, so what is on screen is true at every moment and a failed final
edit leaves the line there. `send_message` admits as `DISCRETE`, `ask_user` admits the body
as `KEYBOARD` before its plain and scheduled arms branch — so the posted keyboard, the stored
scheduled record and every settle edit derived from that body carry the same line — and
`send_media` admits the caption as `CAPTION`, with the length cap applied to the model's body
— never to the line — and the shortened caption re-wrapped under the same admission. When a
streamed reply's page-1 unit did not land — the outcome is anything but `DELIVERED`,
`UNKNOWN` included, since an unconfirmed head may not be showing the line — both finalizers
send the admission's lines once, as their own plain message (`_overflow_head`), before the
overflow pages — only when one follows — which then go exactly as they do when the head
landed; a head whose outcome is `DELIVERED`, a "not modified" edit included, sends no line
message.

What it does not cover: a reply about a file the turn never listed and never tried to read,
or a message sent before the turn's first listing, gets no line; the SDK transcript and
retained memory hold the model's text without the line; and the disclosure is a line, not a
hold — nothing is withheld.

**INV-OUT-003**: A payload stored under an undischarged obligation — a reminder's text, a delegation or engagement brief — is delivered with its resolved note by the turn that sends it, whatever that turn's model writes; a payload stored after the `Read` carries none; and an inherited note is never discharged.

The note is data at every hop: `output_note` on the trigger entry (a key the trigger
schema declares, ill-formed like any other field when malformed), on the `TriggerSpec` the one
constructor builds for the boot loader, the live registration and the sweep's
reconciliation alike, and on the job row (encoded on every row; a row written before the
field decodes as owing nothing); `_inherited_note` on the fired turn's context, on the
record origin and on the synthesized completion turn. `tests/test_output_boundary_reminders.py`
pins that `TriggerSpec(` is constructed nowhere but the dataclass, the constructor and the
loader's webhook branch, because three hand-built constructors were how a field that
survived the file could still fail to reach the firing turn.

What it does not cover: a payload stored by a turn that had no scope bound owes nothing
here, and it is that turn's own emissions that refuse. The note is not a delivery
eligibility: `scheduled_delivery_markers` stamps it for whatever channel the trigger names,
while the scheduled-delivery marker itself stays Telegram-only.

**INV-OUT-004**: A delegated child runs under the view its launcher built at entry — the launcher's identity and markers plus the launch-time note, never the pooled holder's current contents — and an emitting handler that outlives its turn commits under the scope it captured at entry.

Pinned by rewriting the pooled holder in place before the child takes its first step, and
by pausing `send_media` inside its outbox capture while the next turn rewrites the holder:
the child still sees the launcher's chat, cid and note, and the caption still carries the
first turn's line. A parent `Read` after launch does not un-annotate the child's view.

What it does not cover: an engagement scope is minted afresh on each resolution from the
record, so only what the record persists reaches it — an obligation armed on one resolution
is not seen by the next, which matters only if a role with an inbox ever ran under a bound
engagement.

**INV-OUT-005**: A tool that emits or stores annotated text reports the exact line in its result.

`send_message` answers `Message sent via telegram. Casa prefixed: “…”`; `set_reminder`'s
payload carries `note`; `ask_user`'s `awaiting_user` payload gains `casa_prefixed` when a
line was added; `delegate_to_agent` reports the brief's note as `casa_note` on either
pending result — the async one and a synchronous wait that degraded to pending. The
transcript the model builds on therefore says what the operator saw.

**INV-OUT-006**: Whether a turn streams, whether its final reply is closing silence, and where an untrusted webhook turn's discrete send goes are properties of its scope — the first and third registered at mint from the message's own facts, the second intrinsic to final-reply admission: a scheduled turn or an event wake never receives a token callback, a final reply that strips to nothing but `<silent/>` sentinels is suppressed by admission while prose after a sentinel is delivered whole, and an untrusted webhook turn's `send_message` is bound to Telegram whatever channel it named.

The three used to be inline checks — the two-clause callback condition and the sentinel
gate in `handle_message`, the egress clamp in `send_message` — and are now `NoStream`
(read as `TurnScope.streaming_allowed`), the silence judgement inside
`admit(FINAL_REPLY, …)` on the unannotated text (the predicates `strips_to_silence` and
`may_still_be_silence` live in `output_boundary` and are the ones the #650 resume-health
classification and the #666 stream hold use), and `DestinationOperatorOnly` (applied by
`TurnScope.resolve_channel`). The behaviour is unchanged: the tests that pinned the three
checks keep their assertions, and a grep test refuses the old inline forms coming back.

What it does not cover: `send_media` still requires a Telegram origin of its own, so a
webhook turn's media is refused before the binding matters; the #650 retry-tainted-silence
reclassification runs before admission and is not an output decision.

## Failure behavior

**A bare string reaches a model-text method.** Refused with an `ERROR` log naming the
method, zero Bot API calls, and the method's own failure value (INV-OUT-001); a caption
refused reaches the tool as `kind_error: refused`.

**No scope is bound when a tool emits or stores.** `send_message` returns an error result,
`ask_user`, `send_media` and `set_reminder` return `unsupported_origin`, all with the same
message — output not submitted, this call carries no turn provenance. A delegation or
engagement launch with no scope bound stores no note and proceeds.

**The evidence recorder fails.** Logged at WARN with the traceback; the hook returns an
empty decision, so a fault in evidence never breaks the `Read`.

**The page-1 unit of a streamed reply does not land.** The admission's lines go out once, as
their own message, before the overflow pages; the pages themselves go exactly as they do
when the head landed — rich, with the plain fallback — so no page is re-split around a
prefix and no destination is cut; with nothing to follow (a failed single-page edit) no
line message goes out. If the line message itself fails it is logged and the pages still
go (the model's words are never withheld; the pages keep their own send policies): the
operator then sees a reply starting at page 2 with no line, and the log names it. An
`UNKNOWN` head — the edit may have applied with its acknowledgement lost — gets the line
message too; a line shown twice costs less than claims shown with none.

**The persona name is longer than 64 characters.** The line names the role instead — the
rule the authorization challenge headline already applies — so the line is bounded whatever
the operator configured, and the caption cap stays meaningful.

**A reminder fires on a channel other than Telegram.** The turn gets the note but not the
scheduled-delivery marker: the line is prepended to whatever that turn emits, and the
channel's own rules decide what it may deliver.

**An engagement topic receives output.** The sequencer's methods take plain text and apply
no scope; an in-casa engagement's DM sends — `send_media` from a bound engagement — resolve
the engagement's persisted note and nothing else.

## Extension points

**A new obligation** is a subclass of `Obligation` registered only by Casa code — the set is
closed to the model by construction — with its arming site, its discharge evidence and its
remedy decided together, and its wording owned by the scope.

**A new emitting path** must take an `Admitted` and be classified in
`tests/test_output_boundary_sites.py`; a new Casa notice is a recorded `casa_text` site. A
template that interpolates model-supplied text is model text and goes through
`TurnScope.admit`, not `casa_text` — the authorization challenge body, which interpolates
the model's tool arguments, is the worked example.

**A new route that builds a scheduled trigger's spec** goes through `spec_from_entry`; the
construction-site test refuses another `TriggerSpec(` call.

**A new place a stored payload travels** — a new durable row, a new synthesized turn — must
carry `_inherited_note` or `output_note` explicitly; the live scope never survives
persistence, and a field the row does not carry is silently gone after a restart.

**A channel adopting the contract** installs the same `isinstance` refusal in each of its
model-text methods with that method's own failure value; the voice channel has not.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/output_boundary.py`
- `casa/rootfs/opt/casa/hooks.py::read_evidence_matchers`
- `casa/rootfs/opt/casa/channels/telegram.py::_unadmitted`
- `casa/rootfs/opt/casa/channels/telegram.py::_overflow_head`
- `casa/rootfs/opt/casa/tools.py::_current_scope`
- `casa/rootfs/opt/casa/tools.py::_launch_note`
- `casa/rootfs/opt/casa/agent.py::Agent.handle_message`
- `casa/rootfs/opt/casa/provenance.py::scheduled_delivery_markers`
- `casa/rootfs/opt/casa/reminders.py::spec_from_entry`

**Tests**
- `tests/test_output_boundary.py`
- `tests/test_output_boundary_agent.py`
- `tests/test_output_boundary_channel.py`
- `tests/test_output_boundary_delegation.py`
- `tests/test_output_boundary_evidence.py`
- `tests/test_output_boundary_reminders.py`
- `tests/test_output_boundary_sites.py`
- `tests/test_output_boundary_tools.py`
- `tests/test_output_boundary_relocation.py`

**Related**
- [`architecture/inbound-files.md`](../architecture/inbound-files.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/reminders.md`](../architecture/reminders.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/hook-resolution.md`](../architecture/hook-resolution.md)
- [`architecture/jobs-and-delivery.md`](../architecture/jobs-and-delivery.md)
<!-- END SOURCEMAP -->
