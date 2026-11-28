"""The page where a person sees and changes the logins the app keeps itself.

The page is the shared engine (`vault_page`) under the login profile of
`broker/logins_page.py`. These tests drive it through `Page.handle` on a real
`BuiltinVault` and read the result back the two ways that matter: raw, from the
store, and as the broker reads a login when it signs in.

What they protect, in order of cost: a password or an authenticator seed is never
in what a GET returns; a login typed here is one the broker can find, read and
fill, in the shape a live-view save makes; the page and the broker count one
version, so neither saves over a change of the other; and a save the limits
refuse writes nothing and tells nobody.
"""
from __future__ import annotations

import dataclasses
import re
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from casa_browser import vault_page
from casa_browser.broker import BrokerError, logins_page
from casa_browser.broker.builtin import BuiltinVault, one_time_code
from casa_browser.broker.main import _hosts
from casa_browser.vault_page import Request
from casa_browser.vault_store import Field

PREFIX = "/api/hassio_ingress/abc"
ORIGIN = "https://ha.example"
AT = 59.0  # the moment the vault's clock shows, for one-time codes
SEED = "JBSWY3DPEHPK3PXP"
PASSWORD = "sentinel-pw-5551"
OTHER_PASSWORD = "sentinel-pw-7772"
A, B = "https://a.example", "https://b.example"

# The add form's boxes, by what is typed into each.
BOXES = {"addresses": "value0", "username": "value1", "password": "value2", "seed": "value3"}

NOT_SAVED = "Login not saved"
CLEARED = "The values you typed were cleared. Enter them again."
ADDRESS_MISSING = "Enter at least one sign-in address."
ADDRESS_WITHOUT_HOST = "Each sign-in address needs a host, like https://example.com."
TOO_MANY_HOSTS = "Use sign-in addresses on at most 10 different hosts."
USERNAME_TOO_LONG = "Use a username of at most 1,024 bytes."
PASSWORD_TOO_LONG = "Use a password of at most 1,024 bytes."
NEEDS_ONE = "A login needs a username or a password."
NO_CODE = ("This authenticator seed gives no code. Enter the setup key or the otpauth:// "
           "address the site shows.")


@pytest.fixture
def vault(tmp_path):
    return BuiltinVault(tmp_path / "vault", None, clock=lambda: AT)


@pytest.fixture
def told():
    """The ids the broker was told of, in the order it was told."""
    return SimpleNamespace(changed=[], deleted=[])


@pytest.fixture
def page(vault, told):
    return logins_page.page(vault, told.changed.append, told.deleted.append)


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


def _forms(body, action):
    target = f"{PREFIX}/logins{action}"
    return re.findall(r'<form[^>]*action="' + re.escape(target) + r'"[^>]*>(.*?)</form>',
                      body, re.DOTALL)


def form_of(page, get_path, action=None, *, nth=0):
    """The hidden inputs (code, stamp, field) of a form on the page GET_PATH answers."""
    forms = _forms(text(get(page, get_path)), action or get_path)
    return dict(re.findall(r'<input type="hidden" name="([a-z0-9]+)" value="([^"]*)">',
                           forms[nth]))


def names_of(body, action):
    """Every control name the form posting to ACTION renders."""
    [form] = _forms(body, action)
    return set(re.findall(r'\sname="([^"]+)"', form))


class _Read(HTMLParser):
    """What a person reads on a page: its text, and the words of the attributes that are read
    out, shown, or handed to the page's script to show (`placeholder`, `aria-label`, `data-`).
    Ids, classes and addresses are not read."""

    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        self.parts += [value or "" for name, value in attrs
                       if name in ("placeholder", "aria-label") or name.startswith("data-")]

    def handle_data(self, data):
        self.parts.append(data)


def read(body):
    return "\n".join(_Read(body).parts)


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


def add(page, name="Bank", **typed):
    """Post the add form with NAME and what was TYPED into its boxes, by box."""
    form = {BOXES[box]: value for box, value in typed.items()}
    return post(page, "/add", {**form_of(page, "/add"), "name": name, **form})


def raw(item):
    """An item's fields as the store holds them, ids aside."""
    return [(field.label, field.type, field.value) for field in item.fields]


def snapshot(vault):
    return [(item.id, item.title, item.writer, item.updated_at, item.fields)
            for item in vault.store.list()]


def values(login):
    """A login's username and password as the broker reads them to fill a form."""
    return {field.id: field.value for field in login.fields
            if field.id in ("username", "password")}


def urls(login):
    return [website.url for website in login.websites]


def refused(response, *lines):
    """Whether RESPONSE is the form again, saying LINES under the heading of a refused save."""
    return response.status == 200 and error_lines(text(response)) == [NOT_SAVED, *lines]


async def _code_of(call):
    with pytest.raises(BrokerError) as raised:
        await call
    return raised.value.code


# -- adding -----------------------------------------------------------------------------

async def test_add_makes_a_login_the_broker_can_read(vault, page, told):
    response = add(page, name=" Bank ", addresses=f"{A}\r\n{B}/in", username="me@example.com",
                   password=PASSWORD)
    assert response.status == 303
    [made] = vault.store.list()
    assert dict(response.headers)["Location"] == f"{PREFIX}/logins/s/{made.id}"
    assert told.changed == [made.id] and told.deleted == []
    # the raw item is the one a live-view save of the same values makes, writer aside
    twin = await vault.create_login("Twin", "me@example.com", PASSWORD, [A, f"{B}/in"])
    saved = vault.store.get(twin.id)
    assert (made.title, made.category, made.tags) == ("Bank", saved.category, saved.tags)
    assert made.category == "LOGIN"
    assert raw(made) == raw(saved) == [
        ("username", "STRING", "me@example.com"), ("password", "CONCEALED", PASSWORD),
        ("websites", "URL", f"{A}\n{B}/in"), ("version", "STRING", "1")]
    assert [field.id for field in made.fields[:2]] == ["username", "password"]
    assert (made.writer, saved.writer) == (vault_page.WRITER, "live_view")
    # and the broker reads it as it reads that one
    login = await vault.get(made.id)
    assert values(login) == {"username": "me@example.com", "password": PASSWORD}
    assert urls(login) == [A, f"{B}/in"]
    assert login.version == 1
    listed = {overview.id: overview for overview in await vault.list_logins()}
    assert listed[made.id].title == "Bank"
    assert _hosts(listed[made.id].websites) == ["a.example", "b.example"]


async def test_the_page_reads_and_writes_the_vault_s_own_store(vault, page):
    """A second open copy of the store would hold its own view of the file: a login the
    broker saved after the page was made would be missing from the page, and the other way
    round."""
    assert vault.store is vault.store and vault.available
    saved = await vault.create_login("From a live view", "me", PASSWORD, [A])
    assert "From a live view" in read(text(get(page, "/")))
    assert add(page, name="Typed", addresses=B, username="you", password=PASSWORD).status == 303
    assert sorted(overview.title for overview in await vault.list_logins()) == [
        "From a live view", "Typed"]
    assert get(page, f"/s/{saved.id}").status == 200


async def test_add_with_a_seed_stores_it_as_a_one_time_field(vault, page):
    assert add(page, addresses=A, username="me", password=PASSWORD, seed=SEED).status == 303
    [made] = vault.store.list()
    assert raw(made) == [
        ("username", "STRING", "me"), ("password", "CONCEALED", PASSWORD),
        ("websites", "URL", A), ("one-time password", "OTP", SEED), ("version", "STRING", "1")]
    assert await vault.totp_code(made.id) == one_time_code(SEED, AT)
    assert re.fullmatch(r"\d{6}", await vault.totp_code(made.id))
    assert [field.field_type for field in (await vault.get(made.id)).fields] == [
        "Text", "Concealed", "Totp"]


async def test_add_with_a_password_only_stores_an_empty_username(vault, page):
    assert add(page, addresses=A, password=PASSWORD).status == 303
    [made] = vault.store.list()
    twin = await vault.create_login("Twin", "", PASSWORD, [A])
    assert raw(made) == raw(vault.store.get(twin.id))
    assert raw(made)[0] == ("username", "STRING", "")
    assert values(await vault.get(made.id)) == {"username": "", "password": PASSWORD}


async def test_add_with_a_username_only_stores_no_password(vault, page):
    assert add(page, addresses=A, username="me@example.com").status == 303
    [made] = vault.store.list()
    twin = await vault.create_login("Twin", "me@example.com", "", [A])
    assert raw(made) == raw(vault.store.get(twin.id)) == [
        ("username", "STRING", "me@example.com"), ("websites", "URL", A),
        ("version", "STRING", "1")]


async def test_addresses_are_stored_one_per_line(vault, page):
    """The broker reads one address from every line that is not empty. Spaces around an
    address and empty lines between addresses are not stored, and an address typed without
    `https://` has the host the broker gives it."""
    typed = f"  {A} \r\n\r\n\tbank.example/login\r\n   \r\n"
    assert add(page, addresses=typed, username="me", password=PASSWORD).status == 303
    [made] = vault.store.list()
    assert raw(made)[2] == ("websites", "URL", f"{A}\nbank.example/login")
    [listed] = await vault.list_logins()
    assert _hosts(listed.websites) == ["a.example", "bank.example"]


# -- the page and the broker on one login -----------------------------------------------------

async def test_a_live_view_login_edited_on_the_page_keeps_everything_else(vault, page, told):
    login = await vault.create_login("Bank", "", PASSWORD, [A, B])
    before = vault.store.get(login.id)
    assert [field.label for field in before.fields] == [
        "username", "password", "websites", "version"]
    edit = f"/s/{login.id}/edit"
    # the form has a card for each of the three, a box for the seed it lacks, and nothing
    # for the version
    assert names_of(text(get(page, edit)), edit) == {
        "code", "stamp", "value0", "remove0", "value1", "remove1", "replace1", "value2",
        "value4"}
    # a browser sends the plain boxes back as they were drawn
    response = post(page, edit, {**form_of(page, edit), "value0": "",
                                 "value2": f"{A}\r\n{B}", "replace1": "1",
                                 "value1": OTHER_PASSWORD})
    assert response.status == 303
    after = vault.store.get(login.id)
    assert after.fields == (
        before.fields[0], dataclasses.replace(before.fields[1], value=OTHER_PASSWORD),
        before.fields[2], dataclasses.replace(before.fields[3], value="2"))
    assert (after.title, after.category, after.tags) == (
        before.title, before.category, before.tags)
    assert told.changed == [login.id]
    read_back = await vault.get(login.id)
    assert values(read_back) == {"username": "", "password": OTHER_PASSWORD}
    assert urls(read_back) == [A, B]
    assert read_back.version == 2
    # the broker's own write from the version it read before the page's is refused
    assert await _code_of(vault.update_fields(login.id, 1, password="late")) == "conflict"
    assert vault.store.get(login.id) == after


async def test_the_page_s_save_and_a_broker_update_share_one_version(vault, page, told):
    login = await vault.create_login("Bank", "me", PASSWORD, [A])
    edit = f"/s/{login.id}/edit"
    assert post(page, edit, {**form_of(page, edit), "value0": "you"}).status == 303
    assert (await vault.get(login.id)).version == 2
    stale = form_of(page, edit)  # opened at version 2
    updated = await vault.update_fields(login.id, 2, password=OTHER_PASSWORD)
    assert updated.version == 3
    held = vault.store.get(login.id)
    response = post(page, edit, {**stale, "replace1": "1", "value1": "typed-too-late"})
    assert response.status == 409
    assert "This login changed while you were editing." in read(text(response))
    assert "typed-too-late" not in text(response)
    assert vault.store.get(login.id) == held
    assert values(await vault.get(login.id)) == {"username": "you", "password": OTHER_PASSWORD}
    assert told.changed == [login.id]
    # a form opened now saves, at the next version
    assert post(page, edit, {**form_of(page, edit), "value0": "them"}).status == 303
    assert (await vault.get(login.id)).version == 4


# -- what a save is refused for ---------------------------------------------------------------

async def test_seeds(vault, page, told):
    typed = "otpauth://totp/X?secret=jbsw y3dp ehpk 3pxp"
    assert add(page, addresses=A, username="me", password=PASSWORD, seed=typed).status == 303
    [made] = vault.store.list()
    assert raw(made)[3] == ("one-time password", "OTP", typed)  # as typed, not tidied
    assert await vault.totp_code(made.id) == one_time_code(SEED, AT)
    before = snapshot(vault)
    for bad in ("not-a-seed", "otpauth://hotp/X?secret=" + SEED,
                f"otpauth://totp/X?secret={SEED}&digits=9",
                f"otpauth://totp/X?secret={SEED}&period=" + "9" * 400):
        response = add(page, name="Other", addresses=B, username="me", password=PASSWORD,
                       seed=bad)
        assert refused(response, NO_CODE, CLEARED), bad
        assert bad not in text(response) and PASSWORD not in text(response)
    assert snapshot(vault) == before and told.changed == [made.id]
    # the same on the edit form, for a seed added and for one replaced
    bare = await vault.create_login("Bare", "me", PASSWORD, [A])
    before = snapshot(vault)
    edit = f"/s/{bare.id}/edit"
    assert refused(post(page, edit, {**form_of(page, edit), "value4": "not-a-seed"}),
                   NO_CODE, CLEARED)
    edit = f"/s/{made.id}/edit"
    assert refused(post(page, edit, {**form_of(page, edit), "replace3": "1",
                                     "value3": "not-a-seed"}), NO_CODE, CLEARED)
    assert snapshot(vault) == before and told.changed == [made.id]
    # a seed that gives a code is added to a login that had none
    edit = f"/s/{bare.id}/edit"
    assert post(page, edit, {**form_of(page, edit), "value4": SEED.lower()}).status == 303
    assert await vault.totp_code(bare.id) == one_time_code(SEED, AT)
    assert (await vault.get(bare.id)).version == 2


async def test_limits(vault, page, told):
    eleven = "\r\n".join(f"https://h{index}.example" for index in range(11))
    ten_and_a_second_line = "\r\n".join(
        [f"https://h{index}.example" for index in range(10)] + ["https://h3.example/other"])
    whole = {"addresses": A, "username": "me", "password": PASSWORD}
    for typed, lines in (
            ({**whole, "addresses": ""}, [ADDRESS_MISSING, CLEARED]),
            ({**whole, "addresses": " \r\n\t\r\n"}, [ADDRESS_MISSING, CLEARED]),
            ({**whole, "addresses": "https://[broken"}, [ADDRESS_WITHOUT_HOST, CLEARED]),
            ({**whole, "addresses": f"{A}\r\nhttps://[broken"}, [ADDRESS_WITHOUT_HOST, CLEARED]),
            ({**whole, "addresses": eleven}, [TOO_MANY_HOSTS, CLEARED]),
            ({**whole, "password": "p" * 1025}, [PASSWORD_TOO_LONG, CLEARED]),
            # counted in bytes, as the broker counts what it fills: 513 letters, 1,026 bytes
            ({**whole, "password": "é" * 513}, [PASSWORD_TOO_LONG, CLEARED]),
            ({**whole, "username": "u" * 1025}, [USERNAME_TOO_LONG, CLEARED]),
            ({**whole, "username": "é" * 513}, [USERNAME_TOO_LONG, CLEARED])):
        response = add(page, **typed)
        assert refused(response, *lines), (lines, error_lines(text(response)))
        assert PASSWORD not in text(response) and "pppp" not in text(response)
        assert vault.store.list() == [] and told.changed == []
    # a name is refused beside its box
    response = add(page, name="n" * 301, **whole)
    assert refused(response, "Fix the name below.", CLEARED)
    assert ('<p class="v-field-error" id="name-error">Use a name of at most 300 characters.</p>'
            in text(response))
    assert vault.store.list() == [] and told.changed == []
    # each limit itself is accepted
    for name, typed in (
            ("n" * 300, whole),
            ("ten hosts", {**whole, "addresses": ten_and_a_second_line}),
            ("long password", {**whole, "password": "é" * 512}),
            ("long username", {**whole, "username": "é" * 512})):
        assert add(page, name=name, **typed).status == 303, name[:20]
    assert len(vault.store.list()) == 4
    [ten] = [item for item in await vault.list_logins() if item.title == "ten hosts"]
    assert len(_hosts(ten.websites)) == 10 and len(ten.websites) == 11
    # an edit is held to the same limits
    login = await vault.create_login("Bank", "me", PASSWORD, [A])
    before = snapshot(vault)
    edit = f"/s/{login.id}/edit"
    for form, lines in (
            ({"value0": "u" * 1025}, [USERNAME_TOO_LONG]),
            ({"replace1": "1", "value1": "p" * 1025}, [PASSWORD_TOO_LONG, CLEARED]),
            ({"value2": "https://[broken"}, [ADDRESS_WITHOUT_HOST]),
            ({"value2": eleven}, [TOO_MANY_HOSTS]),
            ({"value2": " \r\n "}, [ADDRESS_MISSING]),
            ({"value2": ""}, [ADDRESS_MISSING])):
        response = post(page, edit, {**form_of(page, edit), **form})
        assert refused(response, *lines), (form, error_lines(text(response)))
    assert snapshot(vault) == before
    # the addresses cannot be removed, only changed
    assert "remove2" not in names_of(text(get(page, edit)), edit)
    assert post(page, edit, {**form_of(page, edit), "remove2": "1"}).status == 400
    assert snapshot(vault) == before


async def test_a_login_needs_a_username_or_a_password(vault, page, told):
    for typed in ({"addresses": A}, {"addresses": A, "seed": SEED}):
        response = add(page, **typed)
        assert refused(response, NEEDS_ONE, *([CLEARED] if "seed" in typed else [])), typed
    assert vault.store.list() == [] and told.changed == []
    # a live-view save can leave the username empty; such a login keeps its password
    login = await vault.create_login("Bank", "", PASSWORD, [A])
    before = snapshot(vault)
    edit = f"/s/{login.id}/edit"
    assert refused(post(page, edit, {**form_of(page, edit), "remove1": "1"}), NEEDS_ONE)
    assert snapshot(vault) == before and told.changed == []
    # the username may go while there is a password, and the password while there is a username
    # (a removed username is stored empty, as a live-view save without one stores it)
    for title, removed, left in (("Shop", "remove0", {"username": "", "password": PASSWORD}),
                                 ("Mail", "remove1", {"username": "me"})):
        login = await vault.create_login(title, "me", PASSWORD, [A])
        edit = f"/s/{login.id}/edit"
        assert post(page, edit, {**form_of(page, edit), removed: "1"}).status == 303
        after = await vault.get(login.id)
        assert values(after) == left and after.version == 2
        first = vault.store.get(login.id).fields[0]
        assert (first.label, first.type) == ("username", "STRING")
    # then the other one cannot go too
    assert refused(post(page, edit, {**form_of(page, edit), "value0": ""}), NEEDS_ONE)


# -- what stays off the page ---------------------------------------------------------------

async def test_version_is_kept_from_the_person(vault, page, told):
    login = await vault.create_login("Bank", "me", PASSWORD, [A])
    stored = vault.store.get(login.id)
    assert [field.label for field in stored.fields].index("version") == 3
    edit, show = f"/s/{login.id}/edit", f"/s/{login.id}/show"
    for path in ("/", f"/s/{login.id}", edit, f"/s/{login.id}/delete"):
        assert "version" not in text(get(page, path)).lower(), path
    # one card for each of the other three, and no control of the edit form names it
    assert text(get(page, f"/s/{login.id}")).count("data-value-block") == 3
    assert names_of(text(get(page, edit)), edit) == {
        "code", "stamp", "value0", "remove0", "value1", "remove1", "replace1", "value2",
        "value4"}
    before = snapshot(vault)
    for accept in (None, "application/json"):
        code = form_of(page, f"/s/{login.id}", show)
        assert post(page, show, {**code, "field": "3"}, accept=accept).status == 404
    assert vault.store.reads(login.id) == 0
    # the password before it is a target
    answer = post(page, show, {**form_of(page, f"/s/{login.id}", show), "field": "1"},
                  accept="application/json")
    assert answer.status == 200 and f'"value": "{PASSWORD}"' in text(answer)
    for extra in ({"value3": "9"}, {"remove3": "1"}, {"replace3": "1", "value3": "9"}):
        assert post(page, edit, {**form_of(page, edit), **extra}).status == 400, extra
    assert snapshot(vault) == before and told.changed == []


async def test_a_password_and_a_seed_never_leave_in_a_get(vault, page):
    seed = f"otpauth://totp/X?secret={SEED}"
    assert add(page, addresses=A, username="me@example.com", password=PASSWORD,
               seed=seed).status == 303
    [made] = vault.store.list()
    for path in ("/", f"/s/{made.id}", f"/s/{made.id}/edit", f"/s/{made.id}/delete", "/add"):
        for method in ("GET", "HEAD"):
            body = text(get(page, path, method=method))
            assert PASSWORD not in body and SEED not in body, (path, method)
    shown = text(get(page, f"/s/{made.id}"))
    # the addresses and the username are in the clear, the other two are dots
    assert f'<pre class="v-value" data-plain>\n{A}</pre>' in shown
    assert '<pre class="v-value" data-plain>\nme@example.com</pre>' in shown
    assert shown.count('<span class="v-dots" aria-label="Hidden value">') == 2
    edit = text(get(page, f"/s/{made.id}/edit"))
    assert names_of(edit, f"/s/{made.id}/edit") >= {"replace1", "replace3"}
    assert not names_of(edit, f"/s/{made.id}/edit") & {"replace0", "replace2"}
    # each is given to the person who asks for it, by its own form
    for field, value in (("1", PASSWORD), ("3", seed)):
        show = form_of(page, f"/s/{made.id}", f"/s/{made.id}/show")
        answer = post(page, f"/s/{made.id}/show", {**show, "field": field},
                      accept="application/json")
        assert answer.status == 200 and f'"value": "{value}"' in text(answer)
    # a refused form gives back what is in the clear and nothing else
    response = add(page, name="", addresses=B, username="typed-user", password=OTHER_PASSWORD,
                   seed=seed)
    body = text(response)
    assert response.status == 200 and "Enter a name." in body
    assert re.search(r'name="value0"[^>]*>\n' + re.escape(B) + "</textarea>", body)
    assert re.search(r'name="value1"[^>]*>\ntyped-user</textarea>', body)
    assert OTHER_PASSWORD not in body and SEED not in body and CLEARED in body
    # a value stored under a label the page does not know is hidden too
    odd = vault.store.create(title="Odd", category="LOGIN", tags=[], writer="live_view", fields=[
        Field(id="", label="username", type="STRING", value="me"),
        Field(id="", label="websites", type="URL", value=A),
        Field(id="", label="note", type="STRING", value=OTHER_PASSWORD),
        Field(id="", label="version", type="STRING", value="1")])
    for path in ("/", f"/s/{odd.id}", f"/s/{odd.id}/edit"):
        assert OTHER_PASSWORD not in text(get(page, path)), path


# -- telling the broker -------------------------------------------------------------------

async def test_changed_and_deleted_get_the_login_s_id(vault):
    """Each is called once, with the id, when the store already holds the change: the broker
    acts on what it then reads."""
    calls = []

    def changed(item_id):
        held = dict((label, value) for label, _, value in raw(vault.store.get(item_id)))
        calls.append(("changed", item_id, held["password"], held["version"]))

    def deleted(item_id):
        calls.append(("deleted", item_id, [item.id for item in vault.store.list()]))

    page = logins_page.page(vault, changed, deleted)
    assert add(page, addresses=A, username="me", password=PASSWORD).status == 303
    [made] = vault.store.list()
    assert calls == [("changed", made.id, PASSWORD, "1")]
    edit = f"/s/{made.id}/edit"
    # neither a refused save nor one that changes nothing is a change
    assert refused(post(page, edit, {**form_of(page, edit), "value2": "https://[broken"}),
                   ADDRESS_WITHOUT_HOST)
    assert post(page, edit, {**form_of(page, edit), "value0": "me", "value2": A,
                             "value1": "typed-without-replace"}).status == 303
    assert len(calls) == 1
    assert post(page, edit, {**form_of(page, edit), "replace1": "1",
                             "value1": OTHER_PASSWORD}).status == 303
    assert calls[1:] == [("changed", made.id, OTHER_PASSWORD, "2")]
    other = await vault.create_login("Other", "me", PASSWORD, [B])
    delete = f"/s/{made.id}/delete"
    assert "deleted" not in [call[0] for call in calls]
    response = post(page, delete, form_of(page, delete))
    assert response.status == 303
    assert dict(response.headers)["Location"] == f"{PREFIX}/logins/"
    assert calls[2:] == [("deleted", made.id, [other.id])]
    assert await _code_of(vault.get(made.id)) == "not_found"


# -- words ---------------------------------------------------------------------------------

LINKS = re.compile(r'\s(?:href|action|src)="([^"]*)"')
LINK = re.compile(re.escape(PREFIX) + r"/logins/(|add|s/[a-z2-7]{26}(/edit|/delete|/show)?"
                  r"|static/(ha\.css|vault\.css|theme\.js|page\.js))")
BACK = re.compile(r'<a class="v-back" href="([^"]*)"')


async def test_the_page_says_where_a_login_signs_in_and_how_it_was_saved(vault, page):
    saved = await vault.create_login("Bank", "me", PASSWORD, [f"{A}/sign-in", B])
    assert add(page, name="Shop", addresses="shop.example/login", username="me",
               password=PASSWORD, seed=SEED).status == 303
    [typed] = [item for item in vault.store.list() if item.title == "Shop"]
    listed = text(get(page, "/"))
    assert "<title>Saved logins · Casa Browser</title>" in listed
    assert "<strong>Bank</strong><small>a.example</small></a>" in listed
    assert "<strong>Shop</strong><small>shop.example</small></a>" in listed
    # Back leaves the page for the app's Setup page; every other link stays under the page
    [back] = BACK.findall(listed)
    assert back == f"{PREFIX}/setup"
    for link in LINKS.findall(listed):
        assert link == back or LINK.fullmatch(link), link
    one = text(get(page, f"/s/{saved.id}"))
    assert re.search(r'<p class="v-meta">Saved from a live view · Updated [^<]+</p>', one)
    for heading in ("Username", "Password", "Sign-in addresses"):
        assert f"<h2>{heading}</h2>" in one, heading
    one = text(get(page, f"/s/{typed.id}"))
    assert re.search(r'<p class="v-meta">Typed here · Updated [^<]+</p>', one)
    assert "<h2>Authenticator seed</h2>" in one
    for login in (saved, typed):
        assert ('<div class="v-notice v-notice--warning"><strong>Sites that use this login will '
                "ask you to sign in again.</strong></div>"
                in text(get(page, f"/s/{login.id}/delete")))
    # the add form asks for the four things under their own words, and for no label
    form = text(get(page, "/add"))
    assert re.findall(r'<label class="v-field" for="f-value\d">([^<]+)<textarea', form) == [
        "Sign-in addresses, one per line", "Username", "Password",
        "Authenticator seed (optional)"]
    assert names_of(form, "/add") == {"code", "name", "value0", "value1", "value2", "value3"}


def _every_page(vault, page, directory):
    """Every kind of answer the page gives, by name; DIRECTORY is free for other stores."""
    one = vault.store.create(title="Bank", category="LOGIN", tags=[], writer="live_view", fields=[
        Field(id="", label="username", type="STRING", value="me"),
        Field(id="", label="password", type="CONCEALED", value=PASSWORD),
        Field(id="", label="websites", type="URL", value=A),
        Field(id="", label="version", type="STRING", value="1")])
    many = vault.store.create(title="many", category="LOGIN", tags=[], writer="live_view", fields=[
        Field(id="", label="username", type="STRING", value="me"),
        Field(id="", label="websites", type="URL", value=A),
        Field(id="", label="version", type="STRING", value="1"),
        *(Field(id="", label=f"f{index}", type="STRING", value="v")
          for index in range(vault_page.MAX_FIELDS - 3))])
    edit, show = f"/s/{one.id}/edit", f"/s/{one.id}/show"

    def sent(path, form):
        return text(post(page, path, {**form_of(page, path), **form}))

    pages = {
        "no prefix": text(page.handle(Request("GET", "/", {}, b""))),
        "unavailable": text(get(logins_page.page(
            BuiltinVault(directory / "none", "  "), print, print), "/")),
        "empty list": text(get(logins_page.page(
            BuiltinVault(directory / "empty", None), print, print), "/")),
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
        "name taken": sent("/add", {"name": "Bank", "value0": A, "value1": "u"}),
        "refused": sent("/add", {"name": "x", "value0": A}),
        "too many": sent(f"/s/{many.id}/edit",
                         {f"value{vault_page.MAX_FIELDS}": PASSWORD}),
        "changed": text(post(page, edit, {"code": page.codes.issue(edit), "stamp": "old"})),
        "shown": text(post(page, show, {**form_of(page, f"/s/{one.id}", show), "field": "1"})),
    }
    pages["read once"] = text(get(page, f"/s/{one.id}"))
    vault.store.note_read(one.id)
    pages["read twice"] = text(get(page, f"/s/{one.id}"))
    return pages


async def test_the_words_name_logins(vault, page, tmp_path):
    """No page speaks of what Casa's own page is about. What is read is what a person reads
    or hears and what the page hands its script to show, not the markup: the form's id and a
    stylesheet's name hold words of their own."""
    pages = {name: read(body) for name, body in _every_page(vault, page, tmp_path).items()}
    for name, seen in pages.items():
        for word in ("secret", "Secret", "plugin", "Plugin", "1Password", "References",
                     "Casa started"):
            assert word not in seen, (name, word)
    for sentence in logins_page.WORDS.values():
        for word in ("secret", "Secret", "plugin", "Plugin", "1Password", "References"):
            assert word not in sentence, sentence
    # each page that is about logins says so
    for name, words in {
            "no prefix": "Open this page through Casa Browser's web interface.",
            "unavailable": "The saved logins could not be opened.",
            "empty list": "No saved logins yet", "list": "Search logins", "add": "Add login",
            "item": "Delete login", "edit": "Edit login", "delete": "Delete login",
            "show by get": "Use the Show button on the login's page.",
            "not found": "There is no such login or page.",
            "expired": "Reload page", "not rendered": "Back to saved logins",
            "name missing": NOT_SAVED, "name taken": "A login named “Bank” already exists.",
            "refused": NEEDS_ONE, "too many": "A login can hold at most 40 values.",
            "changed": "Reload login", "shown": PASSWORD,
            "read once": "Shown or copied once since Casa Browser started",
            "read twice": "Shown or copied 2 times since Casa Browser started"}.items():
        assert words in pages[name], (name, words)
    assert set(pages["list"].split("\n")) >= {"{} login", "{} logins"}


# -- no store ---------------------------------------------------------------------------------

async def test_a_store_that_could_not_be_opened_answers_unavailable(tmp_path, told):
    vault = BuiltinVault(tmp_path / "vault", "   ")  # a key of spaces opens nothing
    assert vault.available is False and vault.store is None
    page = logins_page.page(vault, told.changed.append, told.deleted.append)
    for path in ("/", "/add", "/s/" + "a" * 26, "/s/" + "a" * 26 + "/edit"):
        response = get(page, path)
        body = text(response)
        assert response.status == 503, path
        assert "<h2>Saved logins unavailable</h2>" in body
        assert BACK.findall(body) == [f"{PREFIX}/setup"]
        assert (f'<a class="v-button v-button--quiet" href="{PREFIX}/setup">Back to Casa Browser'
                "</a>") in body
    response = post(page, "/add", {"name": "Bank", "value0": A, "value2": PASSWORD})
    assert response.status == 503 and PASSWORD not in text(response)
    assert not (tmp_path / "vault").exists()
    assert told.changed == [] and told.deleted == []
