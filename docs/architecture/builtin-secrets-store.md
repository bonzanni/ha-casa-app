---
last_reviewed: 2026-11-25
---

# Built-in secrets store

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Where an install keeps its secrets when no 1Password service-account token is configured:
how the choice between 1Password and the built-in store is made, the `op` front every call
goes through, the `op` command lines the store answers, the service that owns the store, the
store's files and key, and the secrets page on Home Assistant's ingress. How a secret reference is resolved, and which options resolve
one, is [`configuration.md`](configuration.md); what each caller does with an `op` answer is
its own document's — [`vault-drop-off.md`](vault-drop-off.md),
[`plugin-secret-exploration.md`](plugin-secret-exploration.md),
[`own-voice-lifecycle.md`](own-voice-lifecycle.md).

## Mental model

**One rule, decided once per container start.** The oneshot `init-secrets-backend`
(`scripts/secrets-backend.sh`) reads `onepassword_service_account_token`: set, secrets are
in 1Password; empty (or bashio's `null`), they are in the built-in store. It writes the word
`1password` or `builtin` to `/run/casa-vault/backend`, under another name and then
renamed, so no reader sees it half written. That file is the only thing that says which
backend is in use (INV-VAULT-001): `/usr/local/bin/op` and the start-script helpers in
`/opt/casa/scripts/secrets-backend-lib.sh` (sourced by `svc-casa/run`, `svc-vault/run` and
`setup-configs.sh`) read it with shell builtins alone, and only the exact word `builtin` on
its first line means the built-in store — a missing, unreadable or different file means
1Password. No environment variable selects the backend. A token that is set but does not
work is still 1Password; nothing falls back to the built-in store. Like every option, the
token is reread only at the next start.

**`op` is a front.** The image keeps the 1Password CLI as `/usr/local/bin/op-1password`; the
name `op` belongs to a short POSIX shell script. On `builtin` it runs the store's client,
`vault_op_client.py`, in Python's isolated mode with the same arguments; on anything else it
execs the 1Password CLI with the caller's arguments, standard streams and open descriptors
(INV-VAULT-002). It names every program by absolute path and looks nothing up on PATH. The
image build fails unless `op --version` reaches the 1Password CLI of the pinned version,
because no backend has been chosen in a build.

**On the built-in store the two 1Password options still read as set.** The start scripts
pass both through the helper. On 1Password `casa_op_token` and `casa_op_vault` return the
values as configured (`null` read as empty). On the built-in store
`OP_SERVICE_ACCOUNT_TOKEN` is the fixed word `casa-builtin-store`, which is no secret, and
`ONEPASSWORD_DEFAULT_VAULT` is the option's value, or `Casa` when it is empty. The code that
wants a token, a vault or both before it calls `op` — the drop-off, the agents' bot tokens,
the secret candidates a plugin mutation reports, the configurator's vault tools' default
vault, and the Supervisor discovery, GitHub-token and OAuth blocks of `setup-configs.sh` — therefore runs as it
does on 1Password and is answered by the store, as is every `op read` of the secret
resolver. `set_plugin_env_reference` refuses both names, so a plugin
entry cannot override them ([`plugin-mutation-tools.md`](plugin-mutation-tools.md)).

**One process owns the store.** `svc-vault` runs `vault_service.py`, which opens the store
once, keeps it in memory and listens on `/run/casa-vault/store.sock`. The socket is created
under a `0077` umask and set to `0600`, so only its owner — root, the service's user — can
connect. Every `op` call is one connection: the client sends the arguments, plus standard
input only for the two forms that take a template, and prints the exit status and output the
service answers; it gives up after 60 seconds in all. The service drops, unanswered, a connection
still sending after 70 seconds. One lock covers each read, change and
save, so concurrent callers never lose each other's writes. `init-setup-configs` depends on
`svc-vault`, and s6 holds it until the service writes a newline on descriptor 3, which it
does once the socket is bound. On 1Password `svc-vault` signals ready at once and idles.
The service's log names each request's command words and exit status,
never an argument or a template. On the built-in store the same process also serves the
secrets page on a second socket (see "The page" below); readiness waits for both sockets to
be bound, but a page that cannot start costs only the page (INV-VAULT-009).

**The store answers the command lines Casa and its plugins run, and no others** — eleven
forms, each with the exit status, JSON keys and error wording the 1Password CLI was recorded
giving (`tests/fixtures/op_reference`, CLI 2.30.0):

| Form | |
|---|---|
| `--version` | prints `2.30.0` |
| `read op://VAULT/ITEM/FIELD` | a field by id, else by label in any case |
| `item list [--vault V] [--tags T[,T]] [--include-archive] --format json` | every item, or those with any of the tags |
| `item get REF [--vault V] --format json` | the item's document |
| `item get REF --vault V --fields label=L --reveal` | one field's value, its label matched in any case |
| `item create --vault V --format json -` | from a template on standard input |
| `item create --category "API Credential" --title T --vault V --tags T ASSIGN...` | fields from `LABEL[password]=VALUE` / `LABEL[text]=VALUE` |
| `item create --category ssh --title T --vault V --tags T --ssh-generate-key rsa,BITS` | a new RSA key of 2048, 3072 or 4096 bits |
| `item edit REF --vault V --format json` | the template's fields replace the item's |
| `item edit REF --vault V ASSIGN...` | changes or adds the assigned fields |
| `item delete REF --vault V` | removes the item |

Anything else fails with one error line whose message begins `unsupported by the built-in store:`
(INV-VAULT-003). A command line nearly one of these is not guessed at: a caller wrongly told "done"
would carry on with a secret never stored.
The store has one vault, named by the helper's vault value, and a command naming another
vault fails the way the CLI does for a vault it cannot find. An item is found by its id or by
its exact title; two items with one title make a lookup by that title fail as ambiguous.
A field read by label is the one labelled exactly so, else the first whose label differs only
in case, as the CLI matches a label; a write names its label exactly.

**The files.** `/data/vault/casa.kdbx` is a KeePass (KDBX 4) database and `/data/vault/key`
the text that unlocks it; the directory is `0700` and both files `0600`, and all three are in
the private-state inventory (INV-STATE-005). The first start with no database creates the
directory, a random key — written and synced before anything is encrypted with it, and never
over a key file already there — and an empty database. Every save writes a private temporary
file, syncs it and renames it over the database, so a crash leaves the old file or the new
one. When the container environment sets `CASA_VAULT_KEY`, that value is the key and no key
file is written; it is not an app option. An item is an entry in the root group whose
username and password are the entry's own, so the file opens in any KeePass program with the
key.

**What the store does not do.** No search beyond the id or exact title, no sharing, no
passkeys, no item history and no recycle bin: a delete is final. `--include-archive` changes
nothing. Items are made through `op` by Casa, drop-offs and plugins, and by a person on the page.

### The page

**Who serves it, and who reaches it.** `vault_service.py` listens, besides the `op` socket,
on `/run/casa-vault-page/page.sock` (`--page-socket`, `--page-group www-data` in
`svc-vault/run`): plain HTTP/1.0, one request and one thread per connection,
answered by `vault_page.Page`, a pure function of a request and the store that imports
nothing outside the standard library and `vault_store`. The socket's directory is `0710` and
the socket `0660`, owned by root and given to `www-data`, the group nginx's workers run as;
the `op` socket stays root's alone. nginx routes to the page socket only from the Home
Assistant ingress listener, and only on the built-in store
(INV-VAULT-005): `setup-nginx.sh` reads the backend through the same helper the start
scripts use and, on `builtin`, writes two ingress locations — `/vault/static/`, the page's
stylesheets and scripts (`/opt/casa/vault_static/`) served by nginx itself with their two
types named, and `/vault/`, proxied to the socket with the `X-Ingress-Path` and forwarded
host and scheme headers, `client_max_body_size 300k` and a 75-second read timeout. On any
other backend the ingress `/vault/` answers 404, as the external listener's always does. nginx's
log line says whether the page is served; like the backend, this is decided at each container start. Casa's status page on ingress shows a Secrets button
linking to `/vault/` when the backend file's first line is exactly `builtin`
(`casa_core._status_page`).

**The prefix is for links, not for trust.** Every link on the page sits under the path prefix
the `X-Ingress-Path` header names. A
prefix that is not one to eight `/`-led segments of 1–128 letters, digits, `.`, `_` and `-`
is refused with 403, and any prefix that is, a `.` or `..` segment included, is accepted. Who
may reach the page at all is decided by nginx's peer rule on
the ingress listener ([`http-surface.md`](http-surface.md)). The page answers GET, HEAD and
POST (any other method 405); a request with a query string is answered 404 by the listener
before the page reads it.

**What a GET carries.** The routes are the list (`/`), the add form (`/add`) and, per secret,
`/s/<id>` with `/edit`, `/delete` and `/show`; an id is the store's 26 lower-case base32
characters, and the page finds a secret by id alone. The list names each secret with who
keeps it (an agent's bot token, a drop-off's plugin, an SSH key, added on the page, or
Casa), or, for one with several values, their labels. A secret with exactly one value,
empty or not, and no SSH key has a Copy form on its row, with its own Show code (below); the
others open. A secret's page shows each field whose type is exactly `STRING` with its value;
every other type, including one the page does not know, is hidden: the same fixed run of dots
and Show, empty or not, and only a Show says "Empty" (INV-VAULT-006). It also says who last
wrote it, when it changed, under References each value's `title/label` (what follows
`op://<vault>/`; none for an SSH key; for a title with `/` or `?`, which cannot stand
there, `id/label`) and, once any were, how often its values were read
since the service started — `op read`, `item get` of a field or a document, and a Show — a
count kept in memory only. An SSH key's type and fingerprint are facts about the key; it
cannot be edited, only deleted. The edit form carries plain values in text boxes and, for a
hidden field, only the same dots and a Replace box; Remove drops either. Both forms end with their
buttons; the top bar's Save submits the same form. A value added on
the page (a label and a value in one of three slots) is always stored hidden (`CONCEALED`). The script filters the list by name, offers one-tap labels, says a slip
before a form is sent and turns off the boxes of a closed slot, a removed value or an
unticked Replace; without it every box is still a plain form control, and CSS `:has()`
shows and hides what the boxes open.

**What only a POST carries.** A hidden value leaves the service only in the answer to a POST
to `/s/<id>/show`: the secret's page with that one field shown, or, when the request accepts
JSON, the value and a fresh code. A GET of that path is 405. The script sends a form's code
once, then only the code an answer brings; after a failure it asks for a reload. With
`ClipboardItem`, Copy hands the clipboard a promise of the value inside the tap; with
`writeText` alone, it writes once the value arrives. Without a clipboard, or when it refuses,
the value is shown with "Press and hold the value to copy it." A value has one Show or Copy
running at a time; a new one ends the last, and both are off while a request is out. Each has 30 seconds from its tap: past that it ends, says
it took too long and keeps only the code a late answer brings. A value already handed to the
browser's clipboard (awaiting a permission prompt, say) is beyond the page's reach.
A shown value goes 30 seconds after it appears, however often it is copied. Show and
Copy codes are kept apart from the forms', 4096 of each: opening the list, however often,
pushes out only older Show codes.

**The checks on a state change.** Add, save, delete and Show are form POSTs, and each is
checked before anything is read or written, in this order (INV-VAULT-007): the `Origin`
header must equal the scheme and host the proxy forwarded, or, with no `Origin`,
`Sec-Fetch-Site` must be `same-origin`; the body must be a URL-encoded form of at
most 256 KiB that parses; and it must carry a code the page issued for that very path, unused
and under an hour old. Codes live in memory, so a restart voids every open form, and a code
presented once is spent whether or not it was good there. A refusal is a page that says
nothing was changed and links back. A save also carries a stamp, a keyed digest of
everything the secret held when the form was made: a secret that changed since is not
overwritten, and the answer is 409 (INV-VAULT-008). Adding a secret whose name another already has is refused on the form,
because a reference by name would then match two. A form may carry only the names the page
rendered for it, each at most once: a name sent twice, a name the form did not render (for
the edit form, as rendered for the secret it edits), or text that is not valid UTF-8 refuses
the whole form with 400. A save that would add or replace a hidden value with nothing, or
store a control character the store cannot hold (a form feed, say; tabs and line breaks
are kept) in a name or a new or changed value, is refused on the form;
a hidden value stored empty and left alone stays. A new-value slot is read
only when its `add<N>` box comes back (the add form's first slot always is). A refused form
comes back with its boxes ticked, its labels and its plain text as sent (a plain value the
form did not send, as the script omits one ticked for removal, is as stored), and every new or replacement value box empty. Line ends are stored as
LF (CRLF, then a lone CR); nothing else in a value changes. The page is stored as the
writer of what it makes or changes; its "Added by" and delete warning follow
the last writer and the tags: once changed on the page, a secret reads "Added by you", and
only a drop-off still warns.

**Every answer** carries `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: same-origin` and `Content-Security-Policy: default-src 'none'; script-src
'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self';
frame-ancestors 'self'; base-uri 'none'`; the HTML has no inline script or style, and everything from the
store or the request is HTML-escaped. **The log** has one line per page request — the
method when it is one the page takes, the route by name (`list`, `secret`, `add`, `edit`,
`delete`, `show` or `other`) and the status — never the path, which carries a secret's id,
nor a value, label or name. A failure is logged by its class name alone.

## Contracts & invariants

**INV-VAULT-001**: Which secrets backend `op` and the start scripts use is decided only by `/run/casa-vault/backend`, written at container start from the token option: only the exact word `builtin` on its first line selects the built-in store, and no environment variable selects either backend.

Enforced by the front and `casa_secrets_backend`, which read the file and nothing else, and
by `secrets-backend.sh`, which writes `builtin` for an empty or `null` token and `1password`
for any other value. What it does not cover: the file is written by root under `/run`, and a
root process that rewrites it changes the backend for every later call.

**INV-VAULT-002**: On any backend but `builtin`, the front execs the 1Password CLI with the caller's arguments, standard input and open descriptors unchanged, and an environment that differs only where a POSIX shell script must.

Those differences are three: `PWD` is the directory the call runs in, an entry whose name is
not a shell identifier is not carried over, and a variable named `backend` leaves with what
the front read. The start scripts on 1Password pass the option values through unchanged as
well, pinned beside it (`test_svc_casa_run_on_1password_starts_casa_as_it_did_without_the_helper`,
`test_setup_configs_blocks_on_1password_are_unchanged`).

**INV-VAULT-003**: A command line the built-in store does not answer exits non-zero, prints nothing on standard output and one `unsupported by the built-in store:` line on standard error, and leaves the store as it was.

The answered forms are the table above. What it does not cover: a supported form can still
fail for its own reasons (an unknown item, another vault, a template the store cannot hold
whole), each with the CLI's wording and the store unchanged.

**INV-VAULT-004**: When the store's database exists but the key is missing, empty or does not open it, or the database cannot be read, nothing in the store's directory is created, replaced or removed, no `op` socket is served, and the service still reports ready and stays up.

A database nobody can read now may still hold somebody's secrets, so it is left as it is. Every `op` call then fails with `built-in store unreachable`.

**INV-VAULT-005**: `/vault/` is routed to the page only on the ingress listener and only when the backend is `builtin`.

Enforced by `setup-nginx.sh`, which writes the proxy and static locations into the ingress
server only for the exact word `builtin` and a 404 location everywhere else, the external
listener included. What it does not cover: who may reach the ingress listener itself, which
is the listener's own source restriction ([`http-surface.md`](http-surface.md)).

**INV-VAULT-006**: No GET response contains a hidden field's value.

A field is hidden unless its type is exactly `STRING`. HEAD answers as GET does, without a
body. What it does not cover: a plain field's value is on the page by design, and a hidden
value does leave in the answer to a checked POST (INV-VAULT-007).

**INV-VAULT-007**: A state change is refused without a same-origin POST carrying an unused code issued for that path.

A state change here is an add, a save, a delete, or a Show, which reveals a value and counts
a read; a refused one leaves the store and the read counts as they were. What it does not
cover: the origin check compares against `X-Forwarded-Host` and `X-Forwarded-Proto`, which
nginx relays as they arrive from Home Assistant's ingress proxy, so whatever passes the ingress
listener's peer rule decides them; with no forwarded host, any `Origin` is refused.

**INV-VAULT-008**: A save does not overwrite a secret that changed after its form was opened.

The stamp covers the secret's whole content, its fields' order and types included, rather than its
change time, which the store keeps in whole seconds.
What it does not cover: add and delete carry no stamp; a delete
removes the secret as it is when the POST arrives.

**INV-VAULT-009**: A secrets page that cannot start does not stop the store from serving or from reporting ready.

An unknown group, a path that cannot be bound or a socket whose mode or group cannot be set
leaves one warning naming the kind of failure, no page socket, and the `op` socket
answering. When the store cannot be opened either, readiness and the running service still
hold, with no `op` socket (INV-VAULT-004).

## Failure behavior

**The store cannot be opened** (a missing, empty or wrong key, a damaged or unreadable file).
The service logs one ERROR line with no traceback — for the store's own errors it names the
file and why; for any other it names only the kind of failure, since a library's message may
quote what it read — serves no `op` socket and changes nothing (INV-VAULT-004); a started page answers 503
(below). It reports ready, so
Casa still starts, and s6 has nothing to restart. Every `op` call fails with `built-in store
unreachable`, and each secret reference stays unresolved as
[`configuration.md`](configuration.md) describes. Putting the key back (or setting
`CASA_VAULT_KEY` to it) and restarting the app is the repair.

**The service exits.** Its `finish` script logs a warning for a non-zero exit that is not
a death by signal; s6 starts it again, and
meanwhile `op` calls fail with `built-in store unreachable`.

**A command fails inside the store.** Exit status 1 and one error line. An unexpected error
is reported by its class name alone (`built-in store failure: <Class>`), and no error line
repeats a field value: whatever is printed ends up in the caller's logs. A request larger
than 1 MiB, or one that is not a command line, is refused the same way. A client that cannot
read the template from its standard input exits 1 with one error line and sends nothing.

**The page cannot start.** One WARNING line, `the secrets page is not served (<Class>)`;
the store serves and readiness is signalled as without a page (INV-VAULT-009), and
nginx's `/vault/` location then has no socket to reach until the next start. When the
store cannot be opened but the page starts, every request reaching the
page's routes answers 503 saying the secrets are unavailable (the reason only in the
log); one refused before them (a
method other than GET, HEAD and POST, a bad prefix, a query string) is answered as always.

**A page request fails.** The peer gets a 500 page and the log one ERROR line naming the
failure's kind. A form over the page's limit is answered 413 without its body being read;
a peer that never finishes sending is dropped after 70 seconds. Stopping the service waits
for a page request being answered.

**A token is set but does not work.** Still 1Password: every caller sees its CLI's error.

## Extension points

**A caller needs a new `op` form.** Add it to `vault_op.py` with reference outputs recorded
from the real CLI (`test-local/op-reference/record.sh` writes `tests/fixtures/op_reference`).
If it takes a template on standard input, `wants_stdin` changes in both `vault_op.py` and
`vault_op_client.py`, which cannot import Casa's modules; a test holds the two equal.

**The page gains a route.** Add it to `vault_page.route_name` too, since the log names a
route by that word and never by its path; a route that changes anything is a POST through
`Page._check`, with a code issued for its own path.

**A start script reads a 1Password option.** Source `secrets-backend-lib.sh` and pass the
value through `casa_op_token` or `casa_op_vault`; a script that reads the option directly
gets an empty token on the built-in store and skips its `op` calls.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/vault_store.py`
- `casa/rootfs/opt/casa/vault_op.py`
- `casa/rootfs/opt/casa/vault_service.py`
- `casa/rootfs/opt/casa/vault_op_client.py`
- `casa/rootfs/opt/casa/vault_page.py`
- `casa/rootfs/opt/casa/vault_static/page.js`
- `casa/rootfs/opt/casa/vault_static/theme.js`
- `casa/rootfs/opt/casa/vault_static/ha.css`
- `casa/rootfs/opt/casa/vault_static/vault.css`
- `casa/rootfs/opt/casa/casa_core.py::_status_page`
- `casa/rootfs/usr/local/bin/op`
- `casa/rootfs/opt/casa/scripts/secrets-backend-lib.sh`
- `casa/rootfs/etc/s6-overlay/scripts/secrets-backend.sh`
- `casa/rootfs/etc/s6-overlay/s6-rc.d/svc-vault/run`
- `casa/rootfs/etc/s6-overlay/s6-rc.d/svc-vault/finish`

**Tests**
- `tests/test_vault_store.py`
- `tests/test_vault_op.py`
- `tests/test_vault_service.py`
- `tests/test_vault_page.py`
- `tests/test_vault_page_static.py`
- `tests/test_vault_page_behaviour.py`
- `tests/test_setup_nginx_ingress.py`
- `tests/test_casa_status_page.py`
- `tests/test_vault_real_callers.py`
- `tests/test_vault_finance_plugin.py`
- `tests/test_secrets_backend_scripts.py`
- `tests/test_set_plugin_env_reference_guard.py`

**Related**
- [`architecture/http-surface.md`](../architecture/http-surface.md)
- [`architecture/configuration.md`](../architecture/configuration.md)
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/vault-drop-off.md`](../architecture/vault-drop-off.md)
- [`architecture/plugin-secret-exploration.md`](../architecture/plugin-secret-exploration.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
