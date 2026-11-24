"""The contacts service: the iCloud login, the shared refresh and the daily
job with its one operator line per failure streak.

Real SQLite files in a temporary directory; the source, the model, the
operator channel and the ``op`` front are fakes. The people and the vault
items are invented.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

import logins
from contacts import service
from contacts import store as cstore
from contacts import sync
from contacts.store import ContactsStore
from contacts.sync import SourceError
from contacts.vcard import Card
from op_fake import FakeOp, fresh_logins, install, item

LOGIN = "icloud_contacts"


@pytest.fixture(autouse=True)
def op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeOp:
    """An empty vault and no chosen item; a test adds what it needs."""
    fresh_logins(monkeypatch, tmp_path)
    return install(monkeypatch)


@pytest.fixture
def opened(tmp_path: Path) -> ContactsStore:
    store = ContactsStore(str(tmp_path / "contacts"))
    assert store.open() is True, store.unavailable_reason
    return store


def _card(uid: str, name: str, note: str = "") -> Card:
    return Card(uid=uid, name=name, first_name=name.split()[0], org="",
                note=note, tels=(("+31 20 111 0001", "CELL"),), related=(),
                is_group=False, members=())


class FakeSource:
    """Answers each fetch with the next answer (the last one repeats); an
    optional gate holds every fetch until it is set."""

    name = "icloud"

    def __init__(self, *answers: list[Card] | Exception,
                 gate: asyncio.Event | None = None) -> None:
        self._answers = list(answers)
        self.gate = gate
        self.fetches = 0
        self.credentials: list[tuple[str, str]] = []

    async def fetch(self) -> list[Card]:
        self.fetches += 1
        if self.gate is not None:
            await self.gate.wait()
        answer = self._answers.pop(0) if len(self._answers) > 1 \
            else self._answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeModel:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


class Operator:
    """A fake ``notify_operator``: records every line, answers *delivered*
    (or raises it when it is an exception)."""

    def __init__(self, delivered: bool | Exception = True) -> None:
        self.delivered = delivered
        self.lines: list[str] = []
        #: The lines that reached the operator (the answer was True).
        self.received: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.lines.append(text)
        if isinstance(self.delivered, Exception):
            raise self.delivered
        if self.delivered:
            self.received.append(text)
        return self.delivered


def ICLOUD() -> dict[str, str]:
    """A login read whole, as :func:`logins.read_all` hands it over."""
    return {"username": "fictional-apple-id", "password": "abcd-efgh"}


def _none() -> None:
    return None


#: Stands for the service's own reader, which goes through ``logins``.
THROUGH_LOGINS = object()


def _configure(monkeypatch: pytest.MonkeyPatch, store: ContactsStore,
               source: FakeSource, *, read_login=ICLOUD,
               notify=None, model=None) -> None:
    def build(username: str, password: str) -> FakeSource:
        source.credentials.append((username, password))
        return source
    monkeypatch.setattr(service, "ICloudSource", build)
    if read_login is THROUGH_LOGINS:
        service.configure(store=store, notify=notify, model=model)
    else:
        service.configure(store=store, notify=notify, model=model,
                          read_login=read_login)


async def test_refresh_without_a_login_is_none_and_silent(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch, op: FakeOp,
) -> None:
    source = FakeSource([_card("U1", "Marco Example")])
    operator = Operator()
    _configure(monkeypatch, opened, source, read_login=_none,
               notify=operator)
    assert await service.refresh(reason="test") is None
    await service.run_daily()
    assert source.fetches == 0
    assert operator.lines == []
    assert service.login_problem() is None
    with opened.transaction() as conn:
        assert cstore.sync_state(conn, "icloud")["failing"] is False
    # Never set up: nothing was looked for in the vault either.
    assert op.calls == []


async def test_run_daily_tells_operator_once_per_failure_streak(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    down = SourceError("iCloud did not answer")
    source = FakeSource([_card("U1", "Marco Example")], down, down,
                        [_card("U1", "Marco Example")], down)
    operator = Operator()
    _configure(monkeypatch, opened, source, notify=operator)

    for _night in range(5):
        await service.run_daily()

    assert source.fetches == 5
    line = ("Contacts could not be refreshed from iCloud: iCloud did not "
            "answer. The last good copy is still in use.")
    # Two failing nights, one line; a success ends the streak; the next
    # failure is told again.
    assert operator.lines == [line, line]
    with opened.transaction() as conn:
        assert cstore.count(conn, "icloud") == 1


async def test_run_daily_never_raises_when_notify_or_store_fails(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    down = SourceError("iCloud did not answer")
    # notify raises, then answers False: run_daily logs and returns.
    for delivered in (RuntimeError("channel broke"), False):
        store = ContactsStore(opened.directory + f"-{delivered!s}")
        assert store.open()
        operator = Operator(delivered)
        _configure(monkeypatch, store, FakeSource(down), notify=operator)
        await service.run_daily()
        assert len(operator.lines) == 1

    # No operator channel: the failure is only logged.
    _configure(monkeypatch, opened, FakeSource(down), notify=None)
    with caplog.at_level(logging.WARNING):
        await service.run_daily()
    assert "could not be refreshed" in caplog.text

    # A store that cannot be written, a source that breaks in an unexpected
    # way, a login reader that raises: each is a failed result, never an
    # exception, and run_daily returns after telling the operator.
    def raising() -> dict[str, str] | None:
        raise OSError("no op")
    for store, source, read_login, line in (
            (ContactsStore(opened.directory), FakeSource([_card("U1", "M")]),
             ICLOUD, UNAVAILABLE_LINE),
            (opened, FakeSource(ValueError("not a SourceError")), ICLOUD,
             UNEXPECTED_LINE + " No contacts are stored yet."),
            (opened, FakeSource([]), raising,
             UNEXPECTED_LINE + " No contacts are stored yet.")):
        operator = Operator()
        _configure(monkeypatch, store, source, read_login=read_login,
                   notify=operator)
        result = await service.refresh(reason="test")
        assert result is not None and result.ok is False and result.error
        # That refresh began the streak; configuring again starts afresh.
        _configure(monkeypatch, store, source, read_login=read_login,
                   notify=operator)
        await service.run_daily()
        assert operator.lines == [line]


UNAVAILABLE_LINE = ("Contacts could not be refreshed: the contacts store is "
                    "unavailable.")
UNEXPECTED_LINE = ("Contacts could not be refreshed: the refresh stopped on "
                   "an unexpected error.")


async def test_service_failure_tells_operator_once_per_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A corrupt file: the store is unavailable, the credentials are there.
    store = ContactsStore(str(tmp_path / "contacts"))
    Path(store.directory).mkdir(mode=0o700)
    Path(store.path).write_bytes(b"not a database file " * 64)
    assert store.open() is False
    source = FakeSource([_card("U1", "Marco Example")],
                        RuntimeError("a bug"))
    operator = Operator()
    _configure(monkeypatch, store, source, notify=operator)

    await service.run_daily()
    await service.run_daily()
    # Two failing nights, one line; nothing can be said about the copy.
    assert operator.lines == [UNAVAILABLE_LINE]

    # The file is repaired by hand: the next night works and ends the streak.
    Path(store.path).unlink()
    assert store.open() is True
    await service.run_daily()
    assert operator.lines == [UNAVAILABLE_LINE]

    # A new failure is a new streak, told again.
    await service.run_daily()
    assert operator.lines == [
        UNAVAILABLE_LINE,
        UNEXPECTED_LINE + " The last good copy is still in use."]
    assert source.fetches == 2


SYNC_LINE = ("Contacts could not be refreshed from iCloud: iCloud did not "
             "answer. No contacts are stored yet.")
LOGIN_LINE = ("Contacts could not be refreshed: the iCloud login could not "
              "be read.")
GONE = "The vault item chosen for iCloud contacts is no longer in the vault."


def _failing(kind: str, tmp_path: Path):
    """A store, a source and a login reader that fail tonight, the line they
    cause, and a callable that makes the next night succeed."""
    store = ContactsStore(str(tmp_path / "contacts"))
    if kind == "sync":
        assert store.open() is True
        source = FakeSource(SourceError("iCloud did not answer"))

        def recover() -> None:
            source._answers = [[_card("U1", "Marco Example")]]
        return store, source, ICLOUD, SYNC_LINE, recover
    if kind == "login":
        # An item was chosen once and is no longer in the (empty) vault.
        assert store.open() is True
        logins.choose(LOGIN, "gone1")
        source = FakeSource([_card("U1", "Marco Example")])
        held: dict = {"login": None}
        return (store, source, lambda: held["login"],
                f"{LOGIN_LINE} {GONE} No contacts are stored yet.",
                lambda: held.update(login=ICLOUD()))
    # Never opened: the store is unavailable until it opens.
    source = FakeSource([_card("U1", "Marco Example")])
    return store, source, ICLOUD, UNAVAILABLE_LINE, store.open


@pytest.mark.parametrize("kind", ["sync", "service", "login"])
@pytest.mark.parametrize("first", [RuntimeError("Telegram is down"), False],
                         ids=["notify-raises", "notify-false"])
async def test_undelivered_notice_is_sent_on_the_next_failing_night(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
    first: Exception | bool,
) -> None:
    store, source, read_login, line, _recover = _failing(kind, tmp_path)
    operator = Operator(first)
    _configure(monkeypatch, store, source, read_login=read_login,
               notify=operator)

    await service.run_daily()
    assert operator.lines == [line] and operator.received == []

    operator.delivered = True
    await service.run_daily()
    assert operator.received == [line]

    # Delivered: the rest of the streak stays quiet.
    await service.run_daily()
    assert operator.lines == [line, line]
    assert operator.received == [line]


@pytest.mark.parametrize("kind", ["sync", "service", "login"])
async def test_undelivered_notice_is_dropped_once_a_sync_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    store, source, read_login, line, recover = _failing(kind, tmp_path)
    operator = Operator(False)
    _configure(monkeypatch, store, source, read_login=read_login,
               notify=operator)

    await service.run_daily()
    assert operator.lines == [line]

    recover()
    operator.delivered = True
    await service.run_daily()
    await service.run_daily()
    assert operator.lines == [line]
    assert operator.received == []


async def test_failure_line_says_when_no_contacts_are_stored(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = Operator()
    _configure(monkeypatch, opened,
               FakeSource(SourceError("iCloud did not answer")),
               notify=operator)
    await service.run_daily()
    assert operator.lines == [
        "Contacts could not be refreshed from iCloud: iCloud did not answer. "
        "No contacts are stored yet."]


def _item(item_id: str, title: str, user: str = "fictional-apple-id",
          password: str = "abcd-efgh") -> dict:
    return item(item_id, title, username=user, password=password)


async def test_refresh_reads_the_login_through_the_chosen_item(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch, op: FakeOp,
) -> None:
    op.items = [_item("dflt1", "iCloud Contacts", "default-id", "default-pw"),
                _item("chosen1", "Apple account", "chosen-id", "chosen-pw")]
    source = FakeSource([_card("U1", "Marco Example")])
    _configure(monkeypatch, opened, source, read_login=THROUGH_LOGINS)

    # Nothing chosen: the item under the default title.
    result = await service.refresh(reason="test")
    assert result is not None and result.ok
    assert source.credentials == [("default-id", "default-pw")]

    logins.choose(LOGIN, "chosen1")
    result = await service.refresh(reason="test")
    assert result is not None and result.ok
    assert source.credentials[-1] == ("chosen-id", "chosen-pw")
    assert {ref for ref, _ in op.reads()[-2:]} == {"chosen1"}
    assert service.login_problem() is None


async def test_a_never_configured_installation_stays_silent(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch, op: FakeOp,
) -> None:
    # No successful sync ever, nothing chosen, no item under the default
    # title; and the same with a half-filled item under it.
    source = FakeSource([_card("U1", "Marco Example")])
    operator = Operator()
    _configure(monkeypatch, opened, source, read_login=THROUGH_LOGINS,
               notify=operator)
    for items in ([], [item("dflt1", "iCloud Contacts",
                            username="fictional-apple-id")]):
        op.items = items
        assert await service.refresh(reason="test") is None
        await service.run_daily()
        await service.run_daily()
    assert source.fetches == 0
    assert operator.lines == []
    # Only the reads of the values: the vault is not examined any further.
    assert set(op.kinds()) == {"field"}


async def test_a_login_that_stops_working_is_told_once_per_streak(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch, op: FakeOp,
) -> None:
    op.items = [_item("dflt1", "iCloud Contacts", "default-id", "default-pw"),
                _item("chosen1", "Apple account")]
    logins.choose(LOGIN, "chosen1")
    source = FakeSource([_card("U1", "Marco Example")])
    operator = Operator()
    _configure(monkeypatch, opened, source, read_login=THROUGH_LOGINS,
               notify=operator)

    await service.run_daily()                   # a successful start
    assert operator.lines == [] and source.fetches == 1

    # The chosen item is deleted: the next scheduled sync fails and says so.
    op.remove("chosen1")
    await service.run_daily()
    line = f"{LOGIN_LINE} {GONE} The last good copy is still in use."
    assert operator.lines == [line]
    result = await service.refresh(reason="test")
    assert result is not None and result.ok is False
    assert result.error == service.LOGIN_ERROR
    found = service.login_problem()
    assert found is not None and (found.problem, found.item_is_default) == (
        "item_not_found", False)

    # Not again the next night, and never with the default item's login.
    await service.run_daily()
    assert operator.lines == [line]
    assert source.fetches == 1
    assert source.credentials == [("fictional-apple-id", "abcd-efgh")]
    with opened.transaction() as conn:
        assert cstore.count(conn, "icloud") == 1

    # The item is back: the streak ends; a later loss is told again.
    op.items.append(_item("chosen1", "Apple account"))
    await service.run_daily()
    assert operator.lines == [line] and source.fetches == 2
    assert service.login_problem() is None
    op.get("chosen1")["fields"]["password"] = ""
    await service.run_daily()
    assert operator.lines == [line, (
        f"{LOGIN_LINE} The vault item for iCloud contacts has no "
        "app-specific password in `password`: the field is empty. The last "
        "good copy is still in use.")]


async def test_a_damaged_choice_file_is_a_lost_login_told_once(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch, op: FakeOp,
) -> None:
    op.items = [_item("dflt1", "iCloud Contacts", "default-id", "default-pw"),
                _item("chosen1", "Apple account")]
    logins.choose(LOGIN, "chosen1")
    source = FakeSource([_card("U1", "Marco Example")])
    operator = Operator()
    _configure(monkeypatch, opened, source, read_login=THROUGH_LOGINS,
               notify=operator)
    await service.run_daily()                   # a successful start
    assert operator.lines == [] and source.fetches == 1

    # Casa starts again and finds the file of choices damaged.
    Path(logins.PATH).write_text("{ damaged")
    monkeypatch.setattr(logins, "_choices", None)
    asked = len(op.calls)
    await service.run_daily()
    await service.run_daily()
    assert operator.lines == [
        f"{LOGIN_LINE} Casa can no longer read which vault item was chosen "
        "for iCloud contacts; ask the assistant to connect it again. The "
        "last good copy is still in use."]
    found = service.login_problem()
    assert found is not None and found.problem == "choice_unreadable"
    # No sync ran, and nothing in the vault was asked for: not the item
    # under the default title, complete as it is, nor any other.
    assert source.fetches == 1 and len(op.calls) == asked
    assert source.credentials == [("fictional-apple-id", "abcd-efgh")]

    # The item is taken again: the next night works, with that item.
    logins.choose(LOGIN, "chosen1")
    await service.run_daily()
    assert source.fetches == 2 and len(operator.lines) == 1
    assert source.credentials[-1] == ("fictional-apple-id", "abcd-efgh")
    assert service.login_problem() is None


async def test_a_login_that_synced_an_empty_address_book_and_is_deleted(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch, op: FakeOp,
) -> None:
    # Nothing was ever chosen: the default item worked, into no contacts.
    op.items = [_item("dflt1", "iCloud Contacts")]
    source = FakeSource([])
    operator = Operator()
    _configure(monkeypatch, opened, source, read_login=THROUGH_LOGINS,
               notify=operator)
    await service.run_daily()
    assert operator.lines == [] and source.fetches == 1
    assert service.mirror_empty() is True

    op.remove("dflt1")
    await service.run_daily()
    await service.run_daily()
    assert operator.lines == [
        f"{LOGIN_LINE} The vault has no item titled 'iCloud Contacts' for "
        "iCloud contacts. No contacts are stored yet."]
    assert source.fetches == 1


async def test_a_vault_that_is_gone_is_told_like_a_lost_item(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch, op: FakeOp,
) -> None:
    op.items = [_item("dflt1", "iCloud Contacts")]
    source = FakeSource([_card("U1", "Marco Example")])
    operator = Operator()
    _configure(monkeypatch, opened, source, read_login=THROUGH_LOGINS,
               notify=operator)
    await service.run_daily()

    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "")
    await service.run_daily()
    await service.run_daily()
    assert operator.lines == [
        f"{LOGIN_LINE} No default vault is set, so Casa cannot read the "
        "vault item for iCloud contacts. The last good copy is still in "
        "use."]


async def test_concurrent_refresh_runs_one_sync(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    source = FakeSource([_card("U1", "Marco Example")], gate=gate)
    _configure(monkeypatch, opened, source)

    first = asyncio.create_task(service.refresh(reason="daily"))
    second = asyncio.create_task(service.refresh(reason="operator"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gate.set()
    one, two = await asyncio.gather(first, second)

    assert source.fetches == 1
    assert one is two and one.ok and one.added == 1
    # Once it has finished, a later caller runs a sync of its own.
    await service.refresh(reason="operator")
    assert source.fetches == 2


async def test_refresh_passes_the_model_and_notes_become_marks(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = "My cousin, speaks Italian"
    source = FakeSource([_card("U1", "Marco Example", note)])
    model = FakeModel(json.dumps({"U1": {"relation": "cousin",
                                         "language": "it"}}))
    monkeypatch.setenv("CONTACTS_DEFAULT_REGION", "nl")
    _configure(monkeypatch, opened, source, model=model)
    assert service.mirror_empty() is True

    result = await service.refresh(reason="test")

    assert result is not None and result.ok
    assert source.credentials == [("fictional-apple-id", "abcd-efgh")]
    assert len(model.prompts) == 1 and note in model.prompts[0]
    with opened.transaction() as conn:
        marks = cstore.marks_for(conn, "icloud", "U1")
        origin = conn.execute(
            "SELECT origin FROM marks WHERE uid = 'U1' AND field = "
            "'language'").fetchone()[0]
    assert (marks.relation, marks.language) == ("cousin", "it")
    assert origin == "note"
    assert service.active_store() is opened
    assert service.mirror_empty() is False


def test_default_region_reads_env_uppercased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONTACTS_DEFAULT_REGION", raising=False)
    assert service.default_region() == ""
    monkeypatch.setenv("CONTACTS_DEFAULT_REGION", " nl ")
    assert service.default_region() == "NL"
    monkeypatch.setenv("CONTACTS_DEFAULT_REGION", "")
    assert service.default_region() == ""
