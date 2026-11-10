---
last_reviewed: 2026-11-10
---

# The scratchpad

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The `scratchpad` tool of a delegated client and of a background job's session: which client has
one, whose pad it reaches, when a pad goes, its operations and the size of its replies; a job's
pad file; and `POST /internal/broker/pad`, the route by which a plugin's own code fills and reads
the same pad. The call record that route checks is
[`plugin-operator-calls.md`](plugin-operator-calls.md). The delegated runner it is attached in is
[`delegation.md`](delegation.md); the binding that keeps a long request's conversation is
[`plugin-continuations.md`](plugin-continuations.md); the job's session is
[`background-job-session.md`](background-job-session.md). Residents, engagements that are not
jobs and the bridge have no pad, because nothing attaches one to their clients.

## Mental model

**A pad is a text buffer in the main process's memory, under a key fixed when its client is
built.** `_run_delegated_turn` calls `scratchpad.attach` with the run's session id, and that
builds a tool server for this one client around that value. No tool argument names a pad, the
handler reads no context variable and asks no registry, and a delegated run's pad has no file,
so a call cannot reach any pad but its own client's. A continuation has the binding's session, so all turns of a
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
session. Anything else but a job's pad goes after a day unused, checked each time a delegated
client attaches a pad, and never while a kept binding owns it: a request can write notes, wait
longer than a day, and resume.

**A job's pad is the same tool with a file behind it**, under the job's engagement id; the
section below says how it differs.

**Operations are plain Python on lines, and one script.** `read`, `write`, `append` and
`search`: the pad keeps and finds text. There is no pattern language: `search` matches a
substring in any case. Only a newline ends a line. A section starts at a line beginning `## `,
also in pasted text, and each change's reply says when one appeared; `section` names one and
`""` is the lines above the first. A write replaces its section, so nothing needs deleting: a
section is emptied by writing it with no text. Computing over the pad is `run`: a Python
script the model writes, run once over the sections in a confined process and written back
all at once, only on exit 0 and only when the pad did not change meanwhile
([`script-runner.md`](script-runner.md), where `PadStore.snapshot` and `PadStore.apply` and the
stamp each pad carries are described). `run` takes `text` and `chars`, never `section`, and
its reply carries the page-text sentence like a read's.

## A job's pad

A background job is one long session that is compacted many times and may be resumed after a
Casa restart or the death of its CLI ([`background-job-session.md`](background-job-session.md)),
so its pad must outlive both. `background_jobs.job_session_options`, which every job session's
options pass through at launch and at resume, attaches the pad under the engagement id with a
path, `/data/job-pads/<id>.pad` (`background_jobs.job_pad_path`), its server loaded up front, and
the job's own prompt line and tool description: the notes last through compaction and a Casa restart and
go when the job ends.

**Opening.** `attach` with a path calls `PadStore.bind` and never `seed`. A pad already in memory
is bound and kept as it is; memory is authoritative while the process lives. Otherwise the file
is loaded. With neither, the pad starts bound and empty, or with the brief when the build is a
launch, and is written. Only the launch passes a brief: the job's request and its context, and
the operator's words when the launching turn has them. A resume never does, so a resume that
finds neither (after a launch whose write failed, a brief over the cap, or a job that had no pad
before) starts with an empty pad that is still bound. A file that cannot be read, or is not a list of lines within the cap, is logged and
left on disk; the pad starts empty and the next change overwrites the file.

**Writing.** The file is the pad's lines as one JSON list (`atomic_io.atomic_write_json`, 0600,
in a 0700 directory created when missing), which keeps a lone surrogate and a trailing empty line
exactly. Every change to a bound pad — the tool's write and append, the route's, `bind`'s
creation and `put` — writes it synchronously inside the store's lock, in the same critical
section as the change, so changes, writes and deletes reach the disk in the order they happen in
memory. A failed write is logged; the change and its reply stand. Such a write holds the event
loop for one fsync'd file of at most a few MiB, as `BindingStore` does for its own file.

**Lifetime.** Expiry never drops a pad with a path: a job can wait days for a reply. Every path
that ends a job — the finalize funnel, the launch-death report, and the stale reap and Cancel
through the funnel — runs `tools._settle_job_journal` after the client is closed, and that
discards the pad with its path whatever the journal's settle did, so a job that ended before any
session of this process opened its pad still loses its file. At boot,
`casa_core._settle_ended_job_state` removes each `.pad` file whose record is gone or durably
ended, read through `settled_status`, so a terminal write that rolls back leaves the
pad in place; it runs even when there is no channel or runner to resume jobs with. A tool call
from a retired client after the discard makes a pad in memory with no path, which idle expiry
takes like a delegated one.

**After a compaction** the job's `SessionStart` hook adds `scratchpad.reinject_text` to the job
header: the page-text sentence, the pad's shape and its `## brief` section, cut at 4,000
characters, then "Read your pad before you continue." An empty pad adds nothing.

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

**INV-PAD-004**: A delegated run's pad goes when the run returns, raises or is cancelled unless a kept binding names its session; a binding's pad goes when the last binding naming the session is dropped; a pad unused for 24 hours goes unless a kept binding names its session or it is a job's; and a request's stop or end deletes no pad.

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
one no `attach` saw (a resident's, an engagement's that is not a job), answers `no_pad`. Each build
of a job's session registers a new client id for the same key; all of them go at its `discard`.

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

**INV-PAD-010**: A background job's pad is keyed by its engagement id and opened by every job session build, seeded with a brief only by a launch and only when neither memory nor its file holds one; every change is written atomically, as a JSON list of its lines, to `/data/job-pads/<id>.pad` (0600); a build with no pad in memory loads that file; idle expiry never drops it; and it is deleted, file included, on every path that ends the job and at boot for an id that is unknown or durably ended.

Enforced by `PadStore.bind`, the write-through in `PadStore.respond`, `PadStore.from_plugin` and
`PadStore.put`, `PadStore.expire`, `PadStore.discard` with its `path`,
`background_jobs.job_session_options`, `tools._settle_job_journal` and
`casa_core._settle_ended_job_state`. A delegated pad has no path, so none of this applies to it.

What it does not cover: a failed write leaves the file older than memory until the next change
writes it; a launch cancelled while its options build runs in a worker thread can have that
thread write the brief after the job's discard, and the next boot removes that file; and a Casa
death between a job's end and its discard leaves the file to that boot too.

**INV-PAD-011**: After a compaction, a job session's `SessionStart` text carries, after the job header, its pad's page-text sentence, its shape and its `## brief` section cut at 4,000 characters, when the pad holds anything.

Enforced by `scratchpad.reinject_text`, which reads memory only, and by
`background_jobs.compaction_hook_matcher`, which appends it.

## Failure behavior

**A restart empties every pad that is not a job's.** Pads are memory and each start is a new
process, while bindings and transcripts survive, so a delegated conversation can resume with no
pad; a job's pad is loaded from its file (INV-PAD-010). The tool then answers `No pad for
this conversation`, naming the start time, and the model rebuilds from its source. A pad's words are
in the session's transcript like any other tool output, kept with it
([`persistent-state.md`](persistent-state.md)).

**A refusal is a tool error the model can read,** and changes nothing. A pad written to after it
expired or after a restart starts empty; each reply's shape shows that.

**A job's pad file cannot be written or read.** A failed write is logged and the change stands
in memory; an unreadable file is logged, kept, and overwritten by the next change. Either way the
job goes on, with a pad that is at worst older or empty after a restart.

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
- `casa/rootfs/opt/casa/background_jobs.py::job_pad_path`
- `casa/rootfs/opt/casa/background_jobs.py::job_session_options`
- `casa/rootfs/opt/casa/background_jobs.py::compaction_hook_matcher`
- `casa/rootfs/opt/casa/tools.py::_settle_job_journal`
- `casa/rootfs/opt/casa/casa_core.py::_settle_ended_job_state`

**Tests**
- `tests/test_scratchpad.py`
- `tests/test_scratchpad_lifetime.py`
- `tests/test_scratchpad_route.py`
- `tests/test_job_scratchpad.py`

**Related**
- [`architecture/background-job-session.md`](../architecture/background-job-session.md)
- [`architecture/plugin-operator-calls.md`](../architecture/plugin-operator-calls.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/plugin-continuations.md`](../architecture/plugin-continuations.md)
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/stored-call-buttons.md`](../architecture/stored-call-buttons.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
