"""A fake ``subprocess.run`` for the ``op`` front, holding invented items.

It answers the three command lines Casa runs to find and read a login, from
the items it holds, in the shapes the real CLI prints:

- ``op item list --format json --vault V``: every item's row (id, title,
  category);
- ``op item get REF --format json --vault V``: the item's document, where a
  field with an empty value has no ``value`` key;
- ``op item get REF --vault V --fields label=L --reveal``: that field's value.

``REF`` is an item's id or its title. A label is matched whatever its case,
as the real CLI matches it. A reference that matches no item, or more than
one, and a label the item does not have exit 1; so does every
command while :attr:`FakeOp.down` is set, which is how a vault that cannot be
reached looks: the same exit status as an item that is not there.

:func:`fresh_logins` gives a test its own, empty record of chosen items.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

import pytest

import logins
import vault_read

VAULT = "Home Vault"


def fresh_logins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the record of chosen items at a file of this test's own, with
    nothing chosen and nobody listening, and name the default vault."""
    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", VAULT)
    monkeypatch.setattr(logins, "PATH", str(tmp_path / "logins.json"))
    monkeypatch.setattr(logins, "_choices", None)
    monkeypatch.setattr(logins, "_listeners", {})


def install(monkeypatch: pytest.MonkeyPatch, *items: dict) -> "FakeOp":
    """A :class:`FakeOp` holding *items*, in place of ``subprocess.run``."""
    fake = FakeOp(*items)
    monkeypatch.setattr(vault_read.subprocess, "run", fake)
    return fake


def item(item_id: str, title: str, category: str = "LOGIN",
         **fields: str) -> dict:
    """An item: its fields are ``label=value`` (``label`` as a keyword, or
    any text through ``**{"a label": "value"}``)."""
    return {"id": item_id, "title": title, "category": category,
            "fields": dict(fields)}


def _done(code: int, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(["op"], code, out, err)


class FakeOp:
    def __init__(self, *items: dict, vault: str = VAULT) -> None:
        self.items = list(items)
        self.vault = vault
        #: Every command line run, in order.
        self.calls: list[list[str]] = []
        #: While True every command exits 1: the vault cannot be reached.
        self.down = False
        #: While True the listing exits 1 and the rest still answers.
        self.list_down = False
        #: While True every ``item get`` exits 1 and the listing still answers.
        self.get_down = False
        #: Called with each command line after it is recorded.
        self.on_call: Callable[[list[str]], None] | None = None

    # -- what a test changes ---------------------------------------------

    def get(self, item_id: str) -> dict:
        return next(i for i in self.items if i["id"] == item_id)

    def remove(self, item_id: str) -> None:
        self.items = [i for i in self.items if i["id"] != item_id]

    # -- what a test reads -----------------------------------------------

    def reads(self) -> list[tuple[str, str]]:
        """``(item reference, label)`` of every one-field read, in order."""
        return [(c[3], c[c.index("--fields") + 1].removeprefix("label="))
                for c in self.calls if "--fields" in c]

    def kinds(self) -> list[str]:
        """``list``, ``document`` or ``field`` for every command, in order."""
        return ["list" if c[2] == "list" else
                "field" if "--fields" in c else "document"
                for c in self.calls]

    # -- subprocess.run --------------------------------------------------

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        if self.on_call is not None:
            self.on_call(args)
        assert args[:2] == ["op", "item"], args
        assert kwargs.get("timeout"), "an op call without a timeout"
        assert args[args.index("--vault") + 1] == self.vault, args
        if self.down:
            return _done(1, "", "[ERROR] the vault did not answer")
        if args[2] == "list":
            if self.list_down:
                return _done(1, "", "[ERROR] the vault did not answer")
            return _done(0, json.dumps([
                {"id": i["id"], "title": i["title"], "version": 1,
                 "vault": {"id": self.vault, "name": self.vault},
                 "category": i["category"]} for i in self.items]))
        assert args[2] == "get", args
        if self.get_down:
            return _done(1, "", "[ERROR] the item could not be read")
        ref = args[3]
        found = [i for i in self.items if ref in (i["id"], i["title"])]
        if len(found) != 1:
            return _done(1, "", "[ERROR] no one item of that name")
        held = found[0]
        if "--fields" in args:
            assert "--reveal" in args and "--format" not in args, args
            label = args[args.index("--fields") + 1].removeprefix("label=")
            named = [value for held_label, value in held["fields"].items()
                     if held_label.casefold() == label.casefold()]
            if not named:
                return _done(1, "", "[ERROR] no such field")
            return _done(0, named[0] + "\n")
        assert args[args.index("--format") + 1] == "json", args
        fields = []
        for n, (label, value) in enumerate(held["fields"].items()):
            shown = {"id": f"f{n}", "type": "CONCEALED", "label": label}
            if value:
                shown["value"] = value
            shown["reference"] = f"op://{self.vault}/{held['title']}/{label}"
            fields.append(shown)
        return _done(0, json.dumps({
            "id": held["id"], "title": held["title"], "version": 1,
            "vault": {"id": self.vault, "name": self.vault},
            "category": held["category"], "fields": fields}))
