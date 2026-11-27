"""The saved-logins page: what a login is, for the page engine in `vault_page`.

The engine knows requests, forms and layout; this module is everything it is told
about a login the app keeps itself: the words of the page, the four things a person
types (sign-in addresses, username, password, authenticator seed), which of them
are hidden, and what a save must hold for the broker to find, read and fill the
login afterwards. A login made or changed here has the shape a live-view save
gives it (`builtin.BuiltinVault.create_login`), so the broker reads both alike.

The page and the broker count one version: every save made here raises the
login's `version` by one, as the broker's own does, and a person neither sees
nor types that field.

`page` makes the page over the store the broker's vault holds open, never over a
copy opened for it: a second open copy would not see what the broker writes.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence

from casa_browser.broker.builtin import (
    _CATEGORY, _PASSWORD, _SEED, _STORED_TYPE, _USERNAME, _VERSION, _WEBSITES,
    BuiltinVault, Website, gives_a_code)
# The limits and the reading of an address are the broker's own: a login it would
# leave out of a listing, or whose address it reads no host from, signs in nowhere.
from casa_browser.broker.main import LIST_HOSTS, LIST_TITLE_CHARS, _hosts
from casa_browser.vault_page import WRITER, Box, Page, Profile, Slots
from casa_browser.vault_store import Field, Item

# The most bytes of a username or a password the broker saves from a sign-in a person
# typed in a live view (`main._attempt`). One typed here is held to the same.
MAX_VALUE_BYTES = 1024

# The values a person reads on the page as they are. Every other value is hidden,
# one stored under a label this page does not know included.
_PLAIN = frozenset({_WEBSITES, _USERNAME})

_HEADINGS = {_WEBSITES: "Sign-in addresses", _USERNAME: "Username", _PASSWORD: "Password",
             _SEED: "Authenticator seed"}

# What a person reads when a save is refused. No sentence repeats a value.
ADDRESS_MISSING = "Enter at least one sign-in address."
ADDRESS_WITHOUT_HOST = "Each sign-in address needs a host, like https://example.com."
TOO_MANY_HOSTS = f"Use sign-in addresses on at most {LIST_HOSTS} different hosts."
USERNAME_TOO_LONG = f"Use a username of at most {MAX_VALUE_BYTES:,} bytes."
PASSWORD_TOO_LONG = f"Use a password of at most {MAX_VALUE_BYTES:,} bytes."
NEEDS_ONE = "A login needs a username or a password."
NO_CODE = ("This authenticator seed gives no code. Enter the setup key or the otpauth:// "
           "address the site shows.")

# The page's sentences that name a login or the app, by the engine's keys.
WORDS = {
    "product": "Casa Browser",
    "open_through": "Open this page through Casa Browser's web interface.",
    "list": "Saved logins",
    "search": "Search logins",
    "no_match": "No matching logins",
    "none_yet": "No saved logins yet",
    "none_yet_help": "Add one here. A login you type in a live view is saved here too.",
    "add": "Add login",
    "edit": "Edit login",
    "delete": "Delete login",
    "back_to_list": "Back to saved logins",
    "back_to_app": "Back to Casa Browser",
    "unavailable": "Saved logins unavailable",
    "unavailable_help": "The saved logins could not be opened. See the app's log.",
    "use_show": "Use the Show button on the login's page.",
    "not_found": "There is no such login or page.",
    "name_help": "Shown when Casa Browser asks which login signs in to a site. "
                 "Cannot be renamed.",
    "name_taken": "A login named “{}” already exists. Choose another name.",
    "no_value_left": "Keep at least one value, or delete the login instead.",
    "too_many": "A login can hold at most {} values.",
    "not_saved": "Login not saved",
    "changed": "This login changed while you were editing. Reload it before saving again.",
    "reload": "Reload login",
    # The store counts the values handed out through this page; a sign-in is not counted.
    "read_once": "Shown or copied once since Casa Browser started",
    "read_many": "Shown or copied {} times since Casa Browser started",
    "delete_effect": "",
    "count_one": "{} login",
    "count_many": "{} logins",
}

# The boxes of the add form, in the order a person fills them. The edit form shows a
# stored one as its card and offers the ones the login lacks.
_BOXES = (
    Box(_WEBSITES, "URL", "Sign-in addresses, one per line", multiline=True,
        required=ADDRESS_MISSING),
    Box(_USERNAME, _STORED_TYPE[_USERNAME], "Username"),
    Box(_PASSWORD, _STORED_TYPE[_PASSWORD], "Password"),
    Box(_SEED, "OTP", "Authenticator seed (optional)"),
)


def _addresses(value: str) -> list[str]:
    """The addresses in a `websites` value: its lines, without the spaces around each and
    without the empty ones."""
    return [line.strip() for line in value.splitlines() if line.strip()]


def _address_hosts(addresses: Sequence[str]) -> list[str]:
    """The different hosts the broker reads from `addresses`."""
    return _hosts([Website(address) for address in addresses])


def _summary(item: Item) -> str:
    """A list row's second line: where the login signs in, by its first address."""
    hosts = _address_hosts(_addresses(_value(item.fields, _WEBSITES)))
    return hosts[0] if hosts else "No sign-in address"


def _origin(item: Item) -> str:
    return "Typed here" if item.writer == WRITER else "Saved from a live view"


def _heading(item: Item, field: Field, alone: bool) -> str:
    return _HEADINGS.get(field.label, field.label)


def _is_hidden(field: Field) -> bool:
    return field.label not in _PLAIN


def _value(fields: Sequence[Field], label: str) -> str:
    return next((field.value for field in fields if field.label == label), "")


def _stored(item: Item | None, fields: list[Field]) -> list[Field]:
    """The fields a save writes: the person's, with the addresses one per line and the version.

    A new login gets the fields in the order a live-view save writes them, a
    username even when none was typed (that save stores an empty one), and
    version 1. A changed login keeps its fields where they are and gets the next
    version; one whose username was removed gets the empty one back, first.
    """
    fields = [dataclasses.replace(field, value="\n".join(_addresses(field.value)))
              if field.label == _WEBSITES else field for field in fields]
    typed = {field.label: field for field in fields}
    username = typed.get(_USERNAME) or Field(id="", label=_USERNAME,
                                             type=_STORED_TYPE[_USERNAME], value="")
    if item is not None:
        return [*([] if _USERNAME in typed else [username]),
                *(dataclasses.replace(field, value=str(int(field.value) + 1))
                  if field.label == _VERSION else field for field in fields)]
    return [username,
            *(typed[label] for label in (_PASSWORD, _WEBSITES, _SEED) if label in typed),
            Field(id="", label=_VERSION, type="STRING", value="1")]


def _check(name: str, fields: Sequence[Field]) -> str | None:
    """The sentence that refuses a login the broker could not use as it would be stored."""
    addresses = _addresses(_value(fields, _WEBSITES))
    if not addresses:
        return ADDRESS_MISSING
    if not all(_address_hosts([address]) for address in addresses):
        return ADDRESS_WITHOUT_HOST
    if len(_address_hosts(addresses)) > LIST_HOSTS:
        return TOO_MANY_HOSTS
    username, password = _value(fields, _USERNAME), _value(fields, _PASSWORD)
    if len(username.encode("utf-8")) > MAX_VALUE_BYTES:
        return USERNAME_TOO_LONG
    if len(password.encode("utf-8")) > MAX_VALUE_BYTES:
        return PASSWORD_TOO_LONG
    if not username and not password:
        return NEEDS_ONE
    seed = _value(fields, _SEED)
    if seed and not gives_a_code(seed):
        return NO_CODE
    return None


def profile(changed: Callable[[str], None], deleted: Callable[[str], None]) -> Profile:
    """The login profile. `changed` and `deleted` get a login's id once the store has
    written the login or no longer holds it."""
    return Profile(
        root="logins",
        words=WORDS,
        back=lambda prefix: f"{prefix}/setup",  # the app's Setup page
        summary=_summary,
        origin=_origin,
        delete_warning=lambda item: "Sites that use this login will ask you to sign in again.",
        heading=_heading,
        is_hidden=_is_hidden,
        kept=frozenset({_VERSION}),
        facts=lambda item: None,
        editable=lambda item: True,
        references=lambda item: False,
        slots=Slots(boxes=_BOXES),
        category=_CATEGORY,
        name_limit=LIST_TITLE_CHARS,
        check=_check,
        stored=_stored,
        after_save=lambda item: changed(item.id),
        after_delete=lambda item: deleted(item.id),
    )


def page(vault: BuiltinVault, changed: Callable[[str], None],
         deleted: Callable[[str], None]) -> Page:
    """The saved-logins page over the store `vault` holds open, or the page that says the
    logins are unavailable when it holds none."""
    return Page(vault.store, profile(changed, deleted))
