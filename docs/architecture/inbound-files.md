---
last_reviewed: 2026-09-21
---

# Inbound files

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What happens to a file the operator sends in the Telegram direct chat: the non-text message
handler, the checks a file passes, the per-role folder it is published into under
`/data/agent-inbox/`, the read grant that lets the Telegram default agent open it, the
`list_inbound_files` tool, and the retention sweep. How ordinary text messages become turns
is [`telegram.md`](telegram.md); how `share_inbound_file` passes a copy of one of these files
to a plugin is [`plugin-handoff.md`](plugin-handoff.md); how `path_scope` and the rest of an agent's hooks are built is
[`hook-resolution.md`](hook-resolution.md); the outbound media surface (`send_media`) is
[`tools-interface.md`](tools-interface.md).

## Mental model

**Arrival stores; asking reads.** A file arriving runs no agent turn. The channel downloads
it, checks it, publishes it, and posts one acknowledgement of its own — a channel message,
not model output. The agent reads the file later, in an ordinary text turn, when the operator
asks: it calls `list_inbound_files` to find the path, then opens it with the `Read` tool it
already has. A read puts the file into the model request (a PDF becomes a document block),
which is why the size cap is set by the request, not by Telegram's download limit.

**Only the operator's direct chat, only a closed set of kinds.** A file is downloaded only
from the operator in the direct chat. In the engagement supergroup a person's content — a
file, a photo, a sticker, a voice message and the like — draws a reply pointing at the direct
chat, while topic notices and bot messages draw nothing. An update from any other chat is
logged and dropped, exactly as a text message from it is; a sender who is not the operator
in the configured chat is refused. In the direct chat a document is accepted when its final filename suffix is one of `.pdf`, `.png`,
`.jpg`, `.jpeg`, `.txt`, `.md` or `.csv`, and a photo is accepted as its largest size. Each
extension maps to a content predicate borrowed from the outbound media policies — PDF magic,
PNG/JPEG signature, decodable text — and bytes that fail it are refused as a mismatch. The
text predicate checks encoding, never structure, so a malformed `.csv` is stored as the text
file it is. Every other kind — voice, video, sticker, contact, a chat notice such as a pin,
anything Telegram adds later — gets a refusal naming what does work.

**Received bytes decide the cap, not the declared size.** The per-file cap is 8 MB
(`agent_inbox.py::CAP_BYTES`). A declared size over it is refused before any download; the
download itself counts bytes as they arrive and stops at the cap, with redirects and
decompression off. The file URL must sit under the bot's configured file endpoint: with a
self-hosted Bot API server, `getFile` can return a local path instead of a URL, and that is
refused rather than copied from Casa's own filesystem. The whole download is held in memory
under a five-minute deadline, so a hung or cancelled download leaves nothing on disk.

**Three directories per role, one of them readable.** `/data/agent-inbox/<role>/` holds
`staging/` (a `.part-*` file exists only while one upload is being published), `meta/` (one
JSON per published file carrying the operator's filename for display) and `ready/` (the
published files). All three are created `0700` at boot, before any agent is built and before
channels go live, and boot also removes whatever a crash left in `staging/`. Only `ready/` is
ever granted.

**The read grant is standing, for one agent.** The Telegram default agent — the `assistant`
resident, named once in `casa_core.py::main` for both purposes, so who receives DMs and who
may read their files cannot drift apart — resolves its hooks with `ready/` appended to every
`path_scope` readable list (`hooks.py::resolve_hooks`, `extra_readable`). The grant holds for
every turn of that agent, scheduled turns and event wakes included, not only the turn in which
the operator asked; everything under it is a file the operator chose to send that agent. Every
other agent resolves with nothing appended and keeps its own scope; a path handed to another
agent in a delegation is judged by that agent's hooks, which do not carry the grant.

**The grant is only as safe as the folder's contents.** The `path_scope` prefix check is
lexical and does not resolve links, so a link planted in `ready/` would be followed by the
read. The guarantee is therefore placed on what `ready/` may contain (INV-INBOX-001) rather
than on the prefix check — and the agent, which has no shell and no writable path there,
cannot plant anything in it.

**Full means refuse, never evict.** A role holds at most 50 files or 200 MB. A full folder is
refused before any download, as an early check that decides nothing; the quota scan that
decides and the rename happen inside one per-role lock, so two concurrent uploads cannot both pass a
check each ran alone; the file flush happens before the lock is taken and the directory flush
after it is released, so a slow disk never stalls another arrival. An acknowledged file is
deleted only by age: an hourly sweep removes files seven days after publication, dated from
the Casa-generated name rather than from `mtime`, so nothing can re-date one.

## Contracts & invariants

**INV-INBOX-001**: `ready/` holds only regular, single-link files Casa wrote under a Casa-generated name — publication creates the file exclusively without following links and checks it by descriptor before an atomic rename through a pinned directory descriptor, and the sweep removes, logging at WARN, any other entry it finds.

Every listing applies the same filter, so an entry the sweep has not reached yet is never
offered to the agent as a file. The inbox directories themselves are refused at provisioning
when one is not a real directory, so a symlinked inbox yields no inbox at all.

What it does not cover: a process running as root in the same container can write into
`ready/` directly, and in the window before the next sweep a planted link is admitted by the
lexical prefix check. This is not a sandbox against such a process.

**INV-INBOX-002**: The inbound-file read grant is exactly the Telegram default agent's `ready/` directory, appended to the readable list of every `path_scope` entry that agent resolves — the default bundle and an explicit hooks file alike — and it never adds a writable prefix or reaches any other agent.

`staging/`, `meta/`, the role directory itself and paths that climb out of `ready/` stay
denied, as does every other path the agent could not read before. An inbox that fails to
provision grants nothing, and uploads are then answered with the storage refusal. The
delegated-specialist and in-casa executor hook builds pass no grant at all.

What it does not cover: the grant is standing rather than per turn, and a document once read
stays in that session's history like any other tool result — a later turn of the same
session sees its content without reading the file again.

**INV-INBOX-003**: No byte the operator sent reaches a filesystem path — a published name is `<epoch_ms>-<16 hex>.<ext>` with the extension taken from the allowlist key that matched, and the operator's filename is kept only as bounded display metadata.

Traversal sequences, separators, NUL and control characters, confusables and overlong names
are therefore structurally absent from every path, whatever the upload's filename was.

**INV-INBOX-004**: A full inbox refuses the next upload and evicts nothing; the quota check and the publication are one critical section, and an acknowledged file is deleted only by age, seven days after its publication.

The one other deletion is the best-effort retraction after a failed directory flush
(INV-INBOX-005), which only ever touches a file that was never acknowledged. A read racing
the seven-day boundary fails, and the agent can say so.

**INV-INBOX-005**: An upload is acknowledged only after its file and the `ready/` directory were both flushed; a failed file flush publishes nothing, and a failed directory flush yields an uncertain outcome whose reply claims neither that the file was kept nor that it was not.

A rename is atomic visibility, not durable storage, and the acknowledgement promises seven
days, so the directory flush is strict here even though the shared atomic writer's directory
flush is best-effort. After a failed directory flush Casa tries to remove what it published,
but that removal is no more certain than the rename was, which is why the reply says the
outcome is unknown rather than that nothing was kept. Display metadata is written after
publication and is best-effort: a missing display name falls back to Casa's name and is never
a failure.

**INV-INBOX-006**: Every non-text message posted in the operator's chat — any chat, when no chat id is configured — ends in a stored file or a reply, and none of them starts an agent turn.

The reply is an acknowledgement, a refusal that says why, or — when the outcome is unknown —
the uncertainty reply. There is no silent list in that chat: chat notices such as a pin or a
title change are answered too, with wording that does not call them files. An exception the
handler did not anticipate draws the uncertainty reply rather than none, since it may have
struck after publication. The handler is registered on new messages only, so an edited message
or caption draws nothing, and its filter is disjoint from the text handler's, so each update
reaches exactly one of them.

Outside that chat the channel follows its text path. An update from any other chat is logged
and dropped, never answered. In the engagement supergroup only a person's content draws the
reply pointing at the direct chat; topic notices — many of them caused by Casa's own topic
actions — and bot messages draw nothing.

What it does not cover: a reply whose send fails is logged and not retried — except for
Telegram's flood control, whose stated wait is honoured once, capped at ten seconds.

**The tool reports; it does not read.** `list_inbound_files` takes no arguments and returns,
newest first, each file's display name, kind, size, age and absolute path, and states that
listing a file is not reading it. It resolves the inbox of the agent executing the turn — the
origin's `execution_role`, falling back to `role` — so any agent without an inbox, including an
agent the Telegram default agent delegated to, is told it has no inbound files and is shown no
path (INV-HANDOFF-004). Casa never matches the operator's wording against filenames:
with several candidates the agent lists them and asks.

**What this does not claim.** Nothing yet stops the agent describing a file it did not open;
the tool's wording and the agent's instructions are prose, not a guarantee (#1038). Page
count and read cost are not bounded — only bytes are; a PDF over the model's page limit fails
to read, and the agent says it could not open it. And, as stated under INV-INBOX-001, this is
not a sandbox against a root process in the same container.

## Failure behavior

**The inbox cannot be provisioned at boot** — creating it, reclaiming `staging/`, the first
sweep, or registering the hourly one. Boot continues; the role has no inbox and
therefore no read grant, no sweep is registered, and every accepted upload is answered with
"I couldn't save that one. Nothing's been kept — try sending it again."

**The download fails, times out, or is redirected.** Nothing is written; the operator is told
Telegram would not give the file and to send it again. A local path from `getFile` gets its
own refusal.

**Storage fails before publication.** The `.part` is removed and the storage refusal is sent;
`ready/` is unchanged.

**The directory flush fails after publication, or the upload fails unexpectedly.** The
uncertainty reply is sent (INV-INBOX-005, INV-INBOX-006). Sending the file again is always safe: a re-send is a new file under a new
name.

**The folder is full.** The refusal says the oldest files go after seven days and offers to
read one already held (INV-INBOX-004).

**The sweep finds something Casa did not write.** It removes it and logs at WARN; one
failing inbox never stops the sweep of another.

## Extension points

**A new accepted kind** is one row in `agent_inbox.py::INBOUND_POLICIES`: an extension and a
content predicate that is total over bytes. The refusal wording in the channel names the
accepted kinds and must change with it. Accepting a kind the agent's `Read` cannot open is
storing a file for a consumer that does not exist.

**Another role receiving files** is a second `wire` call for that role, and with it a second
standing read grant — decide it as a grant, not as plumbing.

**Passing a file on.** `share_inbound_file` copies a listed file into the plugin handoff
folder, where any in-Casa plugin can read it for seven days; the inbox copy and its grant are
unchanged. Its contract is in [`plugin-handoff.md`](plugin-handoff.md).

**Changing the retention or the caps** changes what the acknowledgement and the full-folder
refusal promise. The reply texts and the tool's description read the constants; the user-facing
app documentation spells the period out and changes with them.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/agent_inbox.py`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._on_non_text_message`
- `casa/rootfs/opt/casa/channels/telegram.py::_classify_inbound`
- `casa/rootfs/opt/casa/channels/telegram.py::_non_text_filter`
- `casa/rootfs/opt/casa/channels/telegram.py::_inbound_reply`
- `casa/rootfs/opt/casa/tools.py::list_inbound_files`
- `casa/rootfs/opt/casa/hooks.py::resolve_hooks`

**Tests**
- `tests/test_agent_inbox.py`
- `tests/test_inbound_files.py`

**Related**
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/hook-resolution.md`](../architecture/hook-resolution.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
