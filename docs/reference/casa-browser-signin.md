---
last_reviewed: 2026-11-03
---

# Casa Browser — signing in: the sign-in card and the live-view save

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a site that is not set up gets signed in from one operator tap (the sign-in card), how a
registered site is signed in on a page that is not its start page (the look), and how a
login the operator typed in a live view is captured and saved. The broker's registry, vault facade and the
automatic login flow are in [`reference/casa-browser-credentials.md`](../reference/casa-browser-credentials.md);
the vault question (`login_item`) and the code route are in
[`reference/casa-browser-actions.md`](../reference/casa-browser-actions.md).

## The sign-in card

The model judges that the public page a run is on needs a sign-in and calls
`POST /v1/runs/{run}/sign-in` with the site's everyday name, a label only
(`api.py::sign_in` → `controller.py::RunController.ask_sign_in`). The app reads only mechanical
facts: 1Password is configured (else `not_configured`); the run is in its public session (else
`account_site`); the page's address is https (else `not_https`) and gives the origin in the one
spelling registration stores — lowercase host, default port dropped (`controller.py::_sign_in_address`);
the app's own screenshot of that page, taken between two reads of the same view generation and
origin (else `page_changed`). A registered site that covers the origin and reaches anything else
is named (`registered_site`) and never carded; a registered site whose every address is exactly
that origin is the card's site. With vault items, the broker's `list_vault_logins` (metadata only,
no site yet) feeds the existing `login_item` judgement with the card's host as `sign_in_host`
(`judging`); the plugin relays it. The card is a question (`confirmations.py::site_view`, view
`kind: site_signin`): "Sign in to {host} ({name})?", naming the item only when exactly one fits,
plus "Yes, and next time I'll sign in here without asking. You can undo that in Casa Browser's Sites page.", Yes and No only, no live view.

Its Yes (`controller.py::RunController._answer_site`) registers exactly the card's origin as both
the site's origins and login origins, with the sign-in page's path (no query or fragment) as its
start page and the named item linked, or uses the exact registered site; a site registered for
that origin meanwhile is never taken over (`already_saved`). The run gains the site as an account
grant (replacing any grant for it), its spec column is updated while `spec_digest` stays the
admitted request's (`store.py::Store.set_run_spec`, so a crash still owes the broker release), the
broker's run set is replaced, and the start page opens on the site's own profile, which signs in
by the existing flow: the linked item, the vault question when several items fitted, or the live
view when none did (no second check); a start page that does not open is answered `not_opened`
(the site stays saved). No, an expiry or a tap after the run ended registers
nothing. A site whose account then lives on another host is refused there (`site_lock`); the
model says so plainly.

## Signing in off the start page (the look)

The start page's sign-in (`controller.py::RunController._login`) runs only when the run navigates
the site's start page. A step that lands on another page of the site showing a sign-in form —
a judged click on a "Log in" link, a redirect — is signed into by the look. So is a start page
whose password field has a shown, empty box beside it in its form that the page marks with no
role, such as a plain text username: the start page's sign-in does nothing there and answers
`no_form`/`needs_check` (not counted in the ledger, its address taken out of `looked`), and the
next observe looks. A start page that showed no form while its sign-in waited is the same
(`no_form`/`no_form`, about two seconds): one `sign_in_nothing` event says so, and the next observe
looks at the form if one rendered since. The plugin asks for the look:
`POST /v1/runs/{run}/observe` with `{"sign_in": true}` (`api.py::_observe_body`; `{}` stays
the plain observation, any other body is `unsupported`).

**What is looked at** (`controller.py::RunController._look`), mechanical facts only, no I/O: an
account run in its account session, a site with login origins and a linked item, no code wait,
the run's sign-in ledger allows one (below), an http(s) page, and a (browser session, address)
pair not yet in `looked`. Every executed step clears `looked` (`_action_done`, `_executed`);
every sign-in adds the page it ended on; the look adds its own pair first. So an address is
looked at once between two of the plugin's steps, a redirect away and back included.

**The read** (`broker/main.py::Broker.read_sign_in`): in a `login` driver session, one settle and
the driver's `read_sign_in` (`login.py::DriverServer.read_sign_in`), which fills nothing and reads
no vault value. Its plan script (`login.py::_PLAN`) lists, in every frame of a login origin,
each shown input with a sign-in role, each password field and each plain text, email, telephone
or search field, with a short id (`f1`, …), the role the page marks (or none), its label (its
`aria-label`, `<label>` or `placeholder`), whether it is a password field and the label of its
form's one submit button (one that can be pressed, else one a widget or the form's own checks
hold disabled), and marks each such field and button with the look in Patchright's
isolated world. It goes on only for a marked role or a password field on a login origin, the
site's passkey request (`passkey_hint`) or 1Password's prompt. The same read binds what the
check will judge: the look's mark in every credential-bearing frame document (the main document
and each frame holding a planned field or the submit; 1Password's notification frame while it
shows a prompt) and the marked fields' and buttons' facts by id (type, autocomplete, label, the
form's resolved `action`/`method`, a button's own `formaction`/`formmethod`). The broker keeps the
mark and the facts for the run and site; the controller reads the bound frames
(`login.py::DriverServer.sign_in_frames`) to see they read whole. The page's text is not bound.

**The check.** A page with sign-in fields becomes a `sign_in` judgement (`gate.py::sign_in_packet`:
the site, the page as an action packet reads it, and `planned`: the fields by id, marked role,
masked label, `password_field` and masked button label, and whether a passkey would be
answered). The observe reply is `{outcome: judgement_required, judgement}`; the plugin's code
relays it to Casa's check (`action_judgement.py`, kind `sign_in`, rulebook
`rulebooks/sign_in_v1.txt`): `sign_in` only when filling the login into the fields it names and
pressing their button, or answering this passkey, signs the operator in to this site; with it,
`fields` names the planned field that takes the username and the one that takes the password.
It is never listed by `pending_judgements`. A shape judged `not_sign_in` (site, the planned
fields' roles, labels and buttons, passkey) is remembered for the run and not asked about again.

**The verdict** (`controller.py::RunController._apply_sign_in`). The check's `fields` must fit
the reading it was shown (`controller.py::_sign_in_mapping`, mechanical facts only): every id
is a planned field's, a password goes only into a password field and a username never does
(so no field takes both), and a field the page marks keeps its role; otherwise nothing is
filled (`sign_in_mapping`, `refused`). `sign_in`
then applies only while the session and the address are the ones looked at and no step came
between (the pair is still in `looked`); the page's text may have moved, a banner or a price
beside the form changes nothing. The look's bound reading (`gate.py::PageOps.read_page_and_bound`,
one pass giving the packet's reading and the bound one, so an unbound frame such as a CAPTCHA
counts in neither reading) must be whole. A closed shadow root makes a reading incomplete only when it holds a
form field, in the shadow trees below it too (`observe.py::closed_shadow_holds_field`; a failure
to ask counts as one): a password manager's button and a CAPTCHA's frame hold none. A bound
reading over its caps or with such a root, or a page that moved, is not judged; a page with
sign-in fields (a password field, a field with a sign-in role, a passkey) then leaves one
`sign_in_nothing` event, outcome `not_checked`, and a page with only plain boxes none. A sign-in
whose judged fields or button moved (below) sends nothing (outcome `page_moved`) and brings one
fresh look: its `sign_in` judgement is the verdict reply's successor, and a verdict on that fresh
look fills nothing if they move again. The next observe after a step looks again, since an outcome
that acted on nothing is not counted (the ledger, below). Otherwise the
broker's `login` with `auto`, and the named fields as
`fields`, signs in on the page as it is (no navigation): in the judged document only the
look's planned fields count in the driver's discovery, a role the
check named goes to its named field alone (a newsletter box the page marks as an email field
beside the form takes no username then), a role it named nothing for stays with the field the
page marks with it, and a password role goes only to a password field. A look plans at
most 200 fields. `not_sign_in`, no verdict, a verdict outside its
bounds or a page that moved fill nothing. The reply is `{outcome: done, observation}`, the
observation read after the sign-in, its `notice` the sign-in step the look recorded
(`sign_in_sent`: "Casa Browser filled in the sign-in of {site} and sent it; read the page to see whether it worked.", or the `login_needed` text,
`needs_code` included), else null.

**After the send.** A form still showing right after the password went out is the site answering
(`broker/main.py::ANSWER_WAITS`: about ten seconds of reads, nothing filled or pressed); one that
stays is `password_again`.

**The sign-in** (`broker/main.py::Broker.login` with `auto`). Before each fill, submit or passkey
answer of the judged step, the driver's `sign_in_bound` must report every bound document still
marked, every field and the submit of the step one the look marked (so of the judged reading),
no 1Password prompt the look did not bind, and those fields' and that submit's facts as the look
read them (type, autocomplete, label, the form's destination, a button's own); otherwise the
sign-in ends `no_form`/`page_moved` with nothing more done. The judged step is the ordinary step:
`needs_guest` while the guest is not unlocked, 1Password's passkey prompt for the linked item,
username, password and TOTP fills, the refusals `password_change_page` and `ambiguous_fields`; a
username-only form fills the username; a shown, empty box of the password's form the mapping left
out sends nothing (`needs_live_view`/`empty_field`, the rule in
[`casa-browser-credentials.md`](casa-browser-credentials.md)). After its first submit or passkey answer: a page with
neither a password nor a code field to fill is the page after the sign-in (the completion rule);
a password asked again ends `needs_live_view`/`password_again` (carried across the guest reload
and an emailed code); no guest and no passkey answer. A first read that errors is `no_form`/`probe_failed`;
one that finds no field to fill is `no_form`/`no_form`, or `no_form`/`page_moved` when the check
named fields (a form drawn anew has new ones).

**The guest reload.** On `needs_guest` the controller runs the guest flow, loads the looked
address again and looks afresh: the new `sign_in` judgement is the verdict reply's successor
(the plugin relays successors), with no second unlock and the password already sent carried.
Nothing reloaded is filled on the old verdict.

**The ledger** (`controller.py::RunController._ledger`). Every sign-in of the run that acted counts
per site, start page or look; an outcome that acted on nothing does not (`no_form` with
`no_form`, `needs_check`, `probe_failed` or `page_moved`, and any `not_found`). A look may sign in when the count
is 0, or 1 and that one worked. The start page's sign-in does not consult it.

## The capture reader

The driver's capture code runs in isolated worlds on the account and login origins,
reinstalls on frame navigation and new pages, and reads on input, change, submit, pagehide,
a 500 ms timer and final capture collection. A new document that does not take the reader
is tried again a few times (`login.INSTALL_ATTEMPTS`); a document of a capture origin that
never takes it marks the capture incomplete, so nothing of it is saved automatically (an
incomplete attempt is never judged). Each capture supplies a separate random nonce
only through isolated-world evaluation arguments; the receiver drops binding calls without
that nonce. Each payload includes the isolated world's current origin, checked against
capture origins without evaluating the source frame, so delivery survives document teardown.
In a document that existed when the binding was exposed the isolated code calls the raw
CDP binding with its JSON dispatch payload and supplies the callback map used for delivery
acknowledgements; in a document created later it calls the driver library's wrapper with
the arguments themselves (the two take different shapes).
Final collection waits for those acknowledgements before wiping the buffer. A page that
finds the binding name cannot supply capture candidates without the nonce.
A field is a username when its autocomplete says
username or email (a trailing `webauthn` token is dropped first, so `username webauthn` is a
username, as for the sign-in's own reader), or it is an email field, or it is a plain text field with no autocomplete
of its own beside a password field (its form, or the page when there is none). A field that was a password field when the reader
first saw it — present at a read, or changed from `password` while the reader watched the page
— stays one for the whole capture, whatever type the page gives it later, and a field once
reported as a password field reports only password values. A value seen as a password anywhere
in the capture is never a username candidate, whatever field carried it — a "show password"
toggle, a re-read, a text clone, a value since replaced or dropped for room (operator ruling
E-T14-3 = Q1): the buffer remembers every password value seen for the capture's life, at most
`login.MAX_SEEN_PASSWORDS`; past that it offers no username at all and says the capture is
incomplete (with exactly that many remembered, a re-read already counts as past it). What
remains: a value typed only into a field the reader never saw as a password field — one text
from the start, or a copy built off the page and inserted as text — cannot be told from
text. New and
confirmation passwords canonicalise to password candidates. Each read names its field (an id
per field of the document), and a field's latest value replaces its earlier ones — typing
reports every prefix, but one typed value is one candidate (`login.CaptureBuffer`). The
buffer keeps eight fields' values, each at most 1 KiB; overflow marks it incomplete and
drops the oldest. Taking the capture
wipes the driver copy and closes its session. `arm_capture_then` waits for the broker's
acknowledgement before exposing live input.

The broker accepts capture only for login and assist on a registered account site of the
run. Before asking the driver to arm it durably records a pending capture containing only
metadata. Taking the capture (`_review_capture`, internal: at `take_attempt`, `end_capture`
or `end_run`; no broker op returns the values) reuses that row, takes the driver's values and
last sign-in attempt once into broker memory, and leaves out values equal to the linked
item's. Values stay in broker memory for fifteen minutes from the take; a live capture has no
expiry before it. A confirmed arming refusal closes the row silently; an uncertain arming
outcome or restart with an open row leaves a warning. Empty comparisons close silently unless
capture was incomplete. The one save is the automatic one after a `signed_in` verdict
(`save_attempt`, its consent `auto:<capture id>`; [`casa-browser-signin.md`](casa-browser-signin.md)):
it updates the registered item or creates a Login — titled with the site's registered name,
which `register_capture` carries as `title` — and registers its id. Confirmation requires the
write's SDK reply or, for an uncertain update, a re-read containing the chosen values; version
changes alone never confirm a save. `end_capture` is the live view's end (the run controller
calls it at Done): a capture never taken is taken first, then dropped, so the site's next live
view can capture again; ending a capture already ended changes nothing. An unresolved end of a
changed or incomplete capture leaves a warning.

Capture cannot retain an eventless set/send/clear between polls. A generated value never seen
by capture must be set again to one the operator can see. A controller crash loses unsaved in-memory values.
Python cannot guarantee erasure of memory. A site's script can retain a credential and
send it after the credential window closes; normal private event recording then resumes.

## Saving a login typed in the live view

The capture reader also records the
last sign-in **attempt**: a submit, an Enter or a button click on a form (or, with no form, a
document) holding a filled password field, with that form's username — the last one recorded
when the form has none (a two-page sign-in) — and its password (the `new-password` field's in a
password change). A form with no password-role field at all and a filled field the page marks
as an email or a username (type `email`, autocomplete `email` or `username`, a trailing
`webauthn` aside) makes an
**identifier-only** attempt (an emailed code or a link follows), with no password, when the
form's shown filled fields of that kind hold exactly one value and that value is an email
address (`protocol.py::is_email_identifier`, checked again by the broker): a hidden field never
counts, two different values are no attempt, and a one-time code is never an email address
whatever field the page put it in. A username-only sign-in with no password is therefore not
saved. A field whose autocomplete names `one-time-code` is never a username, whatever its
type; only a form's own controls count (an input owned by another form through `form=` is not
one); and a value seen as a password anywhere in the capture is never an identifier. A repeat in the same document is one attempt; a later one replaces it. It is
**failed** when a document loaded afterwards on a login origin shows a password field, or its
own document still shows one when the capture is taken; password typing after it is flagged.
The driver reads the pages after the attempt and at the take with the controller's gate reader
(role and name of each control, no value of any field; the reader's `valueOf` hides secret
fields everywhere else) and keeps them for the controller; the broker never sees them. A secret
the operator puts somewhere other than a recognised secret field — a visible field, a shown
password, a custom or shadow-DOM editor, Done pressed before the form was sent — can appear in a
reading as page text: accepted by operator ruling, which rests on never storing a reading: the
readings and the `signin` packet live in controller memory until the verdict, the run's event
keeps only the verdict and its latency, and Casa logs only the kind, the verdict, the latency and
a digest prefix (its check writes no session transcript). At Done the controller's
`take_attempt` gets the attempt's flags (never a value) and the broker holds that capture for
the verdict. An attempt that failed, was typed after, or whose Done page is off the site's origins is dropped unjudged; otherwise the check's `signin` judgement
decides: `signed_in` calls `save_attempt`, anything else `drop_attempt`, and the run's end drops
an unjudged capture. `save_attempt` re-checks the attempt, then writes with consent id
`auto:<capture id>`, writing the password only when the attempt sent one (an identifier-only
attempt writes just the username, and a new item for it has no password field): a new Login item titled with the site's name, linked and marked
`unverified` until the broker's next automatic sign-in with it works; a linked item only when
it is `unverified` or the registry's `rejected` record names this run and item (set only when a
rejected step sent the item's password and no code); equal values write nothing; any other
linked item is kept (`item_kept`, said once by the agent); a linked item that cannot be read
writes nothing and leaves the warning a failed save leaves (`credential_unknown`), never a silent
decline.

## Invariants

**INV-BRW-053**: A site is registered from a sign-in card only by the operator's Yes on that card, for exactly the origin the card showed (read from the browser, never from the model), and a card never signs in to a registered site that reaches more than that origin.

What INV-BRW-053 and INV-BRW-048 do not cover (ruled residuals): an identifier-only save takes the value the site's own sign-in page shows as its one email field, so that page, on its own login origin, can plant a decoy email (a field hidden by CSS, say) that is saved instead of the one the operator typed; it is saved and filled only for that site, no secret leaks, and a code is never saved, because only an email address is an identifier on its own. A username-only sign-in with no password is not saved. A redirect after the sign-in to a host the card did not show is refused (`site_lock`).

**INV-BRW-059**: Off the start page, Casa Browser fills or presses nothing of a sign-in without a `sign_in` verdict on that page, only fields of the judged reading (a field the page does not mark only as the verdict mapped it, a password only into a password field), and only while the documents, fields, buttons, labels and destinations it judged are unchanged and no step came between; a look sends the password once, answers only its judged page's passkey, and signs in again only after a sign-in of the run that worked.

What INV-BRW-059 does not cover (ruled residuals): the check can misjudge a page; a site's own script changing its form while Casa fills it (another control's value, the operation it submits) is out of scope, since that script can send any request to its own origin anyway; the later steps of a judged sign-in are not judged again, only the limits above apply; the text around the judged fields and button is not held after the check, so a same-document change that keeps their nodes, labels and destination but rewrites the page around them, changes another control or the words of 1Password's prompt (which still has to offer the linked item) goes on.

**INV-BRW-048**: A live-view credential is written only from the capture's last attempt that the check judged `signed_in`, that did not fail, with no password typing after it, ending on one of the site's origins; it overwrites a linked item only when that item is `unverified` or its own password was rejected in this run, and never writes equal values.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/api.py::sign_in`
- `casa_browser/rootfs/opt/casa_browser/api.py::_observe_body`

**Tests**
- `casa_browser/tests/test_sign_in_card.py`
- `casa_browser/tests/test_credential_save.py`
- `casa_browser/tests/test_controller_look.py`
- `casa_browser/tests/test_broker_look.py`
- `casa_browser/tests/test_sign_in_binding.py`
- `casa_browser/tests/integration/test_login.py`
- `casa_browser/tests/integration/test_run_lifecycle.py`

**Related**
- [`reference/casa-browser-credentials.md`](../reference/casa-browser-credentials.md)
- [`reference/casa-browser-actions.md`](../reference/casa-browser-actions.md)
- [`reference/casa-browser-runs.md`](../reference/casa-browser-runs.md)
<!-- END SOURCEMAP -->
