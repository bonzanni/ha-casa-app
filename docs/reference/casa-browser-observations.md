---
last_reviewed: 2026-11-07
---

# Casa Browser — observations and screenshots

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a model and the operator may see of a page in the optional `casa_browser/` app: masked text
and opaque handles behind the page-view gate, and screenshots under one capture rule. Chrome and
its sessions are [`casa-browser-browser.md`](casa-browser-browser.md); what the action check reads
of a page is [`casa-browser-actions.md`](casa-browser-actions.md).

## Mental model

An observation is the only view of a page a model gets. Fixed extraction code reads the page behind
the page-view gate, addresses become origins, recognised secrets become `[masked]`, and every target
is a random handle whose real element and address stay in the controller.

## Observations and handles

`casa_browser/rootfs/opt/casa_browser/observe.py::Observer` produces the only view of a page a
model gets, and the operator's screenshots. An observation's text stops at 20,000 characters and
says so (`text_cut`); `Observer.read_text` reads the same frames and masking whole for `POST …/read`,
whose `complete` is true only when every reading was complete and no limit cut it.
`Observer.read_rows` (`rows: true`) reads the same frames by the observation's own reading,
grouped by record (`observe.py::EXTRACT` in its rows mode): its text blocks, and every element an
observation lists (its `actionable`, with no cap) as a field with what it holds or as what can be
pressed, each given to the record (a table row, a list item or an article) holding its block. A
parent record keeps its own text around a record nested in it; a record that renders no box of its
own shares its block's record; what no record holds comes after a frame's records, in one row
marked `outside`. Every frame's blocks are masked together, joined as an observation joins its
text, and then shared out (`observe.py::rows_text`), so a value spanning blocks in different
records is masked whole and a row shows nothing an observation would not; a field's name and value
and a pressable name are masked on their own, as an observation's labels are. One JSON line each:
`text`, `fields` (a checkbox's state as `true`/`false`; a secret one `[not shown]` under the one
secret rule, named as an observation names it; a select's options as a third element, as an
observation lists them, none for a secret select) and `press`. Field values are what page text leaves
out, and the rows have no control cap; `complete` is false past 20,000 rows, at a reading limit or
when a frame is withheld.

**The page-view gate.** `observe.py::page_view_allowed` asks whether the page belongs to the
session: the run policy, its leases removed, decides the page's full current address —
fragment and client-side route included — as a top-level document. In an account session that
is an address on the site's origins or login origins (`site_document`); in the public session,
any address the floor admits (`public_session`). A lease, an infrastructure host (the
1Password web app, a captcha endpoint) and any non-web scheme (`chrome://`,
`chrome-extension://`, `about:`) never qualify. The gate runs on the address Patchright reports and again on the
address the extraction itself read; nothing is read while a live view or a broker login holds
the browser.

**Extraction.** Fixed code (`observe.py::EXTRACT`), evaluated once per frame in the isolated
world, reads the document and every open shadow root. Each child frame passes the one frame
rule (`observe.py::frame_shown`, which the action check's page reading uses too), parents
before children (`observe.py::child_frames`): every child frame needs a shown parent and a
document origin, as its own extraction reports it, that is not opaque; then a frame with a web
address is shown when the gate admits it, before the extraction and as the extraction itself
read it, and a frame without one (`about:blank`, `about:srcdoc`, a document the page wrote with
`document.open`, as a code playground's result frame is) has no address of its own and is shown
when its origin is its parent's — and an address its extraction reports is admitted too. Any other frame (in an account session, a
frame off the site, a frame sandboxed to an opaque origin, any frame inside a withheld one) is
left out and its place noted; a frame sandboxed with `allow-same-origin` keeps its own origin
and is judged like any other. Inside a shown frame the fields follow the same secret rule as the page's own. Afterwards the page generation
(`browser.py::BrowserSession`, advanced on every navigation of any frame) and the address are
checked again, and the result is discarded if the page moved. A browser-side failure becomes a
typed code, never the browser's error text. `Observer.observe_page` returns, beside the
observation (whose `source_id` is always empty here), the address and document title the
extraction read — controller-private, for the run controller's source record.

**What the model receives.** Text is the visible text of each block, one block per line:
visibility is the browser's own judgement (`checkVisibility`, so a hidden ancestor,
`visibility`, opacity and a closed `<details>` count; text under a `display:contents` element is
judged on the nearest element that renders a box), and a block is the nearest ancestor that is
not inline-level, so a value split across inline elements stays whole. Form fields and editable
content are never read — editability is the browser's own too (`isContentEditable` or a computed
`-webkit-user-modify` that writes), followed up through shadow hosts. The text passes
`casa_browser/rootfs/opt/casa_browser/masking.py::mask_text`: every address, wherever it starts
and up to the next whitespace — `scheme://…`, and for http, https, ws, wss, ftp and file every
form the URL standard accepts (any run of slashes or backslashes after the colon, or none) —
becomes its origin (only after two or more separators and never for `file:`; otherwise, or when
its host is not plainly a host name or address, `link`) and an opaque reference
(`https://site.example ⟨r1⟩`) — a credential inside an address goes with the whole address —
and, outside addresses, strings in known credential formats and long high-entropy tokens become
`[masked]`. Text is masked whole and cut to its limit afterwards, and every cut made before
masking drops the word it would split, and a block that reaches its limit takes no further text,
so no limit leaves part of a value that masking would have recognised. Elements become
`ElementView`s: a random handle, a role from a fixed set (any other `role` attribute falls back
to the element's own), and a masked label: visible, non-editable text first — the
elements `aria-labelledby` names, the field's label, or the element's own text. An element with none
takes what the page itself calls it (`gate.py::CONTROLS_JS`'s `pageName`, which the action check's
reading shares): a button-type input's `value` (`submit`, `button`, `reset`, `image`: what it shows),
its `aria-label`, `title` or `alt`, else the `alt` of a visible image it holds, loaded or not. A field's value never
names it, and nothing on or holding a secret (the one secret rule) does. An attribute's name is
the page's own claim, which the operator may not see on the screen (an icon's `aria-label`); it is
masked like page text, and the check also has the picture. An unnamed control is `(unnamed button 2)`.
A field of
`gate.py::FIELD_KINDS` also carries its `kind` (`time`, `date`, … `number`, and `multi-line`
for a real `<textarea>`, the one field that takes line breaks), a closed-set fact like the
role, never the attribute's text. An ordinary field also carries what it holds (operator
ruling 2026-10-31: the agent picked a date in a calendar widget and could not confirm it): `value_masked` — an input's or textarea's value, a select's chosen
options (each label as the page shows it), an editable element's text, read up to 500 characters (a longer one is cut as text is,
ending in ` …`) and masked like page text — or `checked` for a checkbox, a radio or an
`aria-checked` control. The rule is the screenshots' one: a field the gate's secret rule
recognises (`gate.py::SECRET_JS`; a password, one-time-code or payment-card field, or one inside
or holding one) carries neither, judged as the field is when it is read (operator ruling
2026-10-31, option (b)). Right after the reading every field whose value would be shown is asked
the rule again (`Observer.observe_page`), as a screenshot is re-checked while its marks are on;
one that answers secret now, or cannot be asked, shows nothing. This
loosens the original design (an observation carried no field values) deliberately; the action
check's packet already showed ordinary values under the same rule, and nothing the gate decides
reads an observation. Residual (ruling 2026-10-20, a page can show its own value as text): a
field the page itself later changes — its type or autocomplete removed, as a "show password"
toggle may do, or moved out of a secret element — shows what it then holds, and a value typed
into a field the rule does not recognise is an ordinary value. Which elements are listed is one rule shared with the
action gate's reading (`gate.py::CONTROLS_JS`): the semantic controls, and what
the page makes actionable without saying what it is — an element in the tab order or with an
inline `onclick`, and where the page shows a pointer cursor each element holding its own text
(a list entry, a calendar cell, a menu row) and each icon — as role `clickable`, never a label or
anything inside or holding a semantic control. A widget whose parts carry none of these facts is
not listed (the live view is its way). One cap of 400 holds them, a select counting its options:
past it, what is in the viewport goes first (a menu appended to the body too), a semantic control
left out makes a reading incomplete, the observation opens with `observe.py::CONTROLS_LEFT`, and
`controls_left` counts every control and option left out, so the listed and the left out add up to
what the page has. A visible
select is followed by its options (role `option`, named by the label each shows), each a `select`
target even while the dropdown is closed; a select whose options did not all fit is named with
how many are listed (`Where (394 of 450 options listed)`), so a list cut short never reads as
the whole list; a select the secret rule recognises lists none, its
choice being refused to a model anyway. The real
target stays in `casa_browser/rootfs/opt/casa_browser/handles.py::HandleRegistry`, bound to its
run and page generation; `handles.py::StaleHandle` refuses it from another run or an older
generation. Two residuals, both properties of the string: a short code such as an 8-character
recovery code is not caught, and a value the page itself splits — across separate layout boxes
(block children, flex or grid items, a slot) or by whitespace inside an address (a tab or line
break the URL standard would skip) — is judged piece by piece, so its parts can reach the model
unmasked. Only pages that belong to the session are observed at all.

**A search.** An observation asked with `find` (plain words, in any case, never a pattern) searches
every actionable element of every shown frame, past the cap, and moves nothing. The words never enter
the page. A first evaluation of `observe.py::EXTRACT` returns, by ordinal, what a search may look at:
what each element is named and holds as it would be listed, and a select's options, all under the
one secret rule, so a secret field's value and a secret select's options are not returned at all.
`observe.py::search_ask` masks those as a reply does and matches there, the one match. A second
evaluation receives only ordinals: `gate.py::CONTROLS_JS`'s `actionable` ranks what was found first
and its two neighbours either side next, so they fit the cap and get handles wherever they are on
the page, an option found ahead of the rest of its select. `found`, the list, the option order and how
many that hold the words did not fit (`find`'s `left`) all follow from that match, so changing what
masking hides changes no part of the reply. A page that changes between the two may get a stale
list: it is only a list, and a press goes through the same check. A field that answers secret when
asked again is not marked `found`. The reply's `find` also says whether the search was `whole`: not
when a limit left part of the page unread (5,000 shadow roots), a frame was not shown, or
`observe.py::closed_shadow_any` finds a closed shadow root in the page or any frame's document or
cannot reach one of those documents. A closed root may hold only a password manager's button: the
reply says what it could not reach. A list that loads as it scrolls is searched as loaded so far.
The action gate's `gate.py::READ` passes no ranking, so a search cannot change what a final read
compares; a control found is pressed like any other, its facts read past the reading's list.

**Screenshots** — the operator's own (`Observer.capture_operator`: after a checked press or Enter
([`casa-browser-runs.md`](casa-browser-runs.md)), and the
picture the agent sends with `take_screenshot`) and a question's (`Observer.capture_question`, which also outlines the
target, [`casa-browser-actions.md`](casa-browser-actions.md)) — are the operator's; `Observer.capture_for_model` (the check's picture, `POST …/look`) gives a model
the same picture and keeps it nowhere. All are taken under one rule (`Observer._capture_png`) and
cropped to the page's content (`crop_png`), so a small page is legible on a phone: the
union of the viewport rectangles of its visible text, images, media, frames, form controls and
shadow hosts, in the document and every open shadow root, and a question's target, with a 24 px
margin and at least 480×240 pixels. It is measured right after the capture, with the page's
animations held as the capture held them (finite ones finished, looping ones at their start), and the captured image is cut to it once the capture's checks passed; the
whole image is kept when that fails. A crop only leaves out window area; the mask and its checks
are the same.
Every form field and editable element — in the document and every open shadow root,
inventoried without the observation's limits — that the gate's one secret rule recognises (a
password field, a one-time-code or payment-card field, by `type` or `autocomplete`, or one inside
or holding such a field; `gate.py::SECRET_JS`), and every child frame, is blacked out by
Playwright's screenshot mask. Every other field shows what it holds — a name, an email, a choice,
a comment — and text the page itself renders stays visible, secret-looking or not (operator
rulings 2026-10-20, 2026-10-29 and 2026-11-06: simple redaction of recognised secret fields). No screenshot is taken when the
inventory hit a limit, when editable text renders outside the box of an editable element (an
editable element with no box of its own, or overflow left visible), so the mask could not cover
it, when a closed shadow root holds a form field (`observe.py::closed_shadow_holds_field`; a failure
to ask counts as one; a frame in a root without one is blacked out), when the
page does not belong to the session or moved, or when a second reading taken right after the capture, while the
marks are still on, finds any of the conditions above (a limit reached, such a root),
an element the mask was to cover gone or unmarked, or any secret field or frame without the capture's
mark (a secret field or frame added, replaced or swapped during the capture). Only the final PNG is written, under
the artifacts directory.

**INV-BRW-061**: A picture for a model comes only from the one capture rule (password, one-time-code and payment-card fields and every child frame black), is held in memory only for its judgement or its one reply, and reaches no store row, stored response or file; with no picture a judgement is the same text check.

**INV-BRW-043**: An observation or a screenshot is produced only when the page-view gate admits the page's full current address as the session's own — in an account session, the site's origins or login origins; in the public session, an address the floor admits — before and after it is read, and an observation carries no address beyond an origin, no selector, no attribute but the name of an element with no visible one (a button input's value, `aria-label`, `title`, `alt`), no other field's value as a name, and no value of a field the one secret rule recognises — a password, one-time-code or payment-card field, as the field is when it is read — each field whose value it shows being asked that rule again right after the reading.

**INV-BRW-067**: An observation's `find` searches every actionable control of the page, past the cap, and says no control holds the words only when the search was whole, a closed shadow root in any frame's document ruling that out; its words never enter the page and it matches only the masked text a reply shows, never a secret field's value or a secret select's options, so what masking hides changes no part of the reply; and it changes neither what `gate.py::READ` lists nor its digest.

## Testing

`casa_browser/tests/test_{masking,handles,observe}.py` pin masking, handles and the gate with
fakes; `casa_browser/tests/integration/test_observation.py` runs the real extraction code on
branded Chrome, including shadow roots, split and long secrets, editable content and fields
added or replaced during the capture, and checks the screenshot's masked regions pixel by pixel.
`casa_browser/tests/integration/test_long_page_find.py` runs a search and scrolling on a page of over
700 semantic controls in real Chromium, and `test_long_live_page.py` through the production controller.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa_browser/rootfs/opt/casa_browser/handles.py`
- `casa_browser/rootfs/opt/casa_browser/masking.py`
- `casa_browser/rootfs/opt/casa_browser/observe.py`
- `casa_browser/rootfs/opt/casa_browser/api.py::look`
- `casa_browser/rootfs/opt/casa_browser/api.py::read`

**Tests**
- `casa_browser/tests/test_masking.py`
- `casa_browser/tests/test_handles.py`
- `casa_browser/tests/test_observe.py`
- `casa_browser/tests/integration/test_observation.py`
- `casa_browser/tests/integration/test_long_page_find.py`

**Related**
- [`reference/casa-browser-browser.md`](../reference/casa-browser-browser.md)
- [`reference/casa-browser-actions.md`](../reference/casa-browser-actions.md)
- [`reference/casa-browser.md`](../reference/casa-browser.md)
<!-- END SOURCEMAP -->
