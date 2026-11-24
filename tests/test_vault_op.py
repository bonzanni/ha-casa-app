"""The `op` commands the built-in store answers, held against the real CLI.

The callers are programs written for 1Password's `op`: they parse its JSON
and match its error wording. `tests/fixtures/op_reference` holds what the
real CLI printed for each command line they run; these tests run the same
command lines against a store prepared the same way and compare what a
caller can rely on: the exit status, the parsed JSON apart from ids and
times, and the error text after its time stamp. The other half is that a
command line outside that set never reports success and never writes.
"""

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

import pytest
from Cryptodome.PublicKey import RSA

import op_reference
import vault_op
from vault_store import DATABASE_NAME, Field, KeyProblem, Store

VAULT = "Casa"
STAMP = re.compile(r"\[ERROR\] \d{4}/\d\d/\d\d \d\d:\d\d:\d\d ")
ITEM_ID = re.compile(r"\b[a-z2-7]{26}\b")
# How the finance plugin tells "absent" from "broken" in what `op` prints.
FINANCE_NOT_FOUND = re.compile(
    r"isn't an item|isn't a field|does not have a field|no item[s]? (?:found|matched)",
    re.I)

# Fields 1Password puts on every item of a category by itself. No caller
# reads them and the store does not invent them.
CATEGORY_TEMPLATE_FIELDS = frozenset({
    "notesPlain", "username", "credential", "type", "filename", "validFrom",
    "expires", "hostname"})

# The markers are split so secret scanners do not take test data for a key.
PEM_HEAD = "-----BEGIN PRIVATE " "KEY-----\n"
PEM_TAIL = "-----END PRIVATE " "KEY-----\n"
MASKED_PEM = PEM_HEAD + "REDACTED\n" + PEM_TAIL


def cases(*numbers):
    wanted = set(numbers)
    return [name for name in op_reference.names() if int(name[:2]) in wanted]


SUCCESS = cases(*range(1, 20), 38, 40, 50, 51)
ERRORS = cases(*range(30, 38), *range(41, 44))

# One row per form the store answers, then the edges of the rule. Whoever
# has to decide whether to read standard input before the command runs can
# be checked against this table.
WANTS_STDIN = [
    (["--version"], False),
    (["read", "op://Casa/item/field"], False),
    (["item", "list", "--vault", "Casa", "--tags", "tag", "--include-archive",
      "--format", "json"], False),
    (["item", "get", "ref", "--vault", "Casa", "--format", "json"], False),
    (["item", "get", "ref", "--vault", "Casa", "--fields", "label=password",
      "--reveal"], False),
    (["item", "create", "--vault", "Casa", "--format", "json", "-"], True),
    (["item", "create", "--category", "API Credential", "--title", "t",
      "--vault", "Casa", "--tags", "tag", "f[password]=v"], False),
    (["item", "create", "--category", "ssh", "--title", "t", "--vault", "Casa",
      "--tags", "tag", "--ssh-generate-key", "rsa,4096"], False),
    (["item", "edit", "ref", "--vault", "Casa", "--format", "json"], True),
    (["item", "edit", "ref", "--vault", "Casa", "f[password]=v"], False),
    (["item", "delete", "ref", "--vault", "Casa"], False),
    (["item", "edit", "ref", "--vault=Casa", "--format=json"], True),
    (["item", "edit", "ref", "--vault", "Casa", "--format", "json", "f[text]=v"], False),
    (["item", "edit", "ref", "--vault", "Casa", "--format", "yaml"], False),
    (["item", "get", "-", "--format", "json"], False),
    (["item"], False),
    ([], False),
]


def op(store, *args, stdin=None):
    return vault_op.run(store, VAULT, list(args), stdin)


def template(title, fields, **more):
    return json.dumps({"title": title, "category": "PASSWORD", "fields": fields, **more})


def masked(text):
    """Key material as the recordings show it: the shape stays, the key goes."""
    if text.startswith(PEM_HEAD) and text.endswith(PEM_TAIL):
        lines = text[len(PEM_HEAD):-len(PEM_TAIL)].splitlines()
        assert lines and all(len(line) == 64 for line in lines[:-1])
        assert 0 < len(lines[-1]) <= 64
        return MASKED_PEM
    if text.startswith("ssh-rsa "):
        return "ssh-rsa REDACTED"
    if text.startswith("SHA256:"):
        return "SHA256:REDACTED"
    return text


def relied_on(document):
    """An item document or a list row without what differs by nature.

    Left out: ids and times (only their shape is checked), the version, the
    vault's id, and what 1Password adds on its own account.
    """
    kept = {
        "title": document["title"],
        "category": document["category"],
        "tags": document.get("tags", "no such key"),
        "vault": document["vault"]["name"],
    }
    if "fields" in document:
        kept["fields"] = [
            {key: masked(field[key])
             for key in ("id", "type", "purpose", "label", "value", "reference")
             if key in field}
            | ({"id": "generated"} if ITEM_ID.fullmatch(field["id"]) else {})
            for field in document["fields"]
            if field["id"] not in CATEGORY_TEMPLATE_FIELDS
        ]
    return kept


def after_stamp(err):
    assert STAMP.match(err), err
    return ITEM_ID.sub("<id>", STAMP.sub("", err, count=1))


def snapshot(store, directory):
    return (directory / DATABASE_NAME).read_bytes(), store.list()


@dataclass
class Replay:
    store: Store    # as the last recorded command left it
    results: dict   # case name -> (exit status, standard output, standard error)
    ids: dict       # an id in the recordings -> the same item's id in this store


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    """Every recorded command line, run once in the order it was recorded.

    The recording started from a vault without these items and each command
    saw what the ones before it left, so running them in file-name order
    against one empty store gives every case the state its recording had.
    """
    store = Store.open(tmp_path_factory.mktemp("replay"), None)
    results, ids = {}, {}
    for name in op_reference.names():
        recorded = op_reference.case(name)
        args = [ids.get(arg, arg) for arg in recorded.args]
        if name == "16-create-ssh":
            # Making a 4096-bit key takes seconds; the slow test below runs
            # this command line as recorded.
            args = ["rsa,2048" if arg == "rsa,4096" else arg for arg in args]
        results[name] = vault_op.run(store, VAULT, args, recorded.stdin)
        if recorded.out.startswith("{"):
            ids[json.loads(recorded.out)["id"]] = json.loads(results[name][1])["id"]
    return Replay(store, results, ids)


@pytest.fixture(scope="module")
def directory(tmp_path_factory):
    return tmp_path_factory.mktemp("vault")


@pytest.fixture(scope="module")
def store(directory):
    """One store for the tests below; each uses titles of its own.

    Opening and every write derive the file's key again, so a store per
    test would spend most of the run on that.
    """
    store = Store.open(directory, None)
    for title, tags in (("X", ["kept"]), ("a", ["kept", "more"]), ("apart", ["else"])):
        store.create(title=title, category="PASSWORD", tags=tags, writer="op",
                     fields=[Field("", "password", "CONCEALED", "hunter2"),
                             Field("", "f", "STRING", "plain")])
    return store


# -- the recorded command lines ----------------------------------------------

@pytest.mark.parametrize("name", SUCCESS)
def test_recorded_success_cases(replay, name):
    recorded = op_reference.case(name)
    code, out, err = replay.results[name]
    assert (code, err) == (recorded.code, recorded.err) == (0, "")

    if recorded.out.startswith("ID:"):
        return  # a text summary: no caller reads it, the exit status is all

    if recorded.out.startswith("["):
        ours, theirs = json.loads(out), json.loads(recorded.out)
        assert [relied_on(row) for row in ours] == [relied_on(row) for row in theirs]
        for our_row, their_row in zip(ours, theirs):
            assert ITEM_ID.fullmatch(our_row["id"])
            assert replay.ids.get(their_row["id"], our_row["id"]) == our_row["id"]
            assert "fields" not in our_row
        if not theirs:
            assert out == "[]\n"
        return

    if recorded.out.startswith("{"):
        ours, theirs = json.loads(out), json.loads(recorded.out)
        assert relied_on(ours) == relied_on(theirs)
        assert ours["id"] == replay.ids[theirs["id"]] and ITEM_ID.fullmatch(ours["id"])
        assert ours["vault"] == {"id": VAULT, "name": VAULT}
        assert isinstance(ours["version"], int)
        created = datetime.fromisoformat(ours["created_at"])
        updated = datetime.fromisoformat(ours["updated_at"])
        assert created.tzinfo is not None and created <= updated
        if name == "40-create-duplicate-title":
            first = json.loads(replay.results["02-create-template"][1])
            assert ours["title"] == first["title"] and ours["id"] != first["id"]
        return

    assert masked(out) == recorded.out


@pytest.mark.parametrize("name", ERRORS)
def test_recorded_error_cases_use_the_recorded_wording(replay, name):
    recorded = op_reference.case(name)
    code, out, err = replay.results[name]
    assert (code, out) == (recorded.code, recorded.out) == (1, "")
    assert after_stamp(err) == after_stamp(recorded.err)
    if "More than one item" in recorded.err:
        same_title = {replay.ids[json.loads(op_reference.case(created).out)["id"]]
                      for created in ("02-create-template", "40-create-duplicate-title")}
        assert set(ITEM_ID.findall(STAMP.sub("", err, count=1))) == same_title
        assert len(same_title) == 2


@pytest.mark.parametrize("name", ERRORS)
def test_error_words_fall_on_the_same_side_of_the_finance_pattern(replay, name):
    recorded = op_reference.case(name)
    ours = replay.results[name][2]
    assert bool(FINANCE_NOT_FOUND.search(ours)) == bool(FINANCE_NOT_FOUND.search(recorded.err))


def test_the_replayed_ssh_item_reads_back_the_key_its_document_shows(replay):
    document = json.loads(replay.results["18-get-c-json"][1])
    private = next(f["value"] for f in document["fields"] if f["label"] == "private key")
    assert replay.results["17-read-private-key"][1] == private


def test_the_replayed_delete_removed_that_item_and_no_other(replay):
    def ours(created):
        return replay.ids[json.loads(op_reference.case(created).out)["id"]]

    deleted = replay.ids[op_reference.case("50-delete-id").args[2]]
    assert deleted == ours("02-create-template")
    assert [(item.title, item.id) for item in replay.store.list()] == [
        ("zz-vault-fixture-b", ours("15-get-b-json")),
        ("zz-vault-fixture-c", ours("18-get-c-json")),
        ("zz-vault-fixture-a", ours("40-create-duplicate-title")),
    ]


# -- anything else never succeeds --------------------------------------------

UNSUPPORTED = [
    ["whoami"],
    ["vault", "list"],
    ["item", "list"],
    ["item", "get", "X", "--format", "yaml"],
    ["item", "get", "X", "--fields", "password"],
    ["read", "op://Casa/a/sec/f"],
    ["read", "op://Casa/a/f?attribute=otp"],
    ["item", "create", "--category", "Login", "--title", "t", "--vault", "Casa",
     "--generate-password"],
    ["item", "edit", "X", "--vault", "Casa", "f[otp]=v"],
    ["document", "get", "x"],
    # each strictness rule once more, on a command that is otherwise answered
    [],
    ["--version", "--help"],
    ["read", "Casa/a/f"],
    ["read", "op://Casa/a/f", "--no-newline"],
    ["read", "op://Casa/a/f", "op://Casa/a/f"],
    ["item", "list", "--format", "json", "--favorite"],
    ["item", "list", "--format", "json", "--tags", "kept,"],
    ["item", "list", "--format", "json", "--vault"],
    ["item", "list", "--format", "json", "--format", "json"],
    ["item", "list", "X", "--format", "json"],
    ["item", "get", "X", "a", "--format", "json"],
    ["item", "get", "X", "--vault", "Casa"],
    ["item", "get", "X", "--vault", "Casa", "--format", "json", "--reveal"],
    ["item", "get", "X", "--vault", "Casa", "--fields", "label=password"],
    ["item", "get", "X", "--vault", "Casa", "--fields", "label=password",
     "--reveal=true"],
    ["item", "get", "X", "--vault", "Casa", "--fields", "label=password,label=f",
     "--reveal"],
    ["item", "get", "X", "--fields", "label=password", "--reveal"],
    ["item", "get", "X", "--vault", "Casa", "--fields", "password", "--reveal"],
    ["item", "get", "X", "--vault", "Casa", "--fields", "id=password", "--reveal"],
    ["item", "create", "--vault", "Casa", "--format", "json"],
    ["item", "create", "--vault", "Casa", "-"],
    ["item", "create", "--category", "API Credential", "--title", "t", "--vault",
     "Casa", "--tags", "tag"],
    ["item", "create", "--category", "API Credential", "--title", "t", "--vault",
     "Casa", "f[password]=v"],
    ["item", "create", "--category", "Login", "--title", "t", "--vault", "Casa",
     "--tags", "tag", "f[password]=v"],
    ["item", "create", "--category", "ssh", "--title", "t", "--vault", "Casa",
     "--tags", "tag", "f[password]=v"],
    ["item", "create", "--category", "API Credential", "--title", "t", "--vault",
     "Casa", "--tags", "tag", "f=v"],
    ["item", "create", "--category", "API Credential", "--title", "", "--vault",
     "Casa", "--tags", "tag", "f[text]=v"],
    ["item", "create", "--category", "API Credential", "--title", "t", "--vault",
     "Casa", "--tags", "tag", "section.f[text]=v"],
    ["item", "create", "--category", "API Credential", "--title", "t", "--vault",
     "Casa", "--tags", "tag", "--format", "json", "f[text]=v"],
    ["item", "create", "--category", "ssh", "--title", "t", "--vault", "Casa",
     "--tags", "tag", "--ssh-generate-key", "ed25519"],
    ["item", "create", "--category", "ssh", "--title", "t", "--vault", "Casa",
     "--tags", "tag", "--ssh-generate-key", "rsa,1024"],
    ["item", "create", "--category", "ssh", "--title", "t", "--vault", "Casa",
     "--tags", "tag"],
    ["item", "create", "--category", "API Credential", "--title", "t", "--vault",
     "Casa", "--tags", "tag", "--ssh-generate-key", "rsa,2048"],
    ["item", "edit", "X", "--vault", "Casa"],
    ["item", "edit", "X", "f[text]=v"],
    ["item", "edit", "X", "--vault", "Casa", "--format", "json", "f[text]=v"],
    ["item", "edit", "X", "--vault", "Casa", "f[delete]"],
    ["item", "edit", "X", "--vault", "Casa", "f[text]=v", "--generate-password"],
    ["item", "delete", "X"],
    ["item", "delete", "X", "--vault", "Casa", "--archive"],
    ["item", "delete", "X", "a", "--vault", "Casa"],
    ["item", "delete", "--vault", "Casa"],
    ["item", "move", "X", "--vault", "Casa"],
    ["item"],
]


@pytest.mark.parametrize("args", UNSUPPORTED, ids=" ".join)
def test_unsupported_never_succeeds(store, directory, args):
    before = snapshot(store, directory)
    code, out, err = vault_op.run(store, VAULT, args, "{}")
    assert code != 0 and out == ""
    assert after_stamp(err).startswith("unsupported by the built-in store: ")
    assert err.count("\n") == 1 and err.endswith("\n")
    assert snapshot(store, directory) == before


TEMPLATE_COMMANDS = [
    ["item", "create", "--vault", "Casa", "--format", "json", "-"],
    ["item", "edit", "X", "--vault", "Casa", "--format", "json"],
]


@pytest.mark.parametrize("stdin", [
    pytest.param(None, id="no input"),
    pytest.param("", id="empty input"),
    pytest.param("not json", id="not JSON"),
    pytest.param("[]", id="not an object"),
    pytest.param(json.dumps({"title": "t", "category": "PASSWORD"}), id="no fields"),
    pytest.param(template("t", [], sections=[{"id": "s"}]), id="sections"),
    pytest.param(template("t", [], urls=[{"href": "https://example.org"}]), id="urls"),
    pytest.param(template("t", [{"label": "f", "type": "STRING", "value": "v",
                                 "section": {"id": "s"}}]), id="field in a section"),
    pytest.param(template("t", [{"label": "f", "type": "STRING", "value": 5}]),
                 id="value not text"),
    pytest.param(template("t", [{"type": "STRING", "value": "v"}]), id="field without label"),
    pytest.param(template("t", [{"label": "f", "value": "v"}]), id="field without type"),
    pytest.param(template("t", [{"label": "f", "type": "STRING", "value": "1"},
                                {"label": "f", "type": "STRING", "value": "2"}]),
                 id="one label twice"),
    pytest.param(template("t", [], tags=["one,two"]), id="tag with a comma"),
    pytest.param(template("t", [], tags="one"), id="tags not a list"),
    pytest.param(template("", []), id="empty title"),
])
@pytest.mark.parametrize("command", TEMPLATE_COMMANDS, ids=lambda command: command[1])
def test_a_template_the_store_cannot_hold_whole_is_refused(store, directory, command, stdin):
    before = snapshot(store, directory)
    code, out, err = vault_op.run(store, VAULT, command, stdin)
    assert code != 0 and out == ""
    assert after_stamp(err).startswith("unsupported by the built-in store: ")
    assert snapshot(store, directory) == before


@pytest.mark.parametrize("stdin", [
    pytest.param(json.dumps({"category": "PASSWORD", "fields": []}), id="no title"),
    pytest.param(json.dumps({"title": "t", "fields": []}), id="no category"),
])
def test_a_new_item_needs_a_title_and_a_category(store, directory, stdin):
    before = snapshot(store, directory)
    code, out, err = vault_op.run(store, VAULT, TEMPLATE_COMMANDS[0], stdin)
    assert code != 0 and out == ""
    assert after_stamp(err).startswith("unsupported by the built-in store: ")
    assert snapshot(store, directory) == before


def test_an_edit_keeps_the_title_category_and_tags_its_template_leaves_out(store):
    made = json.loads(op(store, "item", "create", "--vault", "Casa", "--format", "json", "-",
                         stdin=template("kept as it is", [], tags=["one"]))[1])
    code, out, err = op(store, "item", "edit", made["id"], "--vault", "Casa", "--format",
                        "json", stdin=json.dumps({"fields": [
                            {"label": "password", "type": "CONCEALED", "value": "new"}]}))
    assert (code, err) == (0, "")
    edited = json.loads(out)
    assert relied_on(edited) | {"fields": None} == relied_on(made) | {"fields": None}
    assert edited["id"] == made["id"]
    assert [(f["id"], f["label"], f["value"]) for f in edited["fields"]] == [
        ("password", "password", "new")]


@pytest.mark.parametrize("args", [
    ["item", "create", "--vault", "Other", "--format", "json", "-"],
    ["item", "create", "--category", "API Credential", "--title", "t", "--vault",
     "Other", "--tags", "tag", "f[password]=v"],
    ["item", "create", "--category", "ssh", "--title", "t", "--vault", "Other",
     "--tags", "tag", "--ssh-generate-key", "rsa,2048"],
    ["item", "edit", "X", "--vault", "Other", "--format", "json"],
    ["item", "edit", "X", "--vault", "Other", "f[text]=v"],
    ["item", "delete", "X", "--vault", "Other"],
    ["item", "get", "X", "--vault", "Other", "--format", "json"],
    ["item", "get", "X", "--vault", "Other", "--fields", "label=password", "--reveal"],
], ids=" ".join)
def test_another_vault_is_not_this_one(store, directory, args):
    before = snapshot(store, directory)
    code, out, err = vault_op.run(store, VAULT, args, template("t", []))
    assert (code, out) == (1, "")
    assert after_stamp(err) == ('"Other" isn\'t a vault in this account. '
                                "Specify the vault with its ID or name.\n")
    assert not FINANCE_NOT_FOUND.search(err)
    assert snapshot(store, directory) == before


# -- the forms, beyond what was recorded --------------------------------------

def test_flag_equals_value_is_the_same_as_flag_then_value(store):
    spaced = op(store, "item", "get", "X", "--vault", "Casa", "--format", "json")
    joined = op(store, "item", "get", "X", "--vault=Casa", "--format=json")
    assert spaced == joined and spaced[0] == 0
    assert op(store, "item", "get", "--format=json", "X") == spaced
    assert op(store, "item", "get", "X", "--vault=Casa", "--fields=label=password",
              "--reveal") == (0, "hunter2\n", "")


def test_a_reference_names_a_field_by_label_or_by_id(store):
    document = json.loads(op(store, "item", "get", "X", "--format", "json")[1])
    field = next(f for f in document["fields"] if f["label"] == "f")
    assert field["id"] != "f" and field["reference"] == "op://Casa/X/f"
    by_ids = f"op://Casa/{document['id']}/{field['id']}"
    assert op(store, "read", by_ids) == op(store, "read", "op://Casa/X/f") == (0, "plain\n", "")


def test_a_label_is_matched_whatever_its_case_when_read(store):
    # The real CLI finds a field by its label in any case, on both of its
    # read paths.
    code, _, err = op(store, "item", "create", "--category", "API Credential",
                      "--title", "cased", "--vault", "Casa", "--tags", "tag",
                      "Password[password]=hunter3", "API Key[password]=key-one")
    assert (code, err) == (0, "")

    def by_label(label):
        return op(store, "item", "get", "cased", "--vault", "Casa",
                  "--fields", f"label={label}", "--reveal")

    def by_reference(label):
        return op(store, "read", f"op://Casa/cased/{label}")

    for label in ("password", "Password", "PASSWORD"):
        assert by_label(label) == by_reference(label) == (0, "hunter3\n", ""), label
    for label in ("api key", "API Key"):
        assert by_label(label) == by_reference(label) == (0, "key-one\n", ""), label

    # A label the item does not have is still not a field, in the CLI's words.
    code, out, err = by_label("nope")
    assert (code, out) == (1, "") and '"nope" isn\'t a field in the "cased" item' in err
    code, out, err = by_reference("nope")
    assert (code, out) == (1, "") and "does not have a field 'nope'" in err

    # A write names its label exactly, so `password` is kept beside
    # `Password`. Each is then read by its own spelling, and what was just
    # written under a label is what that label reads back; a third spelling
    # finds the first of the two.
    assert op(store, "item", "edit", "cased", "--vault", "Casa",
              "password[password]=lower")[0::2] == (0, "")
    assert [f.label for f in store.get("cased").fields] == [
        "Password", "API Key", "password"]
    assert by_label("password") == by_reference("password") == (0, "lower\n", "")
    assert by_label("Password") == by_reference("Password") == (0, "hunter3\n", "")
    assert by_label("PASSWORD") == by_reference("PASSWORD") == (0, "hunter3\n", "")


def test_a_field_added_later_cannot_take_over_a_reference_by_id(store):
    # Casa wires references as op://vault/item id/field id, so a field whose
    # label happens to equal another field's id must not redirect them.
    code, _, err = op(store, "item", "create", "--category", "ssh", "--title", "shadowed",
                      "--vault", "Casa", "--tags", "tag", "--ssh-generate-key", "rsa,2048")
    assert (code, err) == (0, "")
    item = store.get("shadowed")
    original = next(f.value for f in item.fields if f.id == "private_key")
    by_id = f"op://Casa/{item.id}/private_key"
    assert op(store, "read", by_id) == (0, original, "")

    assert op(store, "item", "edit", item.id, "--vault", "Casa",
              "private_key[password]=other-secret")[0::2] == (0, "")
    assert op(store, "read", by_id) == (0, original, "")
    added = next(f for f in store.get(item.id).fields if f.label == "private_key")
    assert added.id != "private_key"
    assert op(store, "read", f"op://Casa/{item.id}/{added.id}") == (0, "other-secret\n", "")


def test_a_list_without_a_vault_lists_everything_and_tags_narrow_it(store):
    def titles(*flags):
        code, out, err = op(store, "item", "list", *flags, "--format", "json")
        assert (code, err) == (0, "")
        return [row["title"] for row in json.loads(out)]

    everything = titles()
    assert everything == [item.title for item in store.list()]
    assert {"X", "a", "apart"} <= set(everything)
    assert titles("--vault", "Casa") == everything
    assert titles("--tags", "kept") == ["X", "a"]
    assert titles("--tags", "more") == ["a"]
    assert titles("--tags", "absent,else,more") == ["a", "apart"]
    assert titles("--tags=else", "--include-archive") == ["apart"]


def test_an_item_without_tags_or_an_empty_value_leaves_the_key_out(store):
    code, out, err = op(store, "item", "create", "--vault", "Casa", "--format", "json",
                        "-", stdin=template("bare", [{"label": "note", "type": "STRING"}]))
    assert (code, err) == (0, "")
    document = json.loads(out)
    assert "tags" not in document
    assert document["fields"] == [{"id": document["fields"][0]["id"], "type": "STRING",
                                   "label": "note",
                                   "reference": "op://Casa/bare/note"}]
    row = next(row for row in json.loads(op(store, "item", "list", "--format", "json")[1])
               if row["id"] == document["id"])
    assert "tags" not in row and "fields" not in row
    assert op(store, "read", "op://Casa/bare/note") == (0, "\n", "")


def test_assignment_value_with_equals_and_bracket(store):
    value = "a]=b[text]=c==[password]"
    code, out, err = op(store, "item", "create", "--category", "API Credential",
                        "--title", "brackets", "--vault", "Casa", "--tags", "one,two",
                        f"api token[password]={value}")
    assert (code, err) == (0, "")
    assert op(store, "read", "op://Casa/brackets/api token") == (0, value + "\n", "")
    item = store.get("brackets")
    assert (item.category, item.tags, item.writer) == ("API_CREDENTIAL", ("one", "two"), "op")
    assert [(f.label, f.type) for f in item.fields] == [("api token", "CONCEALED")]


def test_assignment_value_starting_with_dash(store):
    assert op(store, "item", "create", "--category", "API Credential", "--title",
              "dashes", "--vault", "Casa", "--tags", "tag", "pin[password]=-1234")[0] == 0
    assert op(store, "item", "edit", "dashes", "--vault", "Casa",
              "note[text]=--format", "other[text]=-")[0] == 0
    assert op(store, "read", "op://Casa/dashes/pin") == (0, "-1234\n", "")
    assert op(store, "read", "op://Casa/dashes/note") == (0, "--format\n", "")
    assert op(store, "read", "op://Casa/dashes/other") == (0, "-\n", "")


def test_read_returns_pem_with_interior_newlines_and_one_final_newline(store):
    ended = "-----BEGIN CERTIFICATE-----\nAAAA\nBBBB\n-----END CERTIFICATE-----\n"
    open_ended = ended[:-1]
    code, _, err = op(
        store, "item", "create", "--vault", "Casa", "--format", "json", "-",
        stdin=template("pem", [{"label": "ended", "type": "CONCEALED", "value": ended},
                               {"label": "open", "type": "CONCEALED", "value": open_ended}]))
    assert (code, err) == (0, "")
    assert op(store, "read", "op://Casa/pem/ended") == (0, ended, "")
    assert op(store, "read", "op://Casa/pem/open") == (0, ended, "")
    assert op(store, "item", "get", "pem", "--vault", "Casa", "--fields", "label=open",
              "--reveal") == (0, ended, "")
    fields = json.loads(op(store, "item", "get", "pem", "--format", "json")[1])["fields"]
    assert [f["value"] for f in fields] == [ended, open_ended]


def test_edit_assignment_adds_a_field_to_an_existing_item(store):
    created = op_reference.case("11-create-api-credential")
    added = op_reference.case("13-edit-assign-new-field")
    assert vault_op.run(store, VAULT, created.args, None)[0] == 0
    before = store.get("zz-vault-fixture-b")
    assert vault_op.run(store, VAULT, added.args, None)[0::2] == (0, "")
    after = store.get("zz-vault-fixture-b")
    assert after.id == before.id
    assert [(f.label, f.type, f.value) for f in after.fields] == [
        ("refresh token", "CONCEALED", "fixture-refresh-one"),
        ("session note", "STRING", "fixture plain text"),
    ]
    assert len([item for item in store.list() if item.title == "zz-vault-fixture-b"]) == 1


def test_edit_assignment_on_missing_item_is_not_found(store, directory):
    recorded = op_reference.case("33-edit-missing-item")
    before = snapshot(store, directory)
    code, out, err = vault_op.run(store, VAULT, recorded.args, None)
    assert (code, out) == (1, "")
    assert after_stamp(err) == after_stamp(recorded.err)
    assert FINANCE_NOT_FOUND.search(err)
    assert snapshot(store, directory) == before


def test_the_text_summary_names_the_item_and_keeps_concealed_values_back(store):
    code, out, err = op(store, "item", "create", "--category", "API Credential",
                        "--title", "summary", "--vault", "Casa", "--tags", "tag",
                        "token[password]=s3cret-value", "note[text]=in the open")
    assert (code, err) == (0, "")
    item = store.get("summary")
    assert f"ID:          {item.id}\n" in out and "Title:       summary\n" in out
    assert "s3cret-value" not in out and "in the open" in out
    code, out, err = op(store, "item", "edit", "summary", "--vault", "Casa",
                        "token[password]=an0ther-value")
    assert (code, err) == (0, "") and item.id in out and "an0ther-value" not in out


def ssh_item(store, title):
    document = json.loads(op(store, "item", "get", title, "--vault", "Casa",
                             "--format", "json")[1])
    assert document["category"] == "SSH_KEY"
    assert [(f["id"], f["label"], f["type"]) for f in document["fields"]] == [
        ("public_key", "public key", "STRING"),
        ("fingerprint", "fingerprint", "STRING"),
        ("private_key", "private key", "SSHKEY"),
        ("key_type", "key type", "STRING"),
    ]
    return {f["label"]: f["value"] for f in document["fields"]}


def test_ssh_key_2048_is_pkcs8_with_64_char_lines_and_loads(store):
    code, out, err = op(store, "item", "create", "--category", "ssh", "--title",
                        "deploy key", "--vault", "Casa", "--tags", "tag",
                        "--ssh-generate-key", "rsa,2048")
    assert (code, err) == (0, "")
    values = ssh_item(store, "deploy key")
    private = values["private key"]
    assert private not in out

    assert masked(private) == MASKED_PEM     # PKCS#8 armour, 64-character lines
    assert op(store, "read", "op://Casa/deploy key/private key") == (0, private, "")
    key = RSA.import_key(private)
    assert key.has_private() and key.size_in_bits() == 2048

    public = key.public_key().export_key(format="OpenSSH").decode()
    assert values["public key"] == public and public.startswith("ssh-rsa ")
    blob = base64.b64decode(public.split()[1])
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    assert values["fingerprint"] == "SHA256:" + digest
    assert values["key type"] == "rsa"


@pytest.mark.slow
def test_ssh_key_4096(store):
    recorded = op_reference.case("16-create-ssh")
    assert vault_op.run(store, VAULT, recorded.args, None)[0::2] == (0, "")
    values = ssh_item(store, "zz-vault-fixture-c")
    assert masked(values["private key"]) == MASKED_PEM
    assert RSA.import_key(values["private key"]).size_in_bits() == 4096


@pytest.mark.parametrize("args, expected", WANTS_STDIN,
                         ids=lambda value: " ".join(value) if isinstance(value, list) else None)
def test_wants_stdin(args, expected):
    assert vault_op.wants_stdin(args) is expected


class FailingStore:
    """A store whose every method fails with an exception that carries a secret."""

    def __init__(self, exception):
        self.exception = exception

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise self.exception
        return fail


@pytest.mark.parametrize("exception, shown", [
    (KeyProblem("the key hunter2 does not open /data/vault/casa.kdbx"), "KeyProblem"),
    (RuntimeError("hunter2"), "RuntimeError"),
    (OSError(28, "No space left on device: hunter2"), "OSError"),
], ids=["KeyProblem", "RuntimeError", "OSError"])
@pytest.mark.parametrize("args, stdin", [
    (["read", "op://Casa/X/password"], None),
    (["item", "list", "--format", "json"], None),
    (["item", "get", "X", "--format", "json"], None),
    (["item", "create", "--vault", "Casa", "--format", "json", "-"],
     template("t", [{"label": "password", "type": "CONCEALED", "value": "hunter2"}])),
    (["item", "edit", "X", "--vault", "Casa", "password[password]=hunter2"], None),
    (["item", "delete", "X", "--vault", "Casa"], None),
], ids=["read", "list", "get", "create", "edit", "delete"])
def test_a_store_failure_reports_the_class_name_only(exception, shown, args, stdin):
    code, out, err = vault_op.run(FailingStore(exception), VAULT, args, stdin)
    assert (code, out) == (1, "")
    assert after_stamp(err) == f"built-in store failure: {shown}\n"
