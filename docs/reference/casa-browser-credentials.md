---
last_reviewed: 2026-10-22
---

# Casa Browser — credential broker and login

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The broker process, its registry and vault facade, the login flow over the controller's
private driver RPC, and credential-operation metadata and notice delivery.

## Process boundary

The broker owns the vault client and guest credential configuration. Page actions run
in the controller process through session handles and the driver socket; the broker never opens a browser itself. Broker
replies contain fixed outcomes, ids and role names. Guest credentials appear only as
sign-in or unlock arguments to the driver; item credentials appear only as fill arguments.

## Registry, vault and login

The broker's operations live in `broker/main.py`. Its registry maps
site ids to item ids and HTTPS login origins in an atomically replaced `0600` JSON file.
Registration accepts only Login or Password items from the configured vault. It returns
available role names and extra field names, without field contents, and warns that the
extension can see the whole browsing vault. Forgetting a site's login changes only this
registry and makes no SDK call: the site stays registered with its login origins and loses only
its item link (and its `unverified` and `rejected` marks), so its next login asks the 1Password
question again; the controller clears the site's item once the broker confirmed. Run membership is checked before any SDK call on login or unlock;
unknown arguments are refused before dispatch.

**Finding the item.** When a site's login finds no linked item
(`not_found`), the controller asks the broker's `list_login_items` (a run's account site only):
id, whole title and website hosts per item, as many as one broker line holds (`truncated`); an
item whose title passes 300 characters is not offered. The item ids stay in the controller;
which items fit is the action check's `login_item` judgement, and the operator's Yes (or the
title he chose) links the item through `register_item` — the item is read, never written. A
site whose question ended without a link is not asked again in the same run.

**Codes the site sends.** With no authenticator in the item, a step that
asks only for a code stops as `needs_code` (the driver's `mark_document` marks that document),
and the broker's `fill_code` later fills the code into that very document (`document_marked`); see [`casa-browser-actions.md`](casa-browser-actions.md) (Sign-in codes).

`broker/items.py` is the only broker module that imports the SDK. It resolves exactly one
vault by name and scopes item reads and writes to that vault. Field updates read the
complete item, change only the selected username/password fields, and write it with its
version, preserving unrelated fields, sections, websites and attachments. `Vault.list_logins`
lists the vault's active Login and Password items (`items.list` with the SDK's by-state
filter) as overviews — id, title, websites — which hold no field value. SDK failures
are mapped from their native typed error identity, or the stable error-type token retained
in the SDK message, to fixed broker codes; human prose and SDK text never become a reply.
A mismatched version is read once more and remains a conflict if it still differs. OTP reads obtain a
fresh code from field details. The facade exposes no removal or password-generation route.

`broker/guest.py` counts the browsing vault's user accessors that can edit and those shaped
as read-only (history permission bit 1024 does not count as write access): a vault with at
least one read-only user is `readonly`, one with none `not_readonly`, unreadable accessors
`unknown`. `status` reports it with the counts; the Setup page warns on `not_readonly`
(design §5.2). Nothing waits on it: login and unlock never refuse for the guest's role.
Login drives the controller only through the driver RPC socket. It settles and inspects
fields for at most six steps and 120 seconds, fills known roles only on a login origin,
reads each OTP immediately before use, and stops on repeated fields with an error.
A page that changed under a step (`page_changed`: only the view generation moved, or the page
re-rendered one of the step's elements away) is settled
and read again, at most eight times per login and without taking a step; past that the reply
is `page_unsettled` (the live view). A read with no fields after the step's own fields were
found is `page_unsettled` too, never a completion. A sign-in that has filled nothing reads a
field-free page up to six more times for a form that renders late; still none on a site
origin is `no_form`, which claims nothing (the item's `unverified` mark stays): `logged_in`
follows only a submit of the broker's own (slice 5u).
New-password and confirmation fields return `password_change_page` before any fill or
submit on that page. Missing or ambiguous fields require a live view.
A form's submit button counts while it is disabled when no other button of the form can be
pressed: a widget or the form's own checks hold it (disabled means the browser's `:disabled`, not
`aria-disabled`, which a measured site leaves set on a working button). The step fills first,
then looks at the button (`login.py::DriverServer.submit_usable`) every quarter second for at
most `SUBMIT_WAIT_S` (20 s), then presses once; never on is `needs_live_view`/`submit_disabled`,
nothing sent and the filled fields left for the operator. A password never goes out beside a
shown, editable, empty box of its form that the step does not fill (anything a person could type
into, whatever its type): at the read (`empty_box`) that is `no_form`/`needs_check`
from the start page with nothing done yet, which hands the page to the look, and
`needs_live_view`/`empty_field` otherwise; the click's own evaluation
(`login.py::_SUBMIT`) checks the form again, so a box that rendered late or a field a re-render
wiped stops it with the same reply. The password goes out once per sign-in on either route: a
form keeps it however the page shows it, so after it went out only one code step may still be
pressed, and any other form, with no error mark to report as `rejected`, or a read that no longer
shows a password this sign-in filled, is `needs_live_view`/`password_again` (`password_sent` also carries across the start page's guest
restart). The button's form must be the form of every field the step fills. 1Password's passkey
prompt (5v) offering the linked item — it has a username, and its title names no other vault
login, titles compared trimmed; the prompt shows no item id) — is tried once per run and site
(a code continuing that sign-in keeps it), its Sign in counting as the step's submit; a sign-in
that answered one ends field-free as `no_form` (reason `passkey`: claims nothing), the form
again is that form's reason, another or unmatched prompt `challenge`.
With `guest_ask` (never on `fill_code`), a read showing a passkey hint (`passkey_hint`: a
shown, enabled input on a login origin whose autocomplete ends with the token `webauthn`; or
`passkey_frame`) ends the sign-in as `needs_guest`, before any further fill or choice.
**Off the start page** (the look, [`casa-browser-signin.md`](casa-browser-signin.md)), the
broker's `read_sign_in` settles and runs the driver's `read_sign_in` once — nothing filled, no
vault read — and keeps that read's binding (its mark and its fields' facts) per run and site; `login` with
`auto` (and `fields`, the check's mapping of the page's unmarked fields, named to the driver's
discovery with the look's mark) signs in only on it: the driver's `sign_in_bound` before each fill, submit or passkey
answer of that first step (`no_form`/`page_moved` otherwise), the item read only once a value is needed,
no form-render wait, and after its first submit or passkey answer no guest, no passkey and no
second password (`password_again`; `password_sent` carries one sent before the guest reload,
and `_code_marks` carries both flags through an emailed code). A first read that errors is
`no_form`/`probe_failed`; one that finds no field to fill is `no_form`/`no_form`, or
`no_form`/`page_moved` when the check named fields. The look's binding ends with its run.
Guest bootstrap reads the driver's state and calls sign-in or unlock with credentials
in call arguments only, within 105 s; other states, `timeout` and driver errors are logged by
the run controller, which goes on (no live view, slice 5s).
Opening a capture has nested bounds, so the innermost answers first and a failed open has let go
of the session when it is closed: the driver's 20 s (`login.py::_CAPTURE_OPEN_S`), the broker's 30 s
transport, the controller's 35 s (`broker_client.py`). The reader skips a frame whose address is off
the capture origins, or an extension's, without asking it; a frame that does not answer in 3 s
marks the capture incomplete and the view still opens. A close under a capture op past its bound
cancels the op rather than closing Chrome (`login.py::DriverServer._capture_let_go`).
The driver implements page-bound
login and guest sessions and context-bound capture sessions. Login sessions own their first
and replacement proxy leases. One per-session lock covers every page and lease operation
through its final effect, including capture installation on navigation and new tabs.
Operations revalidate session ownership, kind, bound page, generation and any required
lease after acquiring it. A look's read (`read_sign_in`) marks, in Patchright's isolated
world, every credential-bearing frame document (the main one, each frame holding a field or the
submit, and 1Password's notification frame while it shows a prompt) and remembers those frames
per run and site (`sign_in_frames`); `sign_in_bound` reads them again. A generation that only moved, every other check passing, is
`page_changed` rather than `refused`. A login session watches its page's frame navigations and
detaches: once a frame holding a live handle leaves the address origin it had when the handle
was issued, or the page, every operation of the session refuses, even after the frame returns.
A step's handles live from the read that issues them until submit is about to click; a submit
stopped after arming the lease close counts as submitted, so the next form found takes a
fresh lease. Nested helpers in the same task retain the lock; other tasks
queue. Dispatch restricts settling, field discovery, fill, submit
and passkey selection to login sessions; guest operations to guest sessions; and capture
operations to capture sessions. Fill and submit also require the session's current lease.
These checks run before origin metadata changes or page actions.
Guest operations (measured on extension 8.12.42.36; 45 s bound, under the broker's 50 s wait)
use only the session's own tab. State comes from the extension's popup page there: lock screen
`locked`, vault list `unlocked`, neither within 6 s `signed_out`; a popup that will not load
(not installed yet) is retried for 15 s, then `unknown`. Sign-in opens a fresh tab at
`https://my.1password.<region>/signin?auth-only=1`: the email, Enter; the Secret Key and
password, Enter; done when the tab lands on the extension's page (a missing step: `unknown`).
Unlock sets the password into the popup's lock screen, Enter. Each value's setter refuses
unless the document is exactly that origin and path, in the same evaluation. A guest session notes
Chrome's target list at its start (unreadable: it opens no tab) and at its end closes every tab
opened since, then brings the site's page to the front.
Passkey selection clicks only the Sign in of 1Password's notification frame that is a child of
the login page's main frame, on a login origin, with exactly one prompt of the measured shape
offering exactly the expected title and username; field discovery reports such a prompt only,
never reads 1Password's frames for an origin, and takes a frame that goes away under the read
as no prompt; a selection the frame went away under (its reads or the click) is
`page_changed` (read again, try spent). Cleanup waits for the current lease's end acknowledgement,
falls back to run revocation on failure, and closes only an owned window token. The
`login_active` guard blocks observations while login or guest work owns the page.

Operations have a thirty-second outer timeout, with shorter existing read and proxy
bounds. Cleanup waits up to ten seconds for the session lock, then marks the session
closed before ending its lease and window. If the drain or cleanup expires, it cancels
pending work and asks `BrowserManager.close(session=..., force=True)` to kill Chrome
through the launcher. Only after the launcher confirms that the main process and all
executing members of its process group have exited does cleanup revoke the run, retire
the session and clear `login_active`. The browser close has a ten-second hard bound;
a failed or unconfirmed close retains the session, lease, credential window and observation
guard, refuses further operations and permits a later cleanup retry. A retry after the
session's browser is already gone from the manager (the run controller closed it after an
unconfirmed close) takes the launcher's exit proof — no Chrome process of the browser user —
as the proof, and then retires the session; while another browser runs it still refuses
(`unavailable`), keeping everything. `DriverServer.open_sessions` lists the sessions not yet
retired, so the run controller can retire every one after the proof. Forced cleanup
performs no concurrent page cleanup and leaves the credential window open. A late browser
result is refused before another effect can follow it. Authenticated in-memory capture
delivery stays available during the final read so its acknowledgement cannot deadlock
collection.

Driver handles remain private to the bound page and browser generation. Field discovery
uses visible, enabled inputs' types and autocomplete roles, with one submit control per
form. Discovery accepts a submit control only when its form owner is the login form,
and submit retains and rechecks that owner and the control's submit type immediately
before clicking. Fill and submit require the element to remain connected to the bound
frame's document at the moment of action; when a check or an action fails on an element no
longer connected (a re-render without a navigation), the reply is `page_changed`, not `refused`.
Fill uses the native input setter in Patchright's isolated world and verifies the
same element's value; it sends no keyboard events carrying a credential. Every action
rechecks its frame origin and generation. Broker-origin metadata narrows the controller's
login origins for production login calls. Field discovery, settling, account-origin checks
and guest-state reads retry any read that fails under them (a destroyed execution context, a
detached frame) after waiting for document load.
Settling then waits for a quarter second without a frame navigation (at most three seconds).
Each read allows three attempts within a time bound (the caller's bound for settling,
ten seconds otherwise); exhausted retries or a load wait that does not settle return
`timeout`. Session ownership and generation are checked again across retries. Fill and
submit actions are not retried. A trailing `webauthn` autocomplete token is dropped before a
field's role is named (`username webauthn` is the username).
Guest sign-in owns its new tab and opens its credential window before navigating.
The proxy admits the configured account host and its exact regional 1Password service
hosts for sandbox browser traffic over HTTPS or WSS, independently of the run's site lock.
It does not attribute requests to the extension or require a guest capability header.
The credential window keeps typed guest values out of request events.
The driver accepts the proxy's account origins only when status names the session's exact
run and site; a policy switch to another run or site causes a fixed refusal.

The live view's capture — the driver's reader and the broker's capture rows — is in
[`reference/casa-browser-signin.md`](../reference/casa-browser-signin.md).

Provider learning accepts one tagged candidate followed by its callback only after a
signed-in outcome. It returns three validated tags and a registered provider origin,
without examining request identity bytes.

The broker service runs as `casa-broker`, after the network floor and proxy, and waits
for the proxy's public CA. Its environment is exactly PATH, LANG, TZ, HOME, PYTHONPATH,
HTTPS_PROXY, https_proxy and SSL_CERT_FILE; both proxy variables name the broker listener,
and the CA is its TLS trust file. It reads its token from the private runtime config,
authenticates lazily through the vault facade, and recovers the journal before listening.
The broker has the controller supplementary group so it can assign that group to its
socket; controller-private directories retain their owner-only access.

Both credential sockets check SO_PEERCRED on every accept: broker callers must be uid
1103, driver callers uid 1102. Their NDJSON transport bounds requests and replies to
64 KiB including the newline, times out stalled calls, and returns fixed codes without
exception text. Calls for one site serialize under a lock; other sites can proceed.
The broker runtime directory becomes `0711` for traversal, its config stays `0600`, and
the broker socket is `0660` with controller group ownership.

The controller's broker client imports notices at its explicit startup hook and through
its refresh hook before delivery. It substitutes
the controller's site display name and uses `broker:<notice_key>` as the store deduplication
key. Notice replies paginate within the wire bound, and replay starts from zero so a
restored controller can recover its delivery copy.

Root bootstrap creates `/data/broker` for `casa-broker` and `/data/proxy` for
`casa-proxy`, both `0700`, and reapplies ownership and modes to existing directories.

The synthetic image login harness replaces both the browser listener and the production
proxy control socket, so Chrome and the controller reach the same enforcing addon.
Its SDK guard resolves the evidence path before yielding and checks the uncensored
call history and broker evidence file during teardown, while the temporary directory
is still available. Guest sign-in failures surface their fixed driver error code before
the harness returns navigation-order evidence.

`broker/journal.py` stores metadata in SQLite with `synchronous=FULL`, in a `0600` file.
Connection setup uses umask `0077`; SQLite transaction, WAL and shared-memory files are
also `0600`. Registry replacements are private `0600` files; startup reapplies that mode
to an existing registry.
Save rows move from `intent` to `attempt` and then to `vault_written`, `vault_failed` or
`vault_unknown`. `Journal.write_failed` records permission and version-conflict refusals
as `vault_failed`, and other failures as `vault_unknown`. A confirmed write closes
its save row in one transaction; the capture becomes `capture_saved` in it only when all its
roles are resolved, and earlier failed or unknown save rows for the capture close with it.
A save issues no notice: every save is the automatic one, which the agent's reply tells.
Startup recovery turns unresolved saves and
`capture_pending` rows into durable `credential_unknown` notices, without reading an item
version or claiming a save. A warning names what was left unsaved — "a username you typed on
{display_name} was not saved to the vault" when only a username of a complete capture was,
else the password warning (also at recovery, where nothing is known). A declined capture
(`drop_attempt`, or the end of the automatic save) is silent unless a write was attempted and
did not complete. Notice keys are stable across repeated recovery,
and `pending_notices(after)` returns only metadata and fixed text with a display-name
placeholder, suitable for deduplicated controller delivery.


Signing in from a card for a site that is not set up, and saving a login typed in the live view (including an email-only sign-in), are in
[`reference/casa-browser-signin.md`](../reference/casa-browser-signin.md).

## Invariants

**INV-BRW-019**: No code path in the broker removes a vault item: the broker package uses only the vault facade's read, create and update calls of the 1Password SDK, has no removal, archive or password-generation call and no dynamic attribute access, and forgetting a site changes only the broker's registry.

**INV-BRW-050**: Forgetting a site's login removes only its item link: the site stays registered with its login origins, no vault call is made, and its next login asks the operator again.

**INV-BRW-049**: The broker never changes a credential on its own: a page offering a new-password field during a login ends that login for the live view before anything is filled, and a captured value reaches the vault only through a live-view sign-in the check judged signed in (INV-BRW-048).

**INV-BRW-060**: An automatic sign-in sends the password at most once per sign-in, and never while a shown, editable, empty box of its form stays unfilled: the read and the click's own evaluation both check it, and a form that asks again goes to the live view.

**INV-BRW-022**: A credential is set only on the validated element of a frame whose origin is one of the active site's registered login origins, in the page and generation the driver session is bound to; it never travels as keyboard input.

**INV-BRW-056**: An automatic sign-in whose page changed under a step reads the page again from settling before any further value is set, and only while every frame holding one of the step's live handles has kept, throughout, the address origin it had when the handle was issued and stayed attached (otherwise it refuses); it reports `logged_in` only after a submit of its own — a read with no fields after the step's fields were found is `page_unsettled`, and a sign-in that filled nothing and found no form is `no_form`.

**INV-BRW-057**: The 1Password guest's email, Secret Key and password are set only into the main frame of the guest session's own tab, and only while that document is exactly `https://my.1password.<region>/signin` (sign-in) or the extension's popup page (unlock), checked in the same evaluation that sets the value.

**INV-BRW-058**: An automatic sign-in answers a passkey prompt only through 1Password's notification frame of the login page itself, on a login origin, when the prompt offers exactly the site's linked item (its username, and its title, which names no other login in the vault once trimmed), at most once per run and site, and a sign-in that answered it — continued with a code or not — claims nothing (`no_form`).

**INV-BRW-023**: While a credential window is open, a proxy event carries only fixed-vocabulary members — its origin only when the run registered it (or it is a fixed extension, broker or infrastructure host), the policy's enums, a normalised method, and keyed binding tags or a callback reference — never request identity, path, query, header or body bytes.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/broker/__init__.py`
- `casa_browser/rootfs/opt/casa_browser/broker/main.py`
- `casa_browser/rootfs/opt/casa_browser/broker/items.py`
- `casa_browser/rootfs/opt/casa_browser/broker/guest.py`
- `casa_browser/rootfs/opt/casa_browser/broker/journal.py`
- `casa_browser/rootfs/opt/casa_browser/broker_client.py`
- `casa_browser/rootfs/opt/casa_browser/credential_rpc.py`
- `casa_browser/rootfs/opt/casa_browser/login.py`
- `casa_browser/rootfs/etc/s6-overlay/s6-rc.d/svc-browser-broker/run`

**Tests**
- `casa_browser/tests/test_broker.py`
- `casa_browser/tests/test_broker_rpc.py`
- `casa_browser/tests/test_login.py`
- `casa_browser/tests/integration/test_login.py`
- `casa_browser/tests/test_credential_save.py`

**Related**
- [`reference/casa-browser.md`](../reference/casa-browser.md)
- [`reference/casa-browser-runtime.md`](../reference/casa-browser-runtime.md)
- [`reference/casa-browser-browser.md`](../reference/casa-browser-browser.md)
<!-- END SOURCEMAP -->
