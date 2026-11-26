"""Casa's secrets page: what a secret is, for the page engine in `vault_page`.

The engine knows requests, forms and layout; this module is everything it is
told about Casa: the words of the page, who a secret belongs to (an agent's
bot token, a plugin's drop-off, an SSH key, the person), which values are
hidden, and what the add and edit forms offer. `casa_page` is the page the
store's service serves.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable

from vault_page import WRITER, Facts, Page, Profile, Slots
from vault_store import Field, Item, Store

# The tag and the title prefix Casa gives the item holding an agent's
# Telegram bot token: `own_voice_lifecycle.TOKEN_TAG` and
# `own_voice_lifecycle.item_title`. Copied rather than imported, so the page
# stays free of Casa's other modules; a test holds them equal.
BOT_TOKEN_TAG = "casa-agent-bot"
BOT_TOKEN_TITLE = "Telegram "
# The tag and the title prefix of a secret a person hands to a plugin:
# `vault_drop_off.TAG` and `vault_drop_off.item_title`.
DROP_OFF_TAG = "casa-drop-off"
DROP_OFF_TITLE = "Casa drop-off "

_DROP_OFF = re.compile(re.escape(DROP_OFF_TITLE) + r"(\S+) .+", re.DOTALL)

_SSH_KEY = "SSH_KEY"
_ADDED_CATEGORY = "API_CREDENTIAL"
# The labels the add form's slots start with, and the ones offered on every slot.
_ADD_LABELS = ("credential", "password", "certificate")
_SUGGESTIONS = ("username", "password", "certificate")
# The plain fields of an SSH key shown as facts about the key rather than as values.
_SSH_FACTS = {"key type": "Type", "fingerprint": "Fingerprint"}

# The page's sentences that name a secret, Casa or a plugin, by the engine's keys.
WORDS = {
    "product": "Casa",
    "open_through": "Open this page through Casa's web interface.",
    "list": "Secrets",
    "search": "Search secrets",
    "no_match": "No matching secrets",
    "none_yet": "No secrets yet",
    "none_yet_help": "Add a key, token or password for Casa to use.",
    "add": "Add secret",
    "edit": "Edit secret",
    "delete": "Delete secret",
    "back_to_list": "Back to secrets",
    "back_to_app": "Back to Casa",
    "unavailable": "Secrets unavailable",
    "unavailable_help": "The secret store could not be opened. Try again in a moment.",
    "use_show": "Use the Show button on the secret's page.",
    "not_found": "There is no such secret or page.",
    "name_help": "Used by Casa and plugins. Cannot be renamed.",
    "name_taken": "A secret named “{}” already exists. Choose another name.",
    "no_value_left": "Keep at least one value, or delete the secret instead.",
    "too_many": "A secret can hold at most {} values.",
    "not_saved": "Secret not saved",
    "changed": "This secret changed while you were editing. Reload it before saving again.",
    "reload": "Reload secret",
    "read_once": "Read once since Casa started",
    "read_many": "Read {} times since Casa started",
    "delete_effect": "Deleting it may stop the features that use it.",
}


def purpose(item: Item) -> str:
    """Who keeps a secret and what for, in a few words a person recognises."""
    if BOT_TOKEN_TAG in item.tags:
        if item.title.startswith(BOT_TOKEN_TITLE) and len(item.title) > len(BOT_TOKEN_TITLE):
            role = item.title[len(BOT_TOKEN_TITLE):]
            # The title capitalises the role's first letter; roles are lower case.
            return f"Casa · Bot token for {role[:1].lower()}{role[1:]}"
        return "Casa · Bot token"
    handed_to = _plugin(item)
    if handed_to:
        return f"Plugin · {handed_to}"
    if item.category == _SSH_KEY:
        return "Casa · SSH key"
    if item.writer == WRITER:
        return "Added by you"
    return "Added by Casa"


def is_hidden(field: Field) -> bool:
    """Whether the field's value is kept off the page until asked for.

    Only a plain-text field (type `STRING`) is shown as it is; every other
    type, including ones the page has never heard of, is treated as secret.
    """
    return field.type != "STRING"


def _summary(item: Item) -> str:
    """A list row's second line: its values' labels when it has several, else its purpose."""
    if item.category != _SSH_KEY and len(item.fields) > 1:
        labels = ", ".join(field.label for field in item.fields)
        return f"{len(item.fields)} values · {labels}"
    return purpose(item)


def _plugin(item: Item) -> str | None:
    """The plugin a secret was handed to, for a drop-off."""
    if DROP_OFF_TAG in item.tags:
        match = _DROP_OFF.fullmatch(item.title)
        if match:
            return match.group(1)
    return None


def _origin(item: Item) -> str:
    if item.writer == WRITER:
        return "Added by you"
    return f"Added by {_plugin(item) or 'Casa'}"


def _delete_warning(item: Item) -> str:
    """Who would miss the secret, or "" for one a person added on the page."""
    if _plugin(item):
        return "A plugin uses this secret."
    if item.writer != WRITER:
        return "Casa uses this secret."
    return ""


def _is_fact(field: Field) -> bool:
    return not is_hidden(field) and field.label.lower() in _SSH_FACTS


def _facts(item: Item) -> Facts | None:
    """An SSH key's type and fingerprint, shown as facts about the key rather than as values."""
    if item.category != _SSH_KEY:
        return None
    return Facts(
        heading="SSH key",
        rows=tuple((name, field.value) for label, name in _SSH_FACTS.items()
                   for field in item.fields if _is_fact(field) and field.label.lower() == label),
        note="Created by Casa. This key can be deleted, but not edited.",
        labels=frozenset(field.label for field in item.fields if _is_fact(field)))


def _heading(item: Item, field: Field) -> str:
    """A value is called by its label; an SSH key's parts with a capital."""
    if item.category == _SSH_KEY:
        return field.label[:1].upper() + field.label[1:]
    return field.label


def _not_an_ssh_key(item: Item) -> bool:
    """An SSH key is made by Casa in one piece: it has no edit form and no references."""
    return item.category != _SSH_KEY


def _nothing(item: Item) -> None:
    """Nobody is told of a save or a delete: every reader asks the store itself."""


PROFILE = Profile(
    root="vault",
    words=WORDS,
    back=lambda prefix: f"{prefix}/",  # Casa's own status page
    summary=_summary,
    origin=_origin,
    delete_warning=_delete_warning,
    heading=_heading,
    is_hidden=is_hidden,
    kept=frozenset(),
    facts=_facts,
    editable=_not_an_ssh_key,
    references=_not_an_ssh_key,
    slots=Slots(defaults=_ADD_LABELS, suggestions=_SUGGESTIONS),
    category=_ADDED_CATEGORY,
    name_limit=None,
    check=lambda name, fields: None,
    stored=lambda item, fields: fields,
    after_save=_nothing,
    after_delete=_nothing,
)


def casa_page(store: Store | None, clock: Callable[[], float] = time.monotonic) -> Page:
    """Casa's secrets page over `store`, or the page that says the store is unavailable."""
    return Page(store, PROFILE, clock)
