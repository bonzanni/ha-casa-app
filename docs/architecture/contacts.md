---
last_reviewed: 2026-11-24
---

# Contacts

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The contacts store: a read-only copy of the operator's address book, synced from iCloud over
CardDAV, plus the marks Casa keeps about a person (priority, relation, language); how a sync
writes it, how a model reads the address book's notes, the caller lookup, the three tools
the resident assistant uses, and the card on which the operator confirms a priority
contact. Which vault item holds the iCloud login is [`logins.md`](logins.md), and how it
is read on either secrets backend [`builtin-secrets-store.md`](builtin-secrets-store.md); the private-state inventory that
holds `/data/contacts` is [`persistent-state.md`](persistent-state.md); the option is in
[`../reference/operator-options.md`](../reference/operator-options.md).

## Mental model

**A mirror per source, and marks Casa owns.** `/data/contacts/contacts.db` (directory 0700,
in the private-state inventory) is a SQLite file with `STRICT` tables
(`contacts/schema.py`). `people` and `numbers` mirror each address-book source, keyed by
the source's name (`icloud` today): every row carries it, and a sync rewrites only its own
source's rows. `marks` holds, per person and field (`priority`, `relation`, `language`), one
row per origin — `operator`, `group`, `note` — and does not reference `people`, so a mark
can outlive its person's absence from a sync. `note_reads` records, per person, a hash of
the note last settled into `note`-origin marks taken together with the note reader's prompt
version (`store.settled_hash`). `sync_state` holds each source's last
success, last error and whether it is failing.

**The store follows the memory store's connection discipline.** `ContactsStore` gives every
access one transaction on its own connection (`BEGIN IMMEDIATE` to write, `BEGIN` to read),
closed on exit. `open()` creates the directory, brings the file to the current schema
version in one transaction (`PRAGMA user_version`), then switches it to WAL; it never
raises. On failure the store is unavailable with its reason kept, and every transaction
raises `ContactsUnavailable`. A file written by a newer schema, or an unversioned file that
already holds tables, is refused and left as it is. Casa boots either way.

**The effective mark is the strongest origin.** `store.marks_for` takes, per field, the row
of the strongest origin, `operator` over `group` over `note`. Priority is on when that
row's value is `1`, so the operator's `0` overrides the group's `1`. Priority has two
origins only — the operator, and membership of the iCloud group named `Priority` — and
`set_mark` refuses a `note`-origin priority (INV-CONTACTS-003).

**A sync is fetch, then one write, then notes.** `sync.run` asks the source for every card
it holds. A source either hands over all of them or raises `SourceError`; it never hands
over part of the book, because a card missing from the result reads as a deleted contact.
Group cards (`X-ADDRESSBOOKSERVER-KIND:group`) give their members a group name; every
person card becomes one row, with its numbers reduced to E.164 by the `phonenumbers`
library (`contacts/numbers.py`). A number written without `+` is read as dialled
from the country the `contacts_default_region` option names, and skipped when the option is
empty;
an unreadable number is skipped and counted, never fatal. One write transaction then
replaces the source's people and numbers (`store.replace_source`), makes the `group`-origin
priority marks match `Priority` membership, and records the success. A person who has left
the source loses its `group`- and `note`-origin marks and its `note_reads` row; its
`operator`-origin marks stay, and apply again if the same uid returns.

**iCloud is one CardDAV source.** `carddav.ICloudSource` signs in with the Apple ID and an
app-specific password (HTTP Basic), walks the standard discovery — the account's
principal, its address-book home, the collection whose resource type is `addressbook` —
and fetches every card with one `addressbook-query` REPORT, following the absolute URLs
iCloud answers with. Each request has 30 seconds. A 401, any other status than 207, a
timeout, or an unreadable answer is a `SourceError` whose text names the step and never
holds the credentials. In the REPORT every response must be accounted for
(`ICloudSource._cards`): an answer that is not a DAV multistatus, a response with no href
or several, a response-level status other than 200 (the collection's own entry included,
which is skipped only when it did not fail), a member without exactly one readable
`address-data`, a card body that is not well formed (`vcard.well_formed`: `BEGIN:VCARD`
and `END:VCARD` lines strictly alternate, at least one block), a body that holds more
than one card (an address-book resource is one vCard), a card with no content, or a member
href or uid (after the fallback to the resource name for a card without one) that appears
twice is a `SourceError` raised before any card is returned. `contacts/vcard.py` reads vCard 3.0
as Apple serves it: folded lines, `itemN.` prefixes, escaped commas, semicolons and
newlines, related names, group cards and their members. Every `BEGIN:VCARD` block yields
exactly one card.

**What a note says is read by a model.** After the write, every note not yet settled — new,
changed, or not read by an earlier sync — is handled outside any transaction
(`sync._read_notes`). A blank note clears the person's `note`-origin marks and is settled
without a model call. A non-empty one goes to `notes.read`, in batches of 20, as one isolated
query on the small model (`haiku`) with no tools, no session and no settings, under the
prompt in `contacts/prompts/note_reader.md`, which tells the model a note is data, never
instructions, and that a country flag in a note marks the language the person speaks. The answer is a JSON object keyed by uid, each entry carrying `relation`
(free text) and `language` (a two-letter code or empty); code checks only that shape, and
an entry that fails it is dropped. Each valid reading rewrites that person's `note`-origin
relation and language marks — only if the note is still the one that was read — and
settles the note by writing its settled hash to `note_reads` (INV-CONTACTS-007). A new
`notes.PROMPT_VERSION` makes every settled note differ once, so the next sync reads each
again and then settles it under the new version. Note reading never
fails the sync.

**The running service.** `contacts/service.py` holds the store Casa opened at boot, the
operator channel, the note model and the login reader, configured once Telegram exists.
The iCloud account is the `icloud_contacts` login ([`logins.md`](logins.md)): the Apple ID
and its app-specific password, both read in one go from the vault item the assistant chose,
else, when no choice is recorded, from the item titled `iCloud Contacts`; while the record
of the choice is unreadable nothing is read from the vault until the login is connected
again. Without both values a refresh does nothing.
Where the login never worked — no successful sync on record and no item chosen — that is
silence: contacts are not set up. Where it did, a refresh that finds no login is a failed
refresh (`service.LOGIN_ERROR`), and `service.login_problem` keeps what the check found
wrong. `service.refresh` never raises, and concurrent callers share the refresh already
running. The scheduler runs `service.run_daily` at 04:10 in Casa's timezone (`casa_tz`), and once
60 seconds after boot while the iCloud mirror is empty.

**The caller lookup is deliberately narrow.** `lookup.lookup(store, raw_number,
default_region=…)` is the read other parts of Casa may use to recognise a phone number. It
returns a `CallerInfo` of five fields and nothing else (INV-CONTACTS-001): `known`,
`priority`, `name`, `first_name`, `language`. A number held by more than one person resolves
to the one with effective priority first, then by name, with source and uid breaking ties,
so the answer is deterministic. Language is the person's effective language mark, else a
short table keyed by the number's calling code, else `en`. It never raises: an unreadable
number or an unavailable store is an unknown caller.

**The assistant's three tools.** `find_contact` searches names and organisations
(case-insensitive) and the number a query reads as, and returns up to ten people, priority
first, each with numbers, organisation, groups and effective marks with the priority's
origin; `more` says others matched. `refresh_contacts` runs a refresh now and reports
added, removed and kept people and skipped numbers, or the error, or `not_connected` with
the login check's problem code when no login can be read, pointing the assistant to
`connect_login`. `set_contact_marks` writes `operator`-origin marks for exactly one match:
relation and language apply at once (`""` removes the operator's setting), as does
`priority: false`; `priority: true` posts a confirmation card and changes nothing yet. When
the contact matches none or several people nothing changes, and the candidates are named
with a number each. The tools are granted to the resident assistant alone, by its shipped
`runtime.yaml` and `role.yaml`; they are in neither `SPECIALIST_CASA_TOOL_ALLOWLIST` nor the
restricted webhook tools.

**Priority waits for the operator's tap.** `_contact_priority_card` posts Casa's own card in
the operator's chat on Casa's bot, with Approve and Cancel, written from the stored contact.
The change it carries — source, uid, name and numbers — is frozen in the broker request's
metadata (namespace `resident_ask`, the operator's `authz` scope) when it is registered,
and only Approve applies it, from the broker's finish hook, outside any turn
(INV-CONTACTS-004). Approve writes an `operator`-origin `1` for that source and uid, so a
person renamed meanwhile is still the one made priority. The card expires after five
minutes (`_PRIORITY_CARD_TIMEOUT_S`): while it waits it holds the operator's authz lane.
On every outcome the card keeps its question and gains one line of its own, not
`ask_retirement`'s wording — made priority, no longer in the contacts, or not changed. A card already waiting for the same person is not posted
again, and a person who is already priority gets no card.

## Contracts & invariants

**INV-CONTACTS-001**: The caller lookup returns exactly five fields — known, priority, name, first name, language — and never raises.

`CallerInfo` is a frozen dataclass of those five; nothing in the store a caller could
misuse — numbers, notes, relation, organisation, groups — crosses this line. What it does
not cover: the tools, which show the operator more about a person in the operator's own
chat.

**INV-CONTACTS-002**: A sync whose source fails, or whose source returns no people while the mirror holds that source's people, changes neither the mirror nor the marks.

The failure is recorded in `sync_state`, and `store.record_sync` reports only the first
failure since the last success, so the daily job tells the operator once per failure
streak, through Casa's operator channel; the refresh tool returns the error to the
assistant instead. What it does not cover: a source that returns a smaller but non-empty
book is believed, and the people it no longer holds are removed.

**INV-CONTACTS-003**: Priority is never taken from a note: a note's reading carries only relation and language, and the store refuses a note-origin priority mark.

**INV-CONTACTS-004**: Making a person priority is applied only by the operator's Approve on Casa's card, and what is applied is the change frozen when the card was registered.

Cancel, a timeout, a contact that a sync removed meanwhile, or a store that cannot be
written apply nothing, and the card says so. Taking someone off the priority list needs no
card.

**INV-CONTACTS-005**: The marks the operator set survive every rewrite of the mirror and win over the group's and the note's.

A sync deletes only `group`- and `note`-origin marks of people who left the source; it
never deletes an `operator`-origin row.

**INV-CONTACTS-006**: The contact tools refuse a turn whose memory clearance is below private before touching the store.

`_contacts_gate` derives the clearance the way memory reads do; a guest chat, a voice turn,
a webhook trigger or an unstamped origin never reads at private. `set_contact_marks`
additionally refuses anything but a direct turn in the operator's own chat (a DM or a
button, outside any engagement). The gate runs before the tool's first await.

**INV-CONTACTS-007**: A non-blank note is settled only once a valid reading of it has landed; a note whose reading failed, was unusable, or was skipped because no model was given is read again by the next sync.

A settled note is not sent to the model again until it changes, or until the note reader's
prompt version does, which makes every settled note read once more.

## Failure behavior

**The store cannot be opened.** Contacts are unavailable until the next start: the tools
answer `unavailable`, a refresh returns a failure, and the lookup answers an unknown caller.
Nothing else in Casa depends on it. The daily job tells the operator once per failure
streak, as it does for an unexpected error in the refresh: that streak is kept in the
service's memory (`service._failed`), since the store may be the broken part, and ends at
the next successful sync or at `configure`.

**iCloud refuses, times out, or answers wrongly.** The mirror and the marks stay as they
were (INV-CONTACTS-002), and the operator is told once per failure streak, with the step or
the cause in the line. The line adds that the last good copy is still in use when the
mirror holds people from iCloud, and that no contacts are stored yet when it holds none; it
adds neither when the store cannot say. A sign-in refused with 401 names the app-specific
password.

**The failure notice does not reach the operator.** Delivery is tracked apart from the
failure streak: a notice the operator channel did not take (it raised, or answered that it
was not delivered) stays pending in the service's memory, and every later daily run sends
it again, one notice and never a stack of them, until the channel takes it or a refresh
succeeds. With no operator channel the notice is only logged.

**The login cannot be read.** When it never worked, a refresh does nothing and logs it at
info, the daily job sends nothing, and `refresh_contacts` answers `not_connected` with what
is wrong. When it worked before (a deleted or emptied item, a chosen item gone), the refresh
fails with `the iCloud login could not be read`, the mirror and the marks stay as they
were, and the daily job tells the operator once per failure streak, adding what is wrong
with the login (`logins.Check.sentence`). Nothing falls back to another item.

**A note cannot be read.** A model error, a timeout (60 seconds per batch) or an unusable
answer leaves that note's marks as they were and its note unsettled, so the next sync reads
it again; the sync still succeeds.

**The priority card cannot be posted.** Without Telegram, a configured operator chat, or a
connected Casa bot, `set_contact_marks` returns `unavailable` and registers nothing; other
fields in the same call have already been applied and are reported. When the card is
registered but Telegram does not take it, the broker withdraws the request,
`set_contact_marks` returns an error saying the card could not be posted, and nothing is
applied.

## Extension points

**A new address-book source** implements `sync.Source` — a `name` and an async `fetch`
that returns every card or raises `SourceError` — and is passed to `sync.run`. Nothing in
the import step knows where cards came from; rows carry the source's name, and sources
never touch each other's rows.

**A new caller of the lookup** goes through `lookup.lookup` and gets the five fields; adding
a sixth flips INV-CONTACTS-001 and needs a new id.

**A schema change** appends a version to `schema.MIGRATIONS`; opening an older file runs
the missing versions in one transaction.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/contacts/__init__.py`
- `casa/rootfs/opt/casa/contacts/schema.py`
- `casa/rootfs/opt/casa/contacts/store.py`
- `casa/rootfs/opt/casa/contacts/sync.py`
- `casa/rootfs/opt/casa/contacts/carddav.py`
- `casa/rootfs/opt/casa/contacts/vcard.py`
- `casa/rootfs/opt/casa/contacts/numbers.py`
- `casa/rootfs/opt/casa/contacts/notes.py`
- `casa/rootfs/opt/casa/contacts/prompts/note_reader.md`
- `casa/rootfs/opt/casa/contacts/lookup.py`
- `casa/rootfs/opt/casa/contacts/service.py`
- `casa/rootfs/opt/casa/tools.py::find_contact`
- `casa/rootfs/opt/casa/tools.py::refresh_contacts`
- `casa/rootfs/opt/casa/tools.py::set_contact_marks`
- `casa/rootfs/opt/casa/tools.py::_contacts_gate`
- `casa/rootfs/opt/casa/tools.py::_contact_priority_card`

**Tests**
- `tests/test_contacts_store.py`
- `tests/test_contacts_sync.py`
- `tests/test_contacts_carddav.py`
- `tests/test_contacts_vcard.py`
- `tests/test_contacts_numbers.py`
- `tests/test_contacts_notes.py`
- `tests/test_contacts_lookup.py`
- `tests/test_contacts_service.py`
- `tests/test_contacts_tools.py`
- `tests/test_contacts_priority_card.py`

**Related**
- [`architecture/logins.md`](../architecture/logins.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/builtin-secrets-store.md`](../architecture/builtin-secrets-store.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/memory-scoping.md`](../architecture/memory-scoping.md)
- [`architecture/tool-questions.md`](../architecture/tool-questions.md)
- [`reference/operator-options.md`](../reference/operator-options.md)
<!-- END SOURCEMAP -->
