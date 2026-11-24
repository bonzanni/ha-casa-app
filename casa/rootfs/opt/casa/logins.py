"""Where a login lives: which vault item holds each login Casa itself signs
in with, and whether that item has what the login needs.

A service (:data:`SERVICES`) needs a few values. They are read from ONE item
in the default vault: the item the resident assistant chose for the service
(:func:`choose`; kept by its ``op`` item id in :data:`PATH`), or, while none
is chosen, the item titled :attr:`Service.default_title`. A chosen item that
is gone or incomplete is never replaced by another one: the login then has
nothing to read, and :func:`check` says what is wrong.

The same holds when the record of the choices itself cannot be read. No file
means nothing was ever chosen, and the default titles apply. A file that is
there and damaged means the choices are unknown: no login reads any item,
the default one included, until an item is chosen for it again. The damaged
file is kept beside a new one that records "unknown" for every login, so the
state outlasts a restart and the repair of another login.

Each value is looked for under a few accepted field labels, in order; the
first one that holds a value wins. A label is matched whatever its case, as
``op`` matches it. Values that are used together are read
from the item resolved once (:func:`read_all`), so they never come from two
different choices.

Two kinds of words leave this module. What an agent may see
(:meth:`Check.for_model`, :func:`candidates`) holds a problem code, Casa's
own name for a value and, for the candidates, each item's title: never a
field label, never a value. What the operator is told (:meth:`Check.sentence`,
:func:`chosen_line`) is for Casa's own lines and the log, and may name a
field label; it never goes into a tool result.

With no default vault configured nothing here runs ``op``: without
``--vault`` it would look in whatever vault the CLI defaults to. Everything
that asks the vault is blocking.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import atomic_io
import vault_read
import vault_tools

logger = logging.getLogger(__name__)

PATH = "/data/logins.json"
#: The most candidates shown for one service, over all its search terms.
MAX_CANDIDATES = 5
#: A candidate's title is shown on one line, cut to this many characters.
TITLE_CHARS = 80

_ITEM_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CATEGORY = re.compile(r"[A-Z_]{1,40}")

Problem = Literal["", "choice_unreadable", "no_vault", "vault_unreadable",
                  "item_not_found", "field_missing", "field_empty"]


@dataclass(frozen=True)
class Service:
    """One login Casa signs in with.

    ``fields`` maps each value Casa needs to the field labels accepted for
    it, the first match winning. ``queries`` are the search terms that find
    the items that could hold the login. ``names`` gives each value Casa's
    own plain name. ``label`` is how a sentence names the service;
    ``now_uses`` and ``set_to_use`` open the operator's line about a
    choice."""

    default_title: str
    fields: dict[str, tuple[str, ...]]
    queries: tuple[str, ...]
    names: dict[str, str]
    label: str
    now_uses: str
    set_to_use: str


SERVICES: dict[str, Service] = {
    "icloud_contacts": Service(
        default_title="iCloud Contacts",
        fields={"username": ("username", "email"),
                "password": ("password", "credential")},
        queries=("icloud", "apple"),
        names={"username": "the Apple ID",
               "password": "the app-specific password"},
        label="iCloud contacts",
        now_uses="Contacts now use",
        set_to_use="Contacts are set to use"),
    "openai_voice": Service(
        default_title="OpenAI Voice",
        fields={"credential": ("credential", "password", "api key"),
                "webhook_secret": ("webhook_secret", "webhook secret")},
        queries=("openai",),
        names={"credential": "the API key",
               "webhook_secret": "the webhook signing secret"},
        label="OpenAI voice",
        now_uses="OpenAI voice now uses",
        set_to_use="OpenAI voice is set to use"),
}

_lock = threading.Lock()
#: service -> chosen item id, as the file holds it, or None for a service
#: whose choice is unknown (the file was damaged). The whole is None until
#: the file is first read.
_choices: dict[str, str | None] | None = None
#: service -> the callbacks told when its choice changes.
_listeners: dict[str, list[Callable[[], None]]] = {}


def _service(service: str) -> Service:
    """The service of that name; ``KeyError`` for any other."""
    return SERVICES[service]


def _bare(name: str) -> str:
    """A value's plain name without its article."""
    return name.removeprefix("the ")


# --- the stored choice --------------------------------------------------------

def _valid(data: object) -> bool:
    """A dict of entries, each with an ``item`` that is an item id, or null
    where the choice is unknown."""
    return isinstance(data, dict) and all(
        isinstance(entry, dict) and "item" in entry
        and (entry["item"] is None
             or isinstance(entry["item"], str)
             and _ITEM_ID.fullmatch(entry["item"]) is not None)
        for entry in data.values())


def _write(choices: dict[str, str | None]) -> None:
    atomic_io.atomic_write_json(
        PATH, {name: {"item": item_id} for name, item_id in choices.items()},
        mode=atomic_io.PRIVATE)


def _set_aside(damaged: bytes) -> None:
    """Keep the damaged file's content beside it, owner-only, for whoever
    looks into it. Best-effort."""
    try:
        fd = os.open(f"{PATH}.corrupt",
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, atomic_io.PRIVATE)
        with os.fdopen(fd, "wb") as aside:
            os.fchmod(aside.fileno(), atomic_io.PRIVATE)
            aside.write(damaged)
    except OSError:
        logger.warning("logins: the damaged %s was not copied aside", PATH,
                       exc_info=True)


def _load() -> dict[str, str | None]:
    """The choices as the file holds them. No file: nothing was chosen. A
    file that cannot be opened, does not parse or is not of the shape: the
    choices are unknown, for every service.

    A damaged file is copied aside and then replaced, in one atomic write,
    by a file that records "unknown", so there is no moment at which the
    file is missing and "unknown" could be taken for "nothing chosen". When
    that write fails the damaged file stays and is found again at the next
    start. A file that cannot even be opened is left as it is."""
    unknown: dict[str, str | None] = {name: None for name in SERVICES}
    try:
        raw = Path(PATH).read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.error("%s cannot be read (%s): which vault item each login "
                     "uses is unknown, and none is read until it is chosen "
                     "again", PATH, type(exc).__name__)
        return unknown
    try:
        data = json.loads(raw)
        if not _valid(data):
            raise ValueError("not a record of chosen items")
    except (ValueError, RecursionError):
        logger.error("%s is damaged: which vault item each login uses is "
                     "unknown, and none is read until it is chosen again; "
                     "the damaged file is kept as %s.corrupt", PATH, PATH)
        _set_aside(raw)
        try:
            _write(unknown)
        except OSError:
            logger.error("logins: %s could not be replaced; it is found "
                         "damaged again at the next start", PATH,
                         exc_info=True)
        return unknown
    return {name: entry["item"] for name, entry in data.items()}


def _held() -> dict[str, str | None]:
    """The stored choices, read from the file once. The caller holds the
    lock."""
    global _choices
    if _choices is None:
        _choices = _load()
    return _choices


def _keep(choices: dict[str, str | None]) -> None:
    """Write *choices* and, once written, hold them. The caller holds the
    lock."""
    global _choices
    _write(choices)
    _choices = choices


def _unreadable(service: str) -> bool:
    """True while the choice for *service* is unknown: the file that held
    it was damaged and no item has been chosen for it since."""
    with _lock:
        held = _held()
        return service in held and held[service] is None


def _changed(service: str) -> None:
    for callback in list(_listeners.get(service, ())):
        try:
            callback()
        except Exception:  # noqa: BLE001 — the choice is already stored
            logger.warning("logins: a listener of %s failed", service,
                           exc_info=True)


def chosen(service: str) -> str | None:
    """The id of the item chosen for *service*, or None when none is, or
    when the choice is unknown (:func:`check` tells the two apart)."""
    _service(service)
    with _lock:
        return _held().get(service)


def choose(service: str, item_id: str) -> None:
    """Store *item_id* as the item *service* reads its login from, and tell
    the service's listeners (also when the same item is chosen again: what
    it holds may be new). This is also what ends an unknown choice, for this
    service only: another service whose choice is unknown stays so in the
    file written. Raises ``ValueError`` for anything that is not an
    ``op`` item id, ``OSError`` when the file cannot be written; nothing is
    changed then."""
    _service(service)
    if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
        raise ValueError("not a vault item id")
    with _lock:
        _keep({**_held(), service: item_id})
    _changed(service)


def forget(service: str) -> None:
    """Drop the choice for *service*, an unknown one included, which then
    reads the item under its default title again, and tell the service's
    listeners."""
    _service(service)
    with _lock:
        held = _held()
        if service not in held:
            return
        _keep({name: item_id for name, item_id in held.items()
               if name != service})
    _changed(service)


def on_change(service: str, callback: Callable[[], None]) -> None:
    """Call *callback* (in the caller's thread, with no argument) after every
    :func:`choose` and :func:`forget` of *service*. Registering the same
    callback again does nothing."""
    _service(service)
    callbacks = _listeners.setdefault(service, [])
    if callback not in callbacks:
        callbacks.append(callback)


def selection(service: str) -> str:
    """What ``op`` is asked for: the chosen item's id, else the default
    title. While the choice is unknown nothing may be asked for at all:
    :func:`read_from`, through which every value is read, refuses."""
    return chosen(service) or _service(service).default_title


# --- reading ------------------------------------------------------------------

def read_from(service: str, item: str, field: str) -> str | None:
    """The value *field* of *service* in the vault item *item* (an id or a
    title): the first accepted label that holds one. None when none does or
    the item cannot be read, and, without asking the vault, while the
    service's choice is unknown. Blocking."""
    if _unreadable(service):
        return None
    for label in _service(service).fields[field]:
        value = vault_read.read_field(item, label)
        if value:
            return value
    return None


def read(service: str, field: str) -> str | None:
    """One value of *service*'s login, from the chosen item or else the item
    under the default title; None when it cannot be read. Blocking."""
    if not vault_tools._default_vault():
        return None
    return read_from(service, selection(service), field)


def read_all(service: str) -> dict[str, str] | None:
    """Every value of *service*'s login, all from the one item resolved
    first, or None unless every one of them is there. Blocking."""
    if not vault_tools._default_vault():
        return None
    item = selection(service)
    values: dict[str, str] = {}
    for field in _service(service).fields:
        value = read_from(service, item, field)
        if not value:
            return None
        values[field] = value
    return values


# --- the check ----------------------------------------------------------------

@dataclass(frozen=True)
class Check:
    """What :func:`check` found. ``field`` is the value (a key of
    :attr:`Service.fields`) a field problem is about. ``label`` and ``found``
    are for the operator's sentence only: the vault label the problem is
    about, and ``(value, label)`` for each value held under a label other
    than the first accepted one."""

    ok: bool
    problem: Problem
    field: str
    item_is_default: bool
    service: str = ""
    label: str = ""
    found: tuple[tuple[str, str], ...] = ()

    def for_model(self) -> dict:
        """What a tool may return: the problem's code and Casa's own name
        for the value concerned. Never a vault field label."""
        if self.ok:
            return {"ok": True}
        said: dict = {"ok": False, "problem": self.problem}
        if self.field:
            said["what"] = _service(self.service).names[self.field]
        return said

    def sentence(self) -> str:
        """What is wrong, in one sentence for the OPERATOR (``""`` when
        nothing is). It may name a vault field: it goes into Casa's own
        lines and the log, never into a tool result."""
        if self.ok:
            return ""
        svc = _service(self.service)
        if self.problem == "choice_unreadable":
            return ("Casa can no longer read which vault item was chosen "
                    f"for {svc.label}; ask the assistant to connect it "
                    "again.")
        if self.problem == "no_vault":
            return ("No default vault is set, so Casa cannot read the vault "
                    f"item for {svc.label}.")
        if self.problem == "vault_unreadable":
            return f"The vault item for {svc.label} could not be read."
        if self.problem == "item_not_found":
            if self.item_is_default:
                return (f"The vault has no item titled '{svc.default_title}' "
                        f"for {svc.label}.")
            return (f"The vault item chosen for {svc.label} is no longer in "
                    "the vault.")
        said = (f"The vault item for {svc.label} has no "
                f"{_bare(svc.names[self.field])} in `{self.label}`")
        if self.problem == "field_empty":
            return f"{said}: the field is empty."
        return f"{said}."


def _asked(ask: Callable[..., dict], *args: str) -> dict:
    """*ask*'s answer, or an error when ``op`` could not be run at all."""
    try:
        return ask(*args)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("logins: op could not be run (%s)", type(exc).__name__)
        return {"error": "op_unrunnable"}


def _locate(doc: dict, labels: tuple[str, ...]) -> tuple[str, str]:
    """Where an item's document holds a value: ``("found", label)`` for the
    first accepted label with something in it; else ``("empty", label)`` for
    the first accepted label that is there with nothing in it; else
    ``("missing", <the first accepted label>)``. *label* is the accepted
    label, as :data:`SERVICES` writes it. A field's label is matched
    whatever its case, as ``op`` matches it when the value is read, so a
    field labelled ``Password`` is the ``password`` the read will find. The
    value itself is only looked at for emptiness."""
    filled: dict[str, bool] = {}
    for entry in doc.get("fields") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("label"),
                                                         str):
            continue
        value = entry.get("value")
        has_value = isinstance(value, str) and bool(value.strip())
        held = entry["label"].casefold()
        filled[held] = filled.get(held, False) or has_value
    for label in labels:
        if filled.get(label.casefold()):
            return "found", label
    for label in labels:
        if label.casefold() in filled:
            return "empty", label
    return "missing", labels[0]


def check(service: str, *, item: str | None = None) -> Check:
    """Whether *service*'s login can be read, and if not, what is wrong.
    *item* checks that item id instead of the stored choice or the default
    title, as it would be if it were chosen. Blocking.

    The item's document is read once and only its labels and the emptiness
    of its values are looked at. ``op`` exits the same way for an item that
    is not there and for a vault that cannot be reached, so after a failed
    read the vault is listed once: the item is ``item_not_found`` only when
    that listing works and lacks it.

    While the service's choice is unknown and no *item* is given, the answer
    is ``choice_unreadable`` and the vault is not asked."""
    svc = _service(service)
    if item is None and _unreadable(service):
        return Check(ok=False, problem="choice_unreadable", field="",
                     item_is_default=False, service=service)
    ref = chosen(service) if item is None else item
    default = ref is None

    def wrong(problem: Problem, field: str = "", label: str = "") -> Check:
        return Check(ok=False, problem=problem, field=field,
                     item_is_default=default, service=service, label=label)

    vault = vault_tools._default_vault()
    if not vault:
        return wrong("no_vault")
    got = _asked(vault_tools._op_item_get, ref or svc.default_title, vault)
    if "error" in got:
        if got["error"] != "op_failed":
            # No answer, or one that is not a document: not a missing item.
            return wrong("vault_unreadable")
        listed = _asked(vault_tools._op_item_list, "", vault)
        if "error" in listed:
            return wrong("vault_unreadable")
        key, wanted = ("title", svc.default_title) if default else ("id", ref)
        listed_there = any(isinstance(row, dict) and row.get(key) == wanted
                           for row in listed["rows"])
        return wrong("vault_unreadable" if listed_there else "item_not_found")
    elsewhere: list[tuple[str, str]] = []
    for field, labels in svc.fields.items():
        state, label = _locate(got["doc"], labels)
        if state != "found":
            return wrong("field_missing" if state == "missing"
                         else "field_empty", field, label)
        if label != labels[0]:
            elsewhere.append((field, label))
    return Check(ok=True, problem="", field="", item_is_default=default,
                 service=service, found=tuple(elsewhere))


# --- the candidates -----------------------------------------------------------

def _read_now(service: str, vault: str) -> str | None:
    """The id of the item *service*'s login reads now, or None when it
    resolves to no item: the chosen item's id; or, with nothing chosen, the
    id of the item ``op`` finds under the default title, asked once, the
    way a read finds it (two items under that title are no item, and so is
    none). None while the choice is unknown. Blocking."""
    if _unreadable(service):
        return None
    chosen_id = chosen(service)
    if chosen_id is not None:
        return chosen_id
    got = _asked(vault_tools._op_item_get, _service(service).default_title,
                 vault)
    item_id = got["doc"].get("id") if "error" not in got else None
    return item_id if isinstance(item_id, str) else None


def candidates(service: str, *, mark_in_use: bool = False) -> list[dict]:
    """The vault items that could hold *service*'s login: those whose title
    contains one of the service's search terms. Each is its ``id``, its
    ``title`` (one line, cut to :data:`TITLE_CHARS`), its ``category`` and
    ``has``: for each value the login needs, by Casa's own name for it,
    whether the item holds one under an accepted label. Never a label or a
    value. Empty when the vault cannot be listed. Blocking.

    At most :data:`MAX_CANDIDATES` items are tried in all, the ones whose
    document could not be read included, so a vault that lists its items
    and fails every read costs that many reads and no more.

    With *mark_in_use* each row also says, as ``in_use``, whether it is the
    item the login really reads now (:func:`_read_now`). No row is marked
    when the login resolves to no item, or to one that is not among the
    rows shown."""
    svc = _service(service)
    vault = vault_tools._default_vault()
    if not vault:
        return []
    read_now = _read_now(service, vault) if mark_in_use else None
    shown: list[dict] = []
    tried: set[str] = set()
    for query in svc.queries:
        if len(tried) >= MAX_CANDIDATES:
            break
        listed = _asked(vault_tools._op_item_list, query, vault)
        if "error" in listed:
            break
        for row in listed["rows"]:
            item_id = row.get("id")
            if (not isinstance(item_id, str) or item_id in tried
                    or not _ITEM_ID.fullmatch(item_id)):
                continue
            if len(tried) >= MAX_CANDIDATES:
                break
            tried.add(item_id)
            got = _asked(vault_tools._op_item_get, item_id, vault)
            if "error" in got:
                continue
            title, category = row.get("title"), row.get("category")
            entry = {
                "id": item_id,
                "title": " ".join(title.split())[:TITLE_CHARS]
                if isinstance(title, str) else "",
                "category": category if isinstance(category, str)
                and _CATEGORY.fullmatch(category) else None,
                "has": {svc.names[field]:
                        _locate(got["doc"], labels)[0] == "found"
                        for field, labels in svc.fields.items()},
            }
            if mark_in_use:
                entry["in_use"] = item_id == read_now
            shown.append(entry)
    return shown


# --- the operator's line about a choice -----------------------------------------

def chosen_line(service: str, title: str, found: Check) -> str:
    """Casa's line to the operator when the item titled *title* is taken for
    *service*, *found* being the check of that item: which item it is and
    its state. When a value is held under a label other than the first
    accepted one, the line says under which."""
    svc = _service(service)
    if not found.ok:
        said = found.sentence()
        return (f"{svc.set_to_use} the vault item '{title}', but "
                f"{said[:1].lower()}{said[1:]}")
    elsewhere = "".join(
        f", {_bare(svc.names[field])} from its `{label}` field"
        for field, label in found.found)
    return f"{svc.now_uses} the vault item '{title}'{elsewhere}."
