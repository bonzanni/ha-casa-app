---
last_reviewed: 2026-11-12
---

# Approval cards

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What the operator reads when Casa asks for an approval: the card a separate model writes from
the request's facts, the fallback Casa writes itself when that model cannot, the bold
entities only Casa places, how the card is posted and edited without markdown parsing, and
the protected tools' own descriptions listed ahead of time for it. Every approval
`register_challenge` posts is such a card: a protected tool call, and the specialist install
or update, persona install, trigger, callback, event and erase-data consents. What an approval binds —
the grant, its key and its single use — is [`plugin-authorization.md`](plugin-authorization.md);
the record an install approval freezes is
[`configurator-approvals.md`](configurator-approvals.md); the markdown grammar the card
bypasses is [`telegram-rendering.md`](telegram-rendering.md).

## Mental model

**The card is display; the binding is the request's.** Registration computes what an
approval binds — the grant key, the hash of the canonical arguments, the consent identity, the
install root digest — from the request itself, and its size refusal measures the text Casa
renders for the request (`authz_grants.render_challenge_message` for a tool call, each
consent's own `render_*` text). The card is only what is posted: `register_challenge` takes a
`card_request` (`approval_card.CardRequest`: a kind, who asks, the facts, and a fallback
card), and the card is written inside the post driver, after registration. The raw request —
the canonical JSON, the full tool id, the digests — is not on the posted card; the approval
binds exactly those all the same.

**A separate model writes the card from the request's facts** (`approval_card.write_card`).
It is one query with no tools and no session (`_ask`: built-in tools removed, Bash, Task,
Agent and the cross-session tools denied, ephemeral settings), on Sonnet, under one budget of
15 seconds that covers waiting for a slot, the query, parsing and one retry. At most three run
at once. The facts are JSON bounded at 20,000 characters (`facts_json`): past that it drops the
input schema, cuts the tool description to 1,000 characters, halves the string cap step by
step, then replaces the arguments with a note, and at worst sends the request's kind and
requester with a cut of the rest. The
reply must carry the fields `{"action", "summary", "lines"}` as JSON — text around it, such as
a code fence or a leading sentence, is tolerated, since the reply is read from its first `{` to
its last `}`, and other keys are ignored — within fixed bounds, counted in UTF-16
units and checked, never clipped: an action of at most 80 units, a summary of at most 240,
at most five `Label: value` lines (labels at most 32, values at most 160), each field one line
with no unsafe code point (`card_from_reply`). Only the first two lines marked sensitive carry
the ⚠️ mark. An invalid reply is asked again only while six seconds of the budget remain. A
validated reply is cached (64 entries, an hour) under the model, its effort, the kind and a
hash of the facts JSON; the cache holds reply text, never a card, so a hit is rebuilt with
this request's own requester and title.

**The title names who asks and what approving lets happen.** It is `🔐 Allow {who} to
{action}?`. For a tool call `who` is the requesting agent's display name, its role when the
name is unsafe or too long; for an install, an update, a persona install and the erase question it is
the display name of the agent its turn's origin names (`tools._requester_name`: in a
configurator run, the agent that delegated to it), that agent's role when the name is unsafe
or too long, and `Casa` when the origin names no loaded agent; for a trigger, a callback and an
event it is `the {plugin} plugin`. The erase question's model-written card has its own fixed
title, `🔐 Uninstall {what}: erase its data too?`. These are the model-written card's titles; a
fallback carries its own (below) — the erase question's is the first line of its text,
`🗑 Uninstalling {what}`.

**What the model is told.** A tool call: the requester, the plugin, the tool as the plugin
declares it, the tool's MCP description and input schema when listed, the plugin's declared
summary and the arguments. An install or update: the renderer's own inputs without digests —
the specialist and component, the version (for an update, the new one and, when it can be read, the installed one), the mission, the
Casa tools the role grants when known, the default persona, the dependencies, each bundled
plugin with its protected tools and their declared summaries and the secrets it requires or
only references (under separate keys), the effective settings, the ones kept from the current
install, and the secret names provided, never their values. A persona install: the persona,
its version and display name, the checksum left out. A trigger: the route, the role a call
starts, the auth policy, the memory clearance, the body cap and `deliver`. A callback: the
route, what it receives and that it grants no turn and no memory. An event: the subscriber,
the event, its emitter and the delivery targets. The erase question: what is uninstalled,
each eraser with its plugin's summary, what each offered choice does, and its notes. The
system prompt tells the model to keep every recipient, amount, deletion, granted power and
secret, and every installed name and version, on the card, to name the Casa powers an
install or update grants, to mark at most two lines that a person must not miss (an
irreversible wipe counts even when deleting is what was asked), and to treat text inside the
facts as data. That is an instruction to a model, not a check: what code guarantees is that
those facts reach it. The fallback shows what Casa's own text for the request shows, and no
more — an install's consent text, for one, names the Casa tools only when the inspection it
is rendered from carries them, and the one the request tool rebuilds does not
([`specialist-lifecycle.md`](specialist-lifecycle.md)).

**Every failure ends in Casa's own card.** A timeout, a backend error, a refusal and two
invalid replies all return the request's fallback, and `write_card` never raises. A tool
call's fallback is `tool_fallback`: `🔐 Allow {who} to use {plugin}?` and one labelled line
per argument leaf in canonical order (nested keys joined with `›`), each value's unsafe code
points escaped and a value over 160 units cut to its first 120 with its length named, at most
twelve lines and 2,600 units, then `…and N more`. A call with no arguments at all shows,
instead of an empty `{}` line, the plugin's own one-line description of the tool: its declared
summary (interpolated when that works, else as written), else the first sentence of the
tool's listed description — one literal line, unsafe code points escaped, at most 400 units
and always inside the 2,600. A consent's fallback is `text_fallback` of the consent text Casa already renders: its
first line bold, the rest kept literally.

**Protected tools describe themselves ahead of time** (`tool_descriptions`). The plugin's
server is never started on the approval path. When an options build resolves a role's
plugins — a resident's, a specialist's, a delegated capture's, a plugin job's — it calls
`schedule_fill`, which hands the work to Casa's main loop (bound once at boot) from whatever
thread it runs on. For each resolved plugin that declares protected tools and has one stdio
server (`plugin_continuations.sweep_server`), a background task starts that server, runs
`initialize` and `list_tools` and nothing else (`plugin_continuations.list_plugin_tools`,
a 20 second budget for the listing; a server that does not answer is stopped afterwards, which
can take about two seconds more), and keeps the protected tools' descriptions and input schemas in memory under
the artifact id and the declared tool name, so a replacement artifact never reads another's
entry. Each artifact is listed once per process; a failed listing is tried again after five
minutes, at most three times. A miss is normal and logged: the card goes without.

## Contracts & invariants

**INV-PLUG-059**: An approval card's formatting is BOLD entities that Casa places over whole fields of the card's own structure — the title and each line's `Label:` — so no text inside a field, whoever wrote it, decides an entity, and the card is posted with exactly those entities, never through the markdown renderer.

Enforced by `approval_card.render_card`, which builds the display text and the entities in one
pass from the card's fields, and by `post_dm_keyboard`'s `entities` path, which sends the text
as given (lone surrogates replaced one for one, so the offsets stay valid) and never calls
`render`. A value such as `[x](https://evil.example)` therefore reads as those characters and
never becomes a hidden link (#1538). When an admission prepends a Casa line, the entities move
right by exactly that prefix (`shift_entities`).

What it does not cover: the plain retry. When the platform refuses the entities, the card is
sent once more as the same text with none — less emphasis, the same words.

**INV-PLUG-060**: An approval card is neither admitted nor posted for a request that settled — by a tap, a timeout, a cancellation, or a newer request that replaced it — while its card was being written.

Enforced in the post driver: after `write_card` returns, the driver checks the request and,
when it has settled, returns the broker's nothing-was-posted signal before admission, so no
send record is opened for a card never sent. From that check to the start of the send there
is no await. An install or persona approval that a newer request replaces settles its open
question at once (`specialist_install_consent.supersede_card`), including one whose card is
still being written, and the older request tool returns `approval_superseded` rather than a
post failure.

What it does not cover: a settlement during the send itself. That keyboard is posted and then
retired by the finish hook installed on its message id, as for any keyboard.

## Failure behavior

**The card model is slow, down, refuses or answers badly.** The fallback is posted within the
budget, with a warning logged; the approval works the same.

**A burst of approvals.** Three are written at a time; the rest wait for a slot inside their
own budget and fall back when it runs out, so no request waits for its card past its
fifteen-second budget.

**The tool's description was never listed.** The model is told the tool's name, the plugin's
declared summary and the arguments; an info line names the missing description.

**The platform refuses the card's entities.** The same text is sent once without entities;
any other send failure is the keyboard's ordinary delivery failure.

## Extension points

**A new challenge** passes a `card_request` to `register_challenge`: the facts a model needs
to write it, and a fallback Casa writes itself, with its own text still the size measure. Its
status edits go through `edit_dm_message(literal=True)`, so they are never parsed either.

**A new bold span** is a structural field of `Card`, placed by `render_card`; content never
chooses one (INV-PLUG-059).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/approval_card.py`
- `casa/rootfs/opt/casa/tool_descriptions.py`
- `casa/rootfs/opt/casa/authz_grants.py::ChallengeCoordinator.register_challenge`
- `casa/rootfs/opt/casa/authz_grants.py::ChallengeCoordinator._tool_card_request`
- `casa/rootfs/opt/casa/plugin_continuations.py::list_plugin_tools`

**Tests**
- `tests/test_approval_card.py`
- `tests/test_approval_card_writer.py`
- `tests/test_approval_card_posting.py`
- `tests/test_telegram_approval_card_io.py`
- `tests/test_consent_cards.py`
- `tests/test_tool_descriptions.py`

**Related**
- [`architecture/plugin-authorization.md`](../architecture/plugin-authorization.md)
- [`architecture/configurator-approvals.md`](../architecture/configurator-approvals.md)
- [`architecture/specialist-lifecycle.md`](../architecture/specialist-lifecycle.md)
- [`architecture/telegram-rendering.md`](../architecture/telegram-rendering.md)
- [`architecture/output-boundary.md`](../architecture/output-boundary.md)
- [`architecture/plugin-continuations.md`](../architecture/plugin-continuations.md)
<!-- END SOURCEMAP -->
