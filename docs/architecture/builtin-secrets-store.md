---
last_reviewed: 2026-11-22
---

# Built-in secrets store

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Where an install keeps its secrets when no 1Password service-account token is configured:
how the choice between 1Password and the built-in store is made, the `op` front every call
goes through, the `op` command lines the store answers, the service that owns the store, and
the store's files and key. How a secret reference is resolved, and which options resolve
one, is [`configuration.md`](configuration.md); what each caller does with an `op` answer is
its own document's — [`vault-drop-off.md`](vault-drop-off.md),
[`plugin-secret-exploration.md`](plugin-secret-exploration.md),
[`own-voice-lifecycle.md`](own-voice-lifecycle.md).

## Mental model

**One rule, decided once per container start.** The oneshot `init-secrets-backend`
(`scripts/secrets-backend.sh`) reads `onepassword_service_account_token`: set, secrets are
in 1Password; empty (or bashio's `null`), they are in the built-in store. It writes the word
`1password` or `builtin` to `/run/casa-vault/backend`, under another name first and then
renamed, so no reader sees it half written. That file is the only thing that says which
backend is in use (INV-VAULT-001): `/usr/local/bin/op` and the start-script helpers in
`/opt/casa/scripts/secrets-backend-lib.sh` (sourced by `svc-casa/run`, `svc-vault/run` and
`setup-configs.sh`) read it with shell builtins alone, and only the exact word `builtin` on
its first line means the built-in store — a missing, unreadable or different file means
1Password. No environment variable selects the backend. A token that is set but does not
work is still 1Password; nothing falls back to the built-in store. Like every option, the
token is read again only at the next start.

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
service answers; it gives up after 60 seconds in all. The service closes a connection that
has not finished sending after 70 seconds, without an answer. One lock covers each read, change and
save, so concurrent callers never lose each other's writes. `init-setup-configs` depends on
`svc-vault`, and s6 holds it until the service writes a newline on descriptor 3, which it
does once the socket is bound. On 1Password `svc-vault` signals ready at once and idles;
there is no socket. The service's log names each request's command words and exit status,
never an argument or a template.

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
(INV-VAULT-003). A command line that is nearly one of these is not guessed at: a caller told
"done" for something the store did not do carries on with a secret that was never stored.
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
nothing, because there is no archive. Nothing in Casa manages items by hand; they are made
by Casa, by drop-offs and by plugins, through `op`.

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

**INV-VAULT-004**: When the store's database exists but the key is missing, empty or does not open it, or the database cannot be read, nothing in the store's directory is created, replaced or removed, no socket is served, and the service still reports ready and stays up.

A database nobody can read right now may still be somebody's secrets, so it is left for
whoever comes to mend it. Every `op` call then fails with `built-in store unreachable`.

## Failure behavior

**The store cannot be opened** (a missing, empty or wrong key, a damaged or unreadable file).
The service logs one ERROR line with no traceback — for the store's own errors it names the
file and why; for any other it names only the kind of failure, since a library's message may
quote what it read — serves nothing and changes nothing (INV-VAULT-004). It reports ready, so
Casa still starts, and s6 has nothing to restart. Every `op` call fails with `built-in store
unreachable`, and each secret reference stays unresolved as
[`configuration.md`](configuration.md) describes. Putting the key back (or setting
`CASA_VAULT_KEY` to it) and restarting the app is the repair.

**The service exits.** Its `finish` script logs a warning for a non-zero exit code that is
not a death by signal, and
s6 starts it again; meanwhile `op` calls fail with `built-in store unreachable`.

**A command fails inside the store.** Exit status 1 and one error line. An unexpected error
is reported by its class name alone (`built-in store failure: <Class>`), and no error line
repeats a field value: whatever is printed ends up in the caller's logs. A request larger
than 1 MiB, or one that is not a command line, is refused the same way. A client that cannot
read the template from its standard input exits 1 with one error line and sends nothing.

**A token is set but does not work.** The backend is 1Password, and the 1Password CLI's
error is what every caller sees.

## Extension points

**A caller needs a new `op` form.** Add it to `vault_op.py` with reference outputs recorded
from the real CLI (`test-local/op-reference/record.sh` writes `tests/fixtures/op_reference`).
If it takes a template on standard input, `wants_stdin` changes in both `vault_op.py` and
`vault_op_client.py`, which cannot import Casa's modules; a test holds the two equal.

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
- `casa/rootfs/usr/local/bin/op`
- `casa/rootfs/opt/casa/scripts/secrets-backend-lib.sh`
- `casa/rootfs/etc/s6-overlay/scripts/secrets-backend.sh`
- `casa/rootfs/etc/s6-overlay/s6-rc.d/svc-vault/run`
- `casa/rootfs/etc/s6-overlay/s6-rc.d/svc-vault/finish`

**Tests**
- `tests/test_vault_store.py`
- `tests/test_vault_op.py`
- `tests/test_vault_service.py`
- `tests/test_vault_real_callers.py`
- `tests/test_vault_finance_plugin.py`
- `tests/test_secrets_backend_scripts.py`
- `tests/test_set_plugin_env_reference_guard.py`

**Related**
- [`architecture/configuration.md`](../architecture/configuration.md)
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/vault-drop-off.md`](../architecture/vault-drop-off.md)
- [`architecture/plugin-secret-exploration.md`](../architecture/plugin-secret-exploration.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
