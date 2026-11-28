---
last_reviewed: 2026-11-28
---

# Built-in secrets store — the secrets page

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The page on Home Assistant's ingress where a person sees, adds, changes and deletes the
secrets Casa keeps in its built-in store: the page's engine and Casa's profile, the listener
that serves it, its nginx locations and Secrets link, its routes, forms and checks, and what
every answer and the log carry. The store, its `op` front, the service that also serves this
page, and what that service does when the page cannot start are
[`builtin-secrets-store.md`](builtin-secrets-store.md).

## Mental model

**An engine and a profile.** `vault_page.py` is the engine: requests, forms, one-time codes,
the stamp and layout. `vault_page.Page` is a pure function of a request and the store that
imports nothing outside the standard library and `vault_store`, and holds no words or rules
of its own: it takes a `vault_page.Profile`. `vault_page_casa.py` is Casa's profile, the only
one in this repository — `PROFILE`, served as `casa_page(store)`: the page's words, which
values are hidden (`is_hidden`), who keeps a secret (`purpose`), an SSH key's facts and the
add form's labels. The script reads the sentences that name a secret from `data-` attributes
on `<body>`, and the form's shape from its controls.

**Who serves it, and who reaches it.** `vault_service.py` listens, besides the `op` socket,
on `/run/casa-vault-page/page.sock` (`--page-socket`, `--page-group www-data` in
`svc-vault/run`), through `vault_page_server.listening`, the page's listener: plain HTTP/1.0,
one request and one thread per connection, answered by Casa's page. The socket's directory is `0710` and
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

**INV-VAULT-005**: `/vault/` is routed to the page only on the ingress listener and only when the backend is `builtin`.

Enforced by `setup-nginx.sh`, which writes the proxy and static locations into the ingress
server only for the exact word `builtin` and a 404 location everywhere else, the external
listener included. What it does not cover: who may reach the ingress listener itself, which
is the listener's own source restriction ([`http-surface.md`](http-surface.md)).

**INV-VAULT-006**: No GET response contains a hidden field's value.

A field is hidden unless its type is exactly `STRING` (`vault_page_casa.is_hidden`). HEAD answers as GET does, without a
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

## Failure behavior

**A page request fails.** The peer gets a 500 page and the log one ERROR line naming the
failure's kind. A form over the page's limit is answered 413 without its body being read;
a peer that never finishes sending is dropped after 70 seconds. Stopping the service waits
for a page request being answered.

A page that cannot start, and the page's answer while the store cannot be opened, are
the service's ([`builtin-secrets-store.md`](builtin-secrets-store.md)).

## Extension points

**The page gains a route.** Add it to `vault_page.route_name` too, since the log names a
route by that word and never by its path; a route that changes anything is a POST through
`Page._check`, with a code issued for its own path.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/vault_page.py`
- `casa/rootfs/opt/casa/vault_page_casa.py`
- `casa/rootfs/opt/casa/vault_page_server.py`
- `casa/rootfs/opt/casa/vault_static/page.js`
- `casa/rootfs/opt/casa/vault_static/theme.js`
- `casa/rootfs/opt/casa/vault_static/ha.css`
- `casa/rootfs/opt/casa/vault_static/vault.css`
- `casa/rootfs/opt/casa/casa_core.py::_status_page`

**Tests**
- `tests/test_vault_page.py`
- `tests/test_vault_page_static.py`
- `tests/test_vault_page_behaviour.py`
- `tests/test_vault_page_profile.py`
- `tests/test_vault_page_server.py`
- `tests/test_setup_nginx_ingress.py`
- `tests/test_casa_status_page.py`

**Related**
- [`architecture/builtin-secrets-store.md`](../architecture/builtin-secrets-store.md)
- [`architecture/http-surface.md`](../architecture/http-surface.md)
<!-- END SOURCEMAP -->
