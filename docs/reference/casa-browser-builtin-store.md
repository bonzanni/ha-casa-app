---
last_reviewed: 2026-11-26
---

# Casa Browser — site logins without 1Password (the app's own store)

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Where the Casa Browser app keeps site logins when its `onepassword_service_account_token`
option is empty: the choice of login store at start, the store's files and key, the second
vault class the broker uses for it, what does not run in that mode, and how a site's link to a
login belongs to one store. The broker, its registry and the login flow themselves are
[`casa-browser-credentials.md`](casa-browser-credentials.md); the live-view save that creates
a login is [`casa-browser-signin.md`](casa-browser-signin.md). Casa itself has a store built
on the same module for its own secrets,
[`../architecture/builtin-secrets-store.md`](../architecture/builtin-secrets-store.md); the
two apps share no file, key or process.

## Mental model

**One word, written once.** Root bootstrap decides where logins are kept, at every start and
before any service, and writes the answer as one word, `login_store`, to the three files the
services start from. No service works it out from the token, so the broker, the controller and
the proxy read the same word, and a token that does not work is not mistaken for none.

**Built-in mode is the same app without 1Password.** The broker answers the same operations
from a KeePass file in its own data directory; everything made only for 1Password — the guest
account, the browser extension, the SDK, the special network access — is not started.

## The three states

`options.py::parse_options` validates every option whichever store is used, then:

| Token option | `login_store` | Set up (`configured`) |
|---|---|---|
| holds anything, spaces included | `onepassword` | when all six 1Password options are set |
| empty, absent or null | `builtin` | always; the four guest values are dropped |
| any option invalid | `none` | never |

`options.py::bootstrap` writes the word to `/run/casa-browser/broker/broker.json` (the
broker), the controller's public options (`Components.login_store`, `Store(path,
login_store=…)`, `Ui.builtin`) and `/run/casa-browser/proxy-private/hosts.json` (the proxy,
`proxy_main.py::load_login_store`). Before those three files it writes Chrome's managed policy
for the store ([`casa-browser-browser.md`](casa-browser-browser.md)); if that fails none of
them is written. The vault class follows the word alone (`broker/main.py::build_vault`):
`BuiltinVault` for `builtin`, the 1Password class for anything else.

`none` takes the 1Password code path: the app is not set up, the 1Password class is built with
an empty token, the texts are the 1Password ones, and the built-in store is neither opened nor
written.

## The store

**Its files.** `broker/main.py::VAULT_DIR` is `/data/broker/vault`, the broker user's: the
directory `0700`, `casa.kdbx` (a KeePass KDBX 4 database) and `key` (the text that opens it)
`0600`. The first start with no database creates the directory, a random key and an empty
database; nothing is created over a database already there. The directory is in Home
Assistant backups, and with it the `key` file of a key the store made itself. A store created
with a usable key from outside (below) has no key file: bootstrap writes that key only to
`broker.json` under `/run`, so it has to be kept apart and supplied again after a restore, or
the restored store cannot be opened. Supplying the key from outside to a store that already has
a key file it made itself leaves that file in place, and in backups.

**The key.** `CASA_BROWSER_VAULT_KEY` is read from the container environment; it is not an app
option. Unset or empty, it is as if no key were supplied: the store uses its own `key` file,
making one when it makes the database. A usable value is the key, and no key file is
generated. A value that holds only whitespace is refused (below). Bootstrap copies the
variable's value, unchanged, into `broker.json` in built-in mode only, and removes the
variable from the s6 container environment in every mode, also when bootstrap fails
(`options.py::scrub_container_environment`), so no service inherits it. The store ignores whitespace around a key (`vault_store.py::Store.open` strips
it), so a supplied key with leading or trailing spaces is the key without them. A key that is
set but holds only whitespace is refused before the store is opened
(`broker/builtin.py::_opened`): the store is then unavailable, and neither the key file nor a
new key is used in its place.

**The module is one file carried in both apps.**
`casa_browser/rootfs/opt/casa_browser/vault_store.py` is a byte-identical copy of Casa's
`casa/rootfs/opt/casa/vault_store.py` (INV-BRW-075): an item is a KeePass entry, its username
and password the entry's own, so the file opens in any KeePass program with the key.

## The second vault

`broker/builtin.py::BuiltinVault` answers the broker with the same methods and result shapes as
the 1Password class. A login is one item of category `LOGIN` with the fields `username`,
`password` (when it has one), `websites` (the sign-in origins, one per line), `one-time
password` (an authenticator seed) and `version`. Its operations are `resolve_vault_id` (always
`builtin`), `get`, `list_logins`, `create_login`, `update_fields` and `totp_code`;
`vault_accessors` answers `None`, since nobody else can open the store. Nothing in it removes an
item, and `test_no_delete_interface` scans it with the broker package (INV-BRW-019).

- **The version rule.** A login is created at version 1; `update_fields` changes it only when
  the version its caller read is the stored one, and writes the next number (INV-BRW-073).
  No method awaits between its check and its write, so on the broker's one event loop a second
  update from the same version is a `conflict`.
- **Errors leave as the broker's fixed codes**, never the store's message: an unknown id or an
  id that is only another login's title is `not_found`, a failing write `refused` or
  `unavailable`, anything else `unavailable`.
- **Codes.** `totp_code` makes the RFC 6238 code from the stored seed (`broker/builtin.py::one_time_code`:
  base-32 or an `otpauth://totp/` address). The broker learns only that a code can be made;
  the seed stays in the store.

## What does not run in built-in mode

The rule the whole mode follows (INV-BRW-071), and where each part of it is enforced:

- **No guest.** `RunController._guest_due` is false, so no guest driver session opens and every
  sign-in carries `guest_ask` false; the broker's `unlock_guest` is `refused`.
- **No SDK.** `build_vault` builds no 1Password client; `Broker.status` answers
  `{'configured': True, 'store': 'builtin', 'available': …}` and reads no vault accessor.
- **No extension.** The written Chrome policy lacks `ExtensionSettings` and
  `DeveloperToolsAvailability`. Chrome's launch arguments are the same in both modes.
- **No special network access for 1Password.** `proxy_main.py::build_addon` gives the broker
  listener no host, the extension list none, and leaves out `infra.py::EXTENSION_RULES`. Visits
  to 1Password's own websites are ordinary site requests. In an account session Chrome's own
  update and extension-download requests are refused as `browser_background`
  ([`casa-browser-runtime.md`](casa-browser-runtime.md)).
- **No passkey step.** A passkey marker on a sign-in page does not stop a sign-in: the stored
  username and password are filled as on any form, the look reports `passkey: false`, and no
  passkey choice is made.
- **Texts.** Pages, cards and event texts produced in this mode name neither 1Password, its
  extension nor a guest: the cards and events say "saved login" (`confirmations.item_word`),
  the pages use their `.builtin` catalog keys, Setup shows the store's row
  ([`casa-browser-ingress.md`](casa-browser-ingress.md)), and `register_item` returns no
  whole-vault warning. Texts stored earlier (a run's events and records) stay as written.

## A site's link belongs to its store

A site's link to a login — its item id and the `unverified` and `rejected` marks
(`broker/main.py::LINK_KEYS`) — is kept per store, in two places:

- **The broker's registry.** 1Password's link stays in the site's record, where it always was;
  the built-in store's sits under the record's `builtin` key. `broker/main.py::site_view` gives
  the flows only the link of the store in use, and `broker/main.py::site_record` writes the
  other store's link back as the file had it.
- **The controller's store.** The `builtin_links` table (schema entry 11) holds the built-in
  link; `Store(path, login_store='builtin')` reads and writes a site's `vault_item_id` there and
  leaves `sites.vault_item_id` to 1Password (empty for a site first made in built-in mode)
  ([`casa-browser.md`](casa-browser.md)).

So setting or clearing the token never mixes the two (INV-BRW-076): a site linked under
1Password reads as having no login in built-in mode, and asks for one there; the token set
again finds the 1Password item it had.

## How a login gets in

Only through the live view: a person signs in there, the sign-in check judges it signed in,
and the broker's automatic save writes what was typed
(`broker/main.py::Broker.save_attempt`, [`casa-browser-signin.md`](casa-browser-signin.md)).
A site with no linked login gets a new one, titled with the site's name and marked
`unverified` until an automatic sign-in with it works. A linked login is replaced only when
its own password was rejected in that run or it is still `unverified`; otherwise it is kept
(`item_kept`), and values equal to the stored ones write nothing. The next sign-in uses it. A
site can also be linked by id on the site form. There is no page to list, change or remove
saved logins, nothing in the app stores an authenticator seed, and no passkey is kept.

## Contracts & invariants

**INV-BRW-070**: The login store is chosen once at start from the validated options and written as one word that the broker, the controller and the proxy read; with invalid options the built-in store is not opened or written.

**INV-BRW-071**: With the built-in store nothing made for 1Password runs — no guest session, no SDK client, no host admitted for 1Password, no extension policy — and no text produced in that mode names 1Password, its extension or a guest.

`test_builtin_rule.py` carries the rule as one start: bootstrap on an options file with an empty
token and all four guest options filled, the real broker served on a socket, the controller,
pages and API over it, and the proxy built from the file bootstrap wrote. It counts guest
sessions, SDK clients, admitted 1Password hosts and extension updates, extension policy keys,
guest values in any file bootstrap wrote, and texts naming 1Password, the extension or a guest:
every count is zero. Beside it, focused tests check single parts in full: the broker
listener's `CONNECT`, all three of Chrome's update requests, the exact two policy keys, the
cards' copy, and the pages in every language, Setup with the store opened and unavailable. The
image lane checks what those tests build by hand is what the services s6 starts were given:
the controller's Setup page through nginx, and the proxy's browser listener refusing Chrome's
update check for the extension as `browser_background`. What it does not cover: texts stored
before the switch, and the cards Casa's browsing plugin draws from its own text.

**INV-BRW-072**: A set token never falls back to the built-in store: a token of spaces is a token, and a token that fails leaves the built-in store unopened.

**INV-BRW-073**: A saved login changes only when the version its writer read is still the stored one, and each write raises it by one.

**INV-BRW-074**: A store file whose key is missing or does not open it, or a supplied key of whitespace only, is never replaced or changed; the broker stays up and answers `unavailable`.

**INV-BRW-075**: The two copies of the store module, Casa's and Casa Browser's, are identical.

**INV-BRW-076**: A site's link to a login, with its marks, belongs to the store it was made in: under the other store the site reads as having no link, and linking, saving or forgetting there leaves the first link as it was.

## Failure behavior

**The store cannot be opened** (no key, a wrong key, a key of whitespace, a damaged file). The
broker logs one fixed line, `casa-browser broker: the saved logins could not be opened`, with no
exception text; it starts and stays up, its `status` says `available: false`, every vault
operation answers `unavailable`, and Setup says the saved logins could not be opened. Every
sign-in — for a site with a linked login and for one without — ends `unavailable` before the
driver is used (`Broker.login`), so nobody is sent to the live view to type a login that could
not be saved. A live-view sign-in made anyway is answered as not saved (`saved: false`,
`reason: unknown`) and the file is not touched. Nothing is retried: putting the key back (or
setting `CASA_BROWSER_VAULT_KEY` to it) and restarting the app is the repair.

**A write fails.** The store forgets its copy in memory and reads the file again, so the next
read shows the version and values from before; the write answers `refused` when the store
refused it and `unavailable` when the file could not be written.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/broker/builtin.py`
- `casa_browser/rootfs/opt/casa_browser/vault_store.py`
- `casa_browser/rootfs/opt/casa_browser/broker/main.py::build_vault`
- `casa_browser/rootfs/opt/casa_browser/broker/main.py::site_view`
- `casa_browser/rootfs/opt/casa_browser/broker/main.py::site_record`
- `casa_browser/rootfs/opt/casa_browser/options.py::parse_options`
- `casa_browser/rootfs/opt/casa_browser/proxy_main.py::build_addon`
- `casa_browser/rootfs/opt/casa_browser/controller.py::RunController._guest_due`
- `casa_browser/rootfs/opt/casa_browser/confirmations.py::item_word`

**Tests**
- `casa_browser/tests/test_broker_builtin.py`
- `casa_browser/tests/test_builtin_vault.py`
- `casa_browser/tests/test_vault_contract.py`
- `casa_browser/tests/test_vault_store_copy.py`
- `casa_browser/tests/test_builtin_rule.py`
- `casa_browser/tests/integration/test_builtin_mode.py`
- `tests/test_vault_store_copies.py`

**Related**
- [`reference/casa-browser-credentials.md`](../reference/casa-browser-credentials.md)
- [`reference/casa-browser.md`](../reference/casa-browser.md)
- [`reference/casa-browser-signin.md`](../reference/casa-browser-signin.md)
- [`reference/casa-browser-runtime.md`](../reference/casa-browser-runtime.md)
- [`reference/casa-browser-browser.md`](../reference/casa-browser-browser.md)
- [`reference/casa-browser-ingress.md`](../reference/casa-browser-ingress.md)
- [`architecture/builtin-secrets-store.md`](../architecture/builtin-secrets-store.md)
<!-- END SOURCEMAP -->
