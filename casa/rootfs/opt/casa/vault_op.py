"""The `op` commands Casa and its plugins run, answered from the built-in store.

The callers are programs written for the 1Password CLI. They parse its JSON
and tell "absent" from "broken" by its error wording, so for the command
lines listed below this module gives the answers that CLI gives: the same
exit status, the same JSON keys, the same words. The wording and the shapes
are those the CLI was recorded printing (`tests/fixtures/op_reference`).

    --version
    read op://VAULT/ITEM/FIELD
    item list [--vault V] [--tags T[,T]] [--include-archive] --format json
    item get REF [--vault V] --format json
    item get REF --vault V --fields label=L --reveal
    item create --vault V --format json -                 (template on stdin)
    item create --category "API Credential" --title T --vault V --tags T ASSIGN...
    item create --category ssh --title T --vault V --tags T --ssh-generate-key rsa,BITS
    item edit REF --vault V --format json                 (template on stdin)
    item edit REF --vault V ASSIGN...
    item delete REF --vault V

Anything else is refused: exit status 1 and a line saying what is
unsupported. A command line that is almost one of these is not guessed at,
because a caller that is told "done" for something the store did not do
carries on with a secret that was never stored.

Error lines never repeat a field value, and an unexpected failure is
reported by its class name alone: whatever is printed here ends up in the
caller's logs.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
import time
from collections.abc import Iterator
from datetime import datetime, timezone

from Cryptodome.PublicKey import RSA

from vault_store import Ambiguous, Field, Item, ItemNotFound, Store

VERSION = "2.30.0"

# Who the store records as the last writer of an item changed through here.
_WRITER = "op"

_REFERENCE = re.compile(r"op://([^/?]+)/([^/?]+)/([^/?]+)")
# LABEL[password]=VALUE or LABEL[text]=VALUE. A label holding ".", "=", "\"
# or a bracket means something else to the real CLI (a section, an escape),
# so it is not taken as a plain label here. With those out of the label, the
# value starts after the first "]=" and may itself hold any character.
_ASSIGNMENT = re.compile(r"([^.=\\\[\]]+)\[(password|text)\]=(.*)", re.DOTALL)
_FIELD_TYPE = {"password": "CONCEALED", "text": "STRING"}
_FIELDS_BY_LABEL = re.compile(r"label=([^,]+)")
_SSH_KEY_SIZES = {"rsa,2048": 2048, "rsa,3072": 3072, "rsa,4096": 4096}
_PLAIN_WORD = re.compile(r"[A-Za-z][A-Za-z-]{0,30}")
_FLAG_NAME = re.compile(r"--?[A-Za-z][A-Za-z-]{0,30}")

_TEMPLATE_KEYS = frozenset({"title", "category", "tags", "fields"})
_TEMPLATE_FIELD_KEYS = frozenset({"id", "type", "purpose", "label", "value"})


class _Failure(Exception):
    """The command ends with exit status 1 and this text after the time stamp."""


def run(store: Store, vault_name: str, args: list[str],
        stdin: str | None) -> tuple[int, str, str]:
    """Answer one `op` command line: exit status, standard output, standard error.

    `vault_name` is the one vault the store stands for. `stdin` is what the
    caller piped in, when `wants_stdin` asked for it. Never raises.
    """
    try:
        return 0, _answer(store, vault_name, list(args), stdin), ""
    except _Failure as failure:
        return 1, "", _error(str(failure))
    except Exception as exc:  # noqa: BLE001 - the caller gets a line, never a traceback
        # The message is left out: it can hold a path, a label or a value.
        return 1, "", _error(f"built-in store failure: {type(exc).__name__}")


def wants_stdin(args: list[str]) -> bool:
    """Whether the command line is one that takes a template on standard input.

    Decided from the arguments alone, so that whoever passes the command on
    knows whether to read its own standard input first.
    """
    command, rest = args[:2], args[2:]
    if command == ["item", "create"]:
        return "-" in rest
    if command == ["item", "edit"]:
        as_json = "--format=json" in rest or ("--format", "json") in zip(rest, rest[1:])
        return as_json and not any("]=" in arg for arg in rest)
    return False


def _answer(store: Store, vault: str, args: list[str], stdin: str | None) -> str:
    if args == ["--version"]:
        return VERSION + "\n"
    if args[:1] == ["read"]:
        return _read(store, vault, args[1:])
    if args[:1] == ["item"] and args[1:2] and args[1] in _ITEM_COMMANDS:
        return _ITEM_COMMANDS[args[1]](store, vault, args[2:], stdin)
    words = [arg for arg in args[:2] if _PLAIN_WORD.fullmatch(arg)]
    raise _unsupported("the command " + " ".join(words) if words else "this command line")


# -- the commands -------------------------------------------------------------

def _read(store: Store, vault: str, args: list[str]) -> str:
    _, positionals = _split("read", args)
    match = _REFERENCE.fullmatch(positionals[0]) if len(positionals) == 1 else None
    if match is None:
        # A fourth segment is a section and "?" starts a query: both reach
        # things the store does not keep.
        raise _unsupported("read with anything but one op://VAULT/ITEM/FIELD reference")
    reference = match.group(0)
    wanted_vault, ref, name = match.groups()
    try:
        _check_vault(vault, wanted_vault)
        with _lookup(vault, ref):
            item = store.get(ref)
    except _Failure as failure:
        raise _Failure(f"could not read secret '{reference}': "
                       f"could not get item {wanted_vault}/{ref}: {failure}") from None
    # A field is named by its id or by its label. Casa wires references of
    # the form op://vault/item id/field id, so the id is tried first: a field
    # added later whose label equals that id must not take the reference over.
    field = (next((f for f in item.fields if f.id == name), None)
             or _field(item, name))
    if field is None:
        raise _Failure(f"could not read secret '{reference}': "
                       f"item '{wanted_vault}/{ref}' does not have a field '{name}'")
    return _line(field.value)


def _item_list(store: Store, vault: str, args: list[str], stdin: str | None) -> str:
    flags, positionals = _split("item list", args,
                                valued=("--vault", "--tags", "--format"),
                                switches=("--include-archive",))
    if positionals:
        raise _unsupported("item list with an argument")
    _json_only("item list", flags)
    if "--vault" in flags:
        _check_vault(vault, flags["--vault"])
    # The store has no archive, so --include-archive changes nothing.
    tags = frozenset(_tags(flags["--tags"].split(","))) if "--tags" in flags else None
    return _json([_row(vault, item) for item in store.list(tags)])


def _item_get(store: Store, vault: str, args: list[str], stdin: str | None) -> str:
    flags, positionals = _split("item get", args,
                                valued=("--vault", "--format", "--fields"),
                                switches=("--reveal",))
    ref = _one_ref("item get", positionals)
    if set(flags) == {"--vault", "--fields", "--reveal"}:
        match = _FIELDS_BY_LABEL.fullmatch(flags["--fields"])
        if match is None:
            raise _unsupported("--fields other than label=NAME")
        _check_vault(vault, flags["--vault"])
        with _lookup(vault, ref):
            item = store.get(ref)
        field = _field(item, match.group(1))
        if field is None:
            raise _Failure(f'"{match.group(1)}" isn\'t a field in the "{item.title}" item')
        return _line(field.value)
    if set(flags) - {"--vault", "--format"}:
        raise _unsupported("item get with this combination of flags")
    _json_only("item get", flags)
    if "--vault" in flags:
        _check_vault(vault, flags["--vault"])
    with _lookup(vault, ref):
        return _json(_document(vault, store.get(ref)))


def _item_create(store: Store, vault: str, args: list[str], stdin: str | None) -> str:
    flags, positionals = _split(
        "item create", args,
        valued=("--vault", "--format", "--category", "--title", "--tags",
                "--ssh-generate-key"))
    given = set(flags)

    if given == {"--vault", "--format"} and positionals == ["-"]:
        _json_only("item create", flags)
        template = _template(stdin, creating=True)
        _check_vault(vault, flags["--vault"])
        return _json(_document(vault, store.create(writer=_WRITER, **template)))

    described = {"--category", "--title", "--vault", "--tags"}
    if flags.get("--title") == "":
        raise _unsupported("an item without a title")
    tags = _tags(flags["--tags"].split(",")) if "--tags" in flags else []
    if given == described and positionals and flags["--category"] == "API Credential":
        fields = _assigned(positionals)
        _check_vault(vault, flags["--vault"])
        return _summary(vault, store.create(
            title=flags["--title"], category="API_CREDENTIAL", tags=tags,
            fields=fields, writer=_WRITER))

    if (given == described | {"--ssh-generate-key"} and not positionals
            and flags["--category"] == "ssh"):
        bits = _SSH_KEY_SIZES.get(flags["--ssh-generate-key"])
        if bits is None:
            raise _unsupported("--ssh-generate-key other than rsa,2048, rsa,3072 or rsa,4096")
        _check_vault(vault, flags["--vault"])
        # Making the key takes seconds; it is done before the store is
        # asked for anything, so nobody else waits on it.
        fields = _ssh_key_fields(bits)
        return _summary(vault, store.create(
            title=flags["--title"], category="SSH_KEY", tags=tags,
            fields=fields, writer=_WRITER))

    raise _unsupported("item create with these arguments")


def _item_edit(store: Store, vault: str, args: list[str], stdin: str | None) -> str:
    flags, positionals = _split("item edit", args, valued=("--vault", "--format"))
    if not positionals or "--vault" not in flags:
        raise _unsupported("item edit without an item and --vault")
    ref, assignments = positionals[0], positionals[1:]

    if "--format" in flags and not assignments:
        _json_only("item edit", flags)
        template = _template(stdin, creating=False)
        _check_vault(vault, flags["--vault"])
        with _lookup(vault, ref):
            return _json(_document(vault, store.replace(ref, writer=_WRITER, **template)))

    if assignments and "--format" not in flags:
        fields = _assigned(assignments)
        _check_vault(vault, flags["--vault"])
        with _lookup(vault, ref):
            for field in fields:
                item = store.set_field(ref, label=field.label, type=field.type,
                                       value=field.value, writer=_WRITER)
        return _summary(vault, item)

    raise _unsupported("item edit with these arguments")


def _item_delete(store: Store, vault: str, args: list[str], stdin: str | None) -> str:
    flags, positionals = _split("item delete", args, valued=("--vault",))
    ref = _one_ref("item delete", positionals)
    if "--vault" not in flags:
        raise _unsupported("item delete without --vault")
    _check_vault(vault, flags["--vault"])
    with _lookup(vault, ref):
        store.delete(ref)
    return ""


_ITEM_COMMANDS = {
    "list": _item_list,
    "get": _item_get,
    "create": _item_create,
    "edit": _item_edit,
    "delete": _item_delete,
}


# -- reading the command line -------------------------------------------------

def _unsupported(what: str) -> _Failure:
    return _Failure(f"unsupported by the built-in store: {what}")


def _split(command: str, args: list[str], valued: tuple[str, ...] = (),
           switches: tuple[str, ...] = ()) -> tuple[dict[str, str], list[str]]:
    """The flags of `command` with their values, and the other arguments in order.

    Only the flags named are known. A value is given as `--flag value` or
    `--flag=value`; a switch takes none.
    """
    flags: dict[str, str] = {}
    positionals: list[str] = []
    rest = iter(args)
    for arg in rest:
        if arg == "-" or not arg.startswith("-"):
            positionals.append(arg)
            continue
        name, joined, value = arg.partition("=")
        if name in switches and not joined:
            value = ""
        elif name in valued:
            if not joined:
                value = next(rest, None)
                if value is None:
                    raise _unsupported(f"{name} without a value")
        else:
            # Only what looks like a flag's name is repeated: an argument
            # that merely starts with a dash could be a secret.
            shown = name if _FLAG_NAME.fullmatch(name) else "this option"
            raise _unsupported(f"{shown} with {command}")
        if name in flags:
            raise _unsupported(f"{name} given twice")
        flags[name] = value
    return flags, positionals


def _one_ref(command: str, positionals: list[str]) -> str:
    if len(positionals) != 1:
        raise _unsupported(f"{command} with anything but one item")
    return positionals[0]


def _json_only(command: str, flags: dict[str, str]) -> None:
    if "--format" not in flags:
        raise _unsupported(f"{command} without --format json")
    if flags["--format"] != "json":
        raise _unsupported("--format other than json")


def _tags(tags: list) -> list[str]:
    for tag in tags:
        # KeePass keeps an entry's tags as one separated string, so these
        # would come back as different tags, or none.
        if not isinstance(tag, str) or not tag or "," in tag or ";" in tag:
            raise _unsupported("a tag that is empty or contains ',' or ';'")
    return tags


def _assigned(assignments: list[str]) -> list[Field]:
    fields = []
    for assignment in assignments:
        match = _ASSIGNMENT.fullmatch(assignment)
        if match is None:
            raise _unsupported("a field assignment other than "
                               "LABEL[password]=VALUE or LABEL[text]=VALUE")
        label, kind, value = match.groups()
        fields.append(Field(id="", label=label, type=_FIELD_TYPE[kind], value=value))
    _one_field_per_label(fields)
    return fields


def _template(stdin: str | None, creating: bool) -> dict:
    """The item a JSON template describes, as the store's keyword arguments.

    A template may hold only what the store keeps. One with anything more (a
    section, a URL, a file) is refused whole rather than stored in part.
    """
    try:
        template = json.loads(stdin or "")
    except ValueError:
        template = None
    if not isinstance(template, dict) or not template.keys() <= _TEMPLATE_KEYS:
        raise _unsupported("a template that is not a JSON object of "
                           "title, category, tags and fields")
    title, category = template.get("title"), template.get("category")
    tags, described = template.get("tags"), template.get("fields")
    for name, text in (("title", title), ("category", category)):
        # An edit may leave either out and keep what the item has.
        if not (isinstance(text, str) and text) and (creating or text is not None):
            raise _unsupported(f"a template without a {name}")
    if tags is not None and not isinstance(tags, list):
        raise _unsupported("a template whose tags are not a list")
    if not isinstance(described, list):
        # The fields given become the item's whole field list, so "none
        # given" must be said with an empty list, not by omission.
        raise _unsupported("a template without a list of fields")
    fields = []
    for field in described:
        if not isinstance(field, dict) or not field.keys() <= _TEMPLATE_FIELD_KEYS:
            raise _unsupported("a template field with more than "
                               "id, type, purpose, label and value")
        text = {key: field.get(key, "") for key in _TEMPLATE_FIELD_KEYS}
        if not all(isinstance(value, str) for value in text.values()):
            raise _unsupported("a template field whose label, type or value is not text")
        if not text["label"] or not text["type"]:
            raise _unsupported("a template field without a label and a type")
        fields.append(Field(id=text["id"], label=text["label"], type=text["type"],
                            value=text["value"], purpose=text["purpose"] or None))
    _one_field_per_label(fields)
    if tags is None and creating:
        tags = []
    return {"title": title, "category": category,
            "tags": None if tags is None else _tags(tags), "fields": fields}


def _one_field_per_label(fields: list[Field]) -> None:
    labels = [field.label for field in fields]
    if len(set(labels)) != len(labels):
        # The store keeps one value per label; the second would replace the first.
        raise _unsupported("two fields with one label")


# -- finding things, in 1Password's words --------------------------------------

def _check_vault(vault: str, wanted: str) -> None:
    if wanted != vault:
        raise _Failure(f'"{wanted}" isn\'t a vault in this account. '
                       "Specify the vault with its ID or name.")


@contextlib.contextmanager
def _lookup(vault: str, ref: str) -> Iterator[None]:
    """Turn the store's "no such item" and "several such items" into the CLI's."""
    try:
        yield
    except ItemNotFound:
        raise _Failure(f'"{ref}" isn\'t an item in the "{vault}" vault. '
                       "Specify the item with its UUID, name, or domain.") from None
    except Ambiguous as several:
        # The store never picks one of several items with the same title.
        choices = "".join(f'\n\t* for the item "{ref}" in vault {vault}: {item_id}'
                          for item_id in several.ids)
        raise _Failure(f'More than one item matches "{ref}". '
                       f"Try again and specify the item by its ID: {choices}") from None


def _field(item: Item, label: str) -> Field | None:
    """The field a read names by *label*, matched whatever its case, as the
    real CLI matches a label.

    A write names its label exactly, so an item can hold `Password` beside
    `password`. The field labelled exactly as asked comes first: what a
    caller has just stored under a label is what that label reads back.
    Failing that, the first field whose label differs only in case.
    """
    wanted = label.casefold()
    return (next((field for field in item.fields if field.label == label), None)
            or next((field for field in item.fields
                     if field.label.casefold() == wanted), None))


# -- what is printed -----------------------------------------------------------

def _error(message: str) -> str:
    return f"[ERROR] {time.strftime('%Y/%m/%d %H:%M:%S')} {message}\n"


def _line(value: str) -> str:
    """A value as `op read` prints it: ending in one newline.

    A value that already ends in one (a PEM block) gets no second.
    """
    return value if value.endswith("\n") else value + "\n"


def _json(answer: dict | list) -> str:
    return json.dumps(answer, indent=2, ensure_ascii=False) + "\n"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(vault: str, item: Item) -> dict:
    """An item as `item list` shows it; keys in the order the real CLI prints them."""
    row: dict = {"id": item.id, "title": item.title}
    if item.tags:
        row["tags"] = list(item.tags)
    # The store does not count an item's edits, so the version does not move.
    row["version"] = 1
    row["vault"] = {"id": vault, "name": vault}
    row["category"] = item.category
    row["created_at"] = _stamp(item.created_at)
    row["updated_at"] = _stamp(item.updated_at)
    return row


def _document(vault: str, item: Item) -> dict:
    document = _row(vault, item)
    document["fields"] = fields = []
    for field in item.fields:
        shown = {"id": field.id, "type": field.type}
        if field.purpose:
            shown["purpose"] = field.purpose
        shown["label"] = field.label
        if field.value:  # like the real CLI, an empty value has no key at all
            shown["value"] = field.value
        shown["reference"] = f"op://{vault}/{item.title}/{field.label}"
        fields.append(shown)
    return document


def _summary(vault: str, item: Item) -> str:
    """What the real CLI prints after a create or an edit without --format json.

    No caller reads it. Only plain-text fields show their value.
    """
    head = [("ID", item.id), ("Title", item.title), ("Vault", f"{vault} ({vault})"),
            ("Created", _stamp(item.created_at)), ("Updated", _stamp(item.updated_at)),
            ("Favorite", "false")]
    if item.tags:
        head.append(("Tags", ", ".join(item.tags)))
    head += [("Version", "1"), ("Category", item.category)]
    lines = [f"{name + ':':<13}{value}" for name, value in head]
    lines.append("Fields:")
    width = max((len(field.label) for field in item.fields), default=0) + 5
    for field in item.fields:
        value = (field.value if field.type == "STRING"
                 else f"[use 'op item get {item.id} --reveal' to reveal]")
        lines.append(f"  {field.label + ':':<{width}}{value}")
    return "\n".join(lines) + "\n"


def _ssh_key_fields(bits: int) -> list[Field]:
    """A new RSA key as the fields 1Password gives an SSH key item."""
    key = RSA.generate(bits)
    public = key.public_key().export_key(format="OpenSSH").decode("ascii")
    blob = base64.b64decode(public.split(" ", 1)[1])
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii")
    # PKCS#8 in PEM armour, ending in a newline, is what `op read` returns
    # for such an item and what a caller loads.
    private = key.export_key(format="PEM", pkcs=8).decode("ascii") + "\n"
    return [
        Field(id="public_key", label="public key", type="STRING", value=public),
        Field(id="fingerprint", label="fingerprint", type="STRING",
              value="SHA256:" + digest.rstrip("=")),
        Field(id="private_key", label="private key", type="SSHKEY", value=private),
        Field(id="key_type", label="key type", type="STRING", value="rsa"),
    ]
