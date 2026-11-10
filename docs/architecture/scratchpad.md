---
last_reviewed: 2026-11-07
---

# The scratchpad

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The `scratchpad` tool of a delegated client: which client has one, whose pad it reaches, when a
pad goes, its operations and the size of its replies; and `POST /internal/broker/pad`, the route by
which a plugin's own code fills and reads the same pad. The call record that route checks is
[`plugin-operator-calls.md`](plugin-operator-calls.md). The delegated runner it is attached in is
[`delegation.md`](delegation.md); the binding that keeps a long request's conversation is
[`plugin-continuations.md`](plugin-continuations.md). Residents, engagements, background jobs
and the bridge have no pad, because nothing attaches one to their clients.

## Mental model

**A pad is a text buffer in the main process's memory, under a key fixed when its client is
built.** `_run_delegated_turn` calls `scratchpad.attach` with the run's session id, and that
builds a tool server for this one client around that value. No tool argument names a pad, the
handler reads no context variable and asks no registry, and no file exists, so a call cannot
reach any pad but its own client's. A continuation has the binding's session, so all turns of a
request share a pad; a relay or nested turn has its own session, so never its worker's.

**It holds what a summary might drop.** It lasts through compaction and through the later turns
of its conversation, and the model decides what goes in it. The attach adds one line to the
client's system prompt saying so, in code, so every delegated specialist is told with no plugin
or role change. The CLI pins a session's system prompt when the session is created
([`turn-loop.md`](turn-loop.md)), so a session created without the line stays without it.

**A conversation's first turn opens its pad with a brief.** `attach` seeds a `## brief` section
with the task the run was given and, when the turn carries them, the operator's own words
(`handoff.operator_words`), so a list he sent is in the pad before the model types any of it. Only
a pad that does not exist yet is seeded, and only a turn that is not a continuation passes a task: a
continuation reads the request's pad as the model left it, and one resumed after a restart starts
with none. A line of either beginning `## ` is kept with one leading space, so the brief stays one
section.

**The tool has its own server, `casa-pad`.** `casa-framework` loads eagerly, so a tool there costs
its schema in every session; this server stays deferred and only its name loads until the model
searches for it. A role with `session.desk_context: false`, a mechanical stand-in for "its requests
are long conversations", gets the server with `alwaysLoad`, since tool search loses a schema at
each compaction. The grant is added in code after the role's own lists are read, so a role file
neither grants it nor needs to; a role that lists it in `tools.disallowed` gets no server, grant
or line.

**A plugin's code can fill the pad with no model in between.** Page text or a downloaded list
can be long, and a model that relays it retypes it. During a `callsCasa` call
([`plugin-operator-calls.md`](plugin-operator-calls.md)) the plugin's code posts
`{"client", "op", "section", "text" | "offset"}` to `POST /internal/broker/pad`: `write` replaces
one named section, creating it when missing, `append` adds to it, and both answer the section's
`{"lines", "bytes"}`; `read` answers `{"text", "next"}`, at most 30,000 characters of the section
from `offset`, with `next` null at its end. A refusal is `{"error", "message"}`. The pad is found
the way the tool's is, never by a name: `attach` registers the client's broker id
(`CASA_BROKER_CLIENT` in its env) for the key it builds the tool around, and the route answers only
while the call record finds that client with one call in flight. The registration goes with its
pad. A line of the plugin's text beginning `## ` is stored with one leading space, so page text
cannot split itself into sections or reach past the one it was sent to.

**A conversation has no single end, so a few deletes and one idle expiry.** A run with no binding
takes its pad with it. A binding keeps its session's pad (`BindingStore.holds_session`); the pad
follows a session the binding adopts and goes when the last binding naming the session is dropped.
A request's stop or end deletes nothing, because two requests of one turn share a binding and a
session. Anything else goes after a day unused, checked each time a client attaches a pad, and
never while a kept binding owns it: a request can write notes, wait longer than a day, and resume.

**Operations are plain Python on lines.** `read`, `write`, `append` and `search`, nothing more:
the pad keeps and finds text, and computing over it is the model's. There is no pattern language:
`search` matches a substring in any case. Only a newline ends a line. A section starts at a line
beginning `## `, also in pasted text, and each change's reply says when one appeared; `section`
names one and `""` is the lines above the first. A write replaces its section, so nothing needs
deleting: a section is emptied by writing it with no text.

## Contracts & invariants

**INV-PAD-001**: The `scratchpad` tool takes no argument that selects a pad and refuses any it does not declare; each client's tool works only on the key it was built with, which for a delegated client is its run's session, so two sessions have two pads and a relay turn cannot read its worker's.

Enforced by `scratchpad.attach`, whose handler closes over the key, by `SCHEMA`, and by
`tools._run_delegated_turn`, which passes `run.session_id` after choosing between resume and
fresh.

What it does not cover: two runs that share one session, one request's turns or a same-CLI
sub-agent, share its pad by design; the desk serialises the turns.

**INV-PAD-002**: Every `scratchpad` reply stays under 50,000 characters, the size past which the CLI's documentation says a reply goes to a file, whatever the pad holds or the call repeats, and while the pad exists opens with its shape, naming at most 20 sections; a read gives at most 100 lines or 8,000 characters unless asked for more and at most 40,000 characters, cuts a long line at 2,000 with the offset that reads on, says what comes next, and a search gives at most 40 lines cut at 300 with its true total.

Enforced by `_op_read`, `_read_line`, `_window`, `_shape`, `_quote` and the constants beside them. A write
that would pass 1 MiB is refused with the sizes, never truncated.

**INV-PAD-003**: `BindingStore.adopt` moves the session's pad to the session it adopts, leaving a copy only when another binding still names the old session.

Enforced in `BindingStore.adopt`, the one place a binding changes session: a fresh session is where
the model needs its notes most.

**INV-PAD-004**: A delegated run's pad goes when the run returns, raises or is cancelled unless a kept binding names its session; a binding's pad goes when the last binding naming the session is dropped; a pad unused for 24 hours goes unless a kept binding names its session; and a request's stop or end deletes no pad.

Enforced by the `finally` of `tools._run_delegated_agent`, by `BindingStore._prune_and_save_locked`,
the one place a binding is dropped, and by `PadStore.expire`, asked with `holds_session`. A binding
past its life counts as kept until a write prunes it, since `BindingStore.get` can still revive it
until then.

What it does not cover: a pad whose owner is gone stays in memory until the next delegated client
attaches, since expiry runs only then; it is unreachable meanwhile, because nothing derives its key.

**INV-PAD-005**: A stored call's client, and one whose role disallows the tool, has no pad tool, no grant and no line about the pad in its system prompt; every other delegated client has all three.

Enforced by the one `parent.get("stored_call")` check around `scratchpad.attach`, keyed as the
stored call's memory skip is, and by `attach` itself for a role's `tools.disallowed`.

**INV-PAD-006**: A `scratchpad` write or append that would make a second section of one name, and an operation that names a section two sections share, changes nothing and says why, on the tool and on the pad route alike.

Enforced by `_kept`, which every change of the tool and of the route passes before it is stored,
and by `_named`, which every operation that takes a section asks; the route answers
`duplicate_section`. A section's name is its whole identity, so a second one would
leave a later read unable to say which it means; a name that is used once stays free to write.
Text whose first line is the named section's own mark is that section's text (`_place`): the mark
is not written twice, so such a write makes no second section (the route holds its marks first, so
this applies to the tool). Line counts are of lines as `_split` makes them.

**INV-PAD-007**: A delegated run that is not a continuation opens its pad, when none exists under its key, with one `## brief` section holding its task and, when the turn has them, the operator's own words; a continuation writes no brief, and a pad that exists is never seeded.

Enforced by `PadStore.seed`, which writes only under a key with no pad, by `scratchpad.attach`,
which seeds only when given a task and only after the role's `tools.disallowed` check, and by
`tools._run_delegated_turn`, which passes the task and the words only when the turn has no
`ConversationTurn`. A relay or nested turn is a conversation of its own, so it gets a brief of its
own.

**INV-PAD-008**: `POST /internal/broker/pad` reaches only the pad whose tool was built for the broker client id the request presents, which `attach` registers from the client's env, and answers only while that client has exactly one call in flight; no field of the request selects a pad, an id registered for two pads reaches neither, and a read gives at most 30,000 characters of one section with the offset that reads on.

Enforced by `PadStore.register`, called only from `attach` after the role's `tools.disallowed`
check, by `PadStore.from_plugin`, which reads the key from the registration alone, and by
`operator_calls.build_pad_handler`, which asks `CALLS.find` first. The registration is dropped with
its pad (`discard`, a move that keeps no copy, expiry). A client whose role disallows the tool, and
one no `attach` saw (a resident's, an engagement's), answers `no_pad`.

What it does not cover: the route trusts the body's client id exactly as the other broker routes
do; any process that can reach the internal socket and knows a live id could use it.

**INV-PAD-009**: Text the pad route writes or appends keeps its lines, but a line beginning `## ` is stored with one leading space, so a plugin's text never starts a section.

Enforced by `_held`, through which the route passes every text it stores and the brief passes its
task and words. The section a route request names is created with its own mark, so a section can
come only from the request's `section`, which names one, on one line.
Page text in a pad reaches the same model that read it: no new reader, sender or network route. What
changes is that the text outlasts forgetting, can read as the agent's own note after a summary, and
can come back in a continuation turn with no operator words, where each action is still checked at
the send. A `read` or `search` reply carries one fixed sentence saying so; nothing is scanned or
marked, and a page can still talk the model into storing and later obeying something. A delegated
agent can copy pad text into its result, which already reaches the assistant by design.

## Failure behavior

**A restart empties every pad.** Pads are memory and each start is a new process, while bindings
and transcripts survive, so a conversation can resume with no pad. The tool then answers `No pad for
this conversation`, naming the start time, and the model rebuilds from its source. A pad's words are
in the session's transcript like any other tool output, kept with it
([`persistent-state.md`](persistent-state.md)).

**A refusal is a tool error the model can read,** and changes nothing. A pad written to after it
expired or after a restart starts empty; each reply's shape shows that.

## Extension points

Attach a pad to another surface by calling `scratchpad.attach` where that surface opens its client,
with a key that surface owns, and by giving that surface's owner the delete. A surface whose client
carries a broker id gets the pad route with it. `attach` is the only
way to the tool: it is not in `CASA_TOOLS`, so the bridge cannot serve it and the ledger does not
enumerate it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/scratchpad.py`
- `casa/rootfs/opt/casa/tools.py::_run_delegated_agent`
- `casa/rootfs/opt/casa/plugin_bindings.py::BindingStore.holds_session`
- `casa/rootfs/opt/casa/operator_calls.py::build_pad_handler`

**Tests**
- `tests/test_scratchpad.py`
- `tests/test_scratchpad_lifetime.py`
- `tests/test_scratchpad_route.py`

**Related**
- [`architecture/plugin-operator-calls.md`](../architecture/plugin-operator-calls.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/plugin-continuations.md`](../architecture/plugin-continuations.md)
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/stored-call-buttons.md`](../architecture/stored-call-buttons.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
