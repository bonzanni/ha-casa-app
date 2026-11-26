"""The page engine under a profile that is not Casa's.

`vault_page.Page` takes everything about what an item is from a `Profile`.
Casa's own tests cannot tell whether the engine reads the profile or still
holds Casa's words and rules, because under Casa's profile both give the
same page. So these tests drive the engine with a profile that differs from
Casa's in every field (`FIXED`: fixed boxes, a label kept from the person,
its own words, texts, limits and rules), through `Page.handle` on a real
store, and each field has a test that fails when the engine uses Casa's
value or a literal instead of the profile's.

What they protect, in order of cost: a value the profile hides is never in a
GET, whatever its stored type; a label the profile keeps from the person is
never shown, never a Show target and never a control, and is carried through
every save; a save the profile's rules refuse writes nothing; and what a
person reads and where each link leads are the profile's.
"""
import dataclasses
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode

import pytest

import vault_page
import vault_page_casa
from vault_page import Box, Facts, Page, Profile, Request, Slots
from vault_page_casa import casa_page
from vault_store import Field, Store

PREFIX = "/api/hassio_ingress/abc"
ORIGIN = "https://ha.example"
SENTINEL = "sentinel5551"
KEPT_VALUE = "kept-value-9"  # no id holds "-" or "9"

# What the profile was told about, in the order it was told.
saved, deleted = [], []

WORDS = {
    "product": "Shelf",
    "open_through": "Reach this through the cupboard.",
    "list": "Inventory",
    "search": "Find a thing",
    "no_match": "Nothing by that name",
    "none_yet": "Bare boards",
    "none_yet_help": "Put the first thing here.",
    "add": "New thing",
    "edit": "Change thing",
    "delete": "Drop thing",
    "back_to_list": "To the inventory",
    "back_to_app": "To the cupboard",
    "unavailable": "Out of reach",
    "unavailable_help": "The cupboard would not open.",
    "use_show": "Press Show beside the thing.",
    "not_found": "No such thing here.",
    "name_help": "A thing keeps its name for good.",
    "name_taken": "A thing called [{}] is there already.",
    "no_value_left": "Leave one value or drop the thing.",
    "too_many": "A thing holds {} values at most.",
    "not_saved": "Thing not kept",
    "changed": "The thing changed meanwhile.",
    "reload": "Fetch the thing again",
    "read_once": "Looked at once",
    "read_many": "Looked at {} times",
    "delete_effect": "Whatever leans on it will fall.",
    "count_one": "{} thing found",
    "count_many": "{} things found",
}
# The words the page hands its script, by the attribute of `<body>` that carries each.
CARRIED = {"not_saved": "data-not-saved", "no_value_left": "data-no-value-left",
           "count_one": "data-count-one", "count_many": "data-count-many"}
ADDRESS_MISSING = "Give at least one address."
BOXES = (
    Box("address", "URL", "Where it lives", multiline=True, required=ADDRESS_MISSING),
    Box("user", "STRING", "Who holds it"),
    Box("pass", "CONCEALED", "Pass phrase"),
    Box("seed", "OTP", "Seed"),
)
NEEDS_ONE = "A thing needs a user or a pass."
NAME_REFUSED = "That name is not for things."


def _facts(item):
    """An item tagged `weighed` shows its `user` as a fact rather than as a value."""
    if "weighed" not in item.tags:
        return None
    return Facts(heading="About this thing",
                 rows=tuple(("Keeper", field.value) for field in item.fields
                            if field.label == "user"),
                 note="Weighed on the shelf.", labels=frozenset({"user"}))


def _check(name, fields):
    have = {field.label: field.value for field in fields}
    if name == "nope":
        return NAME_REFUSED
    if not have.get("user") and not have.get("pass"):
        return NEEDS_ONE
    return None


def _stored(item, fields):
    """The person's fields with `version`: 1 for a new item, one more on every change."""
    if item is None:
        return [*fields, Field(id="", label="version", type="STRING", value="1")]
    return [dataclasses.replace(field, value=str(int(field.value) + 1))
            if field.label == "version" else field for field in fields]


FIXED = Profile(
    root="things",
    words=WORDS,
    back=lambda prefix: f"{prefix}/elsewhere",
    summary=lambda item: f"row of {item.title}",
    origin=lambda item: f"came from {item.writer}",
    delete_warning=lambda item: "" if "quiet" in item.tags else f"Losing {item.title} matters.",
    heading=lambda item, field: f"The {field.label}",
    is_hidden=lambda field: field.label in ("pass", "seed"),
    kept=frozenset({"version"}),
    facts=_facts,
    editable=lambda item: "fixed" not in item.tags,
    references=lambda item: False,
    slots=Slots(boxes=BOXES),
    category="THING",
    name_limit=20,
    check=_check,
    stored=_stored,
    after_save=lambda item: saved.append(item.id),
    after_delete=lambda item: deleted.append(item.id),
)
# The same profile with no box required: every stored value can be removed.
LOOSE = dataclasses.replace(FIXED, slots=Slots(
    boxes=(dataclasses.replace(BOXES[0], required=""), *BOXES[1:])))
# The same profile with free slots, for what only free slots show.
FREE = dataclasses.replace(FIXED, slots=Slots(defaults=("first", "second"),
                                              suggestions=("alpha", "beta")),
                           kept=frozenset(), check=lambda name, fields: None,
                           stored=lambda item, fields: fields)


@pytest.fixture
def store(tmp_path):
    return Store.open(tmp_path, None)


@pytest.fixture(autouse=True)
def _nothing_told_yet():
    saved.clear()
    deleted.clear()


def get(page, path, method="GET"):
    return page.handle(Request(method=method, path=path,
                               headers={"x-ingress-path": PREFIX}, body=b""))


def post(page, path, form, *, accept=None):
    sent = {"x-ingress-path": PREFIX, "x-forwarded-proto": "https",
            "x-forwarded-host": "ha.example", "origin": ORIGIN,
            "content-type": "application/x-www-form-urlencoded"}
    if accept is not None:
        sent["accept"] = accept
    return page.handle(Request("POST", path, sent, urlencode(form).encode("utf-8")))


def text(response):
    return response.body.decode("utf-8")


def _forms(body, root, action):
    target = f"{PREFIX}/{root}{action}"
    return re.findall(r'<form[^>]*action="' + re.escape(target) + r'"[^>]*>(.*?)</form>',
                      body, re.DOTALL)


def form_of(page, get_path, action=None, *, root="things", nth=0):
    """The hidden inputs (code, stamp, field) of a form on the page GET_PATH answers."""
    forms = _forms(text(get(page, get_path)), root, action or get_path)
    return dict(re.findall(r'<input type="hidden" name="([a-z0-9]+)" value="([^"]*)">',
                           forms[nth]))


def names_of(body, action, *, root="things"):
    """Every control name the form posting to ACTION renders."""
    [form] = _forms(body, root, action)
    return set(re.findall(r'\sname="([^"]+)"', form))


class _Read(HTMLParser):
    """What a person reads on a page: its text, and the words of the attributes that are read
    out or shown (`placeholder`, `aria-label`). Ids, classes and addresses are not read."""

    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        self.parts += [value or "" for name, value in attrs
                       if name in ("placeholder", "aria-label")]

    def handle_data(self, data):
        self.parts.append(data)


def read(body):
    return "\n".join(_Read(body).parts)


class _Carried(HTMLParser):
    """The `data-` attributes of a page's `<body>`: the words it hands its script."""

    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.words = {}
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.words = {name: value for name, value in attrs if name.startswith("data-")}


def carried(body):
    return _Carried(body).words


class _ErrorBox(HTMLParser):
    """The lines of the `#form-error` box, or None when the page has none or hides it."""

    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.lines, self._depth, self._line = None, 0, None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self._depth:
            self._depth += 1
            if tag == "p":
                self._line = ""
        elif attrs.get("id") == "form-error" and "hidden" not in attrs:
            self._depth, self.lines = 1, []

    def handle_endtag(self, tag):
        if not self._depth:
            return
        self._depth -= 1
        if tag == "p" and self._line is not None:
            self.lines.append(self._line)
            self._line = None

    def handle_data(self, data):
        if self._line is not None:
            self._line += data


def error_lines(body):
    return _ErrorBox(body).lines


def thing(store, title="t", *, tags=(), writer="op", version="1", **values):
    """An item of the test profile's shape: the given boxes in their order, then `version`.

    `password` stands for the box `pass`, which is a word Python keeps.
    """
    if "password" in values:
        values["pass"] = values.pop("password")
    fields = [Field(id="", label=box.label, type=box.type, value=values[box.label])
              for box in BOXES if box.label in values]
    if version is not None:
        fields.append(Field(id="", label="version", type="STRING", value=version))
    return store.create(title=title, category="THING", tags=list(tags), fields=fields,
                        writer=writer)


def whole(store, title="t", **more):
    """An item with an address, a user and a pass; MORE replaces or adds to them."""
    values = {"address": "https://a.example", "user": "me", "password": "pw-old-1", **more}
    return thing(store, title, **values)


def snapshot(store):
    return [(i.id, i.title, i.writer, i.updated_at, i.fields) for i in store.list()]


def values_of(one):
    return {field.label: field for field in one.fields}


def _freeze_the_store_s_clock(monkeypatch):
    """The store keeps change times in whole seconds; with its clock frozen, a second write
    carries the same time as the first, and only what it wrote tells them apart."""
    import pykeepass.baseelement
    frozen = datetime(2026, 11, 22, 13, 7, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(pykeepass.baseelement, "datetime", Frozen)


# -- the two profiles -------------------------------------------------------------

def test_the_test_profile_differs_from_casa_s_in_every_field():
    """Otherwise a test here could pass on an engine that reads Casa's value."""
    casa = vault_page_casa.PROFILE
    for field in dataclasses.fields(Profile):
        assert getattr(FIXED, field.name) != getattr(casa, field.name), field.name
    assert set(WORDS) == set(casa.words) == set(vault_page.WORDS)
    for key in WORDS:
        assert WORDS[key] != casa.words[key], key
    # No word is part of another, so finding one on a page is finding that one.
    for key, word in WORDS.items():
        for other_key, other in WORDS.items():
            assert key == other_key or word.format("7") not in other.format("7"), (key, other_key)


def test_a_profile_without_one_of_the_engine_s_words_is_refused():
    for key in WORDS:
        short = {name: word for name, word in WORDS.items() if name != key}
        with pytest.raises(ValueError, match=key):
            dataclasses.replace(FIXED, words=short)


# -- adding -------------------------------------------------------------------------

def test_add_with_fixed_boxes_stores_the_profile_s_shape(store):
    page = Page(store, FIXED)
    body = text(get(page, "/add"))
    assert names_of(body, "/add") == {"code", "name", "value0", "value1", "value2", "value3"}
    response = post(page, "/add", {
        **form_of(page, "/add"), "name": " box ",
        "value0": "https://a.example\r\nhttps://b.example", "value1": "me",
        "value2": "pw1", "value3": ""})
    assert response.status == 303
    [one] = store.list()
    assert dict(response.headers)["Location"] == f"{PREFIX}/things/s/{one.id}"
    assert (one.title, one.category, one.tags, one.writer) == (
        "box", "THING", (), vault_page.WRITER)
    # each box under its own label and type, an empty box not at all, then what the profile adds
    assert [(f.label, f.type, f.value) for f in one.fields] == [
        ("address", "URL", "https://a.example\nhttps://b.example"),
        ("user", "STRING", "me"), ("pass", "CONCEALED", "pw1"), ("version", "STRING", "1")]
    assert saved == [one.id] and deleted == []


def test_a_box_is_drawn_with_its_caption_and_its_height(store):
    body = text(get(Page(store, FIXED), "/add"))
    # several lines for the box that takes them, one line for the others; no label to type
    assert ('<label class="v-field" for="f-value0">Where it lives<textarea class="v-textarea" '
            'id="f-value0" name="value0" autocomplete="off"') in body
    for index, caption in ((1, "Who holds it"), (2, "Pass phrase"), (3, "Seed")):
        assert (f'<label class="v-field" for="f-value{index}">{caption}<textarea '
                f'class="v-textarea v-textarea--line" id="f-value{index}" name="value{index}" '
                'rows="1"') in body
    assert 'name="label' not in body and 'name="add' not in body
    assert "v-picks" not in body and "Reference label" not in body
    assert '<p class="v-help" id="name-help">A thing keeps its name for good.</p>' in body
    assert "<title>New thing · Shelf</title>" in body


def _box_tags(body):
    """Each text box of the form by its name: the attributes the script reads from it."""
    return {name: tag for tag, name in re.findall(r'(<textarea[^>]*\sname="([a-z]+\d+)"[^>]*>)',
                                                  body)}


def test_a_fixed_box_tells_the_script_it_is_fixed_and_what_its_refusal_says(store):
    """The script reads a form's shape from the page: a fixed box says so, and a required one
    carries the sentence that refuses it; a free slot says it is one."""
    for body in (text(get(Page(store, FIXED), "/add")),
                 text(get(Page(store, FIXED), f"/s/{thing(store, 'bare', user='me').id}/edit"))):
        tags = _box_tags(body)
        offered = [name for name, tag in tags.items() if "data-fixed-value" in tag]
        assert offered and all("data-new-value" not in tag for tag in tags.values())
        for name in offered:
            required = re.findall(r'\sdata-required="([^"]*)"', tags[name])
            caption = re.search(rf'for="f-{name}">([^<]+)<', body).group(1)
            assert required == ([ADDRESS_MISSING] if caption == "Where it lives" else []), name
        assert "data-required" not in body.replace(f'data-required="{ADDRESS_MISSING}"', "")
    # a stored value is not a box to fill: its card carries neither
    one = whole(store, "whole")
    edit = text(get(Page(store, FIXED), f"/s/{one.id}/edit"))
    for name in ("value0", "value1", "value2"):
        assert "data-fixed-value" not in _box_tags(edit)[name], name
    assert "data-fixed-value" not in text(get(casa_page(store), "/add"))
    assert text(get(casa_page(store), "/add")).count("data-new-value") == 3


def test_a_required_box_left_empty_is_refused_with_its_sentence(store):
    page = Page(store, FIXED)
    response = post(page, "/add", {**form_of(page, "/add"), "name": "box", "value0": "",
                                   "value1": "me", "value2": SENTINEL})
    body = text(response)
    assert response.status == 200
    assert ADDRESS_MISSING in body and "Thing not kept" in body
    assert SENTINEL not in body
    assert store.list() == [] and saved == []
    # With nothing typed at all, that box is still what is asked for.
    response = post(page, "/add", {**form_of(page, "/add"), "name": "box"})
    assert response.status == 200 and ADDRESS_MISSING in text(response)
    assert store.list() == [] and saved == []
    # An edit that empties the required value is the same slip.
    one = whole(store)
    before = snapshot(store)
    response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                "value0": "", "value1": "you"})
    assert response.status == 200
    assert ADDRESS_MISSING in text(response)
    assert snapshot(store) == before and saved == []
    # Its card has no Remove, and a Remove sent for it is not a name the form rendered.
    body = text(get(page, f"/s/{one.id}/edit"))
    cards = re.findall(r'<section class="v-panel v-stack v-existing".*?</section>', body,
                       re.DOTALL)
    assert [("Remove" in card, "Removed when you save." in card) for card in cards] == [
        (False, False), (True, True), (True, True)]
    assert "remove0" not in names_of(body, f"/s/{one.id}/edit")
    response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                "remove0": "1"})
    assert response.status == 400
    assert snapshot(store) == before and saved == []
    # Where the box is not required, the same value has a Remove that removes it.
    loose = Page(store, LOOSE)
    assert "remove0" in names_of(text(get(loose, f"/s/{one.id}/edit")), f"/s/{one.id}/edit")
    assert post(loose, f"/s/{one.id}/edit", {**form_of(loose, f"/s/{one.id}/edit"),
                                             "remove0": "1"}).status == 303
    assert [f.label for f in store.get(one.id).fields] == ["user", "pass", "version"]
    one = whole(store, "again")
    # A box that is not required may stay empty, on both forms.
    assert post(page, "/add", {**form_of(page, "/add"), "name": "other",
                               "value0": "https://a.example", "value2": "pw"}).status == 303
    assert post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                            "remove1": "1"}).status == 303


def test_an_add_with_every_box_empty_is_refused(store):
    optional = dataclasses.replace(FIXED, slots=Slots(boxes=BOXES[1:]),
                                   check=lambda name, fields: None)
    page = Page(store, optional)
    response = post(page, "/add", {**form_of(page, "/add"), "name": "box"})
    assert response.status == 200
    assert "Enter a value." in text(response)
    assert store.list() == [] and saved == []


def test_a_refused_form_gives_plain_boxes_back_and_clears_hidden_ones(store):
    page = Page(store, FIXED)
    response = post(page, "/add", {**form_of(page, "/add"), "name": "",
                                   "value0": "https://typed.example\r\nhttps://more.example",
                                   "value1": "typed-user", "value2": SENTINEL,
                                   "value3": SENTINEL + "seed"})
    body = text(response)
    assert response.status == 200 and "Enter a name." in body
    assert re.search(r'name="value0"[^>]*>\nhttps://typed.example\r?\nhttps://more.example'
                     r"</textarea>", body)
    assert re.search(r'name="value1"[^>]*>\ntyped-user</textarea>', body)
    assert SENTINEL not in body
    assert "The values you typed were cleared. Enter them again." in body
    # Nothing was cleared when only plain boxes were filled.
    response = post(page, "/add", {**form_of(page, "/add"), "name": "",
                                   "value0": "https://typed.example", "value1": "typed-user"})
    assert "cleared" not in text(response)
    # Nor when only stored plain values were typed over, whatever their stored type.
    one = whole(store)
    response = post(page, f"/s/{one.id}/edit", {
        **form_of(page, f"/s/{one.id}/edit"), "value0": "https://typed.example",
        "value1": "typed\x0cuser"})
    body = text(response)
    assert response.status == 200 and "Thing not kept" in body
    assert re.search(r'name="value0"[^>]*>\nhttps://typed.example</textarea>', body)
    assert "cleared" not in body
    # The edit form's offered box does the same.
    one = thing(store, "partial", password="pw-old-1")
    response = post(page, f"/s/{one.id}/edit", {
        **form_of(page, f"/s/{one.id}/edit"), "value2": "https://typed.example",
        "value3": "typed-user", "value4": SENTINEL + "\x0c"})
    body = text(response)
    assert response.status == 200 and "Thing not kept" in body
    assert re.search(r'name="value2"[^>]*>\nhttps://typed.example</textarea>', body)
    assert re.search(r'name="value3"[^>]*>\ntyped-user</textarea>', body)
    assert SENTINEL not in body and "The values you typed were cleared." in body


def test_a_name_over_the_limit_is_refused(store):
    page = Page(store, FIXED)
    form = {"value0": "https://a.example", "value1": "me"}
    response = post(page, "/add", {**form_of(page, "/add"), "name": "n" * 21, **form})
    body = text(response)
    assert response.status == 200
    assert ('<p class="v-field-error" id="name-error">Use a name of at most 20 characters.</p>'
            in body)
    assert store.list() == [] and saved == []
    assert post(page, "/add", {**form_of(page, "/add"), "name": "n" * 20,
                               **form}).status == 303
    # Casa's page has no such limit.
    casa = casa_page(store)
    assert post(casa, "/add", {**form_of(casa, "/add", root="vault"), "name": "c" * 500,
                               "label0": "token", "value0": "v"}).status == 303


def test_check_refuses_add_and_edit_with_its_sentence(store):
    page = Page(store, FIXED)
    response = post(page, "/add", {**form_of(page, "/add"), "name": "box",
                                   "value0": "https://a.example", "value3": SENTINEL})
    body = text(response)
    assert response.status == 200
    assert NEEDS_ONE in body and "Thing not kept" in body
    assert SENTINEL not in body
    assert store.list() == [] and saved == []
    # Removing the pass from an item whose user is empty is refused the same way.
    one = thing(store, "t", address="https://a.example", user="", password="pw-old-1")
    before = snapshot(store)
    response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                "remove2": "1"})
    assert response.status == 200
    assert NEEDS_ONE in text(response)
    assert snapshot(store) == before and saved == []


def test_check_is_given_the_name_and_the_fields_the_save_would_store(store):
    seen = []

    def check(name, fields):
        seen.append((name, [(f.label, f.value) for f in fields]))
        return _check(name, fields)

    page = Page(store, dataclasses.replace(FIXED, check=check))
    response = post(page, "/add", {**form_of(page, "/add"), "name": "nope",
                                   "value0": "https://a.example", "value1": "me"})
    assert response.status == 200 and NAME_REFUSED in text(response)
    assert store.list() == []
    one = whole(store, "nope")
    before = snapshot(store)
    response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                "value1": "you"})
    assert response.status == 200 and NAME_REFUSED in text(response)
    assert snapshot(store) == before
    # what the profile adds or raises is part of what it is asked about
    assert seen == [
        ("nope", [("address", "https://a.example"), ("user", "me"), ("version", "1")]),
        ("nope", [("address", "https://a.example"), ("user", "you"), ("pass", "pw-old-1"),
                  ("version", "2")])]


# -- a label kept from the person -----------------------------------------------------

def test_a_kept_label_has_no_card_no_show_and_no_control(store):
    one = whole(store, version=KEPT_VALUE)
    page = Page(store, FIXED)
    for path in ("/", f"/s/{one.id}", f"/s/{one.id}/edit", f"/s/{one.id}/delete"):
        body = text(get(page, path))
        assert "version" not in body and KEPT_VALUE not in body, path
    # the fourth stored field is the kept one: no control of the edit form names it
    assert names_of(text(get(page, f"/s/{one.id}/edit")), f"/s/{one.id}/edit") == {
        "code", "stamp", "value0", "value1", "remove1", "value2", "remove2", "replace2",
        "value4"}
    before = snapshot(store)
    for accept in (None, "application/json"):
        show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
        response = post(page, f"/s/{one.id}/show", {**show, "field": "3"}, accept=accept)
        assert response.status == 404
        assert KEPT_VALUE not in text(response)
    assert store.reads(one.id) == 0
    # the field before it is still a target
    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
    assert post(page, f"/s/{one.id}/show", {**show, "field": "2"},
                accept="application/json").status == 200
    for extra in ({"value3": "5"}, {"remove3": "1"}, {"replace3": "1", "value3": "5"}):
        response = post(page, f"/s/{one.id}/edit",
                        {**form_of(page, f"/s/{one.id}/edit"), **extra})
        assert response.status == 400, extra
        assert KEPT_VALUE not in text(response)
    assert snapshot(store) == before and saved == []


def test_the_one_value_beside_a_kept_one_is_copied_from_the_list(store):
    """An item with one value a person sees is copied from its row, and the form names that
    value by where it is stored, which is not first when a kept field comes before it."""
    one = store.create(title="solo", category="THING", tags=[], writer="op", fields=[
        Field(id="", label="version", type="STRING", value=KEPT_VALUE),
        Field(id="", label="pass", type="CONCEALED", value=SENTINEL)])
    weighed = thing(store, "weighed", tags=["weighed"], password="pw-old-1")
    page = Page(store, FIXED)
    body = text(get(page, "/"))
    assert SENTINEL not in body and KEPT_VALUE not in body
    [form] = _forms(body, "things", f"/s/{one.id}/show")
    assert 'name="field" value="1"' in form
    show = form_of(page, "/", f"/s/{one.id}/show")
    answer = post(page, f"/s/{one.id}/show", show, accept="application/json")
    assert answer.status == 200 and SENTINEL in text(answer)
    # its page heads the value "Value", as for any item with one
    assert '<h2 class="v-sr-only">Value</h2>' in text(get(page, f"/s/{one.id}"))
    # an item with a facts block opens instead, however many values it has
    assert _forms(body, "things", f"/s/{weighed.id}/show") == []
    assert (f'<a class="v-button v-button--quiet" href="{PREFIX}/things/s/{weighed.id}">Open</a>'
            in body)


def test_an_edit_carries_the_kept_field_and_raises_it(store):
    one = whole(store)
    page = Page(store, FIXED)
    response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                "replace2": "1", "value2": "pw-new-2"})
    assert response.status == 303
    after = store.get(one.id)
    assert after.fields == (
        one.fields[0], one.fields[1],
        dataclasses.replace(one.fields[2], value="pw-new-2"),
        dataclasses.replace(one.fields[3], value="2"))
    assert after.writer == vault_page.WRITER
    assert saved == [one.id]


def test_an_edit_that_changes_nothing_writes_nothing(store):
    one = whole(store)
    page = Page(store, FIXED)
    before = snapshot(store)
    response = post(page, f"/s/{one.id}/edit", {
        **form_of(page, f"/s/{one.id}/edit"), "value0": "https://a.example", "value1": "me",
        "value2": "typed-without-replace"})
    assert response.status == 303
    assert snapshot(store) == before
    after = store.get(one.id)
    assert (after.updated_at, after.writer, values_of(after)["version"].value) == (
        one.updated_at, "op", "1")
    assert saved == []


def test_a_stale_edit_is_refused_when_only_the_kept_field_changed(store, monkeypatch):
    _freeze_the_store_s_clock(monkeypatch)
    one = whole(store)
    page = Page(store, FIXED)
    form = form_of(page, f"/s/{one.id}/edit")
    bumped = [dataclasses.replace(field, value="2") if field.label == "version" else field
              for field in one.fields]
    store.replace(one.id, title=None, category=None, tags=None, fields=bumped,
                  writer=one.writer)
    changed = store.get(one.id)
    assert (changed.updated_at, changed.writer, changed.fields[:3]) == (
        one.updated_at, one.writer, one.fields[:3])
    response = post(page, f"/s/{one.id}/edit", {**form, "replace2": "1", "value2": SENTINEL})
    assert response.status == 409
    assert "The thing changed meanwhile." in text(response)
    assert store.get(one.id) == changed and saved == []


def test_removing_every_value_a_person_sees_is_refused(store):
    """The kept field alone is not a value: an item is never left with nothing to see."""
    one = whole(store)
    page = Page(store, LOOSE)
    before = snapshot(store)
    response = post(page, f"/s/{one.id}/edit", {
        **form_of(page, f"/s/{one.id}/edit"), "remove0": "1", "remove1": "1", "remove2": "1"})
    assert response.status == 200
    assert "Leave one value or drop the thing." in text(response)
    assert snapshot(store) == before and saved == []


# -- the edit form ------------------------------------------------------------------

def test_a_missing_box_is_offered_on_edit_with_its_fixed_label(store):
    one = whole(store)  # no seed
    page = Page(store, FIXED)
    body = text(get(page, f"/s/{one.id}/edit"))
    # one box after the four stored fields, under its caption, with no label to type
    assert names_of(body, f"/s/{one.id}/edit") >= {"value4"}
    assert not names_of(body, f"/s/{one.id}/edit") & {"value5", "add4", "label4"}
    assert ('<label class="v-field" for="f-value4">Seed<textarea class="v-textarea '
            'v-textarea--line" id="f-value4" name="value4" rows="1"') in body
    assert 'name="label' not in body and 'name="add' not in body
    response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                "value4": "JBSWY3DP"})
    assert response.status == 303
    after = store.get(one.id)
    assert [(f.label, f.type, f.value) for f in after.fields] == [
        ("address", "URL", "https://a.example"), ("user", "STRING", "me"),
        ("pass", "CONCEALED", "pw-old-1"), ("version", "STRING", "2"),
        ("seed", "OTP", "JBSWY3DP")]
    assert after.fields[:3] == one.fields[:3]
    # With every box stored, the form offers none.
    assert not any(name in names_of(text(get(page, f"/s/{one.id}/edit")), f"/s/{one.id}/edit")
                   for name in ("value5", "value6"))
    # An item that lacks several is offered each, in the boxes' order.
    bare = thing(store, "bare", user="me")
    body = text(get(page, f"/s/{bare.id}/edit"))
    assert [(index, caption) for index, caption in re.findall(
        r'<label class="v-field" for="f-value(\d)">([^<]+)<textarea', body)
        if int(index) >= 2] == [("2", "Where it lives"), ("3", "Pass phrase"), ("4", "Seed")]


def test_a_value_the_profile_does_not_hide_is_shown_in_clear(store):
    """The profile says which values are hidden, not the stored type: an address stored as
    `URL` is in the clear, and a pass stored as plain text is not."""
    one = store.create(title="t", category="THING", tags=[], writer="op", fields=[
        Field(id="", label="address", type="URL", value="https://clear.example"),
        Field(id="", label="pass", type="STRING", value=SENTINEL),
        Field(id="", label="seed", type="OTP", value=SENTINEL + "seed")])
    page = Page(store, FIXED)
    shown = text(get(page, f"/s/{one.id}"))
    assert '<pre class="v-value" data-plain>\nhttps://clear.example</pre>' in shown
    edit = text(get(page, f"/s/{one.id}/edit"))
    assert re.search(r'name="value0"[^>]*>\nhttps://clear.example</textarea>', edit)
    assert names_of(edit, f"/s/{one.id}/edit") >= {"value0", "replace1", "replace2"}
    assert "replace0" not in names_of(edit, f"/s/{one.id}/edit")
    for path in ("/", f"/s/{one.id}", f"/s/{one.id}/edit", f"/s/{one.id}/delete"):
        for method in ("GET", "HEAD"):
            assert SENTINEL not in text(get(page, path, method=method)), path
    assert shown.count('<span class="v-dots" aria-label="Hidden value">') == 2
    # the hidden one is shown by its own form, with the code the page gave it
    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
    assert show["field"] == "1"
    answer = post(page, f"/s/{one.id}/show", show, accept="application/json")
    assert answer.status == 200 and f'"value": "{SENTINEL}"' in text(answer)
    # a typed value for the hidden one is saved only with Replace
    assert post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                            "value1": "ignored"}).status == 303
    assert store.get(one.id).fields == one.fields
    # and replacing it with nothing is the slip it is for any hidden value
    response = post(page, f"/s/{one.id}/edit", {**form_of(page, f"/s/{one.id}/edit"),
                                                "replace1": "1", "value1": ""})
    assert response.status == 200
    assert "Enter a value or turn off Replace." in text(response)
    assert store.get(one.id).fields == one.fields
    # A plain value may be emptied, whatever its stored type.
    loose = Page(store, LOOSE)
    assert post(loose, f"/s/{one.id}/edit", {**form_of(loose, f"/s/{one.id}/edit"),
                                             "value0": ""}).status == 303
    assert store.get(one.id).fields[0].value == ""


def test_a_value_s_heading_is_the_profile_s(store):
    one = whole(store)
    page = Page(store, FIXED)
    body = text(get(page, f"/s/{one.id}"))
    for label in ("address", "user", "pass"):
        assert f'aria-label="The {label}"><h2>The {label}</h2>' in body
        assert f"<h2>{label}</h2>" not in body
    body = text(get(page, f"/s/{one.id}/edit"))
    for label in ("address", "user", "pass"):
        assert (f'<section class="v-panel v-stack v-existing" aria-label="The {label}">'
                f'<div class="v-heading"><h2>The {label}</h2>') in body


def test_an_item_the_profile_does_not_let_edit_has_no_edit_form(store):
    locked = whole(store, "locked", tags=["fixed"])
    free = whole(store, "free")
    page = Page(store, FIXED)
    assert get(page, f"/s/{locked.id}/edit").status == 404
    assert ">Edit</a>" not in text(get(page, f"/s/{locked.id}"))
    code = page.codes.issue(f"/s/{locked.id}/edit")
    before = snapshot(store)
    response = post(page, f"/s/{locked.id}/edit", {"code": code, "remove1": "1"})
    assert response.status == 404
    assert snapshot(store) == before and saved == []
    # An item of the same category without the tag has its form, and the form saves.
    assert get(page, f"/s/{free.id}/edit").status == 200
    assert (f'<a class="v-button v-button--quiet" href="{PREFIX}/things/s/{free.id}/edit">Edit</a>'
            in text(get(page, f"/s/{free.id}")))
    assert post(page, f"/s/{free.id}/edit", {**form_of(page, f"/s/{free.id}/edit"),
                                             "value1": "you"}).status == 303
    # An item Casa's page would refuse to edit is the profile's to decide.
    key = store.create(title="key", category="SSH_KEY", tags=[], writer="op", fields=[
        Field(id="", label="user", type="STRING", value="me")])
    assert get(page, f"/s/{key.id}/edit").status == 200


# -- deleting -----------------------------------------------------------------------

def test_delete_calls_after_delete_once_and_removes_the_item(store):
    one = whole(store)
    other = whole(store, "other")
    page = Page(store, FIXED)
    # a form the page did not render deletes nothing and tells nobody
    assert post(page, f"/s/{one.id}/delete", {**form_of(page, f"/s/{one.id}/delete"),
                                              "surprise": "1"}).status == 400
    assert deleted == [] and len(store.list()) == 2
    response = post(page, f"/s/{one.id}/delete", form_of(page, f"/s/{one.id}/delete"))
    assert response.status == 303
    assert dict(response.headers)["Location"] == f"{PREFIX}/things/"
    assert [item.id for item in store.list()] == [other.id]
    assert deleted == [one.id] and saved == []


# -- words, texts and links -----------------------------------------------------------

LINKS = re.compile(r'\s(?:href|action|src)="([^"]*)"')
LINK = re.compile(re.escape(PREFIX) + r"/things/(|add|s/[a-z2-7]{26}(/edit|/delete|/show)?"
                  r"|static/(ha\.css|vault\.css|theme\.js|page\.js))")
BACK = re.compile(r'<a class="v-back" href="([^"]*)"')


def test_links_and_static_files_use_the_profile_s_root(store):
    one = whole(store)
    page = Page(store, FIXED)
    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show")
    bodies = [text(get(page, path)) for path in (
        "/add", f"/s/{one.id}", f"/s/{one.id}/edit", f"/s/{one.id}/delete",
        f"/s/{one.id}/show", "/nowhere")]
    bodies += [text(post(page, f"/s/{one.id}/show", show)),
               text(post(page, "/add", {"name": "y"})),                       # form expired
               text(post(page, "/add", {**form_of(page, "/add"), "x": "1"})),  # not rendered
               text(post(page, "/add", {**form_of(page, "/add"), "name": ""})),
               text(post(page, f"/s/{one.id}/edit", {
                   "code": page.codes.issue(f"/s/{one.id}/edit"), "stamp": "old"}))]
    for body in bodies:
        links = LINKS.findall(body)
        assert len(links) >= 6
        for link in links:
            assert LINK.fullmatch(link), link
    # The list's own links are under the root too; only its Back leads out, where the
    # profile says.
    for body in (text(get(page, "/")), text(get(Page(Store.open(store._path.parent / "empty",
                                                                None), FIXED), "/"))):
        [back] = BACK.findall(body)
        assert back == f"{PREFIX}/elsewhere"
        links = LINKS.findall(body)
        assert links.count(back) == 1
        for link in links:
            assert link == back or LINK.fullmatch(link), link
    static = re.findall(re.escape(PREFIX) + r'/things/static/([^"]+)', text(get(page, "/")))
    assert sorted(static) == ["ha.css", "page.js", "theme.js", "vault.css"]
    # A request's own path has no root: the front proxy has taken it off.
    assert get(page, f"/s/{one.id}").status == 200
    assert get(page, f"/things/s/{one.id}").status == 404
    assert vault_page.route_name("GET", f"/s/{one.id}") == "secret"
    assert vault_page.route_name("GET", f"/things/s/{one.id}") == "other"


def test_the_profile_s_texts_are_the_ones_shown(store):
    one = whole(store, tags=["weighed"])
    plain = whole(store, "plain")
    quiet = whole(store, "quiet", tags=["quiet"])
    page = Page(store, FIXED)
    listed = text(get(page, "/"))
    assert "<strong>t</strong><small>row of t</small></a>" in listed
    shown = text(get(page, f"/s/{one.id}"))
    assert re.search(r'<p class="v-meta">came from op · Updated [^<]+</p>', shown)
    assert "<title>t · Shelf</title>" in shown
    # the fact the profile gives, in the block it heads, and no card for the value it stands for
    assert ('<section class="v-panel v-stack"><div class="v-heading"><h2>About this thing</h2>'
            '</div><dl class="v-facts"><div><dt>Keeper</dt><dd class="v-reference">me</dd></div>'
            '</dl><p class="v-help">Weighed on the shelf.</p></section>') in shown
    assert "<h2>The user</h2>" not in shown and "<h2>The address</h2>" in shown
    assert "About this thing" not in text(get(page, f"/s/{plain.id}"))
    assert "<h2>The user</h2>" in text(get(page, f"/s/{plain.id}"))
    delete = text(get(page, f"/s/{one.id}/delete"))
    assert ('<div class="v-notice v-notice--warning"><strong>Losing t matters.</strong>'
            "<p>Whatever leans on it will fall.</p></div>") in delete
    # no warning where the profile gives none
    assert "v-notice--warning" not in text(get(page, f"/s/{quiet.id}/delete"))
    for path in ("/", f"/s/{one.id}", f"/s/{plain.id}", f"/s/{one.id}/edit"):
        assert "References" not in text(get(page, path)), path


def test_a_warning_without_a_second_line_is_one_line(store):
    one = whole(store)
    page = Page(store, dataclasses.replace(FIXED, words={**WORDS, "delete_effect": ""}))
    assert ('<div class="v-notice v-notice--warning"><strong>Losing t matters.</strong></div>'
            in text(get(page, f"/s/{one.id}/delete")))


def test_markup_in_what_the_profile_says_about_an_item_is_shown_as_text(store):
    one = whole(store, "<b>x</b>", user="<i>me</i>", tags=["weighed"])
    page = Page(store, FIXED)
    for path in ("/", f"/s/{one.id}", f"/s/{one.id}/edit", f"/s/{one.id}/delete"):
        body = text(get(page, path))
        assert "<b>x" not in body and "<i>me" not in body, path
    assert "row of &lt;b&gt;x&lt;/b&gt;" in text(get(page, "/"))
    assert '<dd class="v-reference">&lt;i&gt;me&lt;/i&gt;</dd>' in text(get(page, f"/s/{one.id}"))
    assert ("<strong>Losing &lt;b&gt;x&lt;/b&gt; matters.</strong>"
            in text(get(page, f"/s/{one.id}/delete")))


def test_the_unavailable_page_leads_back_by_the_profile():
    page = Page(None, FIXED)
    for path in ("/", "/add", "/s/" + "a" * 26):
        response = get(page, path)
        body = text(response)
        assert response.status == 503
        assert BACK.findall(body) == [f"{PREFIX}/elsewhere"]
        assert (f'<a class="v-button v-button--quiet" href="{PREFIX}/elsewhere">To the cupboard'
                "</a>") in body
        assert (f'<a class="v-button v-button--primary" href="{PREFIX}/things/">Try again</a>'
                in body)
        assert "<h2>Out of reach</h2><p>The cupboard would not open.</p>" in body
        assert "<title>Inventory · Shelf</title>" in body
        for link in LINKS.findall(body):
            assert link == f"{PREFIX}/elsewhere" or LINK.fullmatch(link), link


def _every_page(store):
    """Every kind of answer the engine gives under the test profile, by name."""
    one = whole(store)
    many = store.create(title="many", category="THING", tags=[], writer="op", fields=[
        Field(id="", label=f"f{index}", type="STRING", value="v")
        for index in range(vault_page.MAX_FIELDS)])
    page, loose = Page(store, FIXED), Page(store, LOOSE)
    edit = f"/s/{one.id}/edit"
    show = f"/s/{one.id}/show"

    def sent(path, form):
        return text(post(page, path, {**form_of(page, path), **form}))

    pages = {
        "no prefix": text(page.handle(Request("GET", "/", {}, b""))),
        "unavailable": text(get(Page(None, FIXED), "/")),
        "empty list": text(get(Page(Store.open(store._path.parent / "empty", None), FIXED), "/")),
        "list": text(get(page, "/")),
        "add": text(get(page, "/add")),
        "item": text(get(page, f"/s/{one.id}")),
        "edit": text(get(page, edit)),
        "delete": text(get(page, f"/s/{one.id}/delete")),
        "show by get": text(get(page, show)),
        "not found": text(get(page, "/nowhere")),
        "expired": text(post(page, "/add", {"name": "y"})),
        "not rendered": sent("/add", {"x": "1"}),
        "name missing": sent("/add", {"name": ""}),
        "name taken": sent("/add", {"name": "t", "value0": "https://a.example", "value1": "u"}),
        "no value left": text(post(loose, edit, {
            **form_of(loose, edit), "remove0": "1", "remove1": "1", "remove2": "1"})),
        "too many": sent(f"/s/{many.id}/edit", {f"value{vault_page.MAX_FIELDS}": "https://x"}),
        "changed": text(post(page, edit, {"code": page.codes.issue(edit), "stamp": "old"})),
        "shown": text(post(page, show, form_of(page, f"/s/{one.id}", show))),
    }
    pages["read once"] = text(get(page, f"/s/{one.id}"))
    store.note_read(one.id)
    pages["read twice"] = text(get(page, f"/s/{one.id}"))
    return pages


def test_every_word_a_person_reads_is_the_profile_s(store):
    """Each of the profile's words is on the page it belongs to, and no page says anything in
    Casa's words. What is read is the page's text, not its markup: the form's id and the
    stylesheet's name hold words of their own. The words the page's script shows are read from
    the attributes that carry them to it."""
    bodies = _every_page(store)
    pages = {name: read(body) for name, body in bodies.items()}
    handed = {name: carried(body) for name, body in bodies.items()}
    # every page that runs the script hands it the profile's words, and only those
    for name, words in handed.items():
        assert words == ({} if name == "no prefix"
                         else {attribute: WORDS[key] for key, attribute in CARRIED.items()}), name
    where = {
        "product": "list", "open_through": "no prefix", "list": "list", "search": "list",
        "no_match": "list", "none_yet": "empty list", "none_yet_help": "empty list",
        "add": "add", "edit": "edit", "delete": "delete", "back_to_list": "not found",
        "back_to_app": "unavailable", "unavailable": "unavailable",
        "unavailable_help": "unavailable", "use_show": "show by get", "not_found": "not found",
        "name_help": "add", "name_taken": "name taken", "no_value_left": "no value left",
        "too_many": "too many", "not_saved": "name missing", "changed": "changed",
        "reload": "changed", "read_once": "read once", "read_many": "read twice",
        "delete_effect": "delete", "count_one": "list", "count_many": "list",
    }
    assert set(where) == set(WORDS)
    shown_by_the_script = {"count_one", "count_many"}  # never in the page's own text
    filled = {"name_taken": "t", "too_many": str(vault_page.MAX_FIELDS), "read_many": "2"}
    for key, name in where.items():
        if key in shown_by_the_script:
            assert handed[name][CARRIED[key]] == WORDS[key], (key, name)
            continue
        assert WORDS[key].format(filled.get(key, "")) in pages[name], (key, name)
    # the same words where else they stand
    assert "New thing" in pages["empty list"] and "Drop thing" in pages["item"]
    assert "Change thing" in pages["changed"] and "Thing not kept" in pages["changed"]
    for name in ("not rendered", "show by get"):
        assert "To the inventory" in pages[name], name
    for name, seen in pages.items():
        for word in ("secret", "Secret", "Casa", "plugin", "Plugin", "SSH"):
            assert word not in seen, (name, word)
            assert not any(word in said for said in handed[name].values()), (name, word)


# -- free slots -----------------------------------------------------------------------

def test_free_slots_are_as_many_and_start_as_the_profile_says(store):
    page = Page(store, FREE)
    body = text(get(page, "/add"))
    assert names_of(body, "/add") == {"code", "name", "add0", "label0", "value0",
                                      "add1", "label1", "value1"}
    assert 'name="label0" value="first"' in body and 'name="label1" value="second"' in body
    assert re.findall(r'data-label="([^"]+)"', body) == ["alpha", "beta"] * 2
    for word in ("credential", "certificate", 'data-label="password"', 'data-label="username"'):
        assert word not in body
    response = post(page, "/add", {**form_of(page, "/add"), "name": "box", "label0": "one",
                                   "value0": "v0", "add1": "1", "label1": "two", "value1": "v1"})
    assert response.status == 303
    one = store.get("box")
    assert (one.category, [(f.label, f.type, f.value) for f in one.fields]) == (
        "THING", [("one", "CONCEALED", "v0"), ("two", "CONCEALED", "v1")])
    # a third slot is not one this form rendered
    assert post(page, "/add", {**form_of(page, "/add"), "name": "more", "label0": "one",
                               "value0": "v0", "add2": "1", "label2": "x",
                               "value2": "v"}).status == 400
    # the edit form's slots start with the suggestions not in use, then a numbered label
    body = text(get(page, f"/s/{one.id}/edit"))
    assert names_of(body, f"/s/{one.id}/edit") == {
        "code", "stamp", "value0", "remove0", "value1", "remove1",
        "add2", "label2", "value2", "add3", "label3", "value3"}
    assert [re.search(rf'name="label{index}" value="([^"]*)"', body).group(1)
            for index in (2, 3)] == ["alpha", "beta"]
    used = store.create(title="used", category="THING", tags=[], writer="op", fields=[
        Field(id="", label="alpha", type="STRING", value="a")])
    body = text(get(page, f"/s/{used.id}/edit"))
    assert [re.search(rf'name="label{index}" value="([^"]*)"', body).group(1)
            for index in (1, 2)] == ["beta", "value3"]


# -- Casa's own page ------------------------------------------------------------------

def test_casa_s_page_shows_and_edits_a_field_called_version(store):
    """Another profile keeps a label called `version` from the person. Casa's keeps none: a
    secret with a value of that name shows it, takes it as a Show target and edits it."""
    one = store.create(title="release", category="PASSWORD", tags=[], writer="op", fields=[
        Field(id="", label="version", type="STRING", value="1.2.3"),
        Field(id="", label="token", type="CONCEALED", value=SENTINEL)])
    page = casa_page(store)
    body = text(get(page, f"/s/{one.id}"))
    assert "<h2>version</h2>" in body
    assert '<pre class="v-value" data-plain>\n1.2.3</pre>' in body
    assert '<p class="v-reference">release/version</p>' in body
    assert "2 values · version, token" in text(get(page, "/"))
    show = form_of(page, f"/s/{one.id}", f"/s/{one.id}/show", root="vault")
    answer = post(page, f"/s/{one.id}/show", {**show, "field": "0"}, accept="application/json")
    assert answer.status == 200 and '"value": "1.2.3"' in text(answer)
    edit = text(get(page, f"/s/{one.id}/edit"))
    assert names_of(edit, f"/s/{one.id}/edit", root="vault") >= {"value0", "remove0"}
    assert re.search(r'name="value0"[^>]*>\n1.2.3</textarea>', edit)
    response = post(page, f"/s/{one.id}/edit", {
        **form_of(page, f"/s/{one.id}/edit", root="vault"), "value0": "1.2.4"})
    assert response.status == 303
    after = store.get(one.id)
    # exactly what was typed: nothing raises it, and the other value is as it was
    assert after.fields == (dataclasses.replace(one.fields[0], value="1.2.4"), one.fields[1])
    # and it can be removed like any other value
    assert post(page, f"/s/{one.id}/edit", {
        **form_of(page, f"/s/{one.id}/edit", root="vault"), "remove0": "1"}).status == 303
    assert [f.label for f in store.get(one.id).fields] == ["token"]
    # a new value may be called `version` too
    assert post(page, "/add", {**form_of(page, "/add", root="vault"), "name": "other",
                               "label0": "version", "value0": "9"}).status == 303
    assert [(f.label, f.value) for f in store.get("other").fields] == [("version", "9")]


def test_casa_s_refusals_are_said_in_the_box_a_person_sees(store):
    """Casa's words now also travel to the script in attributes of the page, so finding one in
    the answer proves nothing about the screen: these read the visible box above the form and
    hold it to the texts Casa's page shows today."""
    one = store.create(title="mail", category="PASSWORD", tags=[], writer="op", fields=[
        Field(id="", label="username", type="STRING", value="me"),
        Field(id="", label="password", type="CONCEALED", value=SENTINEL)])
    page = casa_page(store)
    edit = f"/s/{one.id}/edit"
    assert error_lines(text(get(page, edit))) is None  # a fresh form's box is hidden
    response = post(page, edit, {**form_of(page, edit, root="vault"),
                                 "remove0": "1", "remove1": "1"})
    assert response.status == 200
    assert error_lines(text(response)) == [
        "Secret not saved", "Keep at least one value, or delete the secret instead."]
    response = post(page, "/add", {**form_of(page, "/add", root="vault"), "name": "x",
                                   "label0": "token", "value0": "v\x0c"})
    assert response.status == 200
    assert error_lines(text(response)) == [
        "Secret not saved",
        "A value holds a character that cannot be saved, such as a form feed. "
        "Remove it and try again.",
        "The values you typed were cleared. Enter them again."]
