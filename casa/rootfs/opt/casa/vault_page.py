"""The web page where a person sees and changes the items a built-in store keeps.

The page is an engine and a profile. This module is the engine: requests,
forms, single-use codes and layout. What an item is to the app that serves
the page (its words, which values are hidden, what a form offers, what a
save writes) comes in a `Profile`, made once by that app and handed to
`Page`. The engine holds no app's words and no app's rules.

Everything here is a pure function of a request and the store: `Page.handle`
takes a `Request` and returns a `Response`, and whoever listens on a socket
passes them through. A value the profile hides is never in what a GET
returns, only the plain ones are; it leaves only in the answer to a POST of
a Show or Copy form.

Every change is a form POST, and every POST is checked before anything is
read or written: it must come from this page's own origin, be a form of a
sane size, and carry a single-use code that the page handed out with the
form, for the path the form posts to. A form used twice, kept open for more
than an hour or opened before the service restarted is refused.

Every link on the page sits under the path prefix the front proxy names in
the `X-Ingress-Path` header (Home Assistant's ingress sends one). The header
is checked only so that nothing unsafe is written into a link: who may reach
the page at all is decided by the proxy in front of it.

The module depends only on the standard library and `vault_store`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import html
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs

if __package__:
    from .vault_store import Field, Item, ItemNotFound, Store, StoreError
else:
    from vault_store import Field, Item, ItemNotFound, Store, StoreError

# Who the store records as the last writer of an item changed on this page.
WRITER = "page"

# The largest form body the page reads, and the most fields an item made
# or changed on the page may hold.
MAX_FORM = 256 * 1024
MAX_FIELDS = 40
# The most name=value pairs a form body is read with. The shortest name a form
# renders has four characters, so a pair of rendered names takes at least six
# bytes with its `=` and `&`: every form the page renders that fits in
# MAX_FORM fits under this, however many fields its item has.
MAX_PAIRS = MAX_FORM // 5

# The methods the page answers; any other gets 405.
METHODS = ("GET", "HEAD", "POST")

# How long a form's code stays good, and how many codes of each kind are kept at once.
CODE_LIFETIME = 3600.0
MAX_CODES = 4096

INGRESS_PREFIX = re.compile(r"(?:/[A-Za-z0-9._-]{1,128}){1,8}")

# An item id as the store makes it: a UUID in lower-case base32.
_ITEM_ID = r"[a-z2-7]{26}"
_SECRET_PATH = re.compile(rf"/s/({_ITEM_ID})(/edit|/delete|/show)?")
_INDEX = re.compile(r"[0-9]{1,4}")  # a field's position as a form sends it
# Text the store's file format cannot hold: control characters other than tab,
# line feed and carriage return, and the two non-characters U+FFFE and U+FFFF.
_UNSTORABLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")

_FREE_TYPE = "CONCEALED"  # what a value typed into a free slot is stored as
_DOTS = "•" * 8  # the same length whatever the value, so it tells nothing
_HOLD = "Press and hold the value to copy it."
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_HTML = "text/html; charset=utf-8"
_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("Content-Security-Policy",
     "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
     "connect-src 'self'; form-action 'self'; frame-ancestors 'self'; base-uri 'none'"),
)
_FORM_TYPE = "application/x-www-form-urlencoded"

# What a person reads when a save is refused. Only the name is ever repeated.
# The sentences that name the item are the profile's words.
_NAME_MISSING = "Enter a name."
_NAME_TOO_LONG = "Use a name of at most {} characters."
_LABEL_MISSING = "Enter a label."
_LABEL_TWICE = "Each value needs a different label."
_VALUE_MISSING = "Enter a value."
_NEW_WITHOUT_VALUE = "Enter a value, or remove this value card."
_REPLACED_WITH_NOTHING = "Enter a value or turn off Replace."
_CLEARED = "The values you typed were cleared. Enter them again."
_NAME_UNSTORABLE = "The name holds a character that cannot be saved, such as a form feed."
_VALUE_UNSTORABLE = ("A value holds a character that cannot be saved, such as a form feed. "
                     "Remove it and try again.")


@dataclass(frozen=True)
class Request:
    """One request as the front proxy forwards it.

    `path` has the page's own root (the profile's `root`) already removed
    (`/`, `/s/<id>`, ...) and carries no query string; header names are lower
    case.
    """
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class Response:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


# -- the profile ------------------------------------------------------------------

# Every sentence and title of the page or its script that names the item, the
# app or who uses an item, by the key a profile's `words` gives it under. `{}`
# marks where the engine or the script puts a name or a number. The words are
# fixed text, written to the page as they are.
WORDS = {
    "product": "the app's name, after every page title",
    "open_through": "said to a request that did not come through the front proxy",
    "list": "the list's title",
    "search": "the label and the placeholder of the list's search box",
    "no_match": "the heading when a search leaves no row",
    "none_yet": "the heading of an empty list",
    "none_yet_help": "the line under it",
    "add": "the add form's title, and the button of an empty list",
    "edit": "the edit form's title",
    "delete": "the delete screen's title and button, and the link to it",
    "back_to_list": "the link on a page that says something went wrong",
    "back_to_app": "the link out of the page when the store cannot be opened",
    "unavailable": "the heading when the store cannot be opened",
    "unavailable_help": "the line under it",
    "use_show": "said to a GET of the Show address",
    "not_found": "said when there is no such item or page",
    "name_help": "the line under the add form's name box",
    "name_taken": "refuses a name another item has; {} is the name",
    "no_value_left": "refuses an edit that removes every value",
    "too_many": "refuses an edit past the most values; {} is that number",
    "not_saved": "the heading of every refused save",
    "changed": "said when the item changed while its edit form was open",
    "reload": "the link that opens the edit form again",
    "read_once": "the line of an item read once since the service started",
    "read_many": "the same for more reads; {} is how many",
    "delete_effect": "the line under the delete screen's warning, or \"\" for none",
    "count_one": "said by the script when a search leaves one row; {} is 1",
    "count_many": "said by the script when a search leaves another number of rows; {} is it",
}
# The words the page's script shows itself. Every page hands them to it as
# attributes of `<body>` (`data-not-saved`, ...), since the page's content
# policy runs no script written into the page.
_SCRIPT_WORDS = ("not_saved", "no_value_left", "count_one", "count_many")


@dataclass(frozen=True)
class Box:
    """One fixed box of a form: a value with a set label, typed under a caption.

    `label` and `type` are what the value is stored under and as, `caption`
    the words beside the box; `multiline` gives it several lines. `required`
    is the sentence that refuses a save leaving the item without a value for
    this box, or "" when the box may stay empty. A stored value of a
    required box has no Remove on the edit form.
    """
    label: str
    type: str
    caption: str
    multiline: bool = False
    required: str = ""


@dataclass(frozen=True)
class Slots:
    """Where a form takes new values: free slots, or fixed boxes when `boxes` is given.

    Free slots: one for each of `defaults`, the labels the add form's slots
    start with. A person may type any label; `suggestions` are offered at
    one tap, and the edit form's slots start with the ones the item does not
    use yet. A value typed into a free slot is stored as `CONCEALED`.

    Fixed boxes: the add form shows every box, in order. The edit form shows
    each stored value of a box as its card and offers the boxes whose label
    the item lacks. No label is typed, and a box left empty adds nothing. A
    value stored under any other label has its card on the item's page and
    none on the edit form: a save carries it as it is stored. A box whose
    label the profile keeps from the person is not offered.
    """
    defaults: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    boxes: tuple[Box, ...] = ()


@dataclass(frozen=True)
class Facts:
    """A read-only block at the top of an item's page.

    `rows` are (name, value) pairs under `heading`, and `note` is the line
    below them. `labels` are the labels of the fields the block stands for:
    those get no value card. An item with a block is never treated as an
    item of one value: its row on the list opens it rather than copying.
    """
    heading: str
    rows: tuple[tuple[str, str], ...]
    note: str
    labels: frozenset[str]


@dataclass(frozen=True)
class Profile:
    """What an item is to the app that serves the page. It never sees a request.

    `root`: the path segment of every address the page writes, its links
        and its static files (`<prefix>/<root>/...`). The paths the page
        receives carry no such segment.
    `words`: the page's own sentences, one for every key of `WORDS`.
    `back`: from the ingress prefix, where the list's Back leads, which is
        also the way out when the store cannot be opened.
    `summary`: the second line of an item's row on the list.
    `origin`: who made the item, said on its page beside when it last
        changed.
    `delete_warning`: the bold line on the delete screen, or "" for none.
    `heading`: what a value is called on the item's page and on its edit
        card, from the item, the field and whether it stands alone: true on
        the page of an item with this one value and no facts block, where
        the heading is only read out; never on the edit form.
    `is_hidden`: whether a field's value is kept off the page until asked
        for: dots, Show and Copy, and Replace on the edit form.
    `kept`: labels kept from the person: no card, not a Show target, no
        control and no accepted form name, and never the label of a value a
        person adds. A save carries such a field as it is stored, and the
        edit form's stamp covers it.
    `facts`: the read-only block of an item's page, or None for none.
    `editable`: whether an item has an edit form.
    `references`: whether an item's page lists the references to its values.
    `slots`: where a form takes new values.
    `category`: the category of an item made on the page.
    `name_limit`: the most characters a new item's name may have, or None.
    `check`: from the name and the fields a save would store, the sentence
        that refuses it, or None. Asked on Add and on an edit that changes
        something, after `stored`.
    `stored`: from the item (None on Add) and the fields a person's form
        gives, the fields to write: theirs, and whatever the profile adds or
        changes. Not asked for an edit that changes nothing.
    `after_save`, `after_delete`: called with the item once the store has
        written it, or with the item as it was once the store has removed it.

    What `summary`, `origin`, `delete_warning`, `heading`, `facts` and
    `check` return is written as text, and so are a box's caption and its
    sentence. `words` are fixed words, written to the page as they are.
    """
    root: str
    words: Mapping[str, str]
    back: Callable[[str], str]
    summary: Callable[[Item], str]
    origin: Callable[[Item], str]
    delete_warning: Callable[[Item], str]
    heading: Callable[[Item, Field, bool], str]
    is_hidden: Callable[[Field], bool]
    kept: frozenset[str]
    facts: Callable[[Item], Facts | None]
    editable: Callable[[Item], bool]
    references: Callable[[Item], bool]
    slots: Slots
    category: str
    name_limit: int | None
    check: Callable[[str, Sequence[Field]], str | None]
    stored: Callable[[Item | None, list[Field]], list[Field]]
    after_save: Callable[[Item], None]
    after_delete: Callable[[Item], None]

    def __post_init__(self) -> None:
        missing = sorted(set(WORDS) - set(self.words))
        if missing:
            raise ValueError("the profile's words lack: " + ", ".join(missing))


@dataclass(frozen=True)
class _Problem:
    """Why a save was refused, and whether it is about the name."""
    sentence: str
    on_name: bool = False


class Codes:
    """Single-use codes, each good for one POST to the path it was issued for.

    A code lives `CODE_LIFETIME` seconds. Codes for Show and Copy are kept
    apart from those of the add, edit and delete forms, at most `MAX_CODES`
    of each; when one kind is full, its quarter closest to expiry is
    dropped. The list hands out a Show code for every item it copies
    from, so however often it is opened, it pushes out only older Show
    codes (a Copy then asks for a reload), never the code of a form someone
    is filling in. Codes live in memory only, so a restart voids every open
    form.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._shows: dict[str, tuple[str, float]] = {}
        self._forms: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def issue(self, path: str) -> str:
        with self._lock:
            kept = self._shows if path.endswith("/show") else self._forms
            if len(kept) >= MAX_CODES:
                oldest = sorted(kept, key=lambda code: kept[code][1])
                for code in oldest[:MAX_CODES // 4]:
                    del kept[code]
            code = secrets.token_urlsafe(16)
            kept[code] = (path, self._clock() + CODE_LIFETIME)
            return code

    def take(self, code: str | None, path: str) -> bool:
        """Spend `code`: whether it was live and issued for `path`.

        A code presented once is gone, whether or not it was good here.
        """
        if not isinstance(code, str):
            return False
        with self._lock:
            entry = self._shows.pop(code, None) or self._forms.pop(code, None)
        return entry is not None and entry[0] == path and self._clock() < entry[1]


class Page:
    """The page over one store, in one profile's words and by its rules.

    `store` is None when the store could not be opened; every path then
    answers that the items are unavailable. `clock` measures how old a
    form is.
    """

    def __init__(self, store: Store | None, profile: Profile,
                 clock: Callable[[], float] = time.monotonic):
        self._store = store
        self._profile = profile
        self._words = profile.words
        # The fixed boxes a person may fill: a box under a kept label is not theirs.
        self._fillable = tuple(box for box in profile.slots.boxes
                               if box.label not in profile.kept)
        # The attributes of `<body>` that hand the script its words.
        self._script_words = "".join(
            f' data-{key.replace("_", "-")}="{_text(profile.words[key])}"'
            for key in _SCRIPT_WORDS)
        self.codes = Codes(clock)
        # Keys the edit form's stamp, which covers the item's values: the
        # stamp tells whether the item changed, and nothing about them.
        self._stamp_key = secrets.token_bytes(32)

    def handle(self, request: Request) -> Response:
        if request.method not in METHODS:
            return _response(405, _plain("Not allowed",
                                         "This page only answers GET, HEAD and POST."),
                             extra=(("Allow", ", ".join(METHODS)),))
        prefix = request.headers.get("x-ingress-path", "")
        if not INGRESS_PREFIX.fullmatch(prefix):
            return _response(403, _plain("Not available", self._words["open_through"]))
        response = self._route(request, prefix)
        if request.method == "HEAD":
            return Response(response.status, response.headers, b"")
        return response

    def _route(self, request: Request, prefix: str) -> Response:
        words = self._words
        if self._store is None:
            back = self._profile.back(prefix)
            return self._message(prefix, 503, words["list"], words["unavailable"],
                                 words["unavailable_help"],
                                 _link("Try again", self._url(prefix), "v-button--primary")
                                 + _link(words["back_to_app"], back, "v-button--quiet"),
                                 back=back)
        if request.method == "POST":
            return self._post(request, prefix)
        path = request.path
        if path == "/":
            return self._list(prefix)
        if path == "/add":
            return self._add_form(prefix)
        match = _SECRET_PATH.fullmatch(path)
        if not match:
            return self._not_found(prefix)
        item_id, action = match.groups()
        if action == "/show":
            return self._message(prefix, 405, "Not allowed", "Not allowed", words["use_show"],
                                 _link(words["back_to_list"], self._url(prefix),
                                       "v-button--primary"),
                                 extra=(("Allow", "POST"),))
        item = self._find(item_id)
        if item is None:
            return self._not_found(prefix)
        if action is None:
            return self._one(prefix, item)
        if action == "/edit":
            if not self._profile.editable(item):
                return self._not_found(prefix)
            return self._edit_form(prefix, item)
        return self._delete_form(prefix, item)

    def _find(self, item_id: str) -> Item | None:
        try:
            item = self._store.get(item_id)
        except StoreError:
            return None
        # The store also finds by title; the page finds by id only.
        return item if item.id == item_id else None

    def _shown(self, item: Item) -> list[tuple[int, Field]]:
        """The item's fields a person may see, each with the position it is stored at."""
        kept = self._profile.kept
        return [(index, field) for index, field in enumerate(item.fields)
                if field.label not in kept]

    def _cards(self, item: Item) -> list[tuple[int, Field]]:
        """The stored fields the edit form has a card for, each with its stored position.

        With free slots that is every field a person may see. With fixed
        boxes it is the values of the boxes only: a field stored under any
        other label has no control, so no form the page accepts names it,
        and a save carries it as it is stored.
        """
        shown = self._shown(item)
        if not self._profile.slots.boxes:
            return shown
        labels = {box.label for box in self._fillable}
        return [(index, field) for index, field in shown if field.label in labels]

    def _only_value(self, item: Item) -> int | None:
        """Where the item's one value is stored, when a person sees just one and no facts."""
        shown = self._shown(item)
        if len(shown) == 1 and self._profile.facts(item) is None:
            return shown[0][0]
        return None

    # -- reading ------------------------------------------------------------------

    def _list(self, prefix: str) -> Response:
        words = self._words
        items = sorted(self._store.list(), key=lambda item: item.title.casefold())
        add = self._url(prefix, "add")
        back = self._profile.back(prefix)
        if not items:
            body = (f'<section class="v-panel v-empty"><h2>{words["none_yet"]}</h2>'
                    f'<p class="v-help">{words["none_yet_help"]}</p>'
                    + _link(words["add"], add, "v-button--primary") + "</section>")
            return _response(200, self._document(prefix, words["list"], back, body))
        rows = []
        for item in items:
            link = self._url(prefix, "s", item.id)
            # An item with one value is copied from the list, through a Show
            # form of its own; the value itself is never on the list. Whether
            # that value is empty is not said before it is asked for.
            only = self._only_value(item)
            if only is not None:
                action = self._reveal_form(prefix, item, only,
                                           self.codes.issue(f"/s/{item.id}/show"),
                                           copy_only=True)
            else:
                action = _link("Open", link, "v-button--quiet")
            rows.append(
                f'<li class="v-list-item" data-find="{_text(item.title.lower())}" '
                'data-value-block><div class="v-list-row">'
                f'<a class="v-list-link" href="{link}"><strong>{_text(item.title)}</strong>'
                f"<small>{_text(self._profile.summary(item))}</small></a>{action}</div>"
                f"{_feedback()}</li>")
        # Search filters the rows as the person types; only the script shows it.
        body = ('<div class="v-search" hidden><label class="v-sr-only" for="search">'
                f'{words["search"]}</label><input class="v-input" id="search" type="search" '
                f'placeholder="{words["search"]}" {_NO_HELP}></div>'
                '<ul class="v-list">' + "".join(rows) + "</ul>"
                f'<div class="v-empty" id="no-results" hidden><h2>{words["no_match"]}</h2>'
                '<p class="v-help">Try another name.</p>'
                '<button class="v-button" type="button" data-clear-search>Clear search</button>'
                '</div><p class="v-sr-only" id="search-count" role="status"></p>')
        return _response(200, self._document(prefix, words["list"], back, body,
                                             _link("Add", add, "v-button--primary")))

    def _one(self, prefix: str, item: Item, shown: int | None = None) -> Response:
        """One item's page; `shown` is the index of the field whose value it shows."""
        profile = self._profile
        facts = profile.facts(item)
        fields = self._shown(item)
        single = facts is None and len(fields) == 1
        show_path = f"/s/{item.id}/show"
        body = ""
        if facts is not None:
            rows = "".join(
                f"<div><dt>{_text(name)}</dt>"
                f'<dd class="v-reference">{_text(value)}</dd></div>'
                for name, value in facts.rows)
            body += ('<section class="v-panel v-stack"><div class="v-heading">'
                     f"<h2>{_text(facts.heading)}</h2>"
                     f'</div><dl class="v-facts">{rows}</dl><p class="v-help">'
                     f"{_text(facts.note)}</p></section>")
        for index, field in fields:
            if facts is not None and field.label in facts.labels:
                continue
            code = self.codes.issue(show_path) if profile.is_hidden(field) else None
            title = profile.heading(item, field, single)
            body += self._value_card(prefix, item, index, field, code, shown == index, title,
                                     title_hidden=single)
        body += (f'<p class="v-meta">{_text(profile.origin(item))} · '
                 f"Updated {_when(item.updated_at)}</p>")
        reads = self._store.reads(item.id)
        if reads:
            body += f'<p class="v-meta">{self._reads(reads)}</p>'
        if profile.references(item):
            # A title holding "/" or "?" cannot be a reference segment; its id can.
            head = item.id if "/" in item.title or "?" in item.title else item.title
            body += ('<details class="v-label-options"><summary>References</summary>'
                     '<div class="v-stack">'
                     + "".join(f'<p class="v-reference">{_text(head)}/{_text(field.label)}</p>'
                               for _, field in fields)
                     + "</div></details>")
        body += ('<div class="v-danger-zone">'
                 + _link(self._words["delete"], self._url(prefix, "s", item.id, "delete"),
                         "v-button--danger")
                 + "</div>")
        action = ""
        if profile.editable(item):
            action = _link("Edit", self._url(prefix, "s", item.id, "edit"), "v-button--quiet")
        return _response(200, self._document(prefix, item.title, self._url(prefix), body,
                                             action))

    def _reads(self, count: int) -> str:
        if count == 1:
            return self._words["read_once"]
        return self._words["read_many"].format(count)

    # -- the forms ------------------------------------------------------------------

    def _add_form(self, prefix: str, draft: Mapping[str, str] | None = None,
                  problem: _Problem | None = None) -> Response:
        """The add form; `draft` is the refused form it shows again, without its hidden values."""
        code = self.codes.issue("/add")
        name = (draft or {}).get("name", "")
        described = "name-help"
        invalid = ""
        error = ""
        if problem and problem.on_name:
            described += " name-error"
            invalid = ' aria-invalid="true"'
            error = f'<p class="v-field-error" id="name-error">{_text(problem.sentence)}</p>'
        body = (self._form_problem(problem, draft, None)
                + f'<form class="v-form" id="secret-form" method="post" novalidate '
                  f'action="{self._url(prefix, "add")}">'
                + _hidden("code", code)
                + '<label class="v-field" for="f-name">Name<input class="v-input" type="text" '
                  f'id="f-name" name="name" value="{_text(name)}" {_NO_HELP} '
                  f'aria-describedby="{described}"{invalid}></label>'
                + f'<p class="v-help" id="name-help">{self._words["name_help"]}</p>'
                + error
                + self._new_slots(None, draft)
                + _form_end(self._url(prefix))
                + "</form>")
        return _response(200, self._document(prefix, self._words["add"], self._url(prefix),
                                             body, _SAVE_ACTION, form_page=True))

    def _edit_form(self, prefix: str, item: Item, draft: Mapping[str, str] | None = None,
                   problem: _Problem | None = None) -> Response:
        """The edit form; `draft` is the refused form it shows again, without its hidden values."""
        code = self.codes.issue(f"/s/{item.id}/edit")
        existing = "".join(self._existing_card(item, index, field, draft)
                           for index, field in self._cards(item))
        body = (f'<p class="v-subtitle v-name">{_text(item.title)}</p>'
                + self._form_problem(problem, draft, item)
                + '<form class="v-form" id="secret-form" method="post" novalidate '
                  f'action="{self._url(prefix, "s", item.id, "edit")}">'
                + _hidden("code", code)
                + _hidden("stamp", self._stamp(item))
                + existing
                + self._new_slots(item, draft)
                + _form_end(self._url(prefix, "s", item.id))
                + "</form>")
        return _response(200, self._document(prefix, self._words["edit"],
                                             self._url(prefix, "s", item.id),
                                             body, _SAVE_ACTION, form_page=True))

    def _delete_form(self, prefix: str, item: Item) -> Response:
        code = self.codes.issue(f"/s/{item.id}/delete")
        warning = self._profile.delete_warning(item)
        if warning:
            effect = self._words["delete_effect"]
            warning = (f'<div class="v-notice v-notice--warning"><strong>{_text(warning)}</strong>'
                       + (f"<p>{effect}</p>" if effect else "") + "</div>")
        body = (f'<section class="v-panel v-stack"><h2 class="v-title">'
                f"Delete {_text(item.title)}?</h2>"
                "<p>All its values will be deleted. This cannot be undone.</p>"
                + warning
                + f'<form class="v-actions" method="post" '
                  f'action="{self._url(prefix, "s", item.id, "delete")}">'
                + _hidden("code", code)
                + _link("Cancel", self._url(prefix, "s", item.id), "v-button--quiet")
                + '<button class="v-button v-button--delete" type="submit">'
                  f'{self._words["delete"]}</button>'
                  "</form></section>")
        return _response(200, self._document(prefix, self._words["delete"],
                                             self._url(prefix, "s", item.id), body))

    # -- changing -------------------------------------------------------------------

    def _post(self, request: Request, prefix: str) -> Response:
        refused, form = self._check(request, prefix)
        if refused is not None:
            return refused
        if request.path == "/add":
            return self._add(prefix, form)
        match = _SECRET_PATH.fullmatch(request.path)
        if not match or match.group(2) is None:
            return self._not_found(prefix)
        item_id, action = match.groups()
        item = self._find(item_id)
        if item is None:
            return self._not_found(prefix)
        if action == "/show":
            return self._show(request, prefix, item, form)
        if action == "/edit":
            if not self._profile.editable(item):
                return self._not_found(prefix)
            return self._save(prefix, item, form)
        if not set(form) <= self._form_names("delete"):
            return self._not_rendered(prefix)
        try:
            self._store.delete(item.id)
        except ItemNotFound:
            return self._not_found(prefix)
        self._profile.after_delete(item)
        return _redirect(self._url(prefix))

    def _check(self, request: Request, prefix: str) -> tuple[Response | None, dict[str, str]]:
        """Refuse a POST that is not this page's own, unused, form.

        In this order: the origin (the `Origin` header, or without one an
        explicit `Sec-Fetch-Site: same-origin`), the body's type and size,
        the form (read strictly, each name at most once), and last the code,
        so a request refused earlier leaves its code unspent. Whether every
        name is one the form rendered depends on the item, and is checked
        by each route once the code is spent.
        """
        headers = request.headers
        origin = headers.get("origin")
        if origin is not None:
            if origin != _forwarded_origin(headers):
                return self._refusal(prefix, 403,
                                     "This request did not come from this page."), {}
        elif headers.get("sec-fetch-site") != "same-origin":
            return self._refusal(prefix, 403, "This request did not come from this page."), {}
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != _FORM_TYPE:
            return self._refusal(prefix, 403, "This request is not a form of this page."), {}
        if len(request.body) > MAX_FORM:
            return self._refusal(prefix, 413, "This form is too large to save."), {}
        try:
            # Strict both times: a value is never stored with bytes replaced.
            pairs = parse_qs(request.body.decode("utf-8"), keep_blank_values=True,
                             max_num_fields=MAX_PAIRS, encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            return self._refusal(prefix, 400, "This form could not be read."), {}
        if any(len(values) > 1 for values in pairs.values()):
            return self._not_rendered(prefix), {}  # which one was meant is unknown
        form = {name: values[0] for name, values in pairs.items()}
        if not self.codes.take(form.get("code"), request.path):
            return self._message(prefix, 403, "Form expired", "Form expired",
                                 "Reload the page and try again. Nothing was changed.",
                                 _link("Reload page", self._form_page(prefix, request.path),
                                       "v-button--primary")), {}
        return None, form

    def _add(self, prefix: str, form: dict[str, str]) -> Response:
        if not set(form) <= self._form_names("add"):
            return self._not_rendered(prefix)
        profile = self._profile
        name = form.get("name", "").strip()
        fields, problem = self._new_fields(form, None)

        def again(sentence: str, on_name: bool = False) -> Response:
            return self._add_form(prefix, form, _Problem(sentence, on_name))

        if not name:
            return again(_NAME_MISSING, on_name=True)
        if _UNSTORABLE.search(name):
            return again(_NAME_UNSTORABLE, on_name=True)
        if profile.name_limit is not None and len(name) > profile.name_limit:
            return again(_NAME_TOO_LONG.format(profile.name_limit), on_name=True)
        if not problem and fields and fields[0].label and not fields[0].value:
            problem = _VALUE_MISSING  # the first value cannot be removed, only filled in
        problem = problem or _unstorable(fields, ()) or self._emptied(fields, ())
        if problem:
            return again(problem)
        # A reference by name would match two items.
        if any(item.title == name for item in self._store.list()):
            return again(self._words["name_taken"].format(name), on_name=True)
        # The profile's own rules come last, on what would be written.
        problem = self._unfilled(fields)
        if not problem and not fields:
            problem = _VALUE_MISSING  # every fixed box was left empty
        if not problem:
            fields = profile.stored(None, fields)
            problem = profile.check(name, fields)
        if problem:
            return again(problem)
        try:
            item = self._store.create(title=name, category=profile.category, tags=[],
                                      fields=fields, writer=WRITER)
        except StoreError:  # the store's rule: one label, one field
            return again(_LABEL_TWICE)
        profile.after_save(item)
        return _redirect(self._url(prefix, "s", item.id))

    def _save(self, prefix: str, item: Item, form: dict[str, str]) -> Response:
        """Save the edit form: each stored field kept, replaced or removed, and new ones.

        The form names stored fields by position. Its stamp says what the
        item held when the form was made, so an item that changed since
        (and whose positions may have moved) is never overwritten. A field
        without a card on the form (`_cards`) has no control, so no form the
        page accepts names it, and it is carried as it is stored.
        """
        profile, words = self._profile, self._words
        stamp = form.get("stamp", "").encode("utf-8")
        if not hmac.compare_digest(stamp, self._stamp(item).encode("ascii")):
            return self._message(prefix, 409, words["edit"], words["not_saved"],
                                 words["changed"],
                                 _link(words["reload"], self._url(prefix, "s", item.id, "edit"),
                                       "v-button--primary"))
        if not set(form) <= self._form_names("edit", item):
            return self._not_rendered(prefix)
        fields = []
        for index, field in enumerate(item.fields):
            controls = self._stored_controls(index, field)
            if controls.get("remove") in form:
                continue
            submitted = form.get(controls["value"])
            if profile.is_hidden(field):
                if controls["replace"] in form:
                    field = dataclasses.replace(field, value=_lf(submitted or ""))
            # An untouched box comes back with every line break as CRLF; the
            # stored bytes are kept unless the text itself changed.
            elif submitted is not None and _lf(submitted) != _lf(field.value):
                field = dataclasses.replace(field, value=_lf(submitted))
            fields.append(field)
        new, problem = self._new_fields(form, item)
        fields += new

        def again(sentence: str) -> Response:
            return self._edit_form(prefix, item, form, _Problem(sentence))

        problem = (problem or _unstorable(fields, item.fields)
                   or self._emptied(fields, item.fields))
        if problem:
            return again(problem)
        if all(field.label in profile.kept for field in fields):
            return again(words["no_value_left"])
        if len(fields) > max(MAX_FIELDS, len(item.fields)):
            return again(words["too_many"].format(MAX_FIELDS))
        if fields == list(item.fields):  # nothing changed: the last writer stays
            return _redirect(self._url(prefix, "s", item.id))
        # The profile's own rules come last, on what would be written.
        problem = self._unfilled(fields)
        if not problem:
            fields = profile.stored(item, fields)
            problem = profile.check(item.title, fields)
        if problem:
            return again(problem)
        try:
            saved = self._store.replace(item.id, title=None, category=None, tags=None,
                                        fields=fields, writer=WRITER)
        except ItemNotFound:
            return self._not_found(prefix)
        except StoreError:  # the store's rule: one label, one field
            return again(_LABEL_TWICE)
        profile.after_save(saved)
        return _redirect(self._url(prefix, "s", item.id))

    def _show(self, request: Request, prefix: str, item: Item,
              form: dict[str, str]) -> Response:
        if not set(form) <= self._form_names("show"):
            return self._not_rendered(prefix)
        index = form.get("field", "")
        if not (_INDEX.fullmatch(index) and int(index) < len(item.fields)):
            return self._not_found(prefix)
        index = int(index)
        if item.fields[index].label in self._profile.kept:
            return self._not_found(prefix)  # as if there were no such field
        self._store.note_read(item.id)
        if "application/json" in request.headers.get("accept", ""):
            answer = {"value": item.fields[index].value,
                      "code": self.codes.issue(request.path)}
            return Response(200, (("Content-Type", "application/json"),) + _HEADERS,
                            json.dumps(answer).encode("utf-8"))
        return self._one(prefix, item, shown=index)

    def _stamp(self, item: Item) -> str:
        """A digest of everything the item holds, keyed so it tells nothing about it.

        The store's change time alone has whole seconds, and two writes in
        one second would carry the same one.
        """
        content = json.dumps([
            item.updated_at.isoformat(), item.title, item.category, list(item.tags),
            item.writer,
            [[f.id, f.label, f.type, f.value, f.purpose] for f in item.fields],
        ])
        return hmac.new(self._stamp_key, content.encode("utf-8"), hashlib.sha256).hexdigest()

    # -- reading a form ----------------------------------------------------------------

    def _new_fields(self, form: Mapping[str, str], item: Item | None
                    ) -> tuple[list[Field], str | None]:
        """The new values the form carries for `item` (None on the add form), and what is wrong.

        A fixed box gives a field when something was typed into it. A free
        slot is open when its `add` box came back; the add form's first slot
        always is. A closed slot's label and value are ignored, whatever
        they hold.
        """
        slots = self._profile.slots
        if slots.boxes:
            fields = []
            for index, box in self._offered(item):
                value = _lf(form.get(f"value{index}", ""))
                if value:
                    fields.append(Field(id="", label=box.label, type=box.type, value=value))
            return fields, None
        start = len(item.fields) if item is not None else 0
        fields, problem = [], None
        for offset in range(len(slots.defaults)):
            controls = _new_controls(start + offset)
            if not ((item is None and offset == 0) or controls["add"] in form):
                continue
            label = form.get(controls["label"], "").strip()
            if not label:
                problem = problem or _LABEL_MISSING
                continue
            if label in self._profile.kept:  # taken, though the person cannot see by what
                problem = problem or _LABEL_TWICE
                continue
            fields.append(Field(id="", label=label, type=_FREE_TYPE,
                                value=_lf(form.get(controls["value"], ""))))
        return fields, problem

    def _unfilled(self, fields: list[Field]) -> str | None:
        """The sentence of the first required box that `fields` hold no value for, else None."""
        for box in self._fillable:
            if box.required and not any(field.label == box.label and field.value
                                        for field in fields):
                return box.required
        return None

    def _emptied(self, fields: list[Field], before: tuple[Field, ...]) -> str | None:
        """What is wrong when a hidden field in the result has no value, else None.

        A hidden field may be empty only if it was stored empty and left as it
        was; any other empty one is a slip that would lose a value. Remove is
        how a field is dropped.
        """
        for field in fields:
            if self._profile.is_hidden(field) and not field.value and field not in before:
                return _REPLACED_WITH_NOTHING if field.id else _NEW_WITHOUT_VALUE
        return None

    def _cleared(self, form: Mapping[str, str], item: Item | None) -> bool:
        """Whether a refused form carried a value the page does not send back: a new or
        replacement one that is hidden."""
        stored = item.fields if item else ()
        if any(form.get(f"value{index}") for index, field in enumerate(stored)
               if self._profile.is_hidden(field)):
            return True
        slots = self._profile.slots
        if slots.boxes:
            return any(form.get(f"value{index}") for index, box in self._offered(item)
                       if self._hides(box))
        return any(form.get(f"value{index}")
                   for index in range(len(stored), len(stored) + len(slots.defaults)))

    # -- what each form contains ---------------------------------------------------------
    #
    # The renderers name their controls through these helpers, and a POST is
    # accepted only with names from `_form_names`, so what a form shows and what
    # its answer may carry come from one place. A browser leaves out an unticked
    # box and a disabled control and nothing else, so a form's answer carries a
    # subset of the names.

    def _stored_controls(self, index: int, field: Field) -> dict[str, str]:
        """The controls of the stored field at `index` on the edit form, by what they do.

        A plain field's value comes back in its box; a hidden one has a
        Replace box and an empty box for the new value. Either can be
        removed, unless it is the value of a required box.
        """
        controls = {"value": f"value{index}"}
        if not any(box.required and box.label == field.label for box in self._fillable):
            controls["remove"] = f"remove{index}"
        if self._profile.is_hidden(field):
            controls["replace"] = f"replace{index}"
        return controls

    def _offered(self, item: Item | None) -> list[tuple[int, Box]]:
        """The fixed boxes a form offers, each with the number its control is named by.

        The add form (`item` is None) offers every box a person may fill.
        The edit form offers those whose label the item lacks, numbered
        after its stored fields.
        """
        if item is None:
            return list(enumerate(self._fillable))
        have = {field.label for field in item.fields}
        return list(enumerate((box for box in self._fillable if box.label not in have),
                              len(item.fields)))

    def _hides(self, box: Box) -> bool:
        """Whether a value typed into `box` is one the profile hides."""
        return self._profile.is_hidden(Field(id="", label=box.label, type=box.type, value=""))

    def _form_names(self, form: str, item: Item | None = None) -> frozenset[str]:
        """Every control name the `form` ("add", "edit", "delete" or "show") renders for `item`."""
        if form == "show":
            return frozenset({"code", "field", "then"})
        if form == "delete":
            return frozenset({"code"})
        names = {"code"}
        if form == "add":
            names.add("name")
            item = None
        else:
            names.add("stamp")
            for index, field in self._cards(item):
                names.update(self._stored_controls(index, field).values())
        slots = self._profile.slots
        if slots.boxes:
            names.update(f"value{index}" for index, _ in self._offered(item))
        else:
            start = len(item.fields) if item is not None else 0
            for index in range(start, start + len(slots.defaults)):
                names.update(_new_controls(index).values())
        return frozenset(names)

    # -- pieces of a page -----------------------------------------------------------

    def _value_card(self, prefix: str, item: Item, index: int, field: Field, code: str | None,
                    shown: bool, title: str, *, title_hidden: bool) -> str:
        """One value of an item's page: its heading, its value or dots, and Show and Copy.

        A hidden value, empty or not, has the same dots and a form posting to
        Show, with a code of its own; only the answer says it is empty. Its
        Copy button posts the same form, which the page's script turns into a
        copy and which, without the script, shows the value. Once shown, Hide
        is a link back to the item. A plain value is on the page already, so
        its Copy button needs no form, and only the script shows it.
        """
        hidden = self._profile.is_hidden(field)
        heading = (f'<h2 class="v-sr-only">{_text(title)}</h2>' if title_hidden
                   else f"<h2>{_text(title)}</h2>")
        feedback = _feedback()
        if not hidden and not field.value:
            value, controls = '<span class="v-help">Empty</span>', ""
        elif not hidden:
            value = f'<pre class="v-value" data-plain>{_pre(field.value)}</pre>'
            controls = ('<div class="v-actions"><button class="v-button v-button--copy" '
                        'type="button" data-action="copy-plain" hidden>Copy</button></div>'
                        f'<noscript><p class="v-help">{_HOLD}</p></noscript>')
        elif shown and not field.value:
            value = '<pre class="v-value v-revealed" data-empty>Empty</pre>'
            controls = self._reveal_form(prefix, item, index, code, shown=True)
        elif shown:
            value = f'<pre class="v-value v-revealed">{_pre(field.value)}</pre>'
            controls = self._reveal_form(prefix, item, index, code, shown=True)
            feedback = _feedback(_HOLD)
        else:
            value = f'<span class="v-dots" aria-label="Hidden value">{_DOTS}</span>'
            controls = self._reveal_form(prefix, item, index, code)
        return (f'<section class="v-panel v-value-card" data-value-block '
                f'aria-label="{_text(title)}">{heading}'
                f'<div class="v-value-row">{value}{controls}</div>{feedback}</section>')

    def _reveal_form(self, prefix: str, item: Item, index: int, code: str | None, *,
                     shown: bool = False, copy_only: bool = False) -> str:
        """The form posting one hidden value's Show or Copy, with its own code."""
        if copy_only:
            toggle = ""
        elif shown:
            toggle = _link("Hide", self._url(prefix, "s", item.id), "v-button--quiet")
        else:
            toggle = ('<button class="v-button v-button--quiet" type="submit" name="then" '
                      'value="show" data-action="show">Show</button>')
        return ('<form class="v-actions v-reveal" method="post" '
                f'action="{self._url(prefix, "s", item.id, "show")}">'
                + _hidden("code", code or "") + _hidden("field", str(index)) + toggle
                + '<button class="v-button v-button--copy" type="submit" name="then" value="copy" '
                  'data-action="copy">Copy</button></form>')

    def _existing_card(self, item: Item, index: int, field: Field,
                       draft: Mapping[str, str] | None) -> str:
        """One stored field on the edit form.

        A plain field's value is in its box and is saved if it comes back
        different. A hidden field's value is never on the form, only the dots
        the item's page shows: it is replaced only while Replace is ticked.
        Remove drops either, where the field has one. A refused form comes
        back with its boxes ticked as they were and the plain text as typed.
        """
        controls = self._stored_controls(index, field)
        draft = draft or {}
        heading = _text(self._profile.heading(item, field, False))
        remove = removed = ""
        if "remove" in controls:
            remove = ('<label class="v-choice v-choice--remove"><input class="v-remove-input" '
                      f'type="checkbox" name="{controls["remove"]}" value="1"'
                      f'{_checked(controls["remove"] in draft)}>'
                      '<span class="v-remove-off">Remove</span>'
                      '<span class="v-remove-on">Undo</span></label>')
            removed = '<p class="v-removed-note">Removed when you save.</p>'
        if self._profile.is_hidden(field):
            # the same dots whether or not the value is empty
            now = f'<span class="v-dots" aria-label="Hidden value">{_DOTS}</span>'
            body = (f'<div class="v-value-row">{now}<label class="v-choice"><input '
                    f'class="v-replace-input" type="checkbox" name="{controls["replace"]}" '
                    f'value="1"{_checked(controls["replace"] in draft)}>'
                    '<span class="v-replace-off">Replace'
                    '</span><span class="v-replace-on">Replacing</span></label></div>'
                    '<div class="v-replacement">'
                    + _field("New value", controls["value"], "", textarea=True) + "</div>")
        else:
            value = draft.get(controls["value"], field.value)
            body = _field("Value", controls["value"], value, textarea=True,
                          one_line="\n" not in _lf(value))
        return (f'<section class="v-panel v-stack v-existing" aria-label="{heading}">'
                f'<div class="v-heading"><h2>{heading}</h2>{remove}</div>'
                f'<div class="v-edit-body v-stack">{body}</div>'
                f"{removed}</section>")

    def _new_slots(self, item: Item | None, draft: Mapping[str, str] | None) -> str:
        """Where the form for `item` (None on the add form) takes new values."""
        slots = self._profile.slots
        if slots.boxes:
            return self._boxes(item, draft)
        if item is None:
            return self._free_slots(0, slots.defaults, draft, add_form=True)
        taken = [field.label for field in item.fields]
        return self._free_slots(len(taken),
                                _slot_labels(taken, slots.suggestions, len(slots.defaults)),
                                draft, add_form=False)

    def _boxes(self, item: Item | None, draft: Mapping[str, str] | None) -> str:
        """The fixed boxes the form offers, each under its caption.

        A refused form comes back with what was typed into a plain box; a
        box whose value the profile hides is always empty. Each box tells the
        page's script that it is fixed and, when it is required, the sentence
        that refuses it empty.
        """
        parts = []
        for index, box in self._offered(item):
            name = f"value{index}"
            typed = "" if self._hides(box) else (draft or {}).get(name, "")
            shape = " data-fixed-value" + (f' data-required="{_text(box.required)}"'
                                           if box.required else "")
            parts.append(_field(_text(box.caption), name, typed, textarea=True,
                                one_line=not box.multiline and "\n" not in _lf(typed),
                                extra=shape))
        if not parts:
            return ""
        return '<section class="v-panel v-stack">' + "".join(parts) + "</section>"

    def _free_slots(self, start: int, defaults: tuple[str, ...],
                    draft: Mapping[str, str] | None, *, add_form: bool) -> str:
        """One free slot for each of `defaults`, numbered from `start`.

        Each slot but the add form's first is opened by its own "Add another
        value" box (`add{j}`) and closed again by "Remove value", a second
        label of the same box; the add form's first is always open and carries
        its label inside "Reference label". A slot's value box is always empty,
        also on a refused form; its label and whether it is open come back as
        sent.
        """
        parts = []
        for offset, default in enumerate(defaults):
            index = start + offset
            controls = _new_controls(index)
            label = (draft or {}).get(controls["label"], default)
            value = _field("Value", controls["value"], "", textarea=True)
            labeler = (_field("Label", controls["label"], label)
                       + '<div class="v-picks" role="group" aria-label="Label suggestions" hidden>'
                       + "".join(f'<button class="v-button" type="button" data-label="{word}" '
                                 f'aria-pressed="false">{word}</button>'
                                 for word in self._profile.slots.suggestions)
                       + '</div><p class="v-help">Labels identify values in references, like '
                         '<span class="v-reference">Mail/username</span>.</p>')
            if add_form and offset == 0:
                parts.append(
                    '<section class="v-panel v-stack" data-new-value>'
                    + _hidden(controls["add"], "1") + value
                    + f'<details class="v-label-options"{_checked(label != default, "open")}>'
                      f'<summary>Reference label</summary><div class="v-stack">{labeler}</div>'
                      "</details></section>")
                continue
            parts.append(
                '<section class="v-slot" data-new-value><input class="v-slot-enable" '
                f'type="checkbox" id="{controls["add"]}" name="{controls["add"]}" value="1"'
                f'{_checked(controls["add"] in (draft or {}))}>'
                f'<label class="v-button v-slot-switch" for="{controls["add"]}">'
                '<span aria-hidden="true">+</span> Add another value</label>'
                '<div class="v-panel v-slot-body"><div class="v-heading"><h2>New value</h2>'
                f'<label class="v-button v-button--danger" for="{controls["add"]}">Remove value'
                f'</label></div><div class="v-stack">{labeler}{value}</div></div></section>')
        return "".join(parts)

    def _form_problem(self, problem: _Problem | None, draft: Mapping[str, str] | None,
                      item: Item | None) -> str:
        """The box above a form saying why it was refused; empty and hidden on a fresh form."""
        if problem is None:
            return ('<div class="v-notice v-notice--error" id="form-error" role="alert" '
                    'tabindex="-1" hidden></div>')
        sentence = "Fix the name below." if problem.on_name else problem.sentence
        cleared = f"<p>{_CLEARED}</p>" if draft and self._cleared(draft, item) else ""
        return ('<div class="v-notice v-notice--error" id="form-error" role="alert" tabindex="-1">'
                f'<p><strong>{self._words["not_saved"]}</strong></p>'
                f"<p>{_text(sentence)}</p>{cleared}</div>")

    def _url(self, prefix: str, *parts: str) -> str:
        """A link to one of the page's own paths: `prefix/<root>/` and `parts`.

        The parts are fixed words and item ids, never anything a person typed.
        """
        return f"{prefix}/{self._profile.root}/" + "/".join(parts)

    def _form_page(self, prefix: str, path: str) -> str:
        """The page whose form posts to `path`, for a form that has to be opened again."""
        if path == "/add":
            return self._url(prefix, "add")
        match = _SECRET_PATH.fullmatch(path)
        if not match:
            return self._url(prefix)
        item_id, action = match.groups()
        if action in ("/edit", "/delete"):
            return self._url(prefix, "s", item_id, action[1:])
        return self._url(prefix, "s", item_id)

    def _document(self, prefix: str, title: str, back: str, body: str, action: str = "", *,
                  form_page: bool = False) -> str:
        """A whole page: a top bar with a back link, the title (text) and `action` (HTML,
        one button or link at most), then the content.

        A form page ends with room to scroll its last box well clear of a
        phone's keyboard.
        """
        static = self._url(prefix, "static", "")
        main = "v-main v-form-page" if form_page else "v-main"
        return (
            "<!doctype html>"
            '<html lang="en"><head>'
            '<meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="referrer" content="same-origin">'
            f'<title>{_text(title)} · {self._words["product"]}</title>'
            f'<link rel="stylesheet" href="{static}ha.css">'
            f'<link rel="stylesheet" href="{static}vault.css">'
            f'<script src="{static}theme.js" defer></script>'
            f'<script src="{static}page.js" defer></script>'
            f"</head><body{self._script_words}>"
            f'<header class="v-header"><div class="v-bar"><a class="v-back" href="{back}" '
            f'aria-label="Back">{_BACK_ICON}</a><h1>{_text(title)}</h1>{action}</div></header>'
            f'<main class="{main}">{body}</main>'
            "</body></html>"
        )

    def _message(self, prefix: str, status: int, title: str, heading: str, sentence: str,
                 links: str, *, back: str | None = None,
                 extra: tuple[tuple[str, str], ...] = ()) -> Response:
        """A page that says one thing and where to go next; its back link leads to the list."""
        body = (f'<section class="v-panel v-empty"><h2>{heading}</h2><p>{sentence}</p>'
                f"{links}</section>")
        return _response(status, self._document(prefix, title, back or self._url(prefix), body),
                         extra=extra)

    def _not_rendered(self, prefix: str) -> Response:
        return self._refusal(prefix, 400, "This form was not sent by this page.")

    def _not_found(self, prefix: str) -> Response:
        return self._message(prefix, 404, "Not found", "Not found", self._words["not_found"],
                             _link(self._words["back_to_list"], self._url(prefix),
                                   "v-button--primary"))

    def _refusal(self, prefix: str, status: int, sentence: str) -> Response:
        """A refused POST: what happened, and the way back to the list."""
        return self._message(prefix, status, "Not done", "Nothing was changed", sentence,
                             _link(self._words["back_to_list"], self._url(prefix),
                                   "v-button--primary"))


def route_name(method: str, path: str) -> str:
    """The one word a log may say about a request: which of the page's routes it took.

    One of `list`, `secret`, `add`, `edit`, `delete`, `show` and `other`.
    The path itself is never logged: it carries a secret's id.
    """
    if method not in METHODS:
        return "other"
    if path == "/":
        return "list"
    if path == "/add":
        return "add"
    match = _SECRET_PATH.fullmatch(path)
    if not match:
        return "other"
    return (match.group(2) or "/secret")[1:]


def plain_response(status: int, title: str, sentence: str) -> Response:
    """An answer for a request refused before the page could read it: text, no links,
    and the headers every answer carries."""
    return _response(status, _plain(_text(title), _text(sentence)))


def _slot_labels(taken: list[str], suggestions: tuple[str, ...], count: int
                 ) -> tuple[str, ...]:
    """The labels `count` new slots of the edit form start with: each suggestion not in
    use, then `valueN`."""
    used, labels = set(taken), []
    for index in range(len(taken), len(taken) + count):
        label = next((word for word in suggestions if word not in used), None)
        number = index + 1
        while label is None:
            label = None if f"value{number}" in used else f"value{number}"
            number += 1
        used.add(label)
        labels.append(label)
    return tuple(labels)


def _lf(value: str) -> str:
    """A value with every line end made LF: CRLF first, then a lone CR. Nothing else changes.

    A browser sends every line break of a textarea, a lone CR included, as
    CRLF, so a stored value passed through this is what its untouched box
    comes back as.
    """
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _new_controls(index: int) -> dict[str, str]:
    """The controls of the free slot at `index`, by what they do."""
    return {"add": f"add{index}", "label": f"label{index}", "value": f"value{index}"}


def _unstorable(fields: list[Field], before: tuple[Field, ...]) -> str | None:
    """What is wrong when a new or changed value holds text the store cannot keep, else None."""
    for field in fields:
        if field not in before and _UNSTORABLE.search(field.value):
            return _VALUE_UNSTORABLE
    return None


def _forwarded_origin(headers: Mapping[str, str]) -> str | None:
    """The origin the browser sees, from the front proxy's forwarded headers."""
    host = headers.get("x-forwarded-host", "").split(",")[0].strip()
    if not host:
        return None
    proto = headers.get("x-forwarded-proto", "http").split(",")[0].strip().lower()
    return f"{proto}://{host}"


# The arrow of Home Assistant's own back buttons (Material Design's arrow-left).
_BACK_ICON = ('<svg viewBox="0 0 24 24" aria-hidden="true">'
              '<path d="M20 11v2H8l5.5 5.5-1.42 1.42L4.16 12l7.92-7.92L13.5 5.5 8 11h12z"/>'
              "</svg>")

# Attributes that keep a browser and its keyboard from remembering, correcting
# or suggesting what is typed.
_NO_HELP = 'autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"'

# The top bar's Save on a form page: a submit button of the form below it.
_SAVE_ACTION = ('<button class="v-button v-button--primary" type="submit" '
                'form="secret-form">Save</button>')


def _feedback(text: str = "") -> str:
    """The line under a value where copying and its failures are said."""
    return f'<p class="v-feedback" role="status" aria-live="polite">{text}</p>'


def _field(label: str, name: str, value: str, *, textarea: bool = False,
           one_line: bool = False, extra: str = "") -> str:
    """A labelled text box or text area; the label's text is fixed words, never data.

    `extra` is markup added to a text area's attributes.
    """
    if textarea:
        # The newline after the opening tag is dropped by every HTML parser, so a
        # value that itself starts with a newline keeps it.
        kind = "v-textarea v-textarea--line" if one_line else "v-textarea"
        rows = ' rows="1"' if one_line else ""
        box = (f'<textarea class="{kind}" id="f-{name}" name="{name}"{rows} {_NO_HELP}'
               f'{extra}>\n'
               f"{_text(value)}</textarea>")
    else:
        box = (f'<input class="v-input" type="text" id="f-{name}" name="{name}" '
               f'value="{_text(value)}" {_NO_HELP}>')
    return f'<label class="v-field" for="f-{name}">{label}{box}</label>'


def _form_end(cancel: str) -> str:
    """A form's last line: Cancel, then Save."""
    return ('<div class="v-actions v-actions--end">'
            + _link("Cancel", cancel, "v-button--quiet")
            + '<button class="v-button v-button--primary" type="submit">Save</button></div>')


def _link(words: str, href: str, kind: str = "") -> str:
    """A link drawn as a button; `words` are fixed words, never data."""
    classes = f"v-button {kind}".strip()
    return f'<a class="{classes}" href="{href}">{words}</a>'


def _checked(on: bool, word: str = "checked") -> str:
    return f" {word}" if on else ""


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{name}" value="{_text(value)}">'


def _when(moment: datetime) -> str:
    """A moment in this machine's local time, as `22 Nov 2026, 14:07`."""
    local = moment.astimezone()
    return f"{local.day} {_MONTHS[local.month - 1]} {local.year}, {local:%H:%M}"


def _text(text: str) -> str:
    return html.escape(text, quote=True)


def _pre(text: str) -> str:
    """A value as the content of a `<pre>`: every HTML parser drops one newline right after
    the opening tag, so one is put there and a value that starts with a newline keeps it."""
    return "\n" + _text(text)


def _plain(title: str, sentence: str) -> str:
    """A page for a request that cannot be trusted with links: nothing but text."""
    return ("<!doctype html>"
            '<html lang="en"><head><meta name="referrer" content="same-origin">'
            '<meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{title}</title></head>"
            f"<body><main><p>{sentence}</p></main></body></html>")


def _redirect(location: str) -> Response:
    return _response(303, "", extra=(("Location", location),))


def _response(status: int, page: str,
              extra: tuple[tuple[str, str], ...] = ()) -> Response:
    return Response(status, (("Content-Type", _HTML),) + _HEADERS + extra,
                    page.encode("utf-8"))
