"""The secrets page, rendered from a real store: what a person sees, changes, and what never leaves.

What these tests protect, in order of cost: a secret field's value is never
in a page a GET returns, and leaves only in the answer to a Show posted with
a fresh single-use code; nothing is written by a request from another page,
a form used twice or a form that expired; a save never overwrites a change
made while its form was open, and never alters a value it did not mean to;
nothing from the store or the request is read by the browser as markup or
lands in a link; every answer carries the headers that keep it out of caches
and keep foreign script off it; and a person can tell each secret apart by
its name and what it is for.
"""

import json
import random
import re
import sys
import threading
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode

import pytest

import own_voice_lifecycle
import vault_drop_off
import vault_op
import vault_page
from vault_page import Page, Request
from vault_store import Field, Item, Store

PREFIX = "/api/hassio_ingress/abc"
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy":
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'self'; base-uri 'none'",
}
# Every link a page may carry: its own paths under the prefix, and the
# prefix's root, where the list's back link leads.
LINK = re.compile(re.escape(PREFIX) + r"/(vault/(|add|s/[a-z2-7]{26}(/edit|/delete|/show)?"
                  r"|static/(ha\.css|vault\.css|theme\.js|page\.js)))?")
LINKS = re.compile(r'\s(?:href|action|src)="([^"]*)"')  # not data-action


@pytest.fixture
def store(tmp_path):
    return Store.open(tmp_path, None)


def get(page, path, method="GET"):
    return page.handle(Request(method=method, path=path,
                               headers={"x-ingress-path": PREFIX}, body=b""))


def text(response):
    return response.body.decode("utf-8")


def make(store, title, fields, *, tags=(), category="PASSWORD", writer="op"):
    return store.create(title=title, category=category, tags=list(tags),
                        fields=fields, writer=writer)


def secret(label, value):
    return Field(id="", label=label, type="CONCEALED", value=value)


def plain(label, value):
    return Field(id="", label=label, type="STRING", value=value)


def fixtures(store):
    """Three secrets whose names, labels and values are lower-case letters and digits."""
    bot = make(store, own_voice_lifecycle.item_title("alex"),
               [secret("password", "bottoken77")], tags=[own_voice_lifecycle.TOKEN_TAG])
    drop = make(store, vault_drop_off.item_title("finance", "bank-key"),
                [secret("password", "bankkey88")], tags=[vault_drop_off.TAG])
    mine = make(store, "wifi", [plain("network", "homenet5"), secret("password", "wifipass99")],
                writer=vault_page.WRITER)
    return [bot, drop, mine]


def ssh_key(store):
    code, _, err = vault_op.run(store, "Casa", [
        "item", "create", "--category", "ssh", "--title", "k", "--vault", "Casa",
        "--tags", "t", "--ssh-generate-key", "rsa,2048"], None)
    assert (code, err) == (0, "")
    return store.get("k")


def item(title="t", category="PASSWORD", tags=(), writer="op"):
    moment = datetime(2026, 11, 22, 13, 7, tzinfo=timezone.utc)
    return Item(id="a" * 26, title=title, category=category, tags=tuple(tags),
                created_at=moment, updated_at=moment, writer=writer, fields=())


# -- who is answered ------------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, {"x-ingress-path": '/a"b'}], ids=["none", "quote"])
def test_a_request_without_a_valid_ingress_prefix_is_refused(store, headers):
    items = fixtures(store)
    page = Page(store)
    for path in ("/", f"/s/{items[0].id}"):
        response = page.handle(Request("GET", path, headers, b""))
        assert response.status == 403
        for one in items:
            assert one.title not in text(response)


# -- the list ---------------------------------------------------------------------

def test_the_list_names_each_secret_with_its_purpose_and_no_value(store):
    items = fixtures(store)
    response = get(Page(store), "/")
    body = text(response)
    assert response.status == 200
    for one in items:
        assert one.title in body
    assert "Casa \u00b7 Bot token for alex" in body
    assert "Plugin \u00b7 finance" in body
    assert "2 values \u00b7 network, password" in body
    for value in ("bottoken77", "bankkey88", "wifipass99", "homenet5"):
        assert value not in body


def test_the_list_is_in_name_order_and_each_row_links_to_its_secret(store):
    later = make(store, "Zebra", [secret("password", "z1")])
    first = make(store, "apple", [secret("password", "a1")])
    middle = make(store, "Mango", [secret("password", "m1")])
    body = text(get(Page(store), "/"))
    rows = re.findall(r'<a class="v-list-link" href="([^"]+)"', body)
    assert rows == [f"{PREFIX}/vault/s/{one.id}" for one in (first, middle, later)]
    assert 'data-find="mango"' in body
    # the search box is there only when the script shows it, and nothing sends it
    assert '<div class="v-search" hidden>' in body
    assert not re.search(r'<input[^>]*type="search"[^>]*name=', body)
    assert "Search</button>" not in body
    assert (f'<a class="v-button v-button--primary" href="{PREFIX}/vault/add">Add</a>'
            "</div></header>") in body
    assert "No secrets yet" not in body and "backups" not in body


def test_an_empty_list_says_so_and_offers_to_add(store):
    body = text(get(Page(store), "/"))
    assert ('<section class="v-panel v-empty"><h2>No secrets yet</h2>'
            '<p class="v-help">Add a key, token or password for Casa to use.</p>'
            f'<a class="v-button v-button--primary" href="{PREFIX}/vault/add">Add secret</a>'
            "</section>") in body
    # no search box and no second Add on an empty list
    assert 'type="search"' not in body and ">Add</a>" not in body
    assert "backups" not in body


@pytest.mark.parametrize("given, expected", [
    (item(own_voice_lifecycle.item_title("alex"), tags=[own_voice_lifecycle.TOKEN_TAG]),
     "Casa \u00b7 Bot token for alex"),
    (item(own_voice_lifecycle.item_title("finance"), tags=[own_voice_lifecycle.TOKEN_TAG]),
     "Casa \u00b7 Bot token for finance"),
    (item("some token", tags=[own_voice_lifecycle.TOKEN_TAG]), "Casa \u00b7 Bot token"),
    (item(own_voice_lifecycle.item_title(""), tags=[own_voice_lifecycle.TOKEN_TAG]),
     "Casa \u00b7 Bot token"),
    (item(vault_drop_off.item_title("finance", "bank-key"), tags=[vault_drop_off.TAG]),
     "Plugin \u00b7 finance"),
    (item("Casa drop-off finance", tags=[vault_drop_off.TAG]), "Added by Casa"),
    (item("k", category="SSH_KEY"), "Casa \u00b7 SSH key"),
    (item("wifi", writer=vault_page.WRITER), "Added by you"),
    (item("Gmail"), "Added by Casa"),
    (item(own_voice_lifecycle.item_title("alex")), "Added by Casa"),  # no tag, no claim
])
def test_purpose_lines(given, expected):
    assert vault_page.purpose(given) == expected


def test_the_copied_tags_and_titles_match_the_modules_that_make_them():
    assert vault_page.BOT_TOKEN_TAG == own_voice_lifecycle.TOKEN_TAG
    assert vault_page.BOT_TOKEN_TITLE == own_voice_lifecycle.item_title("")
    assert vault_page.DROP_OFF_TAG == vault_drop_off.TAG
    assert (vault_drop_off.item_title("plugin", "name")
            == vault_page.DROP_OFF_TITLE + "plugin name")


# -- one secret -------------------------------------------------------------------

def test_one_secret_shows_plain_fields_and_masks_secret_ones(store):
    one = make(store, "router", [
        plain("username", "admin7"), secret("password", "hunter22"),
        Field(id="", label="code", type="OTP", value="otpseed33"),
        Field(id="", label="pin", type="concealed", value="lowerpin44")])
    store.note_read(one.id)
    store.note_read(one.id)
    response = get(Page(store), f"/s/{one.id}")
    body = text(response)
    assert response.status == 200
    assert "admin7" in body
    for value in ("hunter22", "otpseed33", "lowerpin44"):
        assert value not in body
    for label in ("username", "password", "code", "pin"):
        assert f"<h2>{label}</h2>" in body
        assert f'<p class="v-reference">router/{label}</p>' in body
    assert body.count(f'<span class="v-dots" aria-label="Hidden value">{vault_page._DOTS}'
                      "</span>") == 3
    assert "\u00b7 secret" not in body
    assert '<p class="v-meta">Added by Casa \u00b7 Updated ' in body
    assert '<p class="v-meta">Read 2 times since Casa started</p>' in body
    assert (f'<a class="v-button v-button--quiet" href="{PREFIX}/vault/s/{one.id}/edit">Edit</a>'
            "</div></header>") in body
    assert (f'<div class="v-danger-zone"><a class="v-button v-button--danger" '
            f'href="{PREFIX}/vault/s/{one.id}/delete">Delete secret</a></div>') in body
    assert "<footer" not in body


def test_only_the_exact_type_string_is_shown_as_it_is(store):
    """A type is matched exactly: `string` in lower case is a type the page does not know, and
    is hidden like any other."""
    one = make(store, "router", [plain("username", "admin7"),
                                 Field(id="", label="note", type="string", value=SENTINEL)])
    page = Page(store)
    for path in ("/", f"/s/{one.id}", f"/s/{one.id}/edit"):
        body = text(get(page, path))
        assert SENTINEL not in body, path
    assert "admin7" in text(get(page, f"/s/{one.id}"))


@pytest.mark.parametrize("count, words", [
    (1, "Read once since Casa started"),
    (3, "Read 3 times since Casa started"),
])
def test_the_read_count_is_said_in_words(store, count, words):
    one = make(store, "router", [secret("password", "hunter22")])
    for _ in range(count):
        store.note_read(one.id)
    assert words in text(get(Page(store), f"/s/{one.id}"))


def test_no_reads_line_while_nothing_was_read(store):
    one = make(store, "router", [secret("password", "hunter22")])
    body = text(get(Page(store), f"/s/{one.id}"))
    assert "since Casa started" not in body
    assert body.count('<p class="v-meta">') == 1
    assert re.search(r'<p class="v-meta">Added by Casa \u00b7 Updated [^<]+</p>', body)


def test_a_secret_made_on_the_page_says_so_and_a_label_is_only_its_label(store):
    one = make(store, "cert", [secret("pem", "line1\nline2\nline3\n"),
                               plain("notes", "a\nb")], writer=vault_page.WRITER)
    body = text(get(Page(store), f"/s/{one.id}"))
    assert '<p class="v-meta">Added by you \u00b7 Updated ' in body
    assert "<h2>pem</h2>" in body and "<h2>notes</h2>" in body
    assert "lines" not in body
    assert '<pre class="v-value" data-plain>\na\nb</pre>' in body
    assert "line1" not in body


def test_a_secret_with_one_value_shows_no_label_but_names_it_in_its_reference(store):
    one = make(store, "GitHub", [secret("credential", SENTINEL)], writer=vault_page.WRITER)
    body = text(get(Page(store), f"/s/{one.id}"))
    assert '<h2 class="v-sr-only">Value</h2>' in body
    assert "<h2>credential</h2>" not in body
    assert '<p class="v-reference">GitHub/credential</p>' in body
    assert SENTINEL not in body


@pytest.mark.parametrize("title", ["Home/WiFi", "Q?A"])
def test_a_name_no_reference_can_hold_is_referred_to_by_its_id(store, title):
    one = make(store, title, [secret("password", SENTINEL)], writer=vault_page.WRITER)
    body = text(get(Page(store), f"/s/{one.id}"))
    assert f'<p class="v-reference">{one.id}/password</p>' in body
    assert 'class="v-reference">' + title not in body


def test_an_ssh_key_shows_its_public_parts_and_no_edit_link(store):
    key = ssh_key(store)
    values = {field.label: field.value for field in key.fields}
    body = text(get(Page(store), f"/s/{key.id}"))
    assert values["public key"] in body
    assert values["fingerprint"] in body
    assert values["private key"] not in body
    # The key's first lines encode the modulus, which the public key holds
    # too; its last lines are private numbers only.
    for line in values["private key"].splitlines()[-4:-1]:
        assert line not in body
    assert "BEGIN PRIVATE" not in body
    assert f"/s/{key.id}/edit" not in body
    assert ">Edit</a>" not in body and "References" not in body
    assert body.endswith(f'<div class="v-danger-zone"><a class="v-button v-button--danger" '
                         f'href="{PREFIX}/vault/s/{key.id}/delete">Delete secret</a></div>'
                         "</main></body></html>")
    # one key: its type and fingerprint as facts, then its public and private parts
    assert "<h2>SSH key</h2>" in body
    assert f'<dt>Fingerprint</dt><dd class="v-reference">{values["fingerprint"]}</dd>' in body
    assert '<dt>Type</dt><dd class="v-reference">rsa</dd>' in body
    assert "This key can be deleted, but not edited." in body
    assert "<h2>Public key</h2>" in body and "<h2>Private key</h2>" in body
    assert "<h2>Fingerprint</h2>" not in body


def test_markup_in_a_name_or_value_is_shown_as_text(store):
    one = make(store, "<b>x</b>", [plain("note", '"><script>alert(1)</script>'),
                                   plain("amp", "&amp;")])
    for path in ("/", f"/s/{one.id}"):
        body = text(get(Page(store), path))
        assert "&lt;b&gt;" in body
        assert "<b>x" not in body
        assert "<script>alert" not in body
    body = text(get(Page(store), f"/s/{one.id}"))
    assert "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "&amp;amp;" in body


# -- every answer -----------------------------------------------------------------

def test_every_response_carries_the_headers(store):
    one = make(store, "router", [secret("password", "hunter22")])
    page = Page(store)
    responses = [get(page, "/"), get(page, f"/s/{one.id}"), get(page, "/nowhere"),
                 page.handle(Request("GET", "/", {}, b"")),
                 get(page, "/", method="DELETE"), get(page, "/", method="HEAD"),
                 get(Page(None), "/")]
    assert [r.status for r in responses] == [200, 200, 404, 403, 405, 200, 503]
    for response in responses:
        headers = dict(response.headers)
        assert len(headers) == len(response.headers)  # no header twice
        for name, value in HEADERS.items():
            assert headers[name] == value
        assert headers["Content-Type"] == "text/html; charset=utf-8"


def pages(store):
    """Every page a GET reaches, after the fixtures and an SSH key are stored."""
    items = fixtures(store) + [ssh_key(store)]
    page = Page(store)
    paths = ["/", "/nowhere", "/s/" + "a" * 26] + [f"/s/{one.id}" for one in items]
    return items, [text(get(page, path)) for path in paths]


def test_no_page_has_inline_script_or_style(store):
    _, bodies = pages(store)
    bodies.append(text(get(Page(None), "/")))
    for body in bodies:
        assert "style=" not in body
        assert "<style" not in body
        assert not re.search(r"<script[^>]*>[^<]", body)
        assert not re.search(r" on[a-z]+=", body)


def test_links_are_under_the_ingress_prefix_and_carry_nothing_of_a_secret(store):
    items, bodies = pages(store)
    words = set()
    for one in items:
        words.add(one.title)
        for field in one.fields:
            words.update((field.label, field.value))
    words = {word for word in words if re.fullmatch(r"[a-z0-9]+", word)}
    assert {"bottoken77", "wifipass99", "homenet5", "network"} <= words
    for body in bodies:
        links = LINKS.findall(body)
        assert links
        for link in links:
            assert LINK.fullmatch(link), link
            # Ids are random base32 and may hold a short word by chance.
            rest = re.sub(r"[a-z2-7]{26}", "", link.removeprefix(PREFIX))
            for word in words:
                assert word not in rest, (word, link)


def test_head_is_get_without_a_body(store):
    page = Page(store)
    head, plain_get = get(page, "/", method="HEAD"), get(page, "/")
    assert (head.status, head.headers, head.body) == (plain_get.status, plain_get.headers, b"")


def test_an_unknown_secret_is_a_404_page(store):
    make(store, "router", [secret("password", "hunter22")])
    for path in ("/s/" + "a" * 26, "/s/router", "/s/ROUTER", "/s/"):
        response = get(Page(store), path)
        assert response.status == 404
        assert "hunter22" not in text(response)
        assert "router" not in text(response)


def test_a_method_other_than_get_head_post_is_405(store):
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
        response = get(Page(store), "/", method=method)
        assert response.status == 405
        assert dict(response.headers)["Allow"] == "GET, HEAD, POST"


def test_when_the_store_cannot_be_opened_every_path_says_so():
    page = Page(None)
    for path in ("/", "/s/" + "a" * 26, "/add", "/nowhere"):
        response = get(page, path)
        assert response.status == 503
        body = text(response)
        assert "<h2>Secrets unavailable</h2>" in body
        assert "The secret store could not be opened. Try again in a moment." in body
        assert f'<a class="v-button v-button--primary" href="{PREFIX}/vault/">Try again</a>' in body
        assert f'<a class="v-button v-button--quiet" href="{PREFIX}/">Back to Casa</a>' in body
        assert "<form" not in body and "<input" not in body


# -- changing things ----------------------------------------------------------------

ORIGIN = "https://ha.example"
SENTINEL = "sentinel5551"


def post(page, path, form, *, origin=ORIGIN, accept=None, headers=None,
         content_type="application/x-www-form-urlencoded", body=None):
    sent = {"x-ingress-path": PREFIX, "x-forwarded-proto": "https",
            "x-forwarded-host": "ha.example", "content-type": content_type}
    if origin is not None:
        sent["origin"] = origin
    if accept is not None:
        sent["accept"] = accept
    sent.update(headers or {})
    if body is None:
        body = urlencode(form).encode("utf-8")
    return page.handle(Request("POST", path, sent, body))


def form_of(page, get_path, action=None, nth=0):
    """The hidden inputs (code, stamp, field) of a form on the page GET_PATH answers."""
    body = text(get(page, get_path))
    target = PREFIX + "/vault" + (action or get_path)
    forms = re.findall(r'<form[^>]*action="' + re.escape(target) + r'"[^>]*>(.*?)</form>',
                       body, re.DOTALL)
    return dict(re.findall(r'<input type="hidden" name="([a-z0-9]+)" value="([^"]*)">',
                           forms[nth]))


class _Form(HTMLParser):
    """The controls of the form posting to ACTION in a page, as a browser without the script
    reads them: `controls` every named input and text area as (name, kind, value, checked),
    `buttons` the names of its submit buttons."""

    def __init__(self, body, action):
        super().__init__(convert_charrefs=True)
        self.action, self.inside, self.box = PREFIX + "/vault" + action, False, None
        self.controls, self.buttons = [], []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = {name: value or "" for name, value in attrs}
        if tag == "form":
            self.inside = attrs.get("action") == self.action
        elif not self.inside or "name" not in attrs:
            return
        elif tag == "input":
            self.controls.append([attrs["name"], attrs.get("type", "text"),
                                  attrs.get("value", ""), "checked" in attrs])
        elif tag == "textarea":
            self.box = [attrs["name"], "textarea", "", False]
        elif tag == "button":
            self.buttons.append(attrs["name"])

    def handle_data(self, data):
        if self.box is not None:
            self.box[2] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "textarea" and self.box is not None:
            # the newline after the opening tag is the parser's; a browser sends line ends as CRLF
            self.box[2] = self.box[2].removeprefix("\n").replace("\n", "\r\n")
            self.controls.append(self.box)
            self.box = None

    def sent(self, **changes):
        """The pairs a submit sends, with CHANGES typed into the named boxes."""
        pairs = [(name, changes.get(name, value)) for name, kind, value, checked in self.controls
                 if kind != "checkbox" or checked]
        return urlencode(pairs).encode("utf-8")


def snapshot(store):
    return [(i.id, i.title, i.writer, i.updated_at, i.fields) for i in store.list()]


def values_of(one):
    return {field.label: field for field in one.fields}


def test_adding_a_secret_stores_it_as_written_by_the_page(store):
    page = Page(store)
    response = post(page, "/add", {**form_of(page, "/add"), "name": "GitHub",
                                   "label0": "token", "value0": "abc"})
    assert response.status == 303
    [one] = store.list()
    assert dict(response.headers)["Location"] == f"{PREFIX}/vault/s/{one.id}"
    assert (one.title, one.category, one.tags, one.writer) == (
        "GitHub", "API_CREDENTIAL", (), vault_page.WRITER)
    [field] = one.fields
    assert (field.label, field.type, field.value) == ("token", "CONCEALED", "abc")
    assert field.id
    code, out, err = vault_op.run(store, "Casa", ["read", "op://Casa/GitHub/token"], None)
    assert (code, out.rstrip("\n"), err) == (0, "abc", "")


def test_every_value_added_on_the_page_is_a_secret_one(store):
    page = Page(store)
    assert post(page, "/add", {**form_of(page, "/add"), "name": "box",
                               "label0": "username", "value0": "admin",
                               "add1": "1", "label1": "password", "value1": "pw1"}).status == 303
    one = store.get("box")
    assert [(f.label, f.type, f.value) for f in one.fields] == [
        ("username", "CONCEALED", "admin"), ("password", "CONCEALED", "pw1")]
    assert post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                            "add2": "1", "label2": "note",
                                            "value2": "n1"}).status == 303
    assert [f.type for f in store.get(one.id).fields] == ["CONCEALED"] * 3


def test_the_add_form_starts_with_name_and_value_and_ends_with_save(store):
    body = text(get(Page(store), "/add"))
    assert '<p class="v-help" id="name-help">Used by Casa and plugins. Cannot be renamed.</p>' in body
    # The first slot: Value, then its label inside "Reference label", closed; always sent.
    assert re.search(r'<section class="v-panel v-stack" data-new-value>'
                     r'<input type="hidden" name="add0" value="1"><label class="v-field" '
                     r'for="f-value0">Value<textarea class="v-textarea" id="f-value0" '
                     r'name="value0" autocomplete="off"[^>]*spellcheck="false">\n</textarea>'
                     r'</label><details class="v-label-options"><summary>Reference label'
                     r'</summary>', body)
    # Two more slots, each opened by its own box and closed by Remove value.
    for index, label in ((0, "credential"), (1, "password"), (2, "certificate")):
        assert f'name="label{index}" value="{label}"' in body
    for index in (1, 2):
        assert (f'<input class="v-slot-enable" type="checkbox" id="add{index}" name="add{index}" '
                f'value="1"><label class="v-button v-slot-switch" for="add{index}">') in body
        assert (f'<label class="v-button v-button--danger" for="add{index}">Remove value'
                "</label>") in body
    assert "checked" not in body
    # Suggestions only with the script; the label box takes anything.
    assert body.count('<div class="v-picks" role="group" aria-label="Label suggestions" '
                      "hidden>") == 3
    for gone in ("Keep hidden", "hidden0", "<footer", "<fieldset", "<legend"):
        assert gone not in body
    # The form ends with Cancel and Save, inside it; the top bar's Save submits the same form.
    assert body.endswith('<div class="v-actions v-actions--end"><a class="v-button v-button--quiet" '
                         f'href="{PREFIX}/vault/">Cancel</a><button class="v-button '
                         'v-button--primary" type="submit">Save</button></div></form>'
                         "</main></body></html>")
    assert ('<button class="v-button v-button--primary" type="submit" form="secret-form">Save'
            "</button></div></header>") in body
    assert '<form class="v-form" id="secret-form" method="post" novalidate ' in body
    assert '<main class="v-main v-form-page">' in body


def test_only_a_fresh_add_form_has_its_default_labels(store):
    page = Page(store)
    assert 'name="label0" value="credential"' in text(get(page, "/add"))
    # Shown again after a refusal, the form keeps what was typed, an emptied label too,
    # and opens "Reference label" where the label is not the default.
    for sent, kept in (({"label0": "api key", "value0": SENTINEL}, 'value="api key"'),
                       ({"label0": "", "value0": ""}, 'name="label0" value=""')):
        response = post(page, "/add", {**form_of(page, "/add"), "name": "", **sent})
        body = text(response)
        assert response.status == 200 and "Enter a name." in body
        assert kept in body
        assert "credential" not in body
        assert '<details class="v-label-options" open>' in body
        assert SENTINEL not in body
    # The edit form's new slots start with the suggestions the secret does not use yet.
    one = make(store, "app", [secret("password", "secret-b")])
    body = text(get(page, f"/s/{one.id}/edit"))
    assert "credential" not in body
    assert [re.search(rf'name="label{index}" value="([^"]*)"', body).group(1)
            for index in (1, 2, 3)] == ["username", "certificate", "value4"]
    many = make(store, "mail", [plain("username", "u"), secret("password", "p"),
                                secret("certificate", "c")])
    body = text(get(page, f"/s/{many.id}/edit"))
    assert [re.search(rf'name="label{index}" value="([^"]*)"', body).group(1)
            for index in (3, 4, 5)] == ["value4", "value5", "value6"]


def test_a_hidden_box_is_no_longer_a_name_either_form_takes(store):
    page = Page(store)
    response = post(page, "/add", {**form_of(page, "/add"), "name": "x", "label0": "a",
                                   "value0": "b", "hidden0": "on"})
    assert response.status == 400
    assert "This form was not sent by this page." in text(response)
    assert store.list() == []
    one = _three(store)
    before = snapshot(store)
    for extra in ({"hidden3": "on"}, {"hidden0": "on"}):
        response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                    "label3": "t", "value3": "v", **extra})
        assert response.status == 400, extra
        assert snapshot(store) == before
    assert not any(name.startswith("hidden")
                   for form in ("add", "edit") for name in vault_page._form_names(form, one))


@pytest.mark.parametrize("submitted, stored", [
    ("a\r\nb\r\n", "a\nb\n"),
    ("  x  ", "  x  "),
    ("\r\nfirst", "\nfirst"),
    ("one\rtwo\n", "one\ntwo\n"),
    ("a\r\r\nb", "a\n\nb"),
])
def test_line_ends_are_stored_as_lf_and_nothing_else_changes(store, submitted, stored):
    page = Page(store)
    post(page, "/add", {**form_of(page, "/add"), "name": "  cert  ",
                        "label0": "  pem ", "value0": submitted})
    [one] = store.list()
    assert one.title == "cert"
    assert [(f.label, f.value) for f in one.fields] == [("pem", stored)]


def test_a_pem_key_pasted_with_crlf_comes_back_byte_for_byte_with_lf(store):
    pem = "-----BEGIN KEY-----\nAAAA\nBBBB\n-----END KEY-----\n"
    page = Page(store)
    post(page, "/add", {**form_of(page, "/add"), "name": "key",
                        "label0": "pem", "value0": pem.replace("\n", "\r\n")})
    [one] = store.list()
    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
    answer = post(page, f"/s/{one.id}/show", show, accept="application/json")
    assert json.loads(answer.body)["value"] == pem


def test_a_name_already_in_use_is_refused_on_the_form(store):
    make(store, "GitHub", [secret("token", "old1")])
    page = Page(store)
    before = snapshot(store)
    response = post(page, "/add", {**form_of(page, "/add"), "name": "GitHub",
                                   "label0": "token", "value0": SENTINEL})
    body = text(response)
    assert response.status == 200
    assert "A secret named \u201cGitHub\u201d already exists. Choose another name." in body
    assert '<p class="v-field-error" id="name-error">' in body
    assert 'aria-describedby="name-help name-error" aria-invalid="true"' in body
    assert snapshot(store) == before
    assert SENTINEL not in body
    assert 'value="GitHub"' in body and 'value="token"' in body  # names and labels kept


@pytest.mark.parametrize("form, sentence", [
    ({"name": "  ", "label0": "token", "value0": SENTINEL}, "Enter a name."),
    ({"name": "x", "label0": "credential", "value0": ""}, "Enter a value."),
    ({"name": "x", "label0": " ", "value0": SENTINEL}, "Enter a label."),
])
def test_an_empty_name_or_no_field_is_refused(store, form, sentence):
    page = Page(store)
    response = post(page, "/add", {**form_of(page, "/add"), **form})
    assert response.status == 200
    assert sentence in text(response)
    assert SENTINEL not in text(response)
    assert store.list() == []


def test_two_fields_with_one_label_are_refused(store):
    page = Page(store)
    response = post(page, "/add", {**form_of(page, "/add"), "name": "x",
                                   "label0": "token", "value0": SENTINEL,
                                   "add1": "1", "label1": "token", "value1": "other2"})
    assert response.status == 200
    assert "Each value needs a different label." in text(response)
    assert SENTINEL not in text(response)
    assert store.list() == []


def _many(store, count):
    return make(store, "many", [plain(f"f{index}", "v") for index in range(count)])


def test_an_edit_past_the_field_limit_is_refused_and_the_limit_itself_saves(store):
    page = Page(store)
    full = _many(store, vault_page.MAX_FIELDS - 2)
    before = snapshot(store)
    # A value for each: every value added on the page is a secret one, which may not be empty.
    new = {name: f"n{index}" for index in
           range(vault_page.MAX_FIELDS - 2, vault_page.MAX_FIELDS + 1)
           for name in (f"add{index}", f"label{index}", f"value{index}")}
    response = post(page, f"/s/{full.id}/edit", {**form_of(page, f"/s/{full.id}/edit"), **new})
    assert response.status == 200
    assert f"at most {vault_page.MAX_FIELDS} values" in text(response)
    assert snapshot(store) == before

    store.delete(full.id)
    fits = _many(store, vault_page.MAX_FIELDS - 3)
    new = {name: f"n{index}" for index in
           range(vault_page.MAX_FIELDS - 3, vault_page.MAX_FIELDS)
           for name in (f"add{index}", f"label{index}", f"value{index}")}
    response = post(page, f"/s/{fits.id}/edit", {**form_of(page, f"/s/{fits.id}/edit"), **new})
    assert response.status == 303
    assert len(store.get(fits.id).fields) == vault_page.MAX_FIELDS

    form = {**form_of(page, "/add"), "name": "three"}
    for index in range(3):
        form.update({f"add{index}": "1", f"label{index}": f"f{index}", f"value{index}": "v"})
    assert post(page, "/add", form).status == 303
    assert len(store.get("three").fields) == 3


def test_a_form_with_a_name_the_page_did_not_render_is_refused(store):
    page = Page(store)
    form = {**form_of(page, "/add"), "name": "x"}
    for index in range(2, 42):
        form.update({f"label{index}": f"field{index}", f"value{index}": "v"})
    response = post(page, "/add", form)
    assert response.status == 400
    assert "This form was not sent by this page." in text(response)
    assert f'href="{PREFIX}/vault/"' in text(response)
    assert store.list() == []

    one = _three(store)
    before = snapshot(store)
    for extra in ({"value0": "new", "keep0": "on"},       # keep is never rendered
                  {"value0": "new", "replace0": "on"},    # a plain field has no Replace box
                  {"label42": "new-token", "value42": "PLEASE_STORE"},
                  {"label0": "renamed"},                  # a stored field has no name box
                  {"hidden1": "on"},                      # no form has a hidden box
                  {"label3": "t", "value3": "v", "hidden3": "on"},
                  {"remove3": "on"},                      # 3 is a new row, not a stored field
                  {"value01": "x"}, {"label3x": "x"}, {"remove\u0661": "on"}):
        response = post(page, f"/s/{one.id}/edit",
                        {**form_of(page, f"/s/{one.id}/edit"), **extra})
        assert response.status == 400, extra
        assert "PLEASE_STORE" not in text(response)
        assert snapshot(store) == before
    for extra in ({"keep0": "on"}, {"replace0": "on"}, {"remove0": "on"}, {"hidden0": "on"}):
        response = post(page, "/add", {**form_of(page, "/add"), "name": "y",
                                       "label0": "a", "value0": "b", **extra})
        assert response.status == 400, extra
    assert snapshot(store) == before


def test_a_name_sent_twice_is_refused(store):
    one = _three(store)
    page = Page(store)
    before = snapshot(store)
    form = form_of(page, f"/s/{one.id}/edit")
    body = urlencode(form) + "&value0=first&value0=second"
    response = post(page, f"/s/{one.id}/edit", {}, body=body.encode("ascii"))
    assert response.status == 400
    assert "This form was not sent by this page." in text(response)
    assert snapshot(store) == before


@pytest.mark.parametrize("where", ["value", "label", "name"])
def test_a_form_whose_text_is_not_utf8_is_refused_and_keeps_its_code(store, where):
    one = _three(store)
    page = Page(store)
    before = snapshot(store)
    form = form_of(page, f"/s/{one.id}/edit")
    good = {"label3": "token", "value3": "ok", "replace1": "on", "value1": "new"}
    bad = {"value": "replace1=on&value1=%FF", "label": "label3=%FFx&value3=ok",
           "name": "name=%FF"}[where]
    body = urlencode(form) + "&" + bad
    response = post(page, f"/s/{one.id}/edit", {}, body=body.encode("ascii"))
    assert response.status == 400
    assert snapshot(store) == before
    assert post(page, f"/s/{one.id}/edit", {**form, **good}).status == 303


# Each POST route: the page whose form it posts, and the form's own fields.
def _routes(one):
    return {
        "add": ("/add", "/add", None, {"name": "new", "label0": "token",
                                       "value0": SENTINEL}),
        "edit": (f"/s/{one.id}/edit", f"/s/{one.id}/edit", None,
                 {"replace1": "on", "value1": SENTINEL + "x"}),
        "delete": (f"/s/{one.id}/delete", f"/s/{one.id}/delete", None, {}),
        "show": (f"/s/{one.id}", f"/s/{one.id}/show", None, {}),
        "show-json": (f"/s/{one.id}", f"/s/{one.id}/show", "application/json", {}),
    }


def _target(store):
    return make(store, "router", [plain("user", "admin7"), secret("password", SENTINEL)])


def _send(page, one, route, code_from=None, **kwargs):
    get_path, path, accept, extra = _routes(one)[route]
    form = {**form_of(page, get_path, path if path != get_path else None), **extra}
    if code_from is not None:
        form["code"] = code_from
    return post(page, path, form, accept=accept, **kwargs), form


def _assert_nothing_happened(store, one, response, before, reads):
    assert SENTINEL not in text(response)
    assert snapshot(store) == before
    assert store.reads(one.id) == reads


ROUTES = ["add", "edit", "delete", "show", "show-json"]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("case", ["none", "used", "expired", "other-path"])
def test_a_post_without_a_code_with_a_used_code_or_an_expired_code_is_refused(
        store, route, case):
    now = [1000.0]
    page = Page(store, clock=lambda: now[0])
    one = _target(store)
    get_path, path, accept, extra = _routes(one)[route]
    form = {**form_of(page, get_path, path if path != get_path else None), **extra}
    if case == "none":
        del form["code"]
    elif case == "used":
        assert post(page, path, form, accept=accept).status in (200, 303)
        if route == "add":  # the second add would be refused for its name anyway
            form["name"] = "another"
    elif case == "expired":
        now[0] += vault_page.CODE_LIFETIME + 1
    else:
        form["code"] = form_of(page, "/add")["code"] if route != "add" else (
            form_of(page, f"/s/{one.id}/delete")["code"])
    before, reads = snapshot(store), store.reads(one.id)
    response = post(page, path, form, accept=accept)
    assert response.status == 403
    assert "<h2>Form expired</h2>" in text(response)
    assert ">Reload page</a>" in text(response)
    assert f'href="{PREFIX}/vault/"' in text(response)
    _assert_nothing_happened(store, one, response, before, reads)


def test_a_code_authorises_exactly_one_operation(store):
    page = Page(store)
    one = _target(store)
    form = {**form_of(page, f"/s/{one.id}/edit"), "replace1": "on", "value1": "new1"}
    assert post(page, f"/s/{one.id}/edit", form).status == 303
    before = snapshot(store)
    form["value1"] = "new2"
    assert post(page, f"/s/{one.id}/edit", form).status == 403
    assert snapshot(store) == before
    assert values_of(store.get(one.id))["password"].value == "new1"

    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
    first = json.loads(post(page, f"/s/{one.id}/show", show, accept="application/json").body)
    assert first["value"] == "new1"
    assert post(page, f"/s/{one.id}/show", show, accept="application/json").status == 403
    again = post(page, f"/s/{one.id}/show", {**show, "code": first["code"]},
                 accept="application/json")
    assert again.status == 200
    assert json.loads(again.body)["code"] != first["code"]
    assert post(page, f"/s/{one.id}/show", {**show, "code": first["code"]},
                accept="application/json").status == 403


def test_a_code_is_bound_to_its_path(store):
    page = Page(store)
    one = _target(store)
    code = form_of(page, "/add")["code"]
    response = post(page, f"/s/{one.id}/delete", {"code": code})
    assert response.status == 403
    assert store.get(one.id)
    # The same action on another secret: a list row's Show code for A does not show B.
    first = make(store, "alpha", [secret("pin", "alpha-value-1")])
    second = make(store, "beta", [secret("pin", "beta-value-2")])
    for taken_from, sent_to in ((first, second), (second, first)):
        before = snapshot(store)
        reads = (store.reads(first.id), store.reads(second.id))
        show = form_of(page, "/", f"/s/{taken_from.id}/show")
        for accept in (None, "application/json"):
            response = post(page, f"/s/{sent_to.id}/show", show, accept=accept)
            assert response.status == 403
            assert "alpha-value-1" not in text(response) and "beta-value-2" not in text(response)
            show = form_of(page, "/", f"/s/{taken_from.id}/show")
        assert snapshot(store) == before
        assert (store.reads(first.id), store.reads(second.id)) == reads


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("headers", [
    {"origin": "https://evil.example"},
    {"origin": "null"},
    {"origin": None, "sec-fetch-site": "cross-site"},
    {"origin": None},
    {"origin": None, "sec-fetch-site": "none"},
], ids=["other", "null", "cross-site", "neither", "fetch-site-none"])
def test_a_post_from_another_origin_is_refused(store, route, headers):
    page = Page(store)
    one = _target(store)
    get_path, path, accept, extra = _routes(one)[route]
    form = {**form_of(page, get_path, path if path != get_path else None), **extra}
    before, reads = snapshot(store), store.reads(one.id)
    headers = dict(headers)
    origin = headers.pop("origin")
    response = post(page, path, form, origin=origin, accept=accept, headers=headers)
    assert response.status == 403
    _assert_nothing_happened(store, one, response, before, reads)
    # The origin is checked before the code, so the code is still good.
    assert post(page, path, form, accept=accept).status in (200, 303)


def test_a_post_without_origin_is_accepted_only_with_fetch_site_same_origin(store):
    page = Page(store)
    one = _target(store)
    form = {**form_of(page, "/add"), "name": "x", "label0": "a", "value0": "b"}
    assert post(page, "/add", form, origin=None).status == 403
    assert [item.title for item in store.list()] == ["router"]
    response = post(page, "/add", form, origin=None, headers={"sec-fetch-site": "same-origin"})
    assert response.status == 303
    assert sorted(item.title for item in store.list()) == ["router", "x"]
    # A Show without either header discloses nothing and counts no read.
    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
    response = post(page, f"/s/{one.id}/show", show, origin=None, accept="application/json")
    assert response.status == 403
    assert SENTINEL not in text(response) and store.reads(one.id) == 0


def test_a_post_that_is_not_a_form_or_is_too_large_is_refused(store):
    page = Page(store)
    form = {**form_of(page, "/add"), "name": "x", "label0": "a", "value0": "b"}
    response = post(page, "/add", form, content_type="application/json")
    assert response.status == 403
    too_large = urlencode({**form, "value0": "b" * vault_page.MAX_FORM}).encode()
    response = post(page, "/add", form, body=too_large)
    assert response.status == 413
    assert store.list() == []
    # Neither refusal reached the code.
    assert post(page, "/add", form, content_type="application/x-www-form-urlencoded; "
                "charset=UTF-8").status == 303


def test_a_post_that_is_neither_a_form_nor_small_enough_is_refused_for_its_type(store):
    page = Page(store)
    form = {**form_of(page, "/add"), "name": "x", "label0": "a", "value0": "b"}
    too_large = urlencode({**form, "value0": "b" * vault_page.MAX_FORM}).encode()
    response = post(page, "/add", form, content_type="text/plain", body=too_large)
    assert response.status == 403
    assert "This request is not a form of this page." in text(response)
    assert store.list() == []
    # the code was not reached
    assert post(page, "/add", form).status == 303


def _three(store):
    return make(store, "app", [plain("a", "plain-a"), secret("b", "secret-b"),
                               secret("c", "secret-c")])


def _three_with_purposes(store):
    """Three fields made from an `op item create` template, two with a purpose."""
    template = {"title": "app", "category": "LOGIN", "fields": [
        {"id": "username", "type": "STRING", "purpose": "USERNAME", "label": "username",
         "value": "plain-a"},
        {"type": "CONCEALED", "label": "b", "value": "secret-b"},
        {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password",
         "value": "secret-c"}]}
    code, _, err = vault_op.run(store, "Casa", ["item", "create", "--vault", "Casa",
                                                "--format", "json", "-"], json.dumps(template))
    assert (code, err) == (0, "")
    one = store.get("app")
    assert [f.purpose for f in one.fields] == ["USERNAME", None, "PASSWORD"]
    return one


def test_editing_changes_one_value_and_keeps_the_others(store, monkeypatch):
    import vault_store
    saves = []
    real = vault_store._replace_file
    monkeypatch.setattr(vault_store, "_replace_file",
                        lambda path, data: (saves.append(path), real(path, data)))
    one = _three_with_purposes(store)
    page = Page(store)
    form = form_of(page, f"/s/{one.id}/edit")
    saves.clear()
    response = post(page, f"/s/{one.id}/edit", {
        **form, "value0": "plain-a", "replace1": "on", "value1": "new"})
    assert response.status == 303
    assert dict(response.headers)["Location"] == f"{PREFIX}/vault/s/{one.id}"
    after = store.get(one.id)
    assert len(saves) == 1
    assert after.updated_at >= one.updated_at
    assert after.writer == vault_page.WRITER
    assert after.fields == (one.fields[0],
                            Field(id=one.fields[1].id, label="b", type="CONCEALED",
                                  value="new"),
                            one.fields[2])


def test_a_plain_value_that_comes_back_changed_is_replaced(store):
    one = _three(store)
    page = Page(store)
    post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                     "value0": "new-a\r\nline\r\n", "value1": "ignored"})
    after = store.get(one.id)
    assert [f.value for f in after.fields] == ["new-a\nline\n", "secret-b", "secret-c"]
    assert after.fields[0].id == one.fields[0].id


def test_an_edit_that_changes_nothing_saves_nothing(store):
    one = _three(store)
    page = Page(store)
    before = snapshot(store)
    response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                "value0": "plain-a"})
    assert response.status == 303
    assert snapshot(store) == before


def test_replacing_a_hidden_value_with_nothing_is_refused(store):
    one = _three(store)
    page = Page(store)
    before = snapshot(store)
    response = post(page, f"/s/{one.id}/edit", {
        **form_of(page, f"/s/{one.id}/edit"), "value0": "plain-a", "replace1": "on",
        "value1": ""})
    body = text(response)
    assert response.status == 200
    assert "Enter a value or turn off Replace." in body
    assert snapshot(store) == before
    assert "secret-b" not in body and "secret-c" not in body


def test_a_lone_cr_in_a_plain_value_survives_an_untouched_save(store, monkeypatch):
    import vault_store
    one = make(store, "app", [plain("a", "x\ry"), secret("b", "secret-b")])
    saves = []
    real = vault_store._replace_file
    monkeypatch.setattr(vault_store, "_replace_file",
                        lambda path, data: (saves.append(path), real(path, data)))
    page = Page(store)
    # A browser sends every line break of a textarea, a lone CR too, as CRLF.
    response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                "value0": "x\r\ny"})
    assert response.status == 303
    assert saves == []
    assert store.get(one.id).fields[0].value == "x\ry"


def test_removing_a_secret_field_and_adding_it_back_empty_is_refused(store):
    one = make(store, "app", [secret("token", "KEEP_ME")])
    page = Page(store)
    before = snapshot(store)
    response = post(page, f"/s/{one.id}/edit", {
        **form_of(page, f"/s/{one.id}/edit"), "remove0": "on", "add1": "1",
        "label1": "token", "value1": ""})
    body = text(response)
    assert response.status == 200
    assert "Enter a value, or remove this value card." in body
    assert snapshot(store) == before
    assert "KEEP_ME" not in body


def test_adding_a_secret_field_without_a_value_is_refused(store):
    page = Page(store)
    response = post(page, "/add", {**form_of(page, "/add"), "name": "x",
                                   "label0": "token", "value0": ""})
    assert response.status == 200
    assert "Enter a value." in text(response)
    assert store.list() == []
    # The first slot's own label, left with no value, is the same slip.
    response = post(page, "/add", {**form_of(page, "/add"), "name": "x",
                                   "label0": "credential"})
    assert response.status == 200
    assert "Enter a value." in text(response)
    assert store.list() == []
    # So is an open second slot without a value.
    response = post(page, "/add", {**form_of(page, "/add"), "name": "x", "label0": "a",
                                   "value0": "v", "add1": "1", "label1": "password"})
    assert response.status == 200
    assert "Enter a value, or remove this value card." in text(response)
    assert store.list() == []


def test_a_secret_field_stored_empty_and_left_alone_does_not_stop_a_save(store):
    one = make(store, "app", [plain("a", "plain-a"), secret("pin", "")])
    page = Page(store)
    response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                "value0": "changed"})
    assert response.status == 303
    after = store.get(one.id)
    assert [(f.label, f.value) for f in after.fields] == [("a", "changed"), ("pin", "")]
    assert after.fields[1] == one.fields[1]


def test_the_edit_form_never_contains_a_secret_value(store):
    one = make(store, "app", [plain("a", "\nplain-a"), secret("b", "secret-b"),
                              secret("c", "secret-c")])
    body = text(get(Page(store), f"/s/{one.id}/edit"))
    # The parser drops one newline after <textarea>, so the value's own survives.
    assert re.search(r'name="value0"[^>]*>\n\nplain-a</textarea>', body)
    assert "secret-b" not in body and "secret-c" not in body
    assert body.count(f'<span class="v-dots" aria-label="Hidden value">{vault_page._DOTS}'
                      "</span>") == 2
    assert "Unchanged" not in body and "\u00b7 secret" not in body
    assert 'name="replace1"' in body and 'name="replace2"' in body
    assert 'name="replace0"' not in body
    for index in range(3):
        assert f'name="remove{index}"' in body
    # New slots are numbered after the stored fields, each behind its own box.
    assert 'name="label3"' in body and 'name="label0"' not in body
    for index in (3, 4, 5):
        assert f'name="add{index}" value="1">' in body
    assert "<title>Edit secret \u00b7 Casa</title>" in body
    assert '<p class="v-subtitle v-name">app</p>' in body
    assert re.search(r'<input type="hidden" name="stamp" value="[0-9a-f]{64}">', body)
    assert "checked" not in body
    # Cancel and Save end the form; the top bar's Save submits it; Delete is on the secret's page
    assert body.endswith(
        '<div class="v-actions v-actions--end"><a class="v-button v-button--quiet" '
        f'href="{PREFIX}/vault/s/{one.id}">Cancel</a><button class="v-button v-button--primary" '
        'type="submit">Save</button></div></form></main></body></html>')
    assert 'form="secret-form">Save</button></div></header>' in body
    assert "Delete" not in body


def test_a_stored_field_card_on_the_edit_form(store):
    one = make(store, "app", [plain("a", "plain-a"), secret("b", "secret-b"), secret("e", ""),
                              plain("m", "one\ntwo")])
    body = text(get(Page(store), f"/s/{one.id}/edit"))
    cards = re.findall(r'<section class="v-panel v-stack v-existing".*?</section>', body,
                       re.DOTALL)
    assert len(cards) == 4
    for index, card in enumerate(cards):
        label = one.fields[index].label
        # the label is the card's heading, never a legend; Remove sits beside it
        assert card.startswith(
            f'<section class="v-panel v-stack v-existing" aria-label="{label}">'
            f'<div class="v-heading"><h2>{label}</h2><label class="v-choice v-choice--remove">'
            f'<input class="v-remove-input" type="checkbox" name="remove{index}" value="1">'
            '<span class="v-remove-off">Remove</span><span class="v-remove-on">Undo</span>'
            "</label></div>")
        assert card.endswith('<p class="v-removed-note">Removed when you save.</p></section>')
    assert "Replace" not in cards[0] and "v-dots" not in cards[0]
    # a plain value of one line is a one-line box; one of several lines a taller one
    assert 'class="v-textarea v-textarea--line" id="f-value0" name="value0" rows="1"' in cards[0]
    assert 'class="v-textarea" id="f-value3" name="value3" autocomplete' in cards[3]
    assert (f'<div class="v-value-row"><span class="v-dots" aria-label="Hidden value">'
            f'{vault_page._DOTS}</span><label class="v-choice"><input class="v-replace-input" '
            'type="checkbox" name="replace1" value="1"><span class="v-replace-off">Replace</span>'
            '<span class="v-replace-on">Replacing</span></label></div><div class="v-replacement">'
            '<label class="v-field" for="f-value1">New value<textarea class="v-textarea" '
            'id="f-value1" name="value1"') in cards[1]
    # an empty hidden value has the same dots: the form does not say it is empty
    def same(card, label):
        card = re.sub(r"(remove|replace|value)\d", r"\1N", card)
        return card.replace(f'"{label}"', '"L"').replace(f">{label}<", ">L<")
    assert same(cards[2], "e") == same(cards[1], "b")
    assert "Empty" not in body


def test_removing_a_field_and_adding_a_field_in_one_save(store):
    one = _three(store)
    page = Page(store)
    response = post(page, f"/s/{one.id}/edit", {
        **form_of(page, f"/s/{one.id}/edit"), "remove1": "on",
        "add3": "1", "label3": " token ", "value3": "tok\r\n"})
    assert response.status == 303
    after = store.get(one.id)
    assert [(f.label, f.type, f.value) for f in after.fields] == [
        ("a", "STRING", "plain-a"), ("c", "CONCEALED", "secret-c"),
        ("token", "CONCEALED", "tok\n")]
    assert after.fields[1].id == one.fields[2].id


def test_an_edit_that_repeats_a_label_is_refused_on_the_form(store):
    one = _three(store)
    page = Page(store)
    before = snapshot(store)
    response = post(page, f"/s/{one.id}/edit", {
        **form_of(page, f"/s/{one.id}/edit"), "add3": "1", "label3": "b",
        "value3": SENTINEL})
    assert response.status == 200
    assert "Each value needs a different label." in text(response)
    assert SENTINEL not in text(response)
    assert snapshot(store) == before
    # The form that came back can be sent again as it is, with the label corrected and the
    # value typed again: it carries a fresh code and the stamp.
    again = _Form(text(response), f"/s/{one.id}/edit")
    response = post(page, f"/s/{one.id}/edit", {},
                    body=again.sent(label3="d", value3="typed-again"))
    assert response.status == 303
    after = store.get(one.id)
    assert [(f.label, f.value) for f in after.fields] == [
        ("a", "plain-a"), ("b", "secret-b"), ("c", "secret-c"), ("d", "typed-again")]


def test_removing_every_field_is_refused(store):
    one = _three(store)
    page = Page(store)
    before = snapshot(store)
    response = post(page, f"/s/{one.id}/edit", {
        **form_of(page, f"/s/{one.id}/edit"), "remove0": "on", "remove1": "on",
        "remove2": "on"})
    assert response.status == 200
    assert "Keep at least one value, or delete the secret instead." in text(response)
    assert snapshot(store) == before


def test_a_secret_that_changed_since_the_form_was_opened_is_not_overwritten(store):
    one = _three(store)
    page = Page(store)
    form = form_of(page, f"/s/{one.id}/edit")
    store.set_field(one.id, label="b", type="CONCEALED", value="from-op", writer="op")
    response = post(page, f"/s/{one.id}/edit", {**form, "replace1": "on", "value1": SENTINEL})
    assert response.status == 409
    assert ("This secret changed while you were editing. Reload it before saving again."
            in text(response))
    assert f'href="{PREFIX}/vault/s/{one.id}/edit">Reload secret</a>' in text(response)
    assert SENTINEL not in text(response)
    after = store.get(one.id)
    assert values_of(after)["b"].value == "from-op"
    assert after.writer == "op"


def test_a_change_within_the_same_second_is_still_seen(store, monkeypatch):
    # The store keeps change times in whole seconds, so two writes close
    # together carry the same one. Freeze the store's clock to make it so.
    import pykeepass.baseelement
    frozen = datetime(2026, 11, 22, 13, 7, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(pykeepass.baseelement, "datetime", Frozen)
    one = _three(store)
    page = Page(store)
    form = form_of(page, f"/s/{one.id}/edit")
    store.set_field(one.id, label="b", type="CONCEALED", value="from-op", writer="op")
    assert store.get(one.id).updated_at == one.updated_at
    response = post(page, f"/s/{one.id}/edit", {**form, "replace1": "on", "value1": SENTINEL})
    assert response.status == 409
    assert values_of(store.get(one.id))["b"].value == "from-op"


def test_an_ssh_key_cannot_be_edited(store):
    key = ssh_key(store)
    page = Page(store)
    assert get(page, f"/s/{key.id}/edit").status == 404
    code = page.codes.issue(f"/s/{key.id}/edit")
    before = snapshot(store)
    response = post(page, f"/s/{key.id}/edit", {"code": code, "remove0": "on"})
    assert response.status == 404
    assert snapshot(store) == before
    response = post(page, f"/s/{key.id}/delete", form_of(page, f"/s/{key.id}/delete"))
    assert response.status == 303
    assert store.list() == []


def test_deleting_asks_first_and_then_deletes(store):
    one = _three(store)
    page = Page(store)
    body = text(get(page, f"/s/{one.id}/delete"))
    assert ('<h2 class="v-title">Delete app?</h2>'
            "<p>All its values will be deleted. This cannot be undone.</p>") in body
    assert "removed for good" not in body and "backups" not in body
    assert ("<strong>Casa uses this secret.</strong><p>Deleting it may stop the features that "
            "use it.</p>") in body
    assert (f'<a class="v-button v-button--quiet" href="{PREFIX}/vault/s/{one.id}">Cancel</a>'
            '<button class="v-button v-button--delete" type="submit">Delete secret</button>'
            "</form>") in body
    assert store.get(one.id)
    response = post(page, f"/s/{one.id}/delete", form_of(page, f"/s/{one.id}/delete"))
    assert response.status == 303
    assert dict(response.headers)["Location"] == f"{PREFIX}/vault/"
    assert store.list() == []


def test_deleting_says_who_uses_the_secret_and_nothing_for_one_made_on_the_page(store):
    mine = make(store, "mine", [secret("token", "t1")], writer=vault_page.WRITER)
    body = text(get(Page(store), f"/s/{mine.id}/delete"))
    assert "This cannot be undone." in body
    assert "uses this secret" not in body
    drop = make(store, vault_drop_off.item_title("finance", "bank-key"),
                [secret("password", "bankkey88")], tags=[vault_drop_off.TAG])
    body = text(get(Page(store), f"/s/{drop.id}/delete"))
    assert "<strong>A plugin uses this secret.</strong>" in body
    assert "Added by finance" in text(get(Page(store), f"/s/{drop.id}"))


def test_show_returns_the_value_only_to_a_post_with_a_code(store):
    one = make(store, "app", [plain("user", "admin7"), secret("password", SENTINEL),
                              secret("other", "other-value")])
    page = Page(store)
    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
    assert show["field"] == "1"
    response = post(page, f"/s/{one.id}/show", {**show, "then": "copy"})
    body = text(response)
    assert response.status == 200
    sections = re.findall(r'<section class="v-panel v-value-card".*?</section>', body, re.DOTALL)
    assert [SENTINEL in section for section in sections] == [False, True, False]
    assert f'<pre class="v-value v-revealed">\n{SENTINEL}</pre>' in sections[1]
    assert (f'<a class="v-button v-button--quiet" href="{PREFIX}/vault/s/{one.id}">Hide</a>'
            in sections[1])
    assert ('<p class="v-feedback" role="status" aria-live="polite">Press and hold the value to '
            "copy it.</p>") in sections[1]
    assert ">Show</button>" not in sections[1] and ">Show</button>" in sections[2]
    assert "other-value" not in body

    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show", nth=1)
    response = post(page, f"/s/{one.id}/show", show, accept="application/json")
    headers = dict(response.headers)
    assert headers["Content-Type"] == "application/json"
    for name, value in HEADERS.items():
        assert headers[name] == value
    answer = json.loads(response.body)
    assert answer["value"] == "other-value"
    again = post(page, f"/s/{one.id}/show", {**show, "code": answer["code"]},
                 accept="application/json")
    assert json.loads(again.body)["value"] == "other-value"

    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
    response = post(page, f"/s/{one.id}/show", {**show, "field": "3"})
    assert response.status == 404
    assert SENTINEL not in text(response)
    response = get(page, f"/s/{one.id}/show")
    assert response.status == 405
    assert SENTINEL not in text(response)


@pytest.mark.parametrize("index", ["9" * 5000, "\u00b2", "01234", "-1", " 1", ""],
                         ids=["huge", "superscript", "five-digits", "negative", "space", "empty"])
def test_a_show_of_a_field_that_is_not_a_small_number_is_404(store, index):
    one = _target(store)
    page = Page(store)
    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
    for accept in (None, "application/json"):
        response = post(page, f"/s/{one.id}/show", {**show, "field": index}, accept=accept)
        assert response.status == 404
        assert SENTINEL not in text(response)
        show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
    assert store.reads(one.id) == 0


def test_the_one_secret_page_has_show_and_copy_for_each_kind_of_field(store):
    one = make(store, "app", [plain("user", "admin7"), secret("password", SENTINEL),
                              secret("empty", "")])
    page = Page(store)
    body = text(get(page, f"/s/{one.id}"))
    sections = re.findall(r'<section class="v-panel v-value-card".*?</section>', body, re.DOTALL)
    # a plain value is on the page; its Copy needs the script, which shows it
    assert ('<button class="v-button v-button--copy" type="button" data-action="copy-plain" '
            "hidden>Copy</button>") in sections[0]
    assert "<noscript>" in sections[0]
    assert "<form" not in sections[0]
    assert ('<button class="v-button v-button--quiet" type="submit" name="then" value="show" '
            'data-action="show">Show</button>') in sections[1]
    assert ('<button class="v-button v-button--copy" type="submit" name="then" value="copy" '
            'data-action="copy">Copy</button>') in sections[1]
    # an empty hidden value has the same dots, Show and Copy as any other: only a Show says so
    def same(section, label, index):
        section = re.sub(r'name="code" value="[^"]+"', 'name="code" value="C"', section)
        return section.replace(f'"{label}"', '"L"').replace(f">{label}<", ">L<").replace(
            f'name="field" value="{index}"', 'name="field" value="N"')
    assert same(sections[2], "empty", 2) == same(sections[1], "password", 1)
    assert "Empty" not in body
    codes = re.findall(r'name="code" value="([^"]+)"', body)
    assert len(codes) == len(set(codes)) == 2
    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show", nth=1)
    answer = post(page, f"/s/{one.id}/show", show, accept="application/json")
    assert json.loads(answer.body)["value"] == ""
    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show", nth=1)
    shown = text(post(page, f"/s/{one.id}/show", show))
    assert '<pre class="v-value v-revealed" data-empty>Empty</pre>' in shown


def test_no_get_response_ever_contains_a_secret_value(store):
    key = ssh_key(store)
    private = values_of(key)["private key"].value
    one = make(store, "app", [plain("user", "admin7"), secret("password", SENTINEL)])
    # an SSH key whose facts are hidden fields: they are values like any other, not facts
    hidden_facts = make(store, "key2", [secret("fingerprint", "fingerprint-sentinel-8"),
                                        secret("key type", "keytype-sentinel-9"),
                                        Field(id="", label="private key", type="SSHKEY",
                                              value="private-sentinel-7")], category="SSH_KEY")
    page = Page(store)
    for item_id in (one.id, key.id, hidden_facts.id):
        for path in ("/", f"/s/{item_id}", f"/s/{item_id}/edit", f"/s/{item_id}/delete",
                     "/add", f"/s/{item_id}/show"):
            for method in ("GET", "HEAD"):
                body = text(get(page, path, method=method))
                assert SENTINEL not in body, path
                for sentinel in ("fingerprint-sentinel-8", "keytype-sentinel-9",
                                 "private-sentinel-7"):
                    assert sentinel not in body, path
                assert "PRIVATE" not in body, path
                for line in private.splitlines()[-4:-1]:
                    assert line not in body, path
    assert store.reads(one.id) == store.reads(key.id) == 0


def test_a_show_counts_as_a_read(store):
    one = _target(store)
    page = Page(store)
    post(page, f"/s/{one.id}/show", form_of(page, f"/s/{one.id}", f"/s/{one.id}/show"))
    assert store.reads(one.id) == 1
    assert "Read once since Casa started" in text(get(page, f"/s/{one.id}"))


def test_every_form_page_follows_the_page_rules(store):
    one = make(store, "<b>x</b>", [plain("note", '"><script>alert(1)</script>'),
                                   secret("pw", SENTINEL)])
    page = Page(store)
    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
    bodies = [text(get(page, path)) for path in
              ("/add", f"/s/{one.id}/edit", f"/s/{one.id}/delete")]
    bodies.append(text(post(page, f"/s/{one.id}/show", show)))
    bodies.append(text(post(page, "/add", {"name": "y"})))
    for body in bodies:
        assert "style=" not in body
        assert not re.search(r"<script[^>]*>[^<]", body)
        assert not re.search(r" on[a-z]+=", body)
        assert "<b>x" not in body and "<script>alert" not in body
        for link in LINKS.findall(body):
            assert LINK.fullmatch(link), link


def test_codes_expire_are_single_use_and_are_bounded():
    now = [0.0]
    codes = vault_page.Codes(clock=lambda: now[0])
    first = codes.issue("/add")
    assert codes.take(first, "/add") and not codes.take(first, "/add")
    assert not codes.take(None, "/add") and not codes.take(["x"], "/add")
    late = codes.issue("/add")
    now[0] += vault_page.CODE_LIFETIME
    assert not codes.take(late, "/add")
    issued = [codes.issue("/add") for _ in range(vault_page.MAX_CODES)]
    assert len(codes._forms) == vault_page.MAX_CODES
    codes.issue("/add")
    assert len(codes._forms) == vault_page.MAX_CODES - vault_page.MAX_CODES // 4 + 1
    assert not codes.take(issued[0], "/add")
    assert codes.take(issued[-1], "/add")


def test_a_form_is_good_for_an_hour_and_not_a_second_longer(store):
    now = [0.0]
    page = Page(store, clock=lambda: now[0])
    one = _target(store)
    first = form_of(page, f"/s/{one.id}/edit")
    second = form_of(page, f"/s/{one.id}/edit")
    now[0] = 3599.0
    assert post(page, f"/s/{one.id}/edit",
                {**first, "replace1": "on", "value1": "new1"}).status == 303
    now[0] = 3600.0
    before = snapshot(store)
    response = post(page, f"/s/{one.id}/edit", {**second, "replace1": "on", "value1": "new2"})
    assert response.status == 403
    assert "<h2>Form expired</h2>" in text(response)
    assert snapshot(store) == before


def test_codes_stay_single_use_under_many_threads():
    # The page is shared by one thread per connection.
    codes = vault_page.Codes()
    threads, each = 8, 400
    issued, taken, errors = [], [], []
    start = threading.Barrier(threads)

    def run(work):
        def body():
            try:
                start.wait(30)
                work()
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
        workers = [threading.Thread(target=body) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(60)

    before = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        run(lambda: issued.extend(codes.issue("/add") for _ in range(each)))
        assert len(set(issued)) == threads * each

        def take_all():
            for code in random.sample(issued, len(issued)):
                if codes.take(code, "/add"):
                    taken.append(code)
        run(take_all)

        # Past the bound, while others take theirs: nothing breaks.
        def churn():
            mine = [codes.issue("/add") for _ in range(vault_page.MAX_CODES // 2)]
            for code in mine:
                codes.take(code, "/add")
        run(churn)
    finally:
        sys.setswitchinterval(before)
    assert errors == []
    assert sorted(taken) == sorted(issued)  # every code taken exactly once
    assert len(codes._forms) <= vault_page.MAX_CODES


@pytest.mark.parametrize("method, path, name", [
    ("GET", "/", "list"), ("HEAD", "/", "list"), ("GET", "/add", "add"),
    ("POST", "/add", "add"), ("GET", "/s/" + "a" * 26, "secret"),
    ("GET", "/s/" + "a" * 26 + "/edit", "edit"), ("POST", "/s/" + "a" * 26 + "/delete", "delete"),
    ("POST", "/s/" + "a" * 26 + "/show", "show"), ("GET", "/s/github", "other"),
    ("GET", "/?q=1", "other"), ("PUT", "/", "other"), ("GET", "/static/ha.css", "other"),
])
def test_route_names_are_fixed_words(method, path, name):
    assert vault_page.route_name(method, path) == name


# One name each form renders, to send twice.
TWICE = {"add": "name", "edit": "value1", "delete": "code", "show": "field",
         "show-json": "code"}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("fault", ["unknown", "twice"])
def test_every_form_takes_only_its_own_names_each_once(store, route, fault):
    """An unknown name is judged against the secret, after the code is spent;
    a repeated name is judged with the form, before it."""
    page = Page(store)
    one = _target(store)
    get_path, path, accept, extra = _routes(one)[route]
    form = {**form_of(page, get_path, path if path != get_path else None), **extra}
    if route == "edit":
        form["value1"] = SENTINEL + "x"
    before, reads = snapshot(store), store.reads(one.id)
    if fault == "unknown":
        body = urlencode({**form, "surprise": "1"})
    else:
        body = urlencode(form) + "&" + urlencode({TWICE[route]: form.get(TWICE[route], "x")})
    response = post(page, path, {}, accept=accept, body=body.encode("ascii"))
    assert response.status == 400
    assert "This form was not sent by this page." in text(response)
    _assert_nothing_happened(store, one, response, before, reads)
    again = post(page, path, form, accept=accept)
    if fault == "unknown":
        assert again.status == 403  # the code was spent
        _assert_nothing_happened(store, one, again, before, reads)
    else:
        assert again.status in (200, 303)  # the code is still good


@pytest.mark.parametrize("route", ["add", "edit", "delete", "show"])
def test_the_names_a_form_takes_are_the_names_it_renders(store, route):
    one = make(store, "app", [plain("a", "plain-a"), secret("b", "secret-b")])
    page = Page(store)
    get_path, path, _, _ = _routes(one)[route]
    body = text(get(page, get_path))
    target = PREFIX + "/vault" + path
    [form] = re.findall(r'<form[^>]*action="' + re.escape(target) + r'"[^>]*>(.*?)</form>',
                        body, re.DOTALL)[:1]
    rendered = set(re.findall(r'\sname="([^"]+)"', form))
    expected = vault_page._form_names(route.split("-")[0], one)
    assert rendered == expected
    # Sent together, no name comes twice: every box and checkbox has a name of its own, and
    # the one submit button a tap activates shares none with them.
    controls = _Form(body, path)
    boxes = [name for name, *_ in controls.controls]
    assert sorted(boxes) == sorted(set(boxes))
    assert not set(controls.buttons) & set(boxes)
    assert set(boxes) | set(controls.buttons) == expected


# -- the redesigned page: new-value slots, Copy on the list, the code pools ----------

def test_a_new_value_is_saved_only_when_its_slot_is_open(store):
    one = _three(store)
    page = Page(store)
    before = snapshot(store)
    # A closed slot's label and value come back without the script; they are ignored.
    response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                "label3": "token", "value3": "closed1"})
    assert response.status == 303
    assert snapshot(store) == before
    response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                "add3": "1", "label3": "token", "value3": "open1",
                                                "label4": "other", "value4": "closed2"})
    assert response.status == 303
    assert [(f.label, f.value) for f in store.get(one.id).fields][3:] == [("token", "open1")]
    # On the add form the first slot is always open; the others only when asked for.
    response = post(page, "/add", {**form_of(page, "/add"), "name": "n", "label0": "a",
                                   "value0": "v0", "label1": "b", "value1": "v1",
                                   "add2": "1", "label2": "c", "value2": "v2"})
    assert response.status == 303
    assert [(f.label, f.value) for f in store.get("n").fields] == [("a", "v0"), ("c", "v2")]


def test_an_open_slot_needs_a_label_and_a_value(store):
    one = _three(store)
    page = Page(store)
    before = snapshot(store)
    for extra, sentence in (({"add3": "1", "label3": "x", "value3": ""},
                             "Enter a value, or remove this value card."),
                            ({"add3": "1", "label3": " ", "value3": SENTINEL},
                             "Enter a label.")):
        response = post(page, f"/s/{one.id}/edit",
                        {**form_of(page, f"/s/{one.id}/edit"), **extra})
        assert response.status == 200
        assert sentence in text(response)
        assert SENTINEL not in text(response)
        assert snapshot(store) == before


def test_a_refused_form_comes_back_as_it_was_sent_without_its_values(store):
    one = make(store, "app", [plain("a", "plain-a"), secret("b", "secret-b"),
                              secret("c", "secret-c")])
    page = Page(store)
    response = post(page, f"/s/{one.id}/edit", {
        **form_of(page, f"/s/{one.id}/edit"), "value0": "typed-plain", "remove2": "1",
        "replace1": "1", "value1": SENTINEL, "add3": "1", "label3": "b", "value3": SENTINEL})
    body = text(response)
    assert response.status == 200
    assert "Each value needs a different label." in body
    assert SENTINEL not in body and "secret-b" not in body and "secret-c" not in body
    assert "The values you typed were cleared. Enter them again." in body
    assert re.search(r'name="value0"[^>]*>\ntyped-plain</textarea>', body)
    assert re.search(r'name="replace1" value="1" checked', body)
    assert re.search(r'name="remove2" value="1" checked', body)
    assert not re.search(r'name="remove1" value="1" checked', body)
    assert re.search(r'name="add3" value="1" checked', body)
    assert not re.search(r'name="add4" value="1" checked', body)
    assert 'name="label3" value="b"' in body
    # An add refused with its second slot open comes back with that slot open and labelled,
    # and every value box empty.
    response = post(page, "/add", {**form_of(page, "/add"), "name": "", "label0": "api",
                                   "value0": SENTINEL, "add1": "1", "label1": "username",
                                   "value1": "typed-second"})
    body = text(response)
    assert response.status == 200 and "Enter a name." in body
    assert SENTINEL not in body and "typed-second" not in body
    assert re.search(r'name="add1" value="1" checked', body)
    assert not re.search(r'name="add2" value="1" checked', body)
    assert 'name="label1" value="username"' in body and 'name="label0" value="api"' in body
    sent = _Form(body, "/add").controls
    assert [value for name, kind, value, _ in sent if kind == "textarea"] == ["", "", ""]


def test_the_list_copies_a_one_value_secret_with_a_show_form_of_its_own(store):
    """Every secret with exactly one value, hidden, plain or empty, and no SSH key, has Copy on
    its row; every other one opens. Which it is says nothing about the value."""
    single = make(store, "GitHub", [secret("credential", SENTINEL)], writer=vault_page.WRITER)
    note = make(store, "Note", [plain("text", "plain-note-6")])
    empty = make(store, "Blank", [secret("pin", "")])
    many = make(store, "Mail", [plain("username", "casa-mail"), secret("password", "pw7")])
    half = make(store, "Half", [secret("full", "half-full-3"), secret("none", "")])
    key = ssh_key(store)
    page = Page(store)
    body = text(get(page, "/"))
    for value in (SENTINEL, "plain-note-6", "casa-mail", "pw7", "half-full-3"):
        assert value not in body
    forms = re.findall(r'<form class="v-actions v-reveal" method="post" action="([^"]+)">(.*?)'
                       r'</form>', body, re.DOTALL)
    assert [action for action, _ in forms] == [
        f"{PREFIX}/vault/s/{one.id}/show" for one in (empty, single, note)]  # in name order
    for _, inside in forms:
        assert 'name="field" value="0"' in inside
        assert 'name="then" value="copy"' in inside and ">Show<" not in inside
    for other in (many, half, key):
        assert (f'<a class="v-button v-button--quiet" href="{PREFIX}/vault/s/{other.id}">Open</a>'
                in body)
    assert "2 values · username, password" in body
    for one, value in ((single, SENTINEL), (note, "plain-note-6"), (empty, "")):
        show = form_of(page, "/", f"/s/{one.id}/show")
        answer = post(page, f"/s/{one.id}/show", {**show, "then": "copy"},
                      accept="application/json")
        assert answer.status == 200 and json.loads(answer.body)["value"] == value
    # Without the script the same form shows the value on the secret's page.
    show = form_of(page, "/", f"/s/{single.id}/show")
    shown = text(post(page, f"/s/{single.id}/show", {**show, "then": "copy"}))
    assert f'<pre class="v-value v-revealed">\n{SENTINEL}</pre>' in shown
    assert "Press and hold the value to copy it." in shown
    show = form_of(page, "/", f"/s/{empty.id}/show")
    shown = text(post(page, f"/s/{empty.id}/show", {**show, "then": "copy"}))
    assert '<pre class="v-value v-revealed" data-empty>Empty</pre>' in shown
    assert "Press and hold" not in shown


def test_an_ssh_key_with_one_value_opens_from_the_list(store):
    key = make(store, "deploy", [Field(id="", label="private key", type="SSHKEY",
                                       value=SENTINEL)], category="SSH_KEY")
    body = text(get(Page(store), "/"))
    assert (f'<a class="v-button v-button--quiet" href="{PREFIX}/vault/s/{key.id}">Open</a>'
            in body)
    assert f"/s/{key.id}/show" not in body and "v-reveal" not in body
    assert SENTINEL not in body


def test_show_codes_never_push_out_a_forms_code():
    codes = vault_page.Codes(clock=lambda: 0.0)
    form = codes.issue("/add")
    for _ in range(3 * vault_page.MAX_CODES):
        codes.issue("/s/" + "a" * 26 + "/show")
    assert len(codes._shows) <= vault_page.MAX_CODES
    assert codes.take(form, "/add")
    # A code presented to the wrong path is spent all the same, whichever kind it is.
    show = codes.issue("/s/" + "a" * 26 + "/show")
    assert not codes.take(show, "/add") and not codes.take(show, "/s/" + "a" * 26 + "/show")


def test_an_empty_hidden_value_looks_like_any_other_before_a_show(store):
    """Whether a hidden value is empty is not on any GET: the secret's page, its edit form and
    the list read the same for an empty value as for one of nine characters."""
    page = Page(store)
    rendered = {}
    for length in (0, 9):
        one = make(store, f"len{length}", [secret("pin", "x" * length)])
        bodies = [text(get(page, path)) for path in (f"/s/{one.id}", f"/s/{one.id}/edit")]
        row = re.search(rf'<li class="v-list-item"(?:(?!</li>).)*len{length}.*?</li>',
                        text(get(page, "/")), re.DOTALL)
        normal = []
        for body in bodies + [row.group(0)]:
            body = body.replace(one.id, "ID").replace(f"len{length}", "NAME")
            body = re.sub(r'name="(code|stamp)" value="[^"]+"', r'name="\1" value="X"', body)
            normal.append(re.sub(r"Updated [^<]+<", "Updated T<", body))
        rendered[length] = normal
        store.delete(one.id)
    assert rendered[0] == rendered[9]
    assert "Empty" not in "".join(rendered[0])


@pytest.mark.parametrize("value", ["\nstarts-with-newline", "\n\ntwo-newlines", "plain"])
def test_a_value_shown_in_a_pre_keeps_its_first_newline(store, value):
    """Every HTML parser drops one newline right after `<pre>`; the page puts one there, so
    the value a person reads and copies is the stored one."""
    one = make(store, "app", [plain("note", value), secret("pin", value)])
    page = Page(store)
    body = text(get(page, f"/s/{one.id}"))
    assert f'<pre class="v-value" data-plain>\n{value}</pre>' in body
    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
    shown = text(post(page, f"/s/{one.id}/show", show))
    assert f'<pre class="v-value v-revealed">\n{value}</pre>' in shown


@pytest.mark.parametrize("char", ["\f", "\x00", "\x1b", "￾"], ids=["ff", "nul", "esc", "fffe"])
def test_text_the_store_cannot_hold_is_refused_on_the_form(store, char):
    page = Page(store)
    one = _three(store)
    before = snapshot(store)
    sends = [
        ("/add", {"name": f"scan{char}page", "label0": "a", "value0": "v"},
         "The name holds a character that cannot be saved"),
        ("/add", {"name": "scan", "label0": "a", "value0": f"v{char}w"},
         "A value holds a character that cannot be saved"),
        ("/add", {"name": "scan", "label0": "a", "value0": "v", "add1": "1", "label1": "b",
                  "value1": f"v{char}w"}, "A value holds a character that cannot be saved"),
        (f"/s/{one.id}/edit", {"replace1": "1", "value1": f"v{char}w"},
         "A value holds a character that cannot be saved"),
        (f"/s/{one.id}/edit", {"value0": f"plain{char}"},
         "A value holds a character that cannot be saved"),
        (f"/s/{one.id}/edit", {"add3": "1", "label3": "n", "value3": f"v{char}"},
         "A value holds a character that cannot be saved"),
    ]
    for path, form, sentence in sends:
        response = post(page, path, {**form_of(page, path), **form})
        assert response.status == 200, (path, form)
        assert "Secret not saved" in text(response) and sentence in text(response)
        assert snapshot(store) == before
    # The same forms without the character save.
    assert post(page, "/add", {**form_of(page, "/add"), "name": "scanpage", "label0": "a",
                               "value0": "v"}).status == 303


def test_an_existing_secret_with_many_fields_saves_its_own_edit_form(store):
    """A secret Casa or a plugin made may hold more fields than the page lets anyone add; its
    edit form, sent as rendered, is read and saved like any other."""
    one = make(store, "many", [plain(f"f{index}", "v") for index in range(193)])
    page = Page(store)
    form = _Form(text(get(page, f"/s/{one.id}/edit")), f"/s/{one.id}/edit")
    assert len(form.controls) > 200
    response = post(page, f"/s/{one.id}/edit", {}, body=form.sent(value0="changed"))
    assert response.status == 303
    after = store.get(one.id)
    assert [f.value for f in after.fields] == ["changed"] + ["v"] * 192


def test_a_change_to_the_tags_alone_within_the_same_second_is_still_seen(store, monkeypatch):
    import pykeepass.baseelement
    frozen = datetime(2026, 11, 22, 13, 7, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(pykeepass.baseelement, "datetime", Frozen)
    one = _three(store)
    page = Page(store)
    form = form_of(page, f"/s/{one.id}/edit")
    store.replace(one.id, title=None, category=None, tags=["casa-agent-bot"],
                  fields=list(one.fields), writer=one.writer)
    changed = store.get(one.id)
    assert (changed.updated_at, changed.writer, changed.fields) == (
        one.updated_at, one.writer, one.fields)
    response = post(page, f"/s/{one.id}/edit", {**form, "replace1": "on", "value1": SENTINEL})
    assert response.status == 409
    assert store.get(one.id) == changed


def test_a_change_to_the_title_alone_within_the_same_second_is_still_seen(store, monkeypatch):
    """A secret renamed meanwhile is not changed through a form opened under its old name."""
    import pykeepass.baseelement
    frozen = datetime(2026, 11, 22, 13, 7, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(pykeepass.baseelement, "datetime", Frozen)
    one = _three(store)
    page = Page(store)
    form = form_of(page, f"/s/{one.id}/edit")
    store.replace(one.id, title=f"{one.title} renamed", category=None, tags=None,
                  fields=list(one.fields), writer=one.writer)
    changed = store.get(one.id)
    assert (changed.updated_at, changed.writer, changed.tags, changed.fields) == (
        one.updated_at, one.writer, one.tags, one.fields)
    assert changed.title != one.title
    response = post(page, f"/s/{one.id}/edit", {**form, "remove0": "on"})
    assert response.status == 409
    assert store.get(one.id) == changed


def test_a_reorder_within_the_same_second_is_still_seen(store, monkeypatch):
    """The form names stored values by position, so their order is part of what the stamp
    covers: values moved meanwhile are not overwritten through their old places."""
    import pykeepass.baseelement
    frozen = datetime(2026, 11, 22, 13, 7, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(pykeepass.baseelement, "datetime", Frozen)
    one = _three(store)
    page = Page(store)
    form = form_of(page, f"/s/{one.id}/edit")
    a, b, c = one.fields
    store.replace(one.id, title=None, category=None, tags=None, fields=[a, c, b],
                  writer=one.writer)
    changed = store.get(one.id)
    assert (changed.updated_at, changed.writer) == (one.updated_at, one.writer)
    assert [field.label for field in changed.fields] == ["a", "c", "b"]
    response = post(page, f"/s/{one.id}/edit", {**form, "replace1": "on", "value1": "for-b"})
    assert response.status == 409
    assert store.get(one.id) == changed


def test_a_change_to_a_values_label_alone_within_the_same_second_is_still_seen(store, monkeypatch):
    """A value given another label is not removed through a form that showed the old one."""
    import dataclasses

    import pykeepass.baseelement
    frozen = datetime(2026, 11, 22, 13, 7, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(pykeepass.baseelement, "datetime", Frozen)
    one = _three(store)
    page = Page(store)
    form = form_of(page, f"/s/{one.id}/edit")
    a, b, c = one.fields
    store.replace(one.id, title=None, category=None, tags=None,
                  fields=[dataclasses.replace(a, label=f"{a.label} renamed"), b, c],
                  writer=one.writer)
    changed = store.get(one.id)
    assert (changed.updated_at, changed.writer, changed.tags) == (
        one.updated_at, one.writer, one.tags)
    assert [(f.id, f.type, f.value) for f in changed.fields] == [
        (f.id, f.type, f.value) for f in one.fields]
    assert changed.fields[0].label != a.label
    response = post(page, f"/s/{one.id}/edit", {**form, "remove0": "on"})
    assert response.status == 409
    assert store.get(one.id) == changed


def test_a_change_to_a_values_type_alone_within_the_same_second_is_still_seen(store, monkeypatch):
    """A plain value made hidden keeps its label, text and writer, but is shown and edited
    differently: the stamp covers a value's type, and a form opened before is not saved."""
    import dataclasses

    import pykeepass.baseelement
    frozen = datetime(2026, 11, 22, 13, 7, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(pykeepass.baseelement, "datetime", Frozen)
    one = _three(store)
    page = Page(store)
    form = form_of(page, f"/s/{one.id}/edit")
    a, b, c = one.fields
    store.replace(one.id, title=None, category=None, tags=None,
                  fields=[dataclasses.replace(a, type="CONCEALED"), b, c], writer=one.writer)
    changed = store.get(one.id)
    assert (changed.updated_at, changed.writer, changed.tags) == (
        one.updated_at, one.writer, one.tags)
    assert [(f.id, f.label, f.value) for f in changed.fields] == [
        (f.id, f.label, f.value) for f in one.fields]
    assert [f.type for f in changed.fields] == ["CONCEALED", "CONCEALED", "CONCEALED"]
    response = post(page, f"/s/{one.id}/edit", {**form, "remove0": "on"})
    assert response.status == 409
    assert store.get(one.id) == changed
