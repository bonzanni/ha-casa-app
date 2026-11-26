"""The web page where a person sees and changes the secrets the built-in store keeps.

Everything here is a pure function of a request and the store: `Page.handle`
takes a `Request` and returns a `Response`, and whoever listens on a socket
passes them through. A secret field's value is never in what a GET returns,
only its plain fields are; it leaves only in the answer to a POST of a Show
or Copy form.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs

from vault_store import Field, Item, ItemNotFound, Store, StoreError

# Who the store records as the last writer of a secret changed on this page.
WRITER = "page"

# The largest form body the page reads, and the most fields a secret made
# or changed on the page may hold.
MAX_FORM = 256 * 1024
MAX_FIELDS = 40
# The most name=value pairs a form body is read with. The shortest name a form
# renders has four characters, so a pair of rendered names takes at least six
# bytes with its `=` and `&`: every form the page renders that fits in
# MAX_FORM fits under this, however many fields its secret has.
MAX_PAIRS = MAX_FORM // 5

# The methods the page answers; any other gets 405.
METHODS = ("GET", "HEAD", "POST")

# How long a form's code stays good, and how many codes of each kind are kept at once.
CODE_LIFETIME = 3600.0
MAX_CODES = 4096

# The tag and the title prefix Casa gives the item holding an agent's
# Telegram bot token: `own_voice_lifecycle.TOKEN_TAG` and
# `own_voice_lifecycle.item_title`. Copied rather than imported, so the page
# stays free of Casa's other modules; a test holds them equal.
BOT_TOKEN_TAG = "casa-agent-bot"
BOT_TOKEN_TITLE = "Telegram "
# The tag and the title prefix of a secret a person hands to a plugin:
# `vault_drop_off.TAG` and `vault_drop_off.item_title`.
DROP_OFF_TAG = "casa-drop-off"
DROP_OFF_TITLE = "Casa drop-off "

INGRESS_PREFIX = re.compile(r"(?:/[A-Za-z0-9._-]{1,128}){1,8}")

# An item id as the store makes it: a UUID in lower-case base32.
_ITEM_ID = r"[a-z2-7]{26}"
_SECRET_PATH = re.compile(rf"/s/({_ITEM_ID})(/edit|/delete|/show)?")
_INDEX = re.compile(r"[0-9]{1,4}")  # a field's position as a form sends it
_DROP_OFF = re.compile(re.escape(DROP_OFF_TITLE) + r"(\S+) .+", re.DOTALL)
# Text the store's file format cannot hold: control characters other than tab,
# line feed and carriage return, and the two non-characters U+FFFE and U+FFFF.
_UNSTORABLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")

_SSH_KEY = "SSH_KEY"
_ADDED_CATEGORY = "API_CREDENTIAL"
_NEW_ROWS = 3  # slots for new values on the add and edit forms
# The labels the add form's slots start with, and the ones offered on every slot.
_ADD_LABELS = ("credential", "password", "certificate")
_SUGGESTIONS = ("username", "password", "certificate")
# The plain fields of an SSH key shown as facts about the key rather than as values.
_SSH_FACTS = {"key type": "Type", "fingerprint": "Fingerprint"}
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
_NAME_MISSING = "Enter a name."
_NAME_TAKEN = "A secret named “{}” already exists. Choose another name."
_LABEL_MISSING = "Enter a label."
_LABEL_TWICE = "Each value needs a different label."
_VALUE_MISSING = "Enter a value."
_NEW_WITHOUT_VALUE = "Enter a value, or remove this value card."
_REPLACED_WITH_NOTHING = "Enter a value or turn off Replace."
_NO_FIELD_LEFT = "Keep at least one value, or delete the secret instead."
_TOO_MANY = f"A secret can hold at most {MAX_FIELDS} values."
_CLEARED = "The values you typed were cleared. Enter them again."
_NAME_UNSTORABLE = "The name holds a character that cannot be saved, such as a form feed."
_VALUE_UNSTORABLE = ("A value holds a character that cannot be saved, such as a form feed. "
                     "Remove it and try again.")


@dataclass(frozen=True)
class Request:
    """One request as the front proxy forwards it.

    `path` has the page's own `/vault` prefix already removed (`/`,
    `/s/<id>`, ...) and carries no query string; header names are lower case.
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
    dropped. The list hands out a Show code for every secret it copies
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
    """The secrets page over one store.

    `store` is None when the store could not be opened; every path then
    answers that the secrets are unavailable. `clock` measures how old a
    form is.
    """

    def __init__(self, store: Store | None, clock: Callable[[], float] = time.monotonic):
        self._store = store
        self.codes = Codes(clock)
        # Keys the edit form's stamp, which covers the secret's values: the
        # stamp tells whether the secret changed, and nothing about them.
        self._stamp_key = secrets.token_bytes(32)

    def handle(self, request: Request) -> Response:
        if request.method not in METHODS:
            return _response(405, _plain("Not allowed",
                                         "This page only answers GET, HEAD and POST."),
                             extra=(("Allow", ", ".join(METHODS)),))
        prefix = request.headers.get("x-ingress-path", "")
        if not INGRESS_PREFIX.fullmatch(prefix):
            return _response(403, _plain("Not available",
                                         "Open this page through Casa's web interface."))
        response = self._route(request, prefix)
        if request.method == "HEAD":
            return Response(response.status, response.headers, b"")
        return response

    def _route(self, request: Request, prefix: str) -> Response:
        if self._store is None:
            return _message(prefix, 503, "Secrets", "Secrets unavailable",
                            "The secret store could not be opened. Try again in a moment.",
                            _link("Try again", _url(prefix), "v-button--primary")
                            + _link("Back to Casa", f"{prefix}/", "v-button--quiet"),
                            back=f"{prefix}/")
        if request.method == "POST":
            return self._post(request, prefix)
        path = request.path
        if path == "/":
            return self._list(prefix)
        if path == "/add":
            return self._add_form(prefix)
        match = _SECRET_PATH.fullmatch(path)
        if not match:
            return _not_found(prefix)
        item_id, action = match.groups()
        if action == "/show":
            return _message(prefix, 405, "Not allowed", "Not allowed",
                            "Use the Show button on the secret's page.",
                            _link("Back to secrets", _url(prefix), "v-button--primary"),
                            extra=(("Allow", "POST"),))
        item = self._find(item_id)
        if item is None:
            return _not_found(prefix)
        if action is None:
            return self._one(prefix, item)
        if action == "/edit":
            if item.category == _SSH_KEY:
                return _not_found(prefix)
            return self._edit_form(prefix, item)
        return self._delete_form(prefix, item)

    def _find(self, item_id: str) -> Item | None:
        try:
            item = self._store.get(item_id)
        except StoreError:
            return None
        # The store also finds by title; the page finds by id only.
        return item if item.id == item_id else None

    # -- reading ------------------------------------------------------------------

    def _list(self, prefix: str) -> Response:
        items = sorted(self._store.list(), key=lambda item: item.title.casefold())
        add = _url(prefix, "add")
        if not items:
            body = ('<section class="v-panel v-empty"><h2>No secrets yet</h2>'
                    '<p class="v-help">Add a key, token or password for Casa to use.</p>'
                    + _link("Add secret", add, "v-button--primary") + "</section>")
            return _response(200, _document(prefix, "Secrets", f"{prefix}/", body))
        rows = []
        for item in items:
            link = _url(prefix, "s", item.id)
            # A secret with one value is copied from the list, through a Show
            # form of its own; the value itself is never on the list. Whether
            # that value is empty is not said before it is asked for.
            if item.category != _SSH_KEY and len(item.fields) == 1:
                action = _reveal_form(prefix, item, 0, self.codes.issue(f"/s/{item.id}/show"),
                                      copy_only=True)
            else:
                action = _link("Open", link, "v-button--quiet")
            rows.append(
                f'<li class="v-list-item" data-find="{_text(item.title.lower())}" '
                'data-value-block><div class="v-list-row">'
                f'<a class="v-list-link" href="{link}"><strong>{_text(item.title)}</strong>'
                f"<small>{_text(_summary(item))}</small></a>{action}</div>"
                f"{_feedback()}</li>")
        # Search filters the rows as the person types; only the script shows it.
        body = ('<div class="v-search" hidden><label class="v-sr-only" for="search">'
                'Search secrets</label><input class="v-input" id="search" type="search" '
                f'placeholder="Search secrets" {_NO_HELP}></div>'
                '<ul class="v-list">' + "".join(rows) + "</ul>"
                '<div class="v-empty" id="no-results" hidden><h2>No matching secrets</h2>'
                '<p class="v-help">Try another name.</p>'
                '<button class="v-button" type="button" data-clear-search>Clear search</button>'
                '</div><p class="v-sr-only" id="search-count" role="status"></p>')
        return _response(200, _document(prefix, "Secrets", f"{prefix}/", body,
                                         _link("Add", add, "v-button--primary")))

    def _one(self, prefix: str, item: Item, shown: int | None = None) -> Response:
        """One secret's page; `shown` is the index of the field whose value it shows."""
        ssh_key = item.category == _SSH_KEY
        single = not ssh_key and len(item.fields) == 1
        show_path = f"/s/{item.id}/show"
        body = ""
        if ssh_key:
            facts = "".join(
                f"<div><dt>{name}</dt>"
                f'<dd class="v-reference">{_text(field.value)}</dd></div>'
                for label, name in _SSH_FACTS.items()
                for field in item.fields if _is_fact(field) and field.label.lower() == label)
            body += ('<section class="v-panel v-stack"><div class="v-heading"><h2>SSH key</h2>'
                     f'</div><dl class="v-facts">{facts}</dl><p class="v-help">Created by Casa. '
                     "This key can be deleted, but not edited.</p></section>")
        for index, field in enumerate(item.fields):
            if ssh_key and _is_fact(field):
                continue
            code = self.codes.issue(show_path) if is_hidden(field) else None
            title = "Value" if single else (
                field.label[:1].upper() + field.label[1:] if ssh_key else field.label)
            body += _value_card(prefix, item, index, field, code, shown == index, title,
                                title_hidden=single)
        body += f'<p class="v-meta">{_text(_origin(item))} · Updated {_when(item.updated_at)}</p>'
        reads = self._store.reads(item.id)
        if reads:
            body += f'<p class="v-meta">{_reads(reads)}</p>'
        if not ssh_key:
            # A title holding "/" or "?" cannot be a reference segment; its id can.
            head = item.id if "/" in item.title or "?" in item.title else item.title
            body += ('<details class="v-label-options"><summary>References</summary>'
                     '<div class="v-stack">'
                     + "".join(f'<p class="v-reference">{_text(head)}/{_text(field.label)}</p>'
                               for field in item.fields)
                     + "</div></details>")
        body += ('<div class="v-danger-zone">'
                 + _link("Delete secret", _url(prefix, "s", item.id, "delete"), "v-button--danger")
                 + "</div>")
        action = "" if ssh_key else _link("Edit", _url(prefix, "s", item.id, "edit"),
                                          "v-button--quiet")
        return _response(200, _document(prefix, item.title, _url(prefix), body, action))

    # -- the forms ------------------------------------------------------------------

    def _add_form(self, prefix: str, draft: Mapping[str, str] | None = None,
                  problem: _Problem | None = None) -> Response:
        """The add form; `draft` is the refused form it shows again, without its values."""
        code = self.codes.issue("/add")
        name = (draft or {}).get("name", "")
        described = "name-help"
        invalid = ""
        error = ""
        if problem and problem.on_name:
            described += " name-error"
            invalid = ' aria-invalid="true"'
            error = f'<p class="v-field-error" id="name-error">{_text(problem.sentence)}</p>'
        body = (_form_problem(problem, draft, None)
                + f'<form class="v-form" id="secret-form" method="post" novalidate '
                  f'action="{_url(prefix, "add")}">'
                + _hidden("code", code)
                + '<label class="v-field" for="f-name">Name<input class="v-input" type="text" '
                  f'id="f-name" name="name" value="{_text(name)}" {_NO_HELP} '
                  f'aria-describedby="{described}"{invalid}></label>'
                + '<p class="v-help" id="name-help">Used by Casa and plugins. '
                  "Cannot be renamed.</p>"
                + error
                + _new_slots(0, _ADD_LABELS, draft, add_form=True)
                + _form_end(_url(prefix))
                + "</form>")
        return _response(200, _document(prefix, "Add secret", _url(prefix), body,
                                         _SAVE_ACTION, form_page=True))

    def _edit_form(self, prefix: str, item: Item, draft: Mapping[str, str] | None = None,
                   problem: _Problem | None = None) -> Response:
        """The edit form; `draft` is the refused form it shows again, without its values."""
        code = self.codes.issue(f"/s/{item.id}/edit")
        existing = "".join(_existing_card(index, field, draft)
                           for index, field in enumerate(item.fields))
        body = (f'<p class="v-subtitle v-name">{_text(item.title)}</p>'
                + _form_problem(problem, draft, item)
                + '<form class="v-form" id="secret-form" method="post" novalidate '
                  f'action="{_url(prefix, "s", item.id, "edit")}">'
                + _hidden("code", code)
                + _hidden("stamp", self._stamp(item))
                + existing
                + _new_slots(len(item.fields),
                             _slot_labels([field.label for field in item.fields]),
                             draft, add_form=False)
                + _form_end(_url(prefix, "s", item.id))
                + "</form>")
        return _response(200, _document(prefix, "Edit secret", _url(prefix, "s", item.id),
                                         body, _SAVE_ACTION, form_page=True))

    def _delete_form(self, prefix: str, item: Item) -> Response:
        code = self.codes.issue(f"/s/{item.id}/delete")
        if _plugin(item):
            warning = "A plugin uses this secret."
        elif item.writer != WRITER:
            warning = "Casa uses this secret."
        else:
            warning = ""
        if warning:
            warning = (f'<div class="v-notice v-notice--warning"><strong>{warning}</strong>'
                       "<p>Deleting it may stop the features that use it.</p></div>")
        body = (f'<section class="v-panel v-stack"><h2 class="v-title">'
                f"Delete {_text(item.title)}?</h2>"
                "<p>All its values will be deleted. This cannot be undone.</p>"
                + warning
                + f'<form class="v-actions" method="post" '
                  f'action="{_url(prefix, "s", item.id, "delete")}">'
                + _hidden("code", code)
                + _link("Cancel", _url(prefix, "s", item.id), "v-button--quiet")
                + '<button class="v-button v-button--delete" type="submit">Delete secret</button>'
                  "</form></section>")
        return _response(200, _document(prefix, "Delete secret",
                                         _url(prefix, "s", item.id), body))

    # -- changing -------------------------------------------------------------------

    def _post(self, request: Request, prefix: str) -> Response:
        refused, form = self._check(request, prefix)
        if refused is not None:
            return refused
        if request.path == "/add":
            return self._add(prefix, form)
        match = _SECRET_PATH.fullmatch(request.path)
        if not match or match.group(2) is None:
            return _not_found(prefix)
        item_id, action = match.groups()
        item = self._find(item_id)
        if item is None:
            return _not_found(prefix)
        if action == "/show":
            return self._show(request, prefix, item, form)
        if action == "/edit":
            if item.category == _SSH_KEY:
                return _not_found(prefix)
            return self._save(prefix, item, form)
        if not set(form) <= _form_names("delete"):
            return _not_rendered(prefix)
        try:
            self._store.delete(item.id)
        except ItemNotFound:
            return _not_found(prefix)
        return _redirect(_url(prefix))

    def _check(self, request: Request, prefix: str) -> tuple[Response | None, dict[str, str]]:
        """Refuse a POST that is not this page's own, unused, form.

        In this order: the origin (the `Origin` header, or without one an
        explicit `Sec-Fetch-Site: same-origin`), the body's type and size,
        the form (read strictly, each name at most once), and last the code,
        so a request refused earlier leaves its code unspent. Whether every
        name is one the form rendered depends on the secret, and is checked
        by each route once the code is spent.
        """
        headers = request.headers
        origin = headers.get("origin")
        if origin is not None:
            if origin != _forwarded_origin(headers):
                return _refusal(prefix, 403, "This request did not come from this page."), {}
        elif headers.get("sec-fetch-site") != "same-origin":
            return _refusal(prefix, 403, "This request did not come from this page."), {}
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != _FORM_TYPE:
            return _refusal(prefix, 403, "This request is not a form of this page."), {}
        if len(request.body) > MAX_FORM:
            return _refusal(prefix, 413, "This form is too large to save."), {}
        try:
            # Strict both times: a value is never stored with bytes replaced.
            pairs = parse_qs(request.body.decode("utf-8"), keep_blank_values=True,
                             max_num_fields=MAX_PAIRS, encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            return _refusal(prefix, 400, "This form could not be read."), {}
        if any(len(values) > 1 for values in pairs.values()):
            return _not_rendered(prefix), {}  # which one was meant is unknown
        form = {name: values[0] for name, values in pairs.items()}
        if not self.codes.take(form.get("code"), request.path):
            return _message(prefix, 403, "Form expired", "Form expired",
                            "Reload the page and try again. Nothing was changed.",
                            _link("Reload page", _form_page(prefix, request.path),
                                  "v-button--primary")), {}
        return None, form

    def _add(self, prefix: str, form: dict[str, str]) -> Response:
        if not set(form) <= _form_names("add"):
            return _not_rendered(prefix)
        name = form.get("name", "").strip()
        fields, problem = _new_fields(form, 0, add_form=True)

        def again(sentence: str, on_name: bool = False) -> Response:
            return self._add_form(prefix, form, _Problem(sentence, on_name))

        if not name:
            return again(_NAME_MISSING, on_name=True)
        if _UNSTORABLE.search(name):
            return again(_NAME_UNSTORABLE, on_name=True)
        if not problem and fields and fields[0].label and not fields[0].value:
            problem = _VALUE_MISSING  # the first value cannot be removed, only filled in
        problem = problem or _unstorable(fields, ()) or _emptied(fields, ())
        if problem:
            return again(problem)
        # A reference by name would match two secrets.
        if any(item.title == name for item in self._store.list()):
            return again(_NAME_TAKEN.format(name), on_name=True)
        try:
            item = self._store.create(title=name, category=_ADDED_CATEGORY, tags=[],
                                      fields=fields, writer=WRITER)
        except StoreError:  # the store's rule: one label, one field
            return again(_LABEL_TWICE)
        return _redirect(_url(prefix, "s", item.id))

    def _save(self, prefix: str, item: Item, form: dict[str, str]) -> Response:
        """Save the edit form: each stored field kept, replaced or removed, and new ones.

        The form names stored fields by position. Its stamp says what the
        secret held when the form was made, so a secret that changed since
        (and whose positions may have moved) is never overwritten.
        """
        stamp = form.get("stamp", "").encode("utf-8")
        if not hmac.compare_digest(stamp, self._stamp(item).encode("ascii")):
            return _message(prefix, 409, "Edit secret", "Secret not saved",
                            "This secret changed while you were editing. "
                            "Reload it before saving again.",
                            _link("Reload secret", _url(prefix, "s", item.id, "edit"),
                                  "v-button--primary"))
        if not set(form) <= _form_names("edit", item):
            return _not_rendered(prefix)
        fields = []
        for index, field in enumerate(item.fields):
            controls = _stored_controls(index, field)
            if controls["remove"] in form:
                continue
            submitted = form.get(controls["value"])
            if is_hidden(field):
                if controls["replace"] in form:
                    field = dataclasses.replace(field, value=_lf(submitted or ""))
            # An untouched box comes back with every line break as CRLF; the
            # stored bytes are kept unless the text itself changed.
            elif submitted is not None and _lf(submitted) != _lf(field.value):
                field = dataclasses.replace(field, value=_lf(submitted))
            fields.append(field)
        new, problem = _new_fields(form, len(item.fields), add_form=False)
        fields += new

        def again(sentence: str) -> Response:
            return self._edit_form(prefix, item, form, _Problem(sentence))

        problem = problem or _unstorable(fields, item.fields) or _emptied(fields, item.fields)
        if problem:
            return again(problem)
        if not fields:
            return again(_NO_FIELD_LEFT)
        if len(fields) > max(MAX_FIELDS, len(item.fields)):
            return again(_TOO_MANY)
        if fields == list(item.fields):  # nothing changed: the last writer stays
            return _redirect(_url(prefix, "s", item.id))
        try:
            self._store.replace(item.id, title=None, category=None, tags=None,
                                fields=fields, writer=WRITER)
        except ItemNotFound:
            return _not_found(prefix)
        except StoreError:  # the store's rule: one label, one field
            return again(_LABEL_TWICE)
        return _redirect(_url(prefix, "s", item.id))

    def _show(self, request: Request, prefix: str, item: Item,
              form: dict[str, str]) -> Response:
        if not set(form) <= _form_names("show"):
            return _not_rendered(prefix)
        index = form.get("field", "")
        if not (_INDEX.fullmatch(index) and int(index) < len(item.fields)):
            return _not_found(prefix)
        index = int(index)
        self._store.note_read(item.id)
        if "application/json" in request.headers.get("accept", ""):
            answer = {"value": item.fields[index].value,
                      "code": self.codes.issue(request.path)}
            return Response(200, (("Content-Type", "application/json"),) + _HEADERS,
                            json.dumps(answer).encode("utf-8"))
        return self._one(prefix, item, shown=index)

    def _stamp(self, item: Item) -> str:
        """A digest of everything the secret holds, keyed so it tells nothing about it.

        The store's change time alone has whole seconds, and two writes in
        one second would carry the same one.
        """
        content = json.dumps([
            item.updated_at.isoformat(), item.title, item.category, list(item.tags),
            item.writer,
            [[f.id, f.label, f.type, f.value, f.purpose] for f in item.fields],
        ])
        return hmac.new(self._stamp_key, content.encode("utf-8"), hashlib.sha256).hexdigest()


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


def purpose(item: Item) -> str:
    """Who keeps a secret and what for, in a few words a person recognises."""
    if BOT_TOKEN_TAG in item.tags:
        if item.title.startswith(BOT_TOKEN_TITLE) and len(item.title) > len(BOT_TOKEN_TITLE):
            role = item.title[len(BOT_TOKEN_TITLE):]
            # The title capitalises the role's first letter; roles are lower case.
            return f"Casa · Bot token for {role[:1].lower()}{role[1:]}"
        return "Casa · Bot token"
    handed_to = _plugin(item)
    if handed_to:
        return f"Plugin · {handed_to}"
    if item.category == _SSH_KEY:
        return "Casa · SSH key"
    if item.writer == WRITER:
        return "Added by you"
    return "Added by Casa"


def is_hidden(field: Field) -> bool:
    """Whether the field's value is kept off the page until asked for.

    Only a plain-text field (type `STRING`) is shown as it is; every other
    type, including ones the page has never heard of, is treated as secret.
    """
    return field.type != "STRING"


def _summary(item: Item) -> str:
    """A list row's second line: its values' labels when it has several, else its purpose."""
    if item.category != _SSH_KEY and len(item.fields) > 1:
        labels = ", ".join(field.label for field in item.fields)
        return f"{len(item.fields)} values · {labels}"
    return purpose(item)


def _plugin(item: Item) -> str | None:
    """The plugin a secret was handed to, for a drop-off."""
    if DROP_OFF_TAG in item.tags:
        match = _DROP_OFF.fullmatch(item.title)
        if match:
            return match.group(1)
    return None


def _origin(item: Item) -> str:
    if item.writer == WRITER:
        return "Added by you"
    return f"Added by {_plugin(item) or 'Casa'}"


def _is_fact(field: Field) -> bool:
    return not is_hidden(field) and field.label.lower() in _SSH_FACTS


def _slot_labels(taken: list[str]) -> tuple[str, ...]:
    """The labels the edit form's new slots start with: each suggestion not in use, then `valueN`."""
    used, labels = set(taken), []
    for index in range(len(taken), len(taken) + _NEW_ROWS):
        label = next((word for word in _SUGGESTIONS if word not in used), None)
        number = index + 1
        while label is None:
            label = None if f"value{number}" in used else f"value{number}"
            number += 1
        used.add(label)
        labels.append(label)
    return tuple(labels)


# -- reading a form ----------------------------------------------------------------

def _new_fields(form: Mapping[str, str], start: int, *, add_form: bool
                ) -> tuple[list[Field], str | None]:
    """The new values the form's open slots carry, numbered from `start`, and what is wrong.

    A slot is open when its `add` box came back; the add form's first slot
    always is. A closed slot's label and value are ignored, whatever they
    hold. Every value added on the page is a secret one.
    """
    fields, problem = [], None
    for offset in range(_NEW_ROWS):
        controls = _new_controls(start + offset)
        if not ((add_form and offset == 0) or controls["add"] in form):
            continue
        label = form.get(controls["label"], "").strip()
        if not label:
            problem = problem or _LABEL_MISSING
            continue
        fields.append(Field(id="", label=label, type="CONCEALED",
                            value=_lf(form.get(controls["value"], ""))))
    return fields, problem


def _lf(value: str) -> str:
    """A value with every line end made LF: CRLF first, then a lone CR. Nothing else changes.

    A browser sends every line break of a textarea, a lone CR included, as
    CRLF, so a stored value passed through this is what its untouched box
    comes back as.
    """
    return value.replace("\r\n", "\n").replace("\r", "\n")


# -- what each form contains ---------------------------------------------------------
#
# The renderers name their controls through these helpers, and a POST is
# accepted only with names from `_form_names`, so what a form shows and what
# its answer may carry come from one place. A browser leaves out an unticked
# box and a disabled control and nothing else, so a form's answer carries a
# subset of the names.

def _stored_controls(index: int, field: Field) -> dict[str, str]:
    """The controls of the stored field at `index` on the edit form, by what they do.

    A plain field's value comes back in its box; a secret one has a
    Replace box and an empty box for the new value. Either can be removed.
    """
    controls = {"value": f"value{index}", "remove": f"remove{index}"}
    if is_hidden(field):
        controls["replace"] = f"replace{index}"
    return controls


def _new_controls(index: int) -> dict[str, str]:
    """The controls of the new-value slot at `index`, by what they do."""
    return {"add": f"add{index}", "label": f"label{index}", "value": f"value{index}"}


def _form_names(form: str, item: Item | None = None) -> frozenset[str]:
    """Every control name the `form` ("add", "edit", "delete" or "show") renders for `item`."""
    if form == "show":
        return frozenset({"code", "field", "then"})
    if form == "delete":
        return frozenset({"code"})
    names = {"code"}
    if form == "add":
        names.add("name")
        start = 0
    else:
        names.add("stamp")
        for index, field in enumerate(item.fields):
            names.update(_stored_controls(index, field).values())
        start = len(item.fields)
    for index in range(start, start + _NEW_ROWS):
        names.update(_new_controls(index).values())
    return frozenset(names)


def _unstorable(fields: list[Field], before: tuple[Field, ...]) -> str | None:
    """What is wrong when a new or changed value holds text the store cannot keep, else None."""
    for field in fields:
        if field not in before and _UNSTORABLE.search(field.value):
            return _VALUE_UNSTORABLE
    return None


def _emptied(fields: list[Field], before: tuple[Field, ...]) -> str | None:
    """What is wrong when a secret field in the result has no value, else None.

    A secret field may be empty only if it was stored empty and left as it
    was; any other empty one is a slip that would lose a secret. Remove is
    how a field is dropped.
    """
    for field in fields:
        if is_hidden(field) and not field.value and field not in before:
            return _REPLACED_WITH_NOTHING if field.id else _NEW_WITHOUT_VALUE
    return None


def _cleared(form: Mapping[str, str], item: Item | None) -> bool:
    """Whether a refused form carried a value the page does not send back: a new or replacement one."""
    stored = item.fields if item else ()
    return any(form.get(f"value{index}") for index in range(len(stored) + _NEW_ROWS)
               if index >= len(stored) or is_hidden(stored[index]))


def _forwarded_origin(headers: Mapping[str, str]) -> str | None:
    """The origin the browser sees, from the front proxy's forwarded headers."""
    host = headers.get("x-forwarded-host", "").split(",")[0].strip()
    if not host:
        return None
    proto = headers.get("x-forwarded-proto", "http").split(",")[0].strip().lower()
    return f"{proto}://{host}"


# -- pieces of a page -----------------------------------------------------------

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


def _value_card(prefix: str, item: Item, index: int, field: Field, code: str | None,
                shown: bool, title: str, *, title_hidden: bool) -> str:
    """One value of a secret's page: its label, its value or dots, and Show and Copy.

    A secret value, empty or not, has the same dots and a form posting to
    Show, with a code of its own; only the answer says it is empty. Its
    Copy button posts the same form, which the page's script turns into a
    copy and which, without the script, shows the value. Once shown, Hide
    is a link back to the secret. A plain value is on the page already, so
    its Copy button needs no form, and only the script shows it.
    """
    hidden = is_hidden(field)
    heading = (f'<h2 class="v-sr-only">{_text(title)}</h2>' if title_hidden
               else f"<h2>{_text(title)}</h2>")
    feedback = _feedback()
    if not hidden and not field.value:
        value, controls = '<span class="v-help">Empty</span>', ""
    elif not hidden:
        value = f'<pre class="v-value" data-plain>{_pre(field.value)}</pre>'
        controls = ('<div class="v-actions"><button class="v-button v-button--copy" type="button" '
                    'data-action="copy-plain" hidden>Copy</button></div>'
                    f'<noscript><p class="v-help">{_HOLD}</p></noscript>')
    elif shown and not field.value:
        value = '<pre class="v-value v-revealed" data-empty>Empty</pre>'
        controls = _reveal_form(prefix, item, index, code, shown=True)
    elif shown:
        value = f'<pre class="v-value v-revealed">{_pre(field.value)}</pre>'
        controls = _reveal_form(prefix, item, index, code, shown=True)
        feedback = _feedback(_HOLD)
    else:
        value = f'<span class="v-dots" aria-label="Hidden value">{_DOTS}</span>'
        controls = _reveal_form(prefix, item, index, code)
    return (f'<section class="v-panel v-value-card" data-value-block '
            f'aria-label="{_text(title)}">{heading}'
            f'<div class="v-value-row">{value}{controls}</div>{feedback}</section>')


def _reveal_form(prefix: str, item: Item, index: int, code: str | None, *,
                 shown: bool = False, copy_only: bool = False) -> str:
    """The form posting one hidden value's Show or Copy, with its own code."""
    if copy_only:
        toggle = ""
    elif shown:
        toggle = _link("Hide", _url(prefix, "s", item.id), "v-button--quiet")
    else:
        toggle = ('<button class="v-button v-button--quiet" type="submit" name="then" '
                  'value="show" data-action="show">Show</button>')
    return ('<form class="v-actions v-reveal" method="post" '
            f'action="{_url(prefix, "s", item.id, "show")}">'
            + _hidden("code", code or "") + _hidden("field", str(index)) + toggle
            + '<button class="v-button v-button--copy" type="submit" name="then" value="copy" '
              'data-action="copy">Copy</button></form>')


def _existing_card(index: int, field: Field, draft: Mapping[str, str] | None) -> str:
    """One stored field on the edit form.

    A plain field's value is in its box and is saved if it comes back
    different. A secret field's value is never on the form, only the dots
    the secret's page shows: it is replaced only while Replace is ticked.
    Remove drops either. A refused form comes back with its boxes ticked as
    they were and the plain text as typed.
    """
    controls = _stored_controls(index, field)
    draft = draft or {}
    remove = ('<label class="v-choice v-choice--remove"><input class="v-remove-input" '
              f'type="checkbox" name="{controls["remove"]}" value="1"'
              f'{_checked(controls["remove"] in draft)}><span class="v-remove-off">Remove</span>'
              '<span class="v-remove-on">Undo</span></label>')
    if is_hidden(field):
        # the same dots whether or not the value is empty
        now = f'<span class="v-dots" aria-label="Hidden value">{_DOTS}</span>'
        body = (f'<div class="v-value-row">{now}<label class="v-choice"><input '
                f'class="v-replace-input" type="checkbox" name="{controls["replace"]}" value="1"'
                f'{_checked(controls["replace"] in draft)}><span class="v-replace-off">Replace'
                '</span><span class="v-replace-on">Replacing</span></label></div>'
                '<div class="v-replacement">'
                + _field("New value", controls["value"], "", textarea=True) + "</div>")
    else:
        value = draft.get(controls["value"], field.value)
        body = _field("Value", controls["value"], value, textarea=True,
                      one_line="\n" not in _lf(value))
    return (f'<section class="v-panel v-stack v-existing" aria-label="{_text(field.label)}">'
            f'<div class="v-heading"><h2>{_text(field.label)}</h2>{remove}</div>'
            f'<div class="v-edit-body v-stack">{body}</div>'
            '<p class="v-removed-note">Removed when you save.</p></section>')


def _new_slots(start: int, defaults: tuple[str, ...], draft: Mapping[str, str] | None, *,
               add_form: bool) -> str:
    """The `_NEW_ROWS` slots for new values, numbered from `start`.

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
                             f'aria-pressed="false">{word}</button>' for word in _SUGGESTIONS)
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


def _form_problem(problem: _Problem | None, draft: Mapping[str, str] | None,
                  item: Item | None) -> str:
    """The box above a form saying why it was refused; empty and hidden on a fresh form."""
    if problem is None:
        return ('<div class="v-notice v-notice--error" id="form-error" role="alert" '
                'tabindex="-1" hidden></div>')
    sentence = "Fix the name below." if problem.on_name else problem.sentence
    cleared = f"<p>{_CLEARED}</p>" if draft and _cleared(draft, item) else ""
    return ('<div class="v-notice v-notice--error" id="form-error" role="alert" tabindex="-1">'
            f"<p><strong>Secret not saved</strong></p><p>{_text(sentence)}</p>{cleared}</div>")


def _field(label: str, name: str, value: str, *, textarea: bool = False,
           one_line: bool = False) -> str:
    """A labelled text box or text area; the label's text is fixed words, never data."""
    if textarea:
        # The newline after the opening tag is dropped by every HTML parser, so a
        # value that itself starts with a newline keeps it.
        kind = "v-textarea v-textarea--line" if one_line else "v-textarea"
        rows = ' rows="1"' if one_line else ""
        box = (f'<textarea class="{kind}" id="f-{name}" name="{name}"{rows} {_NO_HELP}>\n'
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


def _reads(count: int) -> str:
    if count == 1:
        return "Read once since Casa started"
    return f"Read {count} times since Casa started"


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


def _url(prefix: str, *parts: str) -> str:
    """A link to one of the page's own paths: `prefix/vault/` and `parts`.

    The parts are fixed words and item ids, never anything a person typed.
    """
    return f"{prefix}/vault/" + "/".join(parts)


def _form_page(prefix: str, path: str) -> str:
    """The page whose form posts to `path`, for a form that has to be opened again."""
    if path == "/add":
        return _url(prefix, "add")
    match = _SECRET_PATH.fullmatch(path)
    if not match:
        return _url(prefix)
    item_id, action = match.groups()
    if action in ("/edit", "/delete"):
        return _url(prefix, "s", item_id, action[1:])
    return _url(prefix, "s", item_id)


def _document(prefix: str, title: str, back: str, body: str, action: str = "", *,
              form_page: bool = False) -> str:
    """A whole page: a top bar with a back link, the title (text) and `action` (HTML,
    one button or link at most), then the content.

    A form page ends with room to scroll its last box well clear of a
    phone's keyboard.
    """
    static = f"{prefix}/vault/static/"
    main = "v-main v-form-page" if form_page else "v-main"
    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="same-origin">'
        f"<title>{_text(title)} · Casa</title>"
        f'<link rel="stylesheet" href="{static}ha.css">'
        f'<link rel="stylesheet" href="{static}vault.css">'
        f'<script src="{static}theme.js" defer></script>'
        f'<script src="{static}page.js" defer></script>'
        "</head><body>"
        f'<header class="v-header"><div class="v-bar"><a class="v-back" href="{back}" '
        f'aria-label="Back">{_BACK_ICON}</a><h1>{_text(title)}</h1>{action}</div></header>'
        f'<main class="{main}">{body}</main>'
        "</body></html>"
    )


def _plain(title: str, sentence: str) -> str:
    """A page for a request that cannot be trusted with links: nothing but text."""
    return ("<!doctype html>"
            '<html lang="en"><head><meta name="referrer" content="same-origin">'
            '<meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{title}</title></head>"
            f"<body><main><p>{sentence}</p></main></body></html>")


def _message(prefix: str, status: int, title: str, heading: str, sentence: str, links: str, *,
             back: str | None = None, extra: tuple[tuple[str, str], ...] = ()) -> Response:
    """A page that says one thing and where to go next; its back link leads to the list."""
    body = (f'<section class="v-panel v-empty"><h2>{heading}</h2><p>{sentence}</p>'
            f"{links}</section>")
    return _response(status, _document(prefix, title, back or _url(prefix), body), extra=extra)


def _not_rendered(prefix: str) -> Response:
    return _refusal(prefix, 400, "This form was not sent by this page.")


def _not_found(prefix: str) -> Response:
    return _message(prefix, 404, "Not found", "Not found", "There is no such secret or page.",
                    _link("Back to secrets", _url(prefix), "v-button--primary"))


def _refusal(prefix: str, status: int, sentence: str) -> Response:
    """A refused POST: what happened, and the way back to the list."""
    return _message(prefix, status, "Not done", "Nothing was changed", sentence,
                    _link("Back to secrets", _url(prefix), "v-button--primary"))


def _redirect(location: str) -> Response:
    return _response(303, "", extra=(("Location", location),))


def _response(status: int, page: str,
              extra: tuple[tuple[str, str], ...] = ()) -> Response:
    return Response(status, (("Content-Type", _HTML),) + _HEADERS + extra,
                    page.encode("utf-8"))
