"""The vault drop-off: a sign-in link reaches a plugin's tool without a brief.

#1047. A sign-in link the resident reads from a mailbox, or the operator
pastes, must reach a specialist's sign-in tool, and the safety kernel forbids
carrying it in a delegation brief. The operator ruled the route: the agent
holding the link stores it in a vault drop-off the consuming plugin declared
(``casa.dropOffs``), tells the specialist only that it is waiting, and the
plugin redeems it from the vault and deletes it.

The write is narrow by construction. The plugin declares only a NAME; the
item's title is built here from the plugin's runtime name and that name, so
no declaration can aim the write at another item — a plugin's credential item
least of all. The item holds nothing but the dropped value, so it is replaced
whole: delete, then create from a JSON template on stdin, so the value never
appears in ``op``'s argv. Only an item Casa itself created, which carries
:data:`TAG`, is ever deleted, and no other item or field is read by value or
changed. The value is never returned, logged or put in an exception.

Leaf module: stdlib only; ``op`` is the one subprocess target.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading

logger = logging.getLogger(__name__)

MAX_VALUE_CHARS = 4096
# Every item Casa creates carries this tag, and Casa deletes only items that
# carry it: an item that merely shares the title — made by hand, or by
# anything else — is never touched, and its presence refuses the drop.
TAG = "casa-drop-off"
_OP_TIMEOUT_S = 30
# Two drops for the same item must not interleave their delete and create,
# or both creates land and the plugin meets two items with one title.
_LOCK = threading.Lock()


def item_title(plugin: str, drop_off: str) -> str:
    """The ONE place the drop-off item's title is spelled — the plugin
    computes the same string from its own manifest name and the declared
    drop-off name. No colon: an ``op://`` reference rejects one in the item
    segment."""
    return f"Casa drop-off {plugin} {drop_off}"


def valid_value(value: object) -> bool:
    """A non-empty string of at most :data:`MAX_VALUE_CHARS` characters with
    no control character — a link or a code, never a document."""
    return (isinstance(value, str) and 0 < len(value) <= MAX_VALUE_CHARS
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _run(cmd: list[str], stdin: str | None = None):
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                          timeout=_OP_TIMEOUT_S,
                          **({} if stdin is not None
                             else {"stdin": subprocess.DEVNULL}))


def store(plugin: str, drop_off: str, value: str) -> dict:
    """Replace the drop-off item for (*plugin*, *drop_off*) with one holding
    *value*. Returns ``{"status": "ok"}`` or a fixed classification —
    ``no_vault``, ``no_token``, ``drop_off_collision`` (an item with the
    title that Casa did not create — nothing is deleted or written),
    ``op_failed`` (with the exit code),
    ``op_timeout``, ``op_unreadable`` — never anything ``op`` printed.

    Blocking: call it off the event loop."""
    vault = os.environ.get("ONEPASSWORD_DEFAULT_VAULT", "")
    if not vault:
        return {"error": "no_vault"}
    if not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        return {"error": "no_token"}
    title = item_title(plugin, drop_off)
    template = json.dumps({
        "title": title, "category": "PASSWORD", "tags": [TAG],
        "fields": [{"id": "password", "type": "CONCEALED",
                    "purpose": "PASSWORD", "label": "password",
                    "value": value}]})
    with _LOCK:
        try:
            r = _run(["op", "item", "list", "--vault", vault,
                      "--format", "json"])
            if r.returncode != 0:
                return {"error": "op_failed", "exit_code": r.returncode}
            try:
                rows = json.loads(r.stdout)
                if not isinstance(rows, list):
                    raise ValueError
            except ValueError:
                return {"error": "op_unreadable"}
            same = [row for row in rows
                    if isinstance(row, dict) and row.get("title") == title]
            if any(TAG not in (row.get("tags") or []) for row in same):
                return {"error": "drop_off_collision"}
            stale = [row["id"] for row in same
                     if isinstance(row.get("id"), str)]
            for item_id in stale:
                r = _run(["op", "item", "delete", item_id, "--vault", vault])
                if r.returncode != 0:
                    return {"error": "op_failed", "exit_code": r.returncode}
            r = _run(["op", "item", "create", "--vault", vault,
                      "--format", "json", "-"], stdin=template)
            if r.returncode != 0:
                return {"error": "op_failed", "exit_code": r.returncode}
        except subprocess.TimeoutExpired:
            return {"error": "op_timeout"}
        except OSError as exc:   # no op binary, or it could not start
            logger.warning("vault drop-off: op did not start (%s)",
                           type(exc).__name__)
            return {"error": "op_failed", "exit_code": -1}
    return {"status": "ok"}
