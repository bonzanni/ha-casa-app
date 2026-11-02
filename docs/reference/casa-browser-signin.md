---
last_reviewed: 2026-11-01
---

# Casa Browser — signing in: the sign-in card and the live-view save

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a site that is not set up gets signed in from one operator tap (the sign-in card), and how a
login the operator typed in a live view is saved. The broker's registry, vault facade and the
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

## Saving a login typed in the live view

The capture reader also records the
last sign-in **attempt**: a submit, an Enter or a button click on a form (or, with no form, a
document) holding a filled password field, with that form's username — the last one recorded
when the form has none (a two-page sign-in) — and its password (the `new-password` field's in a
password change). A form with no password-role field at all and a filled field the page marks
as an email or a username (type `email`, autocomplete `email` or `username`) makes an
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

**INV-BRW-048**: A live-view credential is written only from the capture's last attempt that the check judged `signed_in`, that did not fail, with no password typing after it, ending on one of the site's origins; it overwrites a linked item only when that item is `unverified` or its own password was rejected in this run, and never writes equal values.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/api.py::sign_in`

**Tests**
- `casa_browser/tests/test_sign_in_card.py`
- `casa_browser/tests/test_credential_save.py`
- `casa_browser/tests/integration/test_login.py`

**Related**
- [`reference/casa-browser-credentials.md`](../reference/casa-browser-credentials.md)
- [`reference/casa-browser-actions.md`](../reference/casa-browser-actions.md)
- [`reference/casa-browser-runs.md`](../reference/casa-browser-runs.md)
<!-- END SOURCEMAP -->
