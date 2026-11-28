---
last_reviewed: 2026-11-28
---

# Casa Browser — site logins without 1Password (the app's own store)

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Where the Casa Browser app keeps site logins when its `onepassword_service_account_token`
option is empty: the choice of login store at start, the store's files and key, the second
vault class the broker uses for it, what does not run in that mode, how a site's link to a
login belongs to one store, and the Saved logins page that runs only in that mode. The broker, its registry and the login flow themselves are
[`casa-browser-credentials.md`](casa-browser-credentials.md); the live-view save that creates
a login is [`casa-browser-signin.md`](casa-browser-signin.md). Casa itself has a store built
on the same module for its own secrets, with a page on the same engine,
[`../architecture/builtin-secrets-store.md`](../architecture/builtin-secrets-store.md) and
[`../architecture/builtin-secrets-page.md`](../architecture/builtin-secrets-page.md); at run time
the two apps share no file, key or process.

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

**The shared files are carried in both apps.** `scripts/sync_shared_vault.py` names them
(`SHARED_FILES`): the store module `vault_store.py`, the page's engine `vault_page.py`, its
listener `vault_page_server.py` and the page's four static files under `vault_static/`. Each
under `casa_browser/rootfs/opt/casa_browser/` is a byte-identical copy of Casa's under
`casa/rootfs/opt/casa/` (INV-BRW-075); the script copies them from Casa's tree, and with
`--check` copies nothing and names each copy that differs or is missing. An item is a KeePass
entry, its username and password the entry's own, so the file opens in any KeePass program with
the key.

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
  update from the same version is a `conflict`. A save on the Saved logins page (below) writes
  the next number too, on the same loop.
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

Two ways. **Through the live view**: a person signs in there, the sign-in check judges it
signed in, and the broker's automatic save writes what was typed
(`broker/main.py::Broker.save_attempt`, [`casa-browser-signin.md`](casa-browser-signin.md)).
A site with no linked login gets a new one, titled with the site's name and marked
`unverified` until an automatic sign-in with it works. A linked login is replaced only when
its own password was rejected in that run or it is still `unverified`; otherwise it is kept
(`item_kept`), and values equal to the stored ones write nothing. The next sign-in uses it.
**On the Saved logins page**, below, a person adds one. The site form has no box for a login's
id in this mode, and the form ignores a `vault_item_id` posted to it. No passkey is kept.

## The Saved logins page

**Who serves it.** In built-in mode `broker/main.py::run` serves the page, besides the RPC
socket, on `/run/casa-browser/logins/page.sock` (`PAGE_SOCKET`) through the shared listener
`vault_page_server.listening`, with the directory used as found (`prepared`). `svc-browser-broker/run`
makes that directory in both modes, `casa-broker:casa-ingress` `2710`: set-group-id, so the
socket the broker binds there (`0660`) takes nginx's group, and nginx's group may pass through
the directory but not list it. The listener takes each request in a thread of its own and
hands it to the broker's event loop, where it is answered, so a page write never falls between
a version check of the broker's and the write that follows it. The page is built on the store
the broker's vault holds open (`BuiltinVault.store`), never on a second copy. A page that
cannot start costs the page alone: the broker logs one fixed line, `casa-browser broker: the
saved-logins page could not be started`, and serves on. No RPC operation names the page or
answers with it (`test_no_rpc_operation_reaches_the_page`).

**Who reaches it.** `casa_browser.conf` includes `/run/casa-browser/nginx/logins.conf` in the
ingress server, after the peer rule. `svc-browser-nginx/run` writes that file at each start from
`python -m casa_browser.ui logins-conf` (`ui.py::logins_conf`), which reads the controller's
public options: with `login_store` `builtin` it holds two locations — `/logins/static/`, the
page's static files (`/opt/casa_browser/vault_static/`) served by nginx with their two types
named, and `/logins/`, proxied to the page socket with the `X-Ingress-Path` and forwarded host
and scheme headers, `client_max_body_size 300k` and a 75-second read timeout; with any other
word, or an options file it cannot read, it is empty and `/logins/` reaches the app's own pages,
which have no such path. Setup's store row, while the store is open, and a site's page, for a
site with sign-in addresses, link to `<prefix>/logins/` ([`casa-browser-ingress.md`](casa-browser-ingress.md)). The page's
routes, codes, origin check, stamp, headers and log are the shared engine's, as on Casa's
secrets page ([`../architecture/builtin-secrets-page.md`](../architecture/builtin-secrets-page.md)),
in the login profile's words; the page is in English.

**The login profile** (`broker/logins_page.py::profile`). A login has four boxes, in this order:
sign-in addresses (one per line), username, password and authenticator seed (optional). Only
the addresses and the username are shown as they are; the password, the seed and any value
under a label the profile does not know are hidden until a Show. The `version` field is kept
from the person: it has no box and no card, a save made here writes the next number and a new
login gets 1. A login made here has the shape a live-view save gives one: category `LOGIN`, a
`username` field even when none was typed (stored empty, as is a username removed on the edit
form), and the addresses one per line without empty lines or surrounding spaces. A save is
refused, with a sentence that repeats no value, when it has no sign-in address, an address the
broker reads no host from, addresses on more than `LIST_HOSTS` (10) hosts, a username or a
password over 1,024 bytes, neither a username nor a password, or a seed from which
`broker/builtin.py::one_time_code` makes no code. A name is at most `LIST_TITLE_CHARS` (300)
characters, so every login fits the broker's listing; a name cannot be changed. With the store
unavailable every route says the saved logins could not be opened.

**What a save and a delete do to the links.** The page tells the broker after the store has
written the login or no longer holds it:

- `Broker.login_changed` clears the `unverified` and `rejected` marks of every site linked to
  that login, so a live view afterwards does not save over what the person typed unless a site
  refuses it in its turn (INV-BRW-077). The marks were made about what the login held before.
- `Broker.login_deleted` unlinks every site linked to it, as Forget login does: the link and its
  marks go, and so do the captures held for those sites. Their next sign-in finds no linked
  login (`not_found`).
- A password refusal sets `rejected` only while the login is still at the version the sign-in
  read; a login the person changed or deleted meanwhile is not marked.

**The broker's registry is the truth; the controller keeps a copy.** In built-in mode
`Broker.status` carries `links`, site id to login id for every linked site, left out whole when
it would not fit one broker line. `RunController.refresh_links` reads the `builtin_links` rows
(`Store.builtin_links`), then asks the broker, and clears a row only when the broker does not
name that site with that same login (`Store.clear_builtin_link` deletes only while the row still
names it, so a link made while the reply was on its way stays). A broker that does not answer,
or answers without `links`, changes nothing. It runs before the controller acts on a link: in
`RunController.list_sites`, `ask_sign_in`, `_look` and `_login`, and in the UI's Sites list
and site page.

**With a 1Password token none of this runs**: the broker serves no page (the directory stays
empty), `logins.conf` is empty, `status` carries no `links`, `refresh_links` returns at once
and asks nothing, and the site form keeps its 1Password item box.

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

**INV-BRW-075**: Every file Casa Browser shares with Casa — the store module, the page's engine and listener, and each static file of the page — is a byte-identical copy of Casa's.

`tests/test_vault_store_copies.py` compares each file `SHARED_FILES` names, one case per file,
and checks that the static directory of each app holds exactly the static files named there, so
a file added to one tree alone fails. It runs in Casa's suite, where both trees are present.

**INV-BRW-076**: A site's link to a login, with its marks, belongs to the store it was made in: under the other store the site reads as having no link, and linking, saving or forgetting there leaves the first link as it was.

**INV-BRW-077**: A login changed on the Saved logins page is not replaced by a live-view save made under a permission from before that change: the page's save clears the `unverified` and `rejected` marks of every site linked to the login, and a password refusal sets `rejected` only while the login is still at the version the sign-in read.

Pinned on a real broker and store with the page engine driving the change: a login whose
password a site refused is corrected on the page, and a live view afterwards is answered
`item_kept`; and a sign-in that filled the old password, held at its submit while the person
changes the login, ends rejected with no mark set. Clearing nothing in `login_changed` fails
the first; dropping the version comparison fails the second. What it does not cover: a site
that refuses the corrected password marks it again, and a live view after that replaces it, as
for any login.

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

**The page cannot start** (no socket directory, a socket that cannot be bound). One fixed line
in the log; the broker serves the RPC as before, and nginx's `/logins/` has no socket to reach
until the next start.

**A write fails.** The store forgets its copy in memory and reads the file again, so the next
read shows the version and values from before; the write answers `refused` when the store
refused it and `unavailable` when the file could not be written.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/broker/builtin.py`
- `casa_browser/rootfs/opt/casa_browser/vault_store.py`
- `casa_browser/rootfs/opt/casa_browser/broker/logins_page.py`
- `casa_browser/rootfs/opt/casa_browser/vault_page.py`
- `casa_browser/rootfs/opt/casa_browser/vault_page_server.py`
- `casa_browser/rootfs/opt/casa_browser/vault_static/page.js`
- `casa_browser/rootfs/opt/casa_browser/vault_static/theme.js`
- `casa_browser/rootfs/opt/casa_browser/vault_static/ha.css`
- `casa_browser/rootfs/opt/casa_browser/vault_static/vault.css`
- `scripts/sync_shared_vault.py`
- `casa_browser/rootfs/opt/casa_browser/broker/main.py::run`
- `casa_browser/rootfs/opt/casa_browser/broker/main.py::Broker.login_changed`
- `casa_browser/rootfs/opt/casa_browser/broker/main.py::Broker.login_deleted`
- `casa_browser/rootfs/opt/casa_browser/controller.py::RunController.refresh_links`
- `casa_browser/rootfs/opt/casa_browser/store.py::Store.builtin_links`
- `casa_browser/rootfs/opt/casa_browser/store.py::Store.clear_builtin_link`
- `casa_browser/rootfs/opt/casa_browser/ui.py::logins_conf`
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
- `casa_browser/tests/test_logins_page.py`
- `casa_browser/tests/test_broker_logins_page.py`

**Related**
- [`reference/casa-browser-credentials.md`](../reference/casa-browser-credentials.md)
- [`reference/casa-browser.md`](../reference/casa-browser.md)
- [`reference/casa-browser-signin.md`](../reference/casa-browser-signin.md)
- [`reference/casa-browser-runtime.md`](../reference/casa-browser-runtime.md)
- [`reference/casa-browser-browser.md`](../reference/casa-browser-browser.md)
- [`reference/casa-browser-ingress.md`](../reference/casa-browser-ingress.md)
- [`architecture/builtin-secrets-store.md`](../architecture/builtin-secrets-store.md)
- [`architecture/builtin-secrets-page.md`](../architecture/builtin-secrets-page.md)
<!-- END SOURCEMAP -->
