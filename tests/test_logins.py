"""Where a login lives: the stored choice of a vault item, the reads through
it, the check that says what is wrong, and the candidates a service's search
terms find. The ``op`` front is a fake holding invented items."""
from __future__ import annotations

import dataclasses
import json
import stat
from pathlib import Path

import pytest

import logins
import private_state
from op_fake import VAULT, FakeOp, fresh_logins, install, item

ICLOUD = "icloud_contacts"
VOICE = "openai_voice"
DEFAULT_PATH = logins.PATH


@pytest.fixture(autouse=True)
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh_logins(monkeypatch, tmp_path)


def _complete(item_id: str = "dflt1", title: str = "iCloud Contacts",
              user: str = "fictional-apple-id",
              password: str = "abcd-efgh") -> dict:
    return item(item_id, title, username=user, password=password)


def test_the_services_and_what_each_needs() -> None:
    icloud, voice = logins.SERVICES[ICLOUD], logins.SERVICES[VOICE]
    assert icloud.default_title == "iCloud Contacts"
    assert icloud.fields == {"username": ("username", "email"),
                             "password": ("password", "credential")}
    assert icloud.queries == ("icloud", "apple")
    assert icloud.names == {"username": "the Apple ID",
                            "password": "the app-specific password"}
    assert voice.default_title == "OpenAI Voice"
    assert voice.fields == {
        "credential": ("credential", "password", "api key"),
        "webhook_secret": ("webhook_secret", "webhook secret")}
    assert voice.queries == ("openai",)
    assert voice.names == {"credential": "the API key",
                           "webhook_secret": "the webhook signing secret"}
    assert set(logins.SERVICES) == {ICLOUD, VOICE}


# --- reading ----------------------------------------------------------------

def test_read_uses_the_default_title(monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _complete())
    assert logins.chosen(ICLOUD) is None
    assert logins.read(ICLOUD, "username") == "fictional-apple-id"
    assert op.calls == [
        ["op", "item", "get", "iCloud Contacts", "--vault", VAULT,
         "--fields", "label=username", "--reveal"]]
    assert logins.read_all(ICLOUD) == {"username": "fictional-apple-id",
                                       "password": "abcd-efgh"}


def test_a_stored_id_wins_over_the_default_title(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _complete(user="default-id",
                                        password="default-pw"),
                 _complete("chosen1", "Apple account", "chosen-id",
                           "chosen-pw"))
    logins.choose(ICLOUD, "chosen1")
    assert logins.chosen(ICLOUD) == "chosen1"
    assert logins.read(ICLOUD, "username") == "chosen-id"
    assert logins.read(ICLOUD, "password") == "chosen-pw"
    assert logins.read_all(ICLOUD) == {"username": "chosen-id",
                                       "password": "chosen-pw"}
    assert {ref for ref, _ in op.reads()} == {"chosen1"}
    # The other service still reads its own default title.
    assert logins.read(VOICE, "credential") is None
    assert op.reads()[-1][0] == "OpenAI Voice"


def test_password_falls_back_to_credential(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, item(
        "api1", "iCloud Contacts", "API_CREDENTIAL",
        username="fictional-apple-id", credential="abcd-efgh"))
    assert logins.read(ICLOUD, "password") == "abcd-efgh"
    assert [label for _, label in op.reads()] == ["password", "credential"]
    # The first accepted label wins while it holds a value.
    op.get("api1")["fields"]["password"] = "first-wins"
    assert logins.read(ICLOUD, "password") == "first-wins"
    # A field that is there and empty is passed over.
    op.get("api1")["fields"]["password"] = ""
    assert logins.read(ICLOUD, "password") == "abcd-efgh"


def test_read_all_is_none_unless_every_value_is_there(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, item("dflt1", "iCloud Contacts",
                                   password="abcd-efgh"))
    assert logins.read_all(ICLOUD) is None
    # Without the first value the second is never asked for.
    assert [label for _, label in op.reads()] == ["username", "email"]
    op.get("dflt1")["fields"].update(username="fictional-apple-id",
                                     password="")
    assert logins.read_all(ICLOUD) is None
    op.get("dflt1")["fields"]["password"] = "abcd-efgh"
    assert logins.read_all(ICLOUD) == {"username": "fictional-apple-id",
                                       "password": "abcd-efgh"}


def test_read_all_takes_every_value_from_the_item_resolved_first(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch,
                 _complete("first1", "Apple account", "first-id", "first-pw"),
                 _complete("second2", "Apple work", "second-id", "second-pw"))
    logins.choose(ICLOUD, "first1")

    def switch(args: list[str]) -> None:
        # The choice changes once the first value has been asked for.
        if "--fields" in args and len(op.reads()) == 1:
            logins.choose(ICLOUD, "second2")
    op.on_call = switch

    assert logins.read_all(ICLOUD) == {"username": "first-id",
                                       "password": "first-pw"}
    assert logins.chosen(ICLOUD) == "second2"
    assert {ref for ref, _ in op.reads()} == {"first1"}


def test_a_deleted_chosen_item_never_falls_back_to_the_default_title(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _complete(user="default-id",
                                        password="default-pw"),
                 _complete("chosen1", "Apple account", "chosen-id",
                           "chosen-pw"))
    logins.choose(ICLOUD, "chosen1")
    op.remove("chosen1")

    found = logins.check(ICLOUD)
    assert (found.ok, found.problem, found.item_is_default) == (
        False, "item_not_found", False)
    assert found.sentence() == ("The vault item chosen for iCloud contacts "
                                "is no longer in the vault.")
    assert logins.read(ICLOUD, "username") is None
    assert logins.read(ICLOUD, "password") is None
    assert logins.read_all(ICLOUD) is None
    # The complete item under the default title was never asked for.
    assert "iCloud Contacts" not in {c[3] for c in op.calls if c[2] == "get"}


def test_without_a_default_vault_nothing_is_run(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _complete(), _complete("chosen1", "Apple"))
    logins.choose(ICLOUD, "chosen1")
    for unset in ("", None):
        if unset is None:
            monkeypatch.delenv("ONEPASSWORD_DEFAULT_VAULT")
        else:
            monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", unset)
        assert logins.read(ICLOUD, "username") is None
        assert logins.read_all(ICLOUD) is None
        found = logins.check(ICLOUD)
        assert (found.ok, found.problem) == (False, "no_vault")
        assert logins.check(ICLOUD, item="chosen1").problem == "no_vault"
        assert logins.candidates(ICLOUD) == []
    assert op.calls == []
    assert found.sentence() == ("No default vault is set, so Casa cannot "
                                "read the vault item for iCloud contacts.")
    assert found.for_model() == {"ok": False, "problem": "no_vault"}


# --- the check --------------------------------------------------------------

def test_check_ok_reads_the_document_once(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _complete())
    found = logins.check(ICLOUD)
    assert found == logins.Check(ok=True, problem="", field="",
                                 item_is_default=True, service=ICLOUD)
    assert found.sentence() == ""
    assert found.for_model() == {"ok": True}
    assert op.calls == [["op", "item", "get", "iCloud Contacts",
                         "--format", "json", "--vault", VAULT]]


@pytest.mark.parametrize(
    ("service", "fields", "problem", "field", "sentence", "what"), [
        (ICLOUD, {"password": "abcd-efgh"}, "field_missing", "username",
         "The vault item for iCloud contacts has no Apple ID in `username`.",
         "the Apple ID"),
        (ICLOUD, {"username": "", "password": "abcd-efgh"}, "field_empty",
         "username",
         "The vault item for iCloud contacts has no Apple ID in `username`: "
         "the field is empty.", "the Apple ID"),
        (ICLOUD, {"email": "fictional-apple-id"}, "field_missing",
         "password",
         "The vault item for iCloud contacts has no app-specific password "
         "in `password`.", "the app-specific password"),
        (ICLOUD, {"username": "fictional-apple-id", "credential": "  "},
         "field_empty", "password",
         "The vault item for iCloud contacts has no app-specific password "
         "in `credential`: the field is empty.",
         "the app-specific password"),
        (VOICE, {"webhook_secret": "whsec_x"}, "field_missing", "credential",
         "The vault item for OpenAI voice has no API key in `credential`.",
         "the API key"),
        (VOICE, {"api key": "sk-test-not-a-real-key"}, "field_missing",
         "webhook_secret",
         "The vault item for OpenAI voice has no webhook signing secret in "
         "`webhook_secret`.", "the webhook signing secret"),
    ])
def test_check_names_the_value_that_is_missing_or_empty(
        monkeypatch: pytest.MonkeyPatch, service: str, fields: dict,
        problem: str, field: str, sentence: str, what: str) -> None:
    title = logins.SERVICES[service].default_title
    op = install(monkeypatch, item("dflt1", title, **fields))
    found = logins.check(service)
    assert (found.ok, found.problem, found.field, found.item_is_default) == (
        False, problem, field, True)
    assert found.sentence() == sentence
    assert found.for_model() == {"ok": False, "problem": problem,
                                 "what": what}
    assert op.kinds() == ["document"]


def test_check_takes_a_value_under_any_accepted_label(
        monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, item(
        "api1", "iCloud Contacts", "API_CREDENTIAL", username="",
        email="fictional-apple-id", credential="abcd-efgh"))
    found = logins.check(ICLOUD)
    assert found.ok and found.problem == ""
    # Which label holds a value that is not under the first accepted one.
    assert dict(found.found) == {"username": "email",
                                 "password": "credential"}


def test_labels_are_matched_whatever_their_case(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # ``op`` finds a field by its label whatever the case, so the check and
    # the candidates must see the same fields the read does.
    op = install(
        monkeypatch,
        item("icl1", "iCloud Contacts", Username="fictional-apple-id",
             Password="abcd-efgh"),
        item("oai2", "OpenAI Voice", "API_CREDENTIAL",
             **{"API Key": "sk-test-not-a-real-key",
                "Webhook Secret": "whsec_x"}))

    found = logins.check(ICLOUD)
    assert (found.ok, found.problem, found.found) == (True, "", ())
    assert logins.read_all(ICLOUD) == {"username": "fictional-apple-id",
                                       "password": "abcd-efgh"}
    assert logins.candidates(ICLOUD) == [
        {"id": "icl1", "title": "iCloud Contacts", "category": "LOGIN",
         "has": {"the Apple ID": True, "the app-specific password": True}}]

    found = logins.check(VOICE)
    assert found.ok and dict(found.found) == {
        "credential": "api key", "webhook_secret": "webhook secret"}
    assert logins.read(VOICE, "credential") == "sk-test-not-a-real-key"
    assert logins.read(VOICE, "webhook_secret") == "whsec_x"
    assert logins.candidates(VOICE)[0]["has"] == {
        "the API key": True, "the webhook signing secret": True}

    # The first accepted label that holds a value still wins, and a field
    # that is there and empty is still told apart from one that is missing.
    fields = op.get("icl1")["fields"]
    fields.update(Password="", CREDENTIAL="from-credential")
    found = logins.check(ICLOUD)
    assert found.ok and dict(found.found) == {"password": "credential"}
    assert logins.read(ICLOUD, "password") == "from-credential"
    del fields["CREDENTIAL"]
    found = logins.check(ICLOUD)
    assert (found.problem, found.field) == ("field_empty", "password")
    assert logins.read(ICLOUD, "password") is None
    assert logins.candidates(ICLOUD)[0]["has"] == {
        "the Apple ID": True, "the app-specific password": False}


def test_a_failed_read_is_not_found_only_when_the_listing_lacks_the_item(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _complete("other1", "Something else"))

    # The listing works and has no item under the default title.
    found = logins.check(ICLOUD)
    assert (found.problem, found.item_is_default) == ("item_not_found", True)
    assert found.sentence() == ("The vault has no item titled 'iCloud "
                                "Contacts' for iCloud contacts.")
    assert found.for_model() == {"ok": False, "problem": "item_not_found"}
    assert op.kinds() == ["document", "list"]

    # The same exit status from a vault that cannot be reached.
    op.down = True
    found = logins.check(ICLOUD)
    assert found.problem == "vault_unreadable"
    assert found.sentence() == ("The vault item for iCloud contacts could "
                                "not be read.")
    assert found.for_model() == {"ok": False, "problem": "vault_unreadable"}

    # The read works again but the listing does not: still not "not found".
    op.down, op.list_down = False, True
    assert logins.check(ICLOUD).problem == "vault_unreadable"

    # The item is listed and its read fails: the vault, not a missing item.
    op.list_down = False
    op.items.append(_complete("dflt1"))
    op.get_down = True
    assert logins.check(ICLOUD).problem == "vault_unreadable"
    logins.choose(ICLOUD, "other1")
    assert logins.check(ICLOUD).problem == "vault_unreadable"

    # A chosen id the listing lacks.
    op.get_down = False
    op.remove("other1")
    found = logins.check(ICLOUD)
    assert (found.problem, found.item_is_default) == ("item_not_found", False)
    # A chosen id is looked for among the ids, never among the titles.
    op.items.append(_complete("zzz9", "other1"))
    op.get_down = True
    assert logins.check(ICLOUD).problem == "item_not_found"


def test_an_op_that_cannot_run_or_answers_nonsense_is_an_unreadable_vault(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    import vault_read

    def missing(args, **kwargs):
        raise FileNotFoundError("op")
    monkeypatch.setattr(vault_read.subprocess, "run", missing)
    assert logins.check(ICLOUD).problem == "vault_unreadable"
    assert logins.candidates(ICLOUD) == []

    calls: list[list[str]] = []

    def slow(args, **kwargs):
        calls.append(list(args))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
    monkeypatch.setattr(vault_read.subprocess, "run", slow)
    assert logins.check(ICLOUD).problem == "vault_unreadable"
    # A read that timed out is not followed by a listing.
    assert len(calls) == 1

    def nonsense(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "not json", "")
    monkeypatch.setattr(vault_read.subprocess, "run", nonsense)
    assert logins.check(ICLOUD).problem == "vault_unreadable"


def test_check_of_a_given_id_ignores_the_stored_choice(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _complete("chosen1", "Apple account"),
                 item("half2", "Apple work", username="fictional-apple-id"))
    logins.choose(ICLOUD, "chosen1")
    assert logins.check(ICLOUD).ok
    found = logins.check(ICLOUD, item="half2")
    assert (found.problem, found.field, found.item_is_default) == (
        "field_missing", "password", False)
    assert [c[3] for c in op.calls] == ["chosen1", "half2"]
    assert logins.chosen(ICLOUD) == "chosen1"


#: A table whose accepted labels are words Casa itself never writes, so a
#: label that reached a result is found by searching for it.
_MARKED = {"username": ("zzlabel-user", "zzlabel-mail"),
           "password": ("zzlabel-pass", "zzlabel-cred")}


def _marked_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(logins.SERVICES, ICLOUD, dataclasses.replace(
        logins.SERVICES[ICLOUD], fields=_MARKED))


def test_what_a_tool_may_return_holds_no_label(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _marked_service(monkeypatch)
    title = "iCloud Contacts"
    op = install(monkeypatch)
    cases = {
        "item_not_found": [],
        "field_missing": [item("m1", title, **{"zzlabel-cred": "ZZVALUE"})],
        "field_empty": [item("m1", title, **{"zzlabel-mail": "",
                                             "zzlabel-pass": "ZZVALUE"})],
        "": [item("m1", title, **{"zzlabel-mail": "ZZVALUE-a",
                                  "zzlabel-cred": "ZZVALUE-b"})],
    }
    seen = []
    for problem, items in cases.items():
        op.items = list(items)
        found = logins.check(ICLOUD)
        assert found.problem == problem
        seen.append(found.for_model())
    op.down = True
    seen.append(logins.check(ICLOUD).for_model())
    text = json.dumps(seen)
    assert "zzlabel" not in text and "ZZVALUE" not in text
    # The sentence for the operator is where a label may be named.
    op.down = False
    op.items = cases["field_missing"]
    assert "`zzlabel-user`" in logins.check(ICLOUD).sentence()
    # A result holds only Casa's own words.
    for found in seen:
        assert set(found) <= {"ok", "problem", "what"}
        assert found.get("what") in (None, "the Apple ID",
                                     "the app-specific password")


# --- the candidates ---------------------------------------------------------

def test_candidates_show_titles_and_never_a_label_or_a_value(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(
        monkeypatch,
        item("apl1", "Apple ID  (family)\nsecond line", "LOGIN",
             username="ZZVALUE-user", password="ZZVALUE-pass",
             **{"ZZLABEL-recovery": "ZZVALUE-rec"}),
        # A common case: an API-credential item with the secret
        # under `credential` and an empty `username`.
        item("icl2", "iCloud app password", "API_CREDENTIAL", username="",
             credential="ZZVALUE-cred", **{"ZZLABEL-note": "ZZVALUE-note"}),
        item("oth3", "Streaming service", username="ZZVALUE-u",
             password="ZZVALUE-p"),
        item("oai4", "OpenAI Voice", "API_CREDENTIAL",
             credential="ZZVALUE-key", webhook_secret="ZZVALUE-secret"))
    got = logins.candidates(ICLOUD)
    assert got == [
        {"id": "icl2", "title": "iCloud app password",
         "category": "API_CREDENTIAL",
         "has": {"the Apple ID": False, "the app-specific password": True}},
        {"id": "apl1", "title": "Apple ID (family) second line",
         "category": "LOGIN",
         "has": {"the Apple ID": True, "the app-specific password": True}},
    ]
    text = json.dumps(got)
    assert "ZZVALUE" not in text and "ZZLABEL" not in text
    # One listing per search term, one document per item found.
    assert op.kinds() == ["list", "document", "list", "document"]
    assert not op.reads()

    assert logins.candidates(VOICE) == [
        {"id": "oai4", "title": "OpenAI Voice", "category": "API_CREDENTIAL",
         "has": {"the API key": True, "the webhook signing secret": True}}]


def test_candidates_hold_no_accepted_label(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _marked_service(monkeypatch)
    install(monkeypatch, item(
        "icl1", "iCloud login", **{"zzlabel-mail": "ZZVALUE-a",
                                   "zzlabel-cred": "ZZVALUE-b"}))
    got = logins.candidates(ICLOUD)
    assert [c["has"] for c in got] == [
        {"the Apple ID": True, "the app-specific password": True}]
    text = json.dumps(got)
    assert "zzlabel" not in text and "ZZVALUE" not in text


def test_items_alike_in_everything_but_the_title_are_both_shown(
        monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch,
            _complete("one1", "iCloud (home)", "same-id", "same-pw"),
            _complete("two2", "iCloud (office)", "same-id", "same-pw"))
    got = logins.candidates(ICLOUD)
    assert [(c["id"], c["title"]) for c in got] == [
        ("one1", "iCloud (home)"), ("two2", "iCloud (office)")]
    assert got[0]["has"] == got[1]["has"] == {
        "the Apple ID": True, "the app-specific password": True}


def test_candidates_are_few_distinct_and_cut_short(
        monkeypatch: pytest.MonkeyPatch) -> None:
    long = "Apple " + "x" * 200
    op = install(
        monkeypatch,
        _complete("both1", "Apple iCloud"),          # matches both terms
        _complete("bad id!", "iCloud bad id"),      # an id nobody can pass
        item("odd3", long, "odd category", username="u"),
        *[_complete(f"more{n}", f"Apple spare {n}") for n in range(6)])
    got = logins.candidates(ICLOUD)
    assert [c["id"] for c in got] == ["both1", "odd3", "more0", "more1",
                                      "more2"]
    assert len(got) == logins.MAX_CANDIDATES == 5
    assert got[1]["title"] == long[:80] and got[1]["category"] is None
    assert got[1]["has"] == {"the Apple ID": True,
                             "the app-specific password": False}
    # No more documents are read than items shown.
    assert op.kinds().count("document") == 5


def test_candidates_are_empty_when_the_vault_cannot_be_listed(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _complete("icl1", "iCloud login"))
    op.down = True
    assert logins.candidates(ICLOUD) == []
    # An item whose document cannot be read is left out.
    op.down, op.get_down = False, True
    assert logins.candidates(ICLOUD) == []


def test_no_more_items_are_tried_than_are_shown_when_reads_fail(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # The vault lists its items and fails every document read: the limit
    # counts the items tried, not only those that could be read.
    op = install(monkeypatch, *[_complete(f"more{n:02d}", f"Apple spare {n}")
                                for n in range(25)])
    op.get_down = True
    assert logins.candidates(ICLOUD) == []
    assert op.kinds().count("document") == logins.MAX_CANDIDATES == 5

    # Some fail and some do not: still five tried in all, the readable
    # ones among them shown.
    op.calls.clear()
    op.get_down = False

    def fail_the_first_three(args: list[str]) -> None:
        op.get_down = (args[2] == "get"
                       and op.kinds().count("document") <= 3)
    op.on_call = fail_the_first_three
    assert [c["id"] for c in logins.candidates(ICLOUD)] == ["more03",
                                                            "more04"]
    assert op.kinds().count("document") == 5

    # Reached within the first search term: the second is not even listed.
    op.calls.clear()
    op.on_call = None
    op.items = [_complete(f"icl{n:02d}", f"iCloud spare {n}")
                for n in range(7)] + op.items
    assert len(logins.candidates(ICLOUD)) == 5
    assert op.kinds() == ["list"] + ["document"] * 5


def test_candidates_can_mark_the_item_in_use(
        monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, _complete(), _complete("other1", "Apple account"),
            _complete("other2", "Apple work"))

    def marked() -> dict[str, bool]:
        return {c["id"]: c["in_use"]
                for c in logins.candidates(ICLOUD, mark_in_use=True)}

    # Nothing chosen: the item under the default title is the one read.
    assert marked() == {"dflt1": True, "other1": False, "other2": False}
    logins.choose(ICLOUD, "other2")
    assert marked() == {"dflt1": False, "other1": False, "other2": True}
    # Asked plainly, the rows are as they always were.
    assert all("in_use" not in c for c in logins.candidates(ICLOUD))
    # While the choice is unknown no item is in use.
    Path(logins.PATH).write_text("{ damaged")
    monkeypatch.setattr(logins, "_choices", None)
    assert marked() == {"dflt1": False, "other1": False, "other2": False}


def test_only_the_item_the_login_really_reads_is_marked_in_use(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _complete(),
                 _complete("twin2", "iCloud Contacts", "other-id", "other-pw"),
                 _complete("other3", "Apple account"))

    def marked() -> dict[str, bool]:
        return {c["id"]: c["in_use"]
                for c in logins.candidates(ICLOUD, mark_in_use=True)}

    # Two items under the default title: the login reads neither, so
    # neither is in use.
    assert logins.read_all(ICLOUD) is None and not logins.check(ICLOUD).ok
    assert marked() == {"dflt1": False, "twin2": False, "other3": False}

    # One of them chosen: that one, and only that one.
    logins.choose(ICLOUD, "twin2")
    assert logins.read_all(ICLOUD) == {"username": "other-id",
                                       "password": "other-pw"}
    assert marked() == {"dflt1": False, "twin2": True, "other3": False}

    # A chosen item that is gone: nothing is read, nothing is marked, and
    # the item under the default title, now the only one, is not either.
    op.remove("twin2")
    assert logins.read_all(ICLOUD) is None
    assert marked() == {"dflt1": False, "other3": False}

    # Nothing chosen and one item under the default title: that one. The
    # title is looked up once, the way a read finds it.
    logins.forget(ICLOUD)
    op.calls.clear()
    assert marked() == {"dflt1": True, "other3": False}
    assert [c[3] for c in op.calls if c[2] == "get"].count(
        "iCloud Contacts") == 1

    # No item under the default title: none.
    op.remove("dflt1")
    assert marked() == {"other3": False}

    # The item read is not among the few shown: no row is marked.
    op.items = [_complete(f"icl{n}", f"iCloud spare {n}") for n in range(5)]
    op.items.append(_complete())
    assert logins.read_all(ICLOUD) is not None
    assert marked() == {f"icl{n}": False for n in range(5)}
    # Asked plainly, the title is not looked up at all.
    op.calls.clear()
    logins.candidates(ICLOUD)
    assert "iCloud Contacts" not in [c[3] for c in op.calls if c[2] == "get"]


# --- the stored choice ------------------------------------------------------

def test_choices_are_kept_in_a_private_file(
        monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(logins.PATH)
    assert logins.chosen(ICLOUD) is None and not path.exists()
    logins.choose(ICLOUD, "abc123")
    assert json.loads(path.read_text()) == {ICLOUD: {"item": "abc123"}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    logins.choose(VOICE, "def456")
    logins.choose(ICLOUD, "ghi789")
    assert json.loads(path.read_text()) == {ICLOUD: {"item": "ghi789"},
                                            VOICE: {"item": "def456"}}

    # A later process finds them.
    monkeypatch.setattr(logins, "_choices", None)
    assert (logins.chosen(ICLOUD), logins.chosen(VOICE)) == ("ghi789",
                                                             "def456")
    logins.forget(ICLOUD)
    assert logins.chosen(ICLOUD) is None
    assert json.loads(path.read_text()) == {VOICE: {"item": "def456"}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    logins.forget(ICLOUD)                       # nothing to forget: no error


def test_the_file_is_declared_private_state() -> None:
    assert DEFAULT_PATH == "/data/logins.json"
    [entry] = [e for e in private_state.FILES if e.path == DEFAULT_PATH]
    assert (entry.mode, entry.kind) == (0o600, private_state.PRIVATE)


def test_choose_refuses_what_it_cannot_store() -> None:
    with pytest.raises(KeyError):
        logins.choose("another_service", "abc123")
    for bad in ("", "has space", "a/b", "x" * 65, None):
        with pytest.raises(ValueError):
            logins.choose(ICLOUD, bad)  # type: ignore[arg-type]
    assert logins.chosen(ICLOUD) is None
    assert not Path(logins.PATH).exists()
    with pytest.raises(KeyError):
        logins.chosen("another_service")


UNREADABLE = ("Casa can no longer read which vault item was chosen for "
              "{label}; ask the assistant to connect it again.")
DAMAGED = ["not json", '{"icloud_contacts": "abc123"}',
           '{"icloud_contacts": {"item": "has space"}}',
           '{"icloud_contacts": {}}', "[]", ""]


def _restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a new process holds: nothing read from the file yet."""
    monkeypatch.setattr(logins, "_choices", None)


def _assert_unreadable(service: str, label: str) -> None:
    found = logins.check(service)
    assert found == logins.Check(ok=False, problem="choice_unreadable",
                                 field="", item_is_default=False,
                                 service=service)
    assert found.sentence() == UNREADABLE.format(label=label)
    assert found.for_model() == {"ok": False, "problem": "choice_unreadable"}
    assert logins.chosen(service) is None
    for field in logins.SERVICES[service].fields:
        assert logins.read(service, field) is None
        assert logins.read_from(service, "chosen1", field) is None
    assert logins.read_all(service) is None


@pytest.mark.parametrize("content", DAMAGED)
def test_a_damaged_file_reads_no_item_until_a_choice_is_made_again(
        monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    # Complete items under both default titles, and a chosen one.
    op = install(
        monkeypatch, _complete(user="default-id", password="default-pw"),
        _complete("chosen1", "Apple account", "chosen-id", "chosen-pw"),
        item("oai1", "OpenAI Voice", "API_CREDENTIAL",
             credential="sk-test-not-a-real-key", webhook_secret="whsec_x"))
    logins.choose(ICLOUD, "chosen1")
    path = Path(logins.PATH)
    path.write_text(content)
    _restart(monkeypatch)

    # Which item was chosen is unknown, for every login: nothing is read,
    # least of all the item under a default title, and no ``op`` is run.
    _assert_unreadable(ICLOUD, "iCloud contacts")
    _assert_unreadable(VOICE, "OpenAI voice")
    assert op.calls == []

    # The damaged file is kept beside a new one that says so, owner-only.
    aside = Path(f"{path}.corrupt")
    assert aside.read_text() == content
    assert stat.S_IMODE(aside.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {ICLOUD: {"item": None},
                                            VOICE: {"item": None}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # A restart does not turn "unknown" into "nothing chosen".
    _restart(monkeypatch)
    _assert_unreadable(ICLOUD, "iCloud contacts")
    _assert_unreadable(VOICE, "OpenAI voice")
    assert op.calls == [] and aside.read_text() == content

    # The candidates are still found: they are how a choice is made again.
    assert [c["id"] for c in logins.candidates(ICLOUD)] == ["dflt1",
                                                            "chosen1"]
    # An item given to the check is checked as it would be if chosen.
    assert logins.check(ICLOUD, item="chosen1").ok
    op.calls.clear()

    # Choosing again repairs that login and no other.
    logins.choose(ICLOUD, "chosen1")
    assert logins.check(ICLOUD).ok
    assert logins.read_all(ICLOUD) == {"username": "chosen-id",
                                       "password": "chosen-pw"}
    assert {c[3] for c in op.calls} == {"chosen1"}
    _assert_unreadable(VOICE, "OpenAI voice")
    assert json.loads(path.read_text()) == {ICLOUD: {"item": "chosen1"},
                                            VOICE: {"item": None}}
    _restart(monkeypatch)
    assert logins.chosen(ICLOUD) == "chosen1"
    _assert_unreadable(VOICE, "OpenAI voice")
    assert "OpenAI Voice" not in {c[3] for c in op.calls}

    # Dropping the unknown choice is a knowing return to the default title.
    logins.forget(VOICE)
    assert logins.check(VOICE).ok and logins.check(VOICE).item_is_default
    assert logins.read(VOICE, "credential") == "sk-test-not-a-real-key"
    assert json.loads(path.read_text()) == {ICLOUD: {"item": "chosen1"}}


def test_an_absent_file_means_nothing_was_chosen(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _complete())
    path = Path(logins.PATH)
    assert not path.exists()
    found = logins.check(ICLOUD)
    assert found.ok and found.item_is_default
    assert logins.read_all(ICLOUD) == {"username": "fictional-apple-id",
                                       "password": "abcd-efgh"}
    assert {c[3] for c in op.calls} == {"iCloud Contacts"}
    # Reading wrote nothing.
    assert not path.exists() and not Path(f"{path}.corrupt").exists()


def test_a_file_that_cannot_be_opened_is_left_where_it_is(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(monkeypatch, _complete())
    path = Path(logins.PATH)
    path.mkdir()                    # reading it fails, whoever runs the test
    _assert_unreadable(ICLOUD, "iCloud contacts")
    _assert_unreadable(VOICE, "OpenAI voice")
    assert op.calls == []
    assert path.is_dir() and not Path(f"{path}.corrupt").exists()
    # It cannot be written either: the choice is refused, nothing changes.
    with pytest.raises(OSError):
        logins.choose(ICLOUD, "dflt1")
    _assert_unreadable(ICLOUD, "iCloud contacts")
    # Once it can be written, choosing again repairs the login.
    path.rmdir()
    logins.choose(ICLOUD, "dflt1")
    assert logins.check(ICLOUD).ok
    _assert_unreadable(VOICE, "OpenAI voice")


def test_a_damaged_file_that_cannot_be_replaced_is_found_again(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import atomic_io
    op = install(monkeypatch, _complete())
    path = Path(logins.PATH)
    path.write_text("not json")
    real = atomic_io.atomic_write_json

    def full(*args, **kwargs):
        raise OSError("no space left on device")
    monkeypatch.setattr(atomic_io, "atomic_write_json", full)
    _assert_unreadable(ICLOUD, "iCloud contacts")
    # Never absent: the damaged file stays, so the next start sees it too.
    assert path.read_text() == "not json"

    monkeypatch.setattr(atomic_io, "atomic_write_json", real)
    _restart(monkeypatch)
    _assert_unreadable(ICLOUD, "iCloud contacts")
    assert json.loads(path.read_text()) == {ICLOUD: {"item": None},
                                            VOICE: {"item": None}}
    assert op.calls == []


def test_listeners_hear_a_choice_and_its_removal() -> None:
    heard: list[str] = []
    logins.on_change(ICLOUD, lambda: heard.append("icloud"))
    voice = lambda: heard.append("voice")  # noqa: E731
    logins.on_change(VOICE, voice)
    logins.on_change(VOICE, voice)              # registered once

    logins.choose(ICLOUD, "abc123")
    assert heard == ["icloud"]
    logins.choose(VOICE, "def456")
    assert heard == ["icloud", "voice"]
    # The same item chosen again is still told: what it holds may be new.
    logins.choose(VOICE, "def456")
    logins.forget(VOICE)
    assert heard == ["icloud", "voice", "voice", "voice"]
    # By the time a listener runs, the choice is the new one.
    logins.on_change(ICLOUD, lambda: heard.append(logins.chosen(ICLOUD)))
    logins.choose(ICLOUD, "new999")
    assert heard[-1] == "new999"


def test_a_failing_listener_does_not_undo_the_choice() -> None:
    heard: list[str] = []

    def broken() -> None:
        raise RuntimeError("listener broke")
    logins.on_change(ICLOUD, broken)
    logins.on_change(ICLOUD, lambda: heard.append("second"))
    logins.choose(ICLOUD, "abc123")
    assert logins.chosen(ICLOUD) == "abc123"
    assert heard == ["second"]


# --- the operator's line about a choice --------------------------------------

def test_the_line_about_a_choice_names_the_item_and_its_state(
        monkeypatch: pytest.MonkeyPatch) -> None:
    op = install(
        monkeypatch, _complete("full1", "Apple account"),
        item("api2", "iCloud app password", "API_CREDENTIAL",
             username="fictional-apple-id", credential="abcd-efgh"),
        item("half3", "Apple work", password="abcd-efgh"),
        item("oai4", "OpenAI key", credential="sk-test-not-a-real-key",
             webhook_secret="whsec_x"))

    def line(service: str, item_id: str) -> str:
        return logins.chosen_line(service, op.get(item_id)["title"],
                                  logins.check(service, item=item_id))

    assert line(ICLOUD, "full1") == (
        "Contacts now use the vault item 'Apple account'.")
    assert line(ICLOUD, "api2") == (
        "Contacts now use the vault item 'iCloud app password', "
        "app-specific password from its `credential` field.")
    assert line(ICLOUD, "half3") == (
        "Contacts are set to use the vault item 'Apple work', but the vault "
        "item for iCloud contacts has no Apple ID in `username`.")
    assert line(VOICE, "oai4") == (
        "OpenAI voice now uses the vault item 'OpenAI key'.")
    op.remove("oai4")
    gone = logins.chosen_line(VOICE, "OpenAI key",
                              logins.check(VOICE, item="oai4"))
    assert gone == (
        "OpenAI voice is set to use the vault item 'OpenAI key', but the "
        "vault item chosen for OpenAI voice is no longer in the vault.")


def test_fake_op_is_what_these_tests_think_it_is() -> None:
    fake = FakeOp(_complete())
    done = fake(["op", "item", "get", "nobody", "--vault", VAULT,
                 "--fields", "label=username", "--reveal"], timeout=10)
    fake.down = True
    down = fake(["op", "item", "get", "iCloud Contacts", "--vault", VAULT,
                 "--fields", "label=username", "--reveal"], timeout=10)
    # An item that is not there and a vault that cannot be reached exit alike.
    assert done.returncode == down.returncode == 1
