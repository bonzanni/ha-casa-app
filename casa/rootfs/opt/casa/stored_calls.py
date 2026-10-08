"""S5 §2 — the stored call: the one predicate deciding which of a plugin's
tools a proposal button may name, the argument grammar, and the canonical
form the pin compares byte for byte (INV-PROP-001/003).

Leaf module: stdlib only, plus the names it needs from ``plugin_grants``
(the contract shapes) and ``result_broker.is_reference`` at call time. It is
read by the deposit (``result_broker.deposit``), by the tap's re-check under
the desk lock (``specialist_desk.handle_tap``) and by the pin
(``pinned_run``), so the rule cannot drift between them.

Why the grammar is this narrow (design §2.6, measured on CLI 2.1.273): the
CLI decodes a tool call's arguments in JavaScript, so a JSON ``1.0`` becomes
``1`` and an integer beyond 2^53 is rounded; its MCP wrapper strips the
reserved ``__consentNonce`` key and, behind a flag, trailing XML-like tags
from string values. A stored object the CLI would change can reach the
plugin changed while the pin compared the unchanged one — so every value a
transformation could touch is refused at deposit, and the comparison never
normalises after posting.
"""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
BARE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
STRING_MAX_CHARS = 2000
ARGUMENTS_MAX_BYTES = 4096
SAFE_INT_MAX = 2 ** 53 - 1
OPERATOR_PROPOSAL = "operator_proposal"
OPERATOR_FILE = "operator_file"
# §14.7 trust + tell: the one Casa-composed line above a receipt whose
# reported arguments differed from the stored call
TELL_LINE = "⚠ the CLI reported this call's arguments changed by an installed hook"


def canonical_json(obj: Any) -> str:
    """The one serialisation the pin compares: sorted keys, no whitespace,
    unicode kept as is."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _value_ok(value: Any) -> str | None:
    """The reason a value is refused, or None (bool before int: a bool IS an int)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return None if -SAFE_INT_MAX <= value <= SAFE_INT_MAX else "unsafe_integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        from result_broker import is_reference
        if len(value) > STRING_MAX_CHARS:
            return "string_length"
        if "<" in value or ">" in value:
            return "angle_bracket"
        if is_reference(value):
            return "reference"
        return None
    if isinstance(value, list):
        for item in value:
            why = _value_ok(item)
            if why is not None:
                return why
        return None
    if isinstance(value, dict):
        return _object_ok(value)
    return "type"


def _object_ok(obj: dict) -> str | None:
    for key, value in obj.items():
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            return "key"
        why = _value_ok(value)
        if why is not None:
            return why
    return None


def arguments_ok(arguments: Any) -> str | None:
    """The argument grammar (§2.6): a JSON object whose values are strings,
    booleans, null, safe integers, lists or objects of the same — no floats,
    no `_`-prefixed or non-identifier keys, no angle brackets, no reference
    strings, no string over STRING_MAX_CHARS, at most ARGUMENTS_MAX_BYTES
    serialised, and a canonical form that survives a JSON round trip. The
    reason, or None."""
    if not isinstance(arguments, dict):
        return "not_object"
    why = _object_ok(arguments)
    if why is not None:
        return why
    canonical = canonical_json(arguments)
    if len(canonical.encode("utf-8")) > ARGUMENTS_MAX_BYTES:
        return "size"
    if json.loads(canonical) != arguments or canonical_json(json.loads(canonical)) != canonical:
        return "round_trip"
    return None


@dataclasses.dataclass(frozen=True)
class StoredCall:
    """The three identities of a stored call (§2.1): the ``.mcp.json`` server
    key the launch uses, the bare name the server dispatches on, the Casa
    runtime name every hook and policy compares — and whether the tool is the
    ``More`` exception (a capability whose only slot is its own proposal, or,
    #1303, one file it delivers)."""
    server: str
    wire_name: str
    runtime_name: str
    proposal: bool


def _runtime_name(seg: str, server: str, bare: str) -> str:
    from text_util import sanitize_segment
    return f"mcp__plugin_{seg}_{sanitize_segment(server)}__{sanitize_segment(bare)}"


def _entry_ok(runtime_name: str, entry: Any, *, contract_map: Any, protected: Any) -> str | None:
    """The class checks of §2 items 2–5 on a contract entry, in order."""
    if entry is None:
        return "undeclared"
    servers = tuple(getattr(entry, "servers", ()) or ())
    if len(servers) != 1:
        return "ambiguous_server"
    if getattr(entry, "transport", "") != "stdio":
        return "transport"
    plugin = contract_map.plugins.get(getattr(entry, "plugin_seg", ""))
    if plugin is not None and runtime_name in plugin.setup_tools:
        return "setup"
    if protected and runtime_name in protected:
        return "protected"
    if entry.consumes:
        return "consumes"
    if entry.kind == "safe":
        return None
    if entry.kind == "capability":
        provides = tuple(entry.provides or ())
        delivers = dict(entry.delivers or {})
        # the `More` exception, and (#1303) its file sibling: one slot, which
        # delivers the next proposal or one file
        if (len(provides) == 1
                and delivers in ({provides[0]: OPERATOR_PROPOSAL}, {provides[0]: OPERATOR_FILE})):
            return None
        return "capability"
    return "capability"


def delivers_file(entry: Any) -> bool:
    """#1362: the entry is #1303's file sibling — a ``capability`` whose one
    provided slot delivers ``operator_file`` (the only call a ``keep_card``
    button may store)."""
    if entry is None or getattr(entry, "kind", "") != "capability":
        return False
    provides = tuple(getattr(entry, "provides", ()) or ())
    return (len(provides) == 1
            and dict(getattr(entry, "delivers", None) or {}) == {provides[0]: OPERATOR_FILE})


def _is_proposal_exception(entry: Any) -> bool:
    return entry.kind == "capability"


def resolve_stored_call(bare_name: Any, *, contract_map: Any, protected: Any,
                        seg: str, server: str) -> tuple[StoredCall | None, str | None]:
    """At deposit: resolve the plugin's BARE tool name against the depositing
    call's own plugin segment and server — never against a string the plugin
    supplies beyond the name — and judge the entry. ``(call, None)`` or
    ``(None, reason)``."""
    if (not isinstance(bare_name, str) or not BARE_NAME_RE.fullmatch(bare_name)
            or "__" in bare_name):          # a runtime name, never a bare one
        return None, "bare_name"
    runtime_name = _runtime_name(seg, server, bare_name)
    entry = contract_map.tools.get(runtime_name)
    why = _entry_ok(runtime_name, entry, contract_map=contract_map, protected=protected)
    if why is not None:
        return None, why
    return StoredCall(server=server, wire_name=bare_name, runtime_name=runtime_name,
                      proposal=_is_proposal_exception(entry)), None


def stored_call_still_ok(runtime_name: str, *, contract_map: Any, protected: Any) -> str | None:
    """At tap time, under the desk lock, against the LIVE maps: the same
    entry checks by the recorded runtime name. The reason, or None."""
    entry = contract_map.tools.get(runtime_name)
    return _entry_ok(runtime_name, entry, contract_map=contract_map, protected=protected)
