"""The assistant's ``connect_login``: it shows the items that could hold a
service's login, takes one only from that list, and stores the choice only
after Casa's own line naming the item reached the operator. The ``op`` front
is a fake holding invented items."""
from __future__ import annotations

import dataclasses
import json
import pathlib
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

import logins
from contacts import service as contacts_service
from contacts.store import ContactsStore
from op_fake import fresh_logins, install, item
from output_boundary import Admitted

ICLOUD = "icloud_contacts"
VOICE = "openai_voice"
GRANT = "mcp__casa-framework__connect_login"
DEFAULTS = pathlib.Path("casa/rootfs/opt/casa/defaults")

NOT_PRIVATE = "Logins are only available in the operator's own chat."
NOT_DIRECT = ("A login can only be connected in a direct chat with the "
              "operator.")


class Channel:
    """Casa's own endpoint: records each line it is asked to send and what
    was stored at that moment; answers *delivered* (or raises it)."""

    def __init__(self) -> None:
        self.delivered: bool | Exception = True
        #: Answers for the next lines, one each, before ``delivered``.
        self.answers: list[bool | Exception] = []
        self.lines: list[str] = []
        self.stored_when_sent: list[tuple[str | None, str | None]] = []

    async def notify_operator(self, text: str) -> bool:
        assert isinstance(text, Admitted) and text.source == "casa"
        self.lines.append(str(text))
        self.stored_when_sent.append((logins.chosen(ICLOUD),
                                      logins.chosen(VOICE)))
        answer = self.answers.pop(0) if self.answers else self.delivered
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Channel:
    import tools as tools_mod
    fresh_logins(monkeypatch, tmp_path)
    found = Channel()
    monkeypatch.setattr(tools_mod, "_casa_endpoint", lambda: found)
    return found


@contextmanager
def _turn(clearance: str = "private", *, engagement=None,
          drop: tuple[str, ...] = (), **overrides):
    """A turn in the operator's direct Telegram chat, stamped *clearance*;
    *drop* removes keys from that origin."""
    import agent as agent_mod
    import tools as tools_mod
    origin = {
        "role": "assistant", "execution_role": "assistant",
        "channel": "telegram", "source": "telegram",
        "message_type": "channel_in", "chat_id": "500", "user_id": 500,
        "cid": "c1", "_origin_route": "telegram",
        "_origin_clearance": clearance,
    }
    origin.update(overrides)
    for key in drop:
        del origin[key]
    tok = agent_mod.origin_var.set(origin)
    eng_tok = tools_mod.engagement_var.set(engagement)
    try:
        yield
    finally:
        tools_mod.engagement_var.reset(eng_tok)
        agent_mod.origin_var.reset(tok)


async def _call(args: dict, name: str = "connect_login") -> dict:
    import tools as tools_mod
    res = await getattr(tools_mod, name).handler(args)
    return json.loads(res["content"][0]["text"])


def _apple(item_id: str = "apl1", title: str = "Apple account") -> dict:
    return item(item_id, title, username="fictional-apple-id",
                password="abcd-efgh")


FULL = {"the Apple ID": True, "the app-specific password": True}


async def test_without_an_item_it_reports_connected_or_the_candidates(
        channel: Channel, monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _apple(),
                 item("str2", "Streaming service", username="someone",
                      password="else"))
    with _turn():
        got = await _call({"service": ICLOUD})
        assert got == {
            "status": "not_connected", "ok": False,
            "problem": "item_not_found",
            "candidates": [{"id": "apl1", "title": "Apple account",
                            "category": "LOGIN", "has": FULL}]}
        # Looking stores nothing and sends nothing.
        assert logins.chosen(ICLOUD) is None and channel.lines == []

        op.items.append(item("dflt3", "iCloud Contacts",
                             username="fictional-apple-id",
                             password="abcd-efgh"))
        assert await _call({"service": ICLOUD}) == {"status": "connected",
                                                    "ok": True}
        # An empty item is the same as none given.
        assert await _call({"service": ICLOUD, "item": ""}) == {
            "status": "connected", "ok": True}
        for bad in ({}, {"service": "another_service"}, {"service": 7},
                    {"service": ICLOUD, "item": 7}):
            got = await _call(bad)
            assert got["status"] == "error", bad
    assert channel.lines == []


async def test_another_item_can_be_looked_for_while_the_login_works(
        channel: Channel, monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch,
                 item("dflt1", "iCloud Contacts",
                      username="fictional-apple-id", password="abcd-efgh"),
                 _apple(), _apple("apl2", "Apple family"))

    def row(item_id: str, title: str, in_use: bool) -> dict:
        return {"id": item_id, "title": title, "category": "LOGIN",
                "has": FULL, "in_use": in_use}

    with _turn():
        # Not asked for: connected, and the vault is not searched.
        for plain in ({"service": ICLOUD},
                      {"service": ICLOUD, "show_candidates": False}):
            assert await _call(plain) == {"status": "connected", "ok": True}
        assert "list" not in op.kinds()

        # Asked for: the candidates beside "connected", the one in use
        # (here the item under the default title) marked.
        got = await _call({"service": ICLOUD, "show_candidates": True})
        assert got == {"status": "connected", "ok": True, "candidates": [
            row("dflt1", "iCloud Contacts", True),
            row("apl1", "Apple account", False),
            row("apl2", "Apple family", False)]}
        assert channel.lines == [] and logins.chosen(ICLOUD) is None

        # Taking another one goes the usual way, and it is then the one
        # marked.
        assert await _call({"service": ICLOUD, "item": "apl2"}) == {
            "status": "connected", "ok": True}
        assert channel.lines == [
            "Contacts now use the vault item 'Apple family'."]
        got = await _call({"service": ICLOUD, "show_candidates": True})
        assert [(c["id"], c["in_use"]) for c in got["candidates"]] == [
            ("dflt1", False), ("apl1", False), ("apl2", True)]

        # A login that does not work marks them too when asked to.
        op.get("apl2")["fields"]["password"] = ""
        got = await _call({"service": ICLOUD, "show_candidates": True})
        assert (got["status"], got["problem"]) == ("not_connected",
                                                   "field_empty")
        assert [(c["id"], c["in_use"]) for c in got["candidates"]] == [
            ("dflt1", False), ("apl1", False), ("apl2", True)]
        assert all("in_use" not in c for c in
                   (await _call({"service": ICLOUD}))["candidates"])

        for bad in ("yes", 1, ["true"]):
            got = await _call({"service": ICLOUD, "show_candidates": bad})
            assert got["status"] == "error", bad
        # A null is the same as leaving it out.
        got = await _call({"service": ICLOUD, "show_candidates": None})
        assert "in_use" not in got["candidates"][0]
    assert len(channel.lines) == 1


async def test_two_items_under_the_default_title_are_neither_in_use(
        channel: Channel, monkeypatch: pytest.MonkeyPatch) -> None:
    def voice(item_id: str) -> dict:
        return item(item_id, "OpenAI Voice", "API_CREDENTIAL",
                    credential="sk-test-not-a-real-key",
                    webhook_secret="whsec_x")
    op = install(monkeypatch, voice("oai1"), voice("oai2"))
    with _turn():
        # The title names two items, so the login reads neither: not
        # connected, and no row says it is the one in use.
        got = await _call({"service": VOICE, "show_candidates": True})
        assert got["status"] == "not_connected"
        assert [(c["id"], c["in_use"]) for c in got["candidates"]] == [
            ("oai1", False), ("oai2", False)]
        assert logins.read_all(VOICE) is None

        # One of the two is left: connected, and that row is marked.
        op.remove("oai2")
        got = await _call({"service": VOICE, "show_candidates": True})
        assert got["status"] == "connected"
        assert [(c["id"], c["in_use"]) for c in got["candidates"]] == [
            ("oai1", True)]
        assert logins.read_all(VOICE) is not None
    assert channel.lines == []


async def test_an_id_that_is_not_a_candidate_is_refused(
        channel: Channel, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, _apple(),
            # Complete, and about something else: never offered, never taken.
            item("str2", "Streaming service", username="someone",
                 password="else"),
            item("oai3", "OpenAI Voice", "API_CREDENTIAL",
                 credential="sk-test-not-a-real-key",
                 webhook_secret="whsec_x"))
    with _turn():
        for service, wrong in ((ICLOUD, "str2"), (ICLOUD, "oai3"),
                               (ICLOUD, "nobody9"), (VOICE, "apl1"),
                               (ICLOUD, "Apple account")):
            got = await _call({"service": service, "item": wrong})
            assert got["status"] == "error", (service, wrong)
            assert "candidates" in got["message"]
    assert logins.chosen(ICLOUD) is None and logins.chosen(VOICE) is None
    assert channel.lines == []


async def test_a_fitting_item_is_announced_and_then_stored(
        channel: Channel, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, _apple())
    with _turn():
        got = await _call({"service": ICLOUD, "item": "apl1"})
    assert got == {"status": "connected", "ok": True}
    assert channel.lines == [
        "Contacts now use the vault item 'Apple account'."]
    # The line went first; the choice was stored only after it.
    assert channel.stored_when_sent == [(None, None)]
    assert logins.chosen(ICLOUD) == "apl1"
    assert logins.check(ICLOUD).ok
    assert logins.read_all(ICLOUD) == {"username": "fictional-apple-id",
                                       "password": "abcd-efgh"}


async def test_the_line_says_which_field_holds_the_password(
        channel: Channel, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, item(
        "icl1", "iCloud app password", "API_CREDENTIAL",
        username="fictional-apple-id", credential="abcd-efgh"),
        item("oai2", "OpenAI key", "API_CREDENTIAL",
             credential="sk-test-not-a-real-key", webhook_secret="whsec_x"))
    with _turn():
        assert (await _call({"service": ICLOUD, "item": "icl1"}))[
            "status"] == "connected"
        assert (await _call({"service": VOICE, "item": "oai2"}))[
            "status"] == "connected"
    assert channel.lines == [
        "Contacts now use the vault item 'iCloud app password', "
        "app-specific password from its `credential` field.",
        "OpenAI voice now uses the vault item 'OpenAI key'."]
    assert (logins.chosen(ICLOUD), logins.chosen(VOICE)) == ("icl1", "oai2")


@pytest.mark.parametrize("failure", [False, RuntimeError("Telegram is down"),
                                     None], ids=["false", "raises",
                                                 "no-channel"])
async def test_a_line_that_cannot_be_sent_stores_nothing(
        channel: Channel, monkeypatch: pytest.MonkeyPatch, failure) -> None:
    import tools as tools_mod
    install(monkeypatch, _apple())
    if failure is None:
        monkeypatch.setattr(tools_mod, "_casa_endpoint", lambda: None)
    else:
        channel.delivered = failure
    with _turn():
        got = await _call({"service": ICLOUD, "item": "apl1"})
        assert got == {"status": "not_stored", "problem": "operator_not_told"}
        assert logins.chosen(ICLOUD) is None
        assert not Path(logins.PATH).exists()
        # Asked again without an item, the login is still not connected.
        again = await _call({"service": ICLOUD})
        assert again["status"] == "not_connected"
        assert [c["id"] for c in again["candidates"]] == ["apl1"]

        # A second attempt, with the line delivered, stores it.
        monkeypatch.setattr(tools_mod, "_casa_endpoint", lambda: channel)
        channel.delivered = True
        got = await _call({"service": ICLOUD, "item": "apl1"})
    assert got == {"status": "connected", "ok": True}
    assert logins.chosen(ICLOUD) == "apl1"
    assert channel.lines[-1] == (
        "Contacts now use the vault item 'Apple account'.")
    assert channel.lines.count(channel.lines[-1]) == (
        1 if failure is None else 2)


TAKEN = "Contacts now use the vault item 'Apple account'."
NOT_SAVED = ("The vault item 'Apple account' could not be saved as the "
             "choice for iCloud contacts.")


@pytest.mark.parametrize(("before", "applies"), [
    ("nothing chosen",
     "Casa still looks for the item titled 'iCloud Contacts'."),
    ("an item chosen", "The item chosen before, 'Apple old', stays in use."),
    ("an item chosen and gone", "The item chosen before stays in use."),
    ("the choice unknown",
     "No vault item is in use for iCloud contacts until one is connected."),
])
async def test_a_choice_that_cannot_be_saved_is_taken_back_in_a_second_line(
        channel: Channel, monkeypatch: pytest.MonkeyPatch, before: str,
        applies: str) -> None:
    import atomic_io
    op = install(monkeypatch, _apple(), _apple("old1", "Apple old"))
    path = Path(logins.PATH)
    if before.startswith("an item chosen"):
        logins.choose(ICLOUD, "old1")
    if before == "an item chosen and gone":
        op.remove("old1")

    def full(*args, **kwargs):
        raise OSError("no space left on device")
    monkeypatch.setattr(atomic_io, "atomic_write_json", full)
    if before == "the choice unknown":
        path.write_text("{ damaged")
        monkeypatch.setattr(logins, "_choices", None)
    on_disk = path.read_bytes() if path.exists() else None
    was = logins.chosen(ICLOUD)

    with _turn():
        got = await _call({"service": ICLOUD, "item": "apl1"})
    assert got == {"status": "not_stored", "problem": "store_failed"}
    # He was told the item is taken; the second line takes that back and
    # says what applies now.
    assert channel.lines == [TAKEN, f"{NOT_SAVED} {applies}"]
    assert logins.chosen(ICLOUD) == was
    assert (path.read_bytes() if path.exists() else None) == on_disk
    assert logins.check(ICLOUD).problem == {
        "nothing chosen": "item_not_found", "an item chosen": "",
        "an item chosen and gone": "item_not_found",
        "the choice unknown": "choice_unreadable"}[before]


@pytest.mark.parametrize("second", [False, RuntimeError("Telegram is down")],
                         ids=["false", "raises"])
async def test_a_second_line_that_cannot_be_sent_is_not_tried_again(
        channel: Channel, monkeypatch: pytest.MonkeyPatch, second) -> None:
    import atomic_io
    install(monkeypatch, _apple())

    def full(*args, **kwargs):
        raise OSError("no space left on device")
    monkeypatch.setattr(atomic_io, "atomic_write_json", full)
    channel.answers = [True, second]
    with _turn():
        got = await _call({"service": ICLOUD, "item": "apl1"})
        assert got == {"status": "not_stored", "problem": "store_failed"}
        assert len(channel.lines) == 2 and channel.lines[1].startswith(
            NOT_SAVED)
        # Nothing is owed afterwards: a later call sends no line of its own.
        again = await _call({"service": ICLOUD})
    assert again["status"] == "not_connected"
    assert len(channel.lines) == 2 and logins.chosen(ICLOUD) is None


async def test_an_incomplete_item_is_stored_and_the_operator_told_why(
        channel: Channel, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path) -> None:
    op = install(monkeypatch, item("apl1", "Apple account",
                                   password="abcd-efgh"))
    store = ContactsStore(str(tmp_path / "contacts"))
    assert store.open() is True, store.unavailable_reason
    signed_in: list[tuple[str, str]] = []

    class Source:
        name = "icloud"

        def __init__(self, username: str, password: str) -> None:
            signed_in.append((username, password))

        async def fetch(self) -> list:
            return []
    monkeypatch.setattr(contacts_service, "ICloudSource", Source)
    for name in ("_store", "_notify", "_model", "_read"):
        # Put back after the test whatever the service held before it.
        monkeypatch.setattr(contacts_service, name,
                            getattr(contacts_service, name))
    contacts_service.configure(store=store, notify=channel.notify_operator,
                               model=None)

    with _turn():
        got = await _call({"service": ICLOUD, "item": "apl1"})
        assert got == {"status": "not_connected", "ok": False,
                       "problem": "field_missing", "what": "the Apple ID"}
        assert logins.chosen(ICLOUD) == "apl1"
        assert channel.lines == [
            "Contacts are set to use the vault item 'Apple account', but "
            "the vault item for iCloud contacts has no Apple ID in "
            "`username`."]

        # The operator fills the field in: the next refresh signs in with
        # it, and nothing more needs saying.
        op.get("apl1")["fields"]["username"] = "fictional-apple-id"
        got = await _call({}, "refresh_contacts")
        assert got["status"] == "ok"
        assert signed_in == [("fictional-apple-id", "abcd-efgh")]
        assert await _call({"service": ICLOUD}) == {"status": "connected",
                                                    "ok": True}
    assert len(channel.lines) == 1


async def test_no_result_holds_a_label_or_a_value(
        channel: Channel, monkeypatch: pytest.MonkeyPatch) -> None:
    # Accepted labels that are words Casa itself never writes.
    monkeypatch.setitem(logins.SERVICES, ICLOUD, dataclasses.replace(
        logins.SERVICES[ICLOUD], fields={
            "username": ("zzlabel-user", "zzlabel-mail"),
            "password": ("zzlabel-pass", "zzlabel-cred")}))
    op = install(
        monkeypatch,
        item("icl1", "iCloud login", **{"zzlabel-mail": "ZZVALUE-a",
                                        "zzlabel-cred": "ZZVALUE-b",
                                        "ZZLABEL-other": "ZZVALUE-c"}),
        item("icl2", "iCloud spare", **{"zzlabel-pass": "ZZVALUE-d",
                                        "zzlabel-user": ""}),
        item("str3", "Streaming service", **{"zzlabel-user": "ZZVALUE-e"}))
    results = []
    with _turn():
        results.append(await _call({"service": ICLOUD}))
        results.append(await _call({"service": ICLOUD, "item": "str3"}))
        results.append(await _call({"service": ICLOUD, "item": "icl2"}))
        results.append(await _call({"service": ICLOUD}))
        results.append(await _call({"service": ICLOUD, "item": "icl1"}))
        results.append(await _call({"service": ICLOUD}))
        # Connected, and asked which other items there are.
        shown = await _call({"service": ICLOUD, "show_candidates": True})
        assert shown["status"] == "connected"
        assert [(c["id"], c["in_use"]) for c in shown["candidates"]] == [
            ("icl1", True), ("icl2", False)]
        results.append(shown)
        channel.delivered = False
        results.append(await _call({"service": ICLOUD, "item": "icl2"}))
        op.down = True
        results.append(await _call({"service": ICLOUD}))
        results.append(await _call({"service": ICLOUD, "item": "icl1"}))
    assert [r["status"] for r in results] == [
        "not_connected", "error", "not_connected", "not_connected",
        "connected", "connected", "connected", "not_stored", "not_connected",
        "error"]
    text = json.dumps(results)
    assert "zzlabel" not in text.lower() and "ZZVALUE" not in text
    # The titles are there (the assistant judges by them), and the operator's
    # own lines are where a field may be named.
    assert "iCloud login" in text and "iCloud spare" in text
    assert any("`zzlabel-user`" in line for line in channel.lines)
    assert "ZZVALUE" not in "".join(channel.lines)


async def test_a_damaged_choice_file_is_repaired_by_connecting_again(
        channel: Channel, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path) -> None:
    store = ContactsStore(str(tmp_path / "contacts"))
    assert store.open() is True, store.unavailable_reason
    for name in ("_store", "_notify", "_model", "_read"):
        # Put back after the test whatever the service held before it.
        monkeypatch.setattr(contacts_service, name,
                            getattr(contacts_service, name))
    contacts_service.configure(store=store, notify=None, model=None)
    # Accepted labels that are words Casa itself never writes.
    monkeypatch.setitem(logins.SERVICES, ICLOUD, dataclasses.replace(
        logins.SERVICES[ICLOUD], fields={
            "username": ("zzlabel-user", "zzlabel-mail"),
            "password": ("zzlabel-pass", "zzlabel-cred")}))
    op = install(
        monkeypatch,
        item("dflt1", "iCloud Contacts", **{"zzlabel-user": "ZZVALUE-a",
                                            "zzlabel-pass": "ZZVALUE-b"}),
        item("apl2", "Apple account", **{"zzlabel-user": "ZZVALUE-c",
                                         "zzlabel-cred": "ZZVALUE-d"}),
        item("oai3", "OpenAI Voice", "API_CREDENTIAL",
             credential="ZZVALUE-e", webhook_secret="ZZVALUE-f"))
    logins.choose(ICLOUD, "apl2")
    path = Path(logins.PATH)
    path.write_text("{ damaged")
    monkeypatch.setattr(logins, "_choices", None)

    results = []
    with _turn():
        # Which item was chosen is unknown: not connected, and the item
        # under the default title, complete as it is, is not read instead.
        results.append(await _call({"service": ICLOUD}))
        assert results[-1] == {
            "status": "not_connected", "ok": False,
            "problem": "choice_unreadable",
            "candidates": [
                {"id": "dflt1", "title": "iCloud Contacts",
                 "category": "LOGIN", "has": FULL},
                {"id": "apl2", "title": "Apple account", "category": "LOGIN",
                 "has": FULL}]}
        assert logins.read_all(ICLOUD) is None
        assert not op.reads() and channel.lines == []
        results.append(await _call({"service": VOICE}))
        assert results[-1]["problem"] == "choice_unreadable"
        # The contacts tool says the same, by the same code.
        results.append(await _call({}, "refresh_contacts"))
        assert results[-1] == {"status": "not_connected", "ok": False,
                               "problem": "choice_unreadable"}
        assert not op.reads()

        # Taking the item again: the operator is told, the choice is
        # stored, and the login reads again.
        results.append(await _call({"service": ICLOUD, "item": "apl2"}))
        assert results[-1] == {"status": "connected", "ok": True}
        assert channel.lines == [
            "Contacts now use the vault item 'Apple account', app-specific "
            "password from its `zzlabel-cred` field."]
        assert channel.stored_when_sent == [(None, None)]
        assert logins.chosen(ICLOUD) == "apl2"
        assert logins.read_all(ICLOUD) == {"username": "ZZVALUE-c",
                                           "password": "ZZVALUE-d"}
        results.append(await _call({"service": ICLOUD}))
        assert results[-1] == {"status": "connected", "ok": True}

        # The other login stays unknown until it is connected again too.
        results.append(await _call({"service": VOICE}))
        assert results[-1]["problem"] == "choice_unreadable"
        assert logins.read(VOICE, "credential") is None
        results.append(await _call({"service": VOICE, "item": "oai3"}))
        assert results[-1] == {"status": "connected", "ok": True}
        assert logins.read(VOICE, "credential") == "ZZVALUE-e"
    assert json.loads(path.read_text()) == {ICLOUD: {"item": "apl2"},
                                            VOICE: {"item": "oai3"}}
    # No result holds a label, a value, or where Casa keeps the choices.
    text = json.dumps(results)
    assert "zzlabel" not in text.lower() and "ZZVALUE" not in text
    assert "logins.json" not in text and str(path.parent) not in text


async def test_refused_below_private_clearance(
        channel: Channel, monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _apple())
    calls = [{"service": ICLOUD}, {"service": ICLOUD, "item": "apl1"},
             {"service": ICLOUD, "show_candidates": True}]
    for clearance in ("family", "public"):
        with _turn(clearance):
            for args in calls:
                assert await _call(args) == {
                    "status": "error", "message": NOT_PRIVATE}, clearance
    origins = [
        ("telegram, no clearance stamp", {}, ("_origin_clearance",)),
        ("guest chat", {"_origin_route": "telegram_guest",
                        "channel": "telegram_guest"}, ()),
        ("voice, no route", {"channel": "voice"},
         ("_origin_route", "_origin_clearance")),
        ("webhook trigger claiming private",
         {"_origin_route": "webhook_trigger"}, ()),
    ]
    for label, overrides, drop in origins:
        with _turn(drop=drop, **overrides):
            for args in calls:
                assert await _call(args) == {
                    "status": "error", "message": NOT_PRIVATE}, label
    assert op.calls == [] and channel.lines == []
    assert logins.chosen(ICLOUD) is None


async def test_refused_outside_the_operators_direct_chat(
        channel: Channel, monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _apple())
    engagement = MagicMock()
    engagement.context_rebuild_pending = False
    engagement.kind = "executor"
    engagement.origin = None
    turns = [
        {"execution_role": "butler"},          # a delegated run
        {"synthetic": "scheduled"},            # not the operator's words
        {"message_type": "scheduled"},
        {"engagement": engagement},
    ]
    for overrides in turns:
        with _turn(**overrides):
            # Not even the titles are shown outside his direct chat.
            for args in ({"service": ICLOUD},
                         {"service": ICLOUD, "item": "apl1"},
                         {"service": ICLOUD, "show_candidates": True}):
                assert await _call(args) == {
                    "status": "error", "message": NOT_DIRECT}, overrides
    assert op.calls == [] and channel.lines == []
    assert logins.chosen(ICLOUD) is None
    # A tapped button in the operator's chat is still his direct chat.
    with _turn(synthetic="button"):
        got = await _call({"service": ICLOUD, "item": "apl1"})
    assert got["status"] == "connected"


def _allowed(path: pathlib.Path) -> set[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return set((data.get("tools") or {}).get("allowed") or [])


def test_only_the_assistant_is_granted_it() -> None:
    granted = {
        DEFAULTS / "agents" / "assistant" / "runtime.yaml",
        DEFAULTS / "roles" / "resident" / "assistant" / "role.yaml",
    }
    configs = sorted({*DEFAULTS.rglob("runtime.yaml"),
                      *DEFAULTS.rglob("role.yaml")})
    assert granted <= set(configs)
    for path in configs:
        assert (GRANT in _allowed(path)) is (path in granted), path


def test_not_in_the_specialist_allowlist_or_the_restricted_webhook_tools(
        ) -> None:
    import agent as agent_mod
    import tools as tools_mod
    from specialist_component import SPECIALIST_CASA_TOOL_ALLOWLIST

    assert "connect_login" in {t.name for t in tools_mod.CASA_TOOLS}
    assert "connect_login" not in SPECIALIST_CASA_TOOL_ALLOWLIST
    assert GRANT not in agent_mod._RESTRICTED_WEBHOOK_TOOLS
    assert "connect_login" not in agent_mod._RESTRICTED_WEBHOOK_TOOLS

