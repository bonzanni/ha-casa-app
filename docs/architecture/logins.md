---
last_reviewed: 2026-11-24
---

# Casa's own logins

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The logins Casa itself signs in with — the iCloud account of the contacts and the OpenAI key
and webhook secret of the phone line — and how Casa finds the vault item that holds each: the
default titles, the item the resident assistant chose, the accepted field labels, the check
that says what is wrong, the candidates the assistant judges by, the `connect_login` tool and
the operator's line about a choice. Who uses each login is [`contacts.md`](contacts.md) and
[`phone.md`](phone.md); the built-in store that answers `op` when no 1Password token is set
is [`builtin-secrets-store.md`](builtin-secrets-store.md); `/data/logins.json` is in
[`persistent-state.md`](persistent-state.md).

## Mental model

**A login is one vault item, chosen or found by its default title.** `logins.SERVICES`
holds each login: `icloud_contacts` (the Apple ID and the app-specific password) and
`openai_voice` (the API key and the webhook signing secret). Every value is read from ONE
item in the default vault (`ONEPASSWORD_DEFAULT_VAULT`): the item the assistant chose for the
login, kept by its `op` item id, or, while none is chosen, the item titled `iCloud Contacts`
or `OpenAI Voice`. A third state is an unknown choice, left by a damaged `logins.json`: the
login then reads no item at all, its default one included, until an item is chosen for it
again (`logins.read_from` refuses without asking the vault). Values used together are read from the item resolved once
(`logins.read_all`), so they never come from two different choices. A chosen item that is
gone or incomplete is never replaced by another one: the login then has nothing to read.
With no default vault configured nothing here runs `op` at all.

**Each value has a few accepted labels.** The Apple ID is `username` or `email`; its password
`password` or `credential`; the API key `credential`, `password` or `api key`; the signing
secret `webhook_secret` or `webhook secret`. The first accepted label that holds a value wins,
and a label is matched whatever its case, as `op` matches it. A value is read through
`vault_read.read_field`, `op item get <item> --vault <vault> --fields label=<label> --reveal`
with a 10-second timeout, in a worker thread; it takes an item's id or its title, both
secrets backends answer it, and `op` output is never logged or returned except the value
itself.

**The check says what is wrong.** While the choice is unknown, `logins.check` answers
`choice_unreadable` without asking the vault, and the operator's sentence asks him to have
the assistant connect the login again. Otherwise it reads the item's document once and looks
only at its labels and whether their values are empty, and answers a `Check` with one
problem: `no_vault`, `vault_unreadable`, `item_not_found`, `field_missing` or `field_empty`. `op` exits
the same way for a missing item and an unreachable vault, so after a failed read the vault is
listed once, and the item is `item_not_found` only when that listing works and lacks it.
`Check.for_model` is what a tool may return: the problem and Casa's own name for the value
concerned ("the app-specific password"), never a vault label. `Check.sentence` is the
operator's line and the log's, and may name a label.

**The assistant chooses; code never searches on its own.** `connect_login`, granted to the
resident assistant alone, refuses unless the turn reads at private clearance and is a direct
turn in the operator's own chat outside any engagement (the contact tools' guards). Without
`item` it answers `connected`, or `not_connected` with the problem and the candidates:
`logins.candidates`, the items whose title contains one of the login's search terms
(`icloud` or `apple`; `openai`), at most five items tried in all (an item whose document
cannot be read counts), each with its id, its title on one line cut to
80 characters, its category and, per value, whether it holds one under an accepted label. It
is the one tool that shows an agent the titles of vault items, because a title is all she can
judge an item by; no result of it carries a field label or a value. Its description tells her
to take an item only when its title clearly belongs to that login, and to ask the operator
when none does or several could. With `show_candidates` a connected login also gets its
candidates, each marked `in_use` when it is the item the login really resolves to now (no
row is, when that is no item: two items under the default title, none, or an unknown choice), so she can find
another item when the operator wants one; without it a connected login searches nothing.
Nothing at boot or in a scheduled sync looks for an item.

**The operator is told first, then the choice is stored.** With `item`, the id must be among
the candidates the login's own search finds at that moment; anything else is refused and
nothing is stored. Casa checks the item, sends the operator its own line naming it by title
(`logins.chosen_line`: "Contacts now use the vault item '…'", saying from which field a value
came when it is not the first accepted label, or what is wrong with the item), and stores the
choice in `/data/logins.json` only once that line was delivered. Storing a choice ends an
unknown choice for that login only; another whose choice is unknown stays so, as `null` in
the file. An unsent line answers
`not_stored` with `operator_not_told`; a file that cannot be written answers `not_stored`
with `store_failed`, and Casa then sends the operator a second line saying the item could not
be saved and what applies now: the item chosen before, the item under the default title, or
none while the choice is unknown. An incomplete item is stored all the same, and the answer and the line
say what it lacks.

**A choice reaches the services at once.** `logins.choose` and `logins.forget` call the
listeners a service registered with `logins.on_change`: the phone line drops every value it
had kept and discards a read that the choice overtook, so after a choice a key and a secret
never come from two different items. Under the default title each value is kept on its own
for up to ten minutes: when the item under that title is replaced in the vault, the two may
come from different items until those minutes are up. The contacts service
reads the login afresh at each refresh.

## Contracts & invariants

This document defines no invariant of its own. The guards of `connect_login` are those of
INV-CONTACTS-006, and the tests that pin what the tool returns and when it stores a choice are
in `tests/test_connect_login_tool.py`.

## Failure behavior

**`logins.json` cannot be read.** No file means nothing was chosen, and the default titles
apply. A file that does not parse or does not hold item ids (or `null`) is logged as an
error, its content copied to `logins.json.corrupt` (0600), and it is replaced in one atomic
write by a file that records every login's choice as unknown (`null`), so the state outlasts
a restart; when that write fails the damaged file stays and is found again at the next
start. A file that cannot be opened is left as it is, and every choice is unknown. Either
way no login reads any vault item until `connect_login` stores a choice for it: the phone
line answers webhook requests 503 and tells the operator, at most once an hour; contacts
that synced before fail their refresh and tell him once per streak; contacts that never
synced stay silent, and `refresh_contacts` and `connect_login` answer `choice_unreadable`.
A file damaged while Casa runs is noticed at the next start, since the choices are held in
memory after the first read.

**The chosen item was deleted or emptied.** The login has nothing to read and nothing falls
back to the default title; the check names `item_not_found`, `field_missing` or `field_empty`,
and the service that needs it says so in its own way. The check and contacts read the vault
afresh each time; the phone line keeps each value it already read for up to ten minutes
(`phone.service.CACHE_S`, 600 seconds), so it notices a deleted item only once that has
run out.

**`op` cannot be run or answers something unreadable.** The check answers
`vault_unreadable`, and the candidates are empty.

## Extension points

**A new login** is a `Service` in `logins.SERVICES` — its default title, the accepted labels
per value, the search terms, Casa's own names for the values and the words of the operator's
line — and a value of `connect_login`'s `service` enum. Its user reads it with
`logins.read_all` or `logins.read` and registers `logins.on_change` when it keeps what it read.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/logins.py`
- `casa/rootfs/opt/casa/vault_read.py`
- `casa/rootfs/opt/casa/tools.py::connect_login`
- `casa/rootfs/opt/casa/tools.py::_operator_chat_refusal`

**Tests**
- `tests/test_logins.py`
- `tests/test_connect_login_tool.py`
- `tests/test_vault_read.py`

**Related**
- [`architecture/contacts.md`](../architecture/contacts.md)
- [`architecture/phone.md`](../architecture/phone.md)
- [`architecture/builtin-secrets-store.md`](../architecture/builtin-secrets-store.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
