"""The contacts service: the iCloud secret, the shared refresh and the daily
job with its one operator line per failure streak.

Real SQLite files in a temporary directory; the source, the model, the
operator channel and the ``op`` front are fakes. The people are invented.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path

import pytest

from contacts import service
from contacts import store as cstore
from contacts import sync
from contacts.store import ContactsStore
from contacts.sync import SourceError
from contacts.vcard import Card


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


def _secrets(**fields: str):
    def read(field: str) -> str | None:
        return fields.get(field)
    return read


ICLOUD = _secrets(username="fictional-apple-id", password="abcd-efgh")


def _configure(monkeypatch: pytest.MonkeyPatch, store: ContactsStore,
               source: FakeSource, *, read_secret=ICLOUD,
               notify=None, model=None) -> None:
    def build(username: str, password: str) -> FakeSource:
        source.credentials.append((username, password))
        return source
    monkeypatch.setattr(service, "ICloudSource", build)
    service.configure(store=store, notify=notify, model=model,
                      read_secret=read_secret)


async def test_refresh_without_secret_is_none_and_silent(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeSource([_card("U1", "Marco Example")])
    operator = Operator()
    for secret in (_secrets(), _secrets(username="fictional-apple-id"),
                   _secrets(password="abcd-efgh")):
        _configure(monkeypatch, opened, source, read_secret=secret,
                   notify=operator)
        assert await service.refresh(reason="test") is None
        await service.run_daily()
    assert source.fetches == 0
    assert operator.lines == []
    with opened.transaction() as conn:
        assert cstore.sync_state(conn, "icloud")["failing"] is False


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
    # way, a secret reader that raises: each is a failed result, never an
    # exception, and run_daily returns after telling the operator.
    def raising(field: str) -> str | None:
        raise OSError("no op")
    for store, source, read_secret, line in (
            (ContactsStore(opened.directory), FakeSource([_card("U1", "M")]),
             ICLOUD, UNAVAILABLE_LINE),
            (opened, FakeSource(ValueError("not a SourceError")), ICLOUD,
             UNEXPECTED_LINE + " No contacts are stored yet."),
            (opened, FakeSource([]), raising,
             UNEXPECTED_LINE + " No contacts are stored yet.")):
        operator = Operator()
        _configure(monkeypatch, store, source, read_secret=read_secret,
                   notify=operator)
        result = await service.refresh(reason="test")
        assert result is not None and result.ok is False and result.error
        # That refresh began the streak; configuring again starts afresh.
        _configure(monkeypatch, store, source, read_secret=read_secret,
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


def _failing(kind: str, tmp_path: Path):
    """A store and a source that fail tonight, the line they cause, and a
    callable that makes the next night succeed."""
    store = ContactsStore(str(tmp_path / "contacts"))
    if kind == "sync":
        assert store.open() is True
        source = FakeSource(SourceError("iCloud did not answer"))

        def recover() -> None:
            source._answers = [[_card("U1", "Marco Example")]]
        return store, source, SYNC_LINE, recover
    # Never opened: the store is unavailable until it opens.
    source = FakeSource([_card("U1", "Marco Example")])
    return store, source, UNAVAILABLE_LINE, store.open


@pytest.mark.parametrize("kind", ["sync", "service"])
@pytest.mark.parametrize("first", [RuntimeError("Telegram is down"), False],
                         ids=["notify-raises", "notify-false"])
async def test_undelivered_notice_is_sent_on_the_next_failing_night(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
    first: Exception | bool,
) -> None:
    store, source, line, _recover = _failing(kind, tmp_path)
    operator = Operator(first)
    _configure(monkeypatch, store, source, notify=operator)

    await service.run_daily()
    assert operator.lines == [line] and operator.received == []

    operator.delivered = True
    await service.run_daily()
    assert operator.received == [line]

    # Delivered: the rest of the streak stays quiet.
    await service.run_daily()
    assert operator.lines == [line, line]
    assert operator.received == [line]


@pytest.mark.parametrize("kind", ["sync", "service"])
async def test_undelivered_notice_is_dropped_once_a_sync_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    store, source, line, recover = _failing(kind, tmp_path)
    operator = Operator(False)
    _configure(monkeypatch, store, source, notify=operator)

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


async def test_missing_username_reads_no_password(
    opened: ContactsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Home Vault")
    fake = _Op({"username": _done(1, "", "[ERROR] no such item"),
                "password": _done(0, "abcd-efgh\n")})
    monkeypatch.setattr(service.subprocess, "run", fake)
    source = FakeSource([_card("U1", "Marco Example")])
    _configure(monkeypatch, opened, source,
               read_secret=service.read_icloud_secret)

    assert await service.refresh(reason="test") is None
    assert [call[call.index("--fields") + 1] for call in fake.calls] == [
        "label=username"]
    assert source.fetches == 0


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


class _Op:
    """A fake ``subprocess.run`` for the ``op`` front."""

    def __init__(self, answers: dict[str, subprocess.CompletedProcess]):
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        assert kwargs.get("timeout") == 10
        field = args[args.index("--fields") + 1].removeprefix("label=")
        answer = self.answers[field]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _done(code: int, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(["op"], code, out, err)


def test_read_secret_uses_op_front_and_hides_output(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Home Vault")
    fake = _Op({"username": _done(0, "fictional-apple-id\n"),
                "password": _done(0, "abcd-efgh\n")})
    monkeypatch.setattr(service.subprocess, "run", fake)

    assert service.read_icloud_secret("username") == "fictional-apple-id"
    assert service.read_icloud_secret("password") == "abcd-efgh"
    assert fake.calls == [
        ["op", "item", "get", "iCloud Contacts", "--vault", "Home Vault",
         "--fields", f"label={field}", "--reveal"]
        for field in ("username", "password")]

    leak = "secret-looking-op-diagnostic"
    for answer in (_done(1, "", f"[ERROR] {leak}"), _done(0, "\n", leak),
                   subprocess.TimeoutExpired(["op"], 10, output=leak,
                                             stderr=leak)):
        monkeypatch.setattr(service.subprocess, "run",
                            _Op({"password": answer}))
        with caplog.at_level(logging.DEBUG):
            assert service.read_icloud_secret("password") is None
    assert leak not in caplog.text
    assert "abcd-efgh" not in caplog.text
    assert all(leak not in str(r.args) for r in caplog.records)

    monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT")
    unused = _Op({})
    monkeypatch.setattr(service.subprocess, "run", unused)
    assert service.read_icloud_secret("username") is None
    assert unused.calls == []


def test_default_region_reads_env_uppercased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONTACTS_DEFAULT_REGION", raising=False)
    assert service.default_region() == ""
    monkeypatch.setenv("CONTACTS_DEFAULT_REGION", " nl ")
    assert service.default_region() == "NL"
    monkeypatch.setenv("CONTACTS_DEFAULT_REGION", "")
    assert service.default_region() == ""
