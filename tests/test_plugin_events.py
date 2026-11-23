"""casa.emits / casa.subscribes manifest parse + declaration-digest /
ack-identity consent primitives (plugin-events facility)."""
from __future__ import annotations

import pytest

from plugin_events import (
    MAX_EFFECTIVE_LEN,
    MAX_EMITS,
    MAX_SUBSCRIBES,
    ack_identity,
    effective_name,
    parse_and_validate_emits,
    parse_and_validate_subscribes,
    subscribe_declaration_digest,
)

pytestmark = pytest.mark.unit


def _emits(entries):
    return {"casa": {"emits": entries}}


def _subs(entries):
    return {"casa": {"subscribes": entries}}


# ---------------------------------------------------------------------------
# constants / effective_name
# ---------------------------------------------------------------------------


def test_constants():
    assert MAX_EMITS == 4
    assert MAX_SUBSCRIBES == 4
    assert MAX_EFFECTIVE_LEN == 128


def test_effective_name_helper():
    assert effective_name("finance", "invoice-created") == "plg-finance--invoice-created"


# ---------------------------------------------------------------------------
# parse_and_validate_emits — happy path
# ---------------------------------------------------------------------------


def test_valid_single_emit():
    entries, errs = parse_and_validate_emits("finance", _emits([{"name": "invoice-created"}]))
    assert errs == []
    assert entries == [{"declared": "invoice-created",
                        "effective": "plg-finance--invoice-created"}]


def test_valid_multiple_emits():
    entries, errs = parse_and_validate_emits(
        "finance", _emits([{"name": "a"}, {"name": "b"}]))
    assert errs == []
    assert len(entries) == 2


# --- absent / malformed casa -------------------------------------------

def test_absent_emits_is_empty_not_error():
    assert parse_and_validate_emits("p", {"casa": {}}) == ([], [])
    assert parse_and_validate_emits("p", {}) == ([], [])
    assert parse_and_validate_emits("p", {"casa": "nonsense"}) == ([], [])
    assert parse_and_validate_emits("p", {"casa": {"emits": None}}) == ([], [])


def test_non_list_emits_rejected():
    _, errs = parse_and_validate_emits("p", {"casa": {"emits": {"name": "x"}}})
    assert errs


def test_non_dict_emit_entry_rejected():
    _, errs = parse_and_validate_emits("p", _emits(["not-a-dict"]))
    assert errs


# --- entry shape: exactly {"name"} --------------------------------------

def test_emit_unknown_key_rejected():
    _, errs = parse_and_validate_emits("p", _emits([{"name": "x", "bogus": 1}]))
    assert any("bogus" in e or "unknown" in e.lower() for e in errs)


def test_emit_missing_name_key_rejected():
    _, errs = parse_and_validate_emits("p", _emits([{}]))
    assert errs


# --- naming ---------------------------------------------------------------

def test_emit_bad_name_charset_rejected():
    _, errs = parse_and_validate_emits("p", _emits([{"name": "has space"}]))
    assert errs


def test_emit_double_dash_in_declared_rejected():
    _, errs = parse_and_validate_emits("p", _emits([{"name": "a--b"}]))
    assert any("--" in e for e in errs)


def test_emit_double_dash_in_plugin_name_rejected():
    _, errs = parse_and_validate_emits("a--b", _emits([{"name": "x"}]))
    assert errs


def test_emit_plg_prefixed_declared_rejected():
    _, errs = parse_and_validate_emits("p", _emits([{"name": "plg-x"}]))
    assert errs


def test_emit_duplicate_declared_names_rejected():
    _, errs = parse_and_validate_emits("p", _emits([{"name": "d"}, {"name": "d"}]))
    assert any("duplicate" in e.lower() for e in errs)


def test_emit_plugin_name_trailing_dash_rejected():
    _, errs = parse_and_validate_emits("a-", _emits([{"name": "x"}]))
    assert any("end with '-'" in e for e in errs)


def test_emit_declared_name_leading_dash_rejected():
    _, errs = parse_and_validate_emits("a", _emits([{"name": "-x"}]))
    assert any("start with '-'" in e for e in errs)


def test_emit_declared_name_trailing_dash_rejected():
    """Critical-3 pin: a declared event name ending in '-' would misparse
    the emission filename split (`<event>--<u32hex>.json`'s FIRST '--' no
    longer lands where the caller expects), so the emission reads as
    unfoldable and sweep deletes it outright."""
    _, errs = parse_and_validate_emits("a", _emits([{"name": "x-"}]))
    assert any("end with '-'" in e for e in errs)


# --- counts -----------------------------------------------------------

def test_too_many_emits_rejected():
    many = [{"name": f"e{i}"} for i in range(5)]
    _, errs = parse_and_validate_emits("p", _emits(many))
    assert any("4" in e or "too many" in e.lower() for e in errs)


# --- effective-length boundary (128) ---------------------------------------

_LONG_PLUGIN = "casa-specialist-finance.enable-banking"


def test_emit_effective_name_length_ok_at_128():
    prefix_len = len(f"plg-{_LONG_PLUGIN}--")
    declared = "x" * (128 - prefix_len)
    entries, errs = parse_and_validate_emits(_LONG_PLUGIN, _emits([{"name": declared}]))
    assert errs == []
    assert len(entries[0]["effective"]) == 128


def test_emit_effective_name_length_rejected_at_129():
    prefix_len = len(f"plg-{_LONG_PLUGIN}--")
    declared = "x" * (129 - prefix_len)
    _, errs = parse_and_validate_emits(_LONG_PLUGIN, _emits([{"name": declared}]))
    assert any("128" in e or "long" in e.lower() for e in errs)


# --- all-or-nothing ----------------------------------------------------

def test_emit_all_or_nothing():
    entries, errs = parse_and_validate_emits(
        "p", _emits([{"name": "ok"}, {"name": "bad name"}]))
    assert errs
    assert entries == []


# ---------------------------------------------------------------------------
# parse_and_validate_subscribes — happy path
# ---------------------------------------------------------------------------


def test_valid_single_subscribe():
    entries, errs = parse_and_validate_subscribes(
        "reporting", _subs([{"plugin": "finance", "event": "invoice-created"}]))
    assert errs == []
    assert len(entries) == 1
    assert entries[0]["plugin"] == "finance"
    assert entries[0]["event"] == "invoice-created"
    assert len(entries[0]["digest"]) == 64


def test_scoped_emitter_accepted():
    """Round-1 P0 pin: a scoped registry name (owned-plugin form) must be
    accepted as an emitter reference."""
    entries, errs = parse_and_validate_subscribes(
        "reporting", _subs([{"plugin": "finance.bank-feed", "event": "tx-posted"}]))
    assert errs == []
    assert entries[0]["plugin"] == "finance.bank-feed"


def test_valid_multiple_subscribes():
    entries, errs = parse_and_validate_subscribes(
        "reporting", _subs([{"plugin": "finance", "event": "a"},
                            {"plugin": "finance", "event": "b"}]))
    assert errs == []
    assert len(entries) == 2


# --- absent / malformed casa -------------------------------------------

def test_absent_subscribes_is_empty_not_error():
    assert parse_and_validate_subscribes("p", {"casa": {}}) == ([], [])
    assert parse_and_validate_subscribes("p", {}) == ([], [])
    assert parse_and_validate_subscribes("p", {"casa": "nonsense"}) == ([], [])
    assert parse_and_validate_subscribes("p", {"casa": {"subscribes": None}}) == ([], [])


def test_non_list_subscribes_rejected():
    _, errs = parse_and_validate_subscribes(
        "p", {"casa": {"subscribes": {"plugin": "x", "event": "y"}}})
    assert errs


def test_non_dict_subscribe_entry_rejected():
    _, errs = parse_and_validate_subscribes("p", _subs(["not-a-dict"]))
    assert errs


# --- entry shape: exactly {"plugin", "event"} ---------------------------

def test_subscribe_unknown_key_rejected():
    _, errs = parse_and_validate_subscribes(
        "p", _subs([{"plugin": "finance", "event": "x", "bogus": 1}]))
    assert any("bogus" in e or "unknown" in e.lower() for e in errs)


def test_subscribe_missing_plugin_key_rejected():
    _, errs = parse_and_validate_subscribes("p", _subs([{"event": "x"}]))
    assert errs


def test_subscribe_missing_event_key_rejected():
    _, errs = parse_and_validate_subscribes("p", _subs([{"plugin": "finance"}]))
    assert errs


# --- registry-name grammar for the emitter ref --------------------------

def test_subscribe_bad_emitter_charset_rejected():
    _, errs = parse_and_validate_subscribes(
        "p", _subs([{"plugin": "Finance!", "event": "x"}]))
    assert errs


def test_subscribe_emitter_uppercase_rejected():
    # plugin_registry.NAME_RE is lowercase-only, unlike the event charset.
    _, errs = parse_and_validate_subscribes(
        "p", _subs([{"plugin": "Finance", "event": "x"}]))
    assert errs


def test_subscribe_scoped_emitter_malformed_rejected():
    _, errs = parse_and_validate_subscribes(
        "p", _subs([{"plugin": "finance.", "event": "x"}]))
    assert errs


# --- self-subscription ---------------------------------------------------

def test_self_subscription_refused():
    _, errs = parse_and_validate_subscribes(
        "finance", _subs([{"plugin": "finance", "event": "invoice-created"}]))
    assert any("own" in e.lower() for e in errs)


def test_self_subscription_scoped_refused():
    _, errs = parse_and_validate_subscribes(
        "finance.bank-feed",
        _subs([{"plugin": "finance.bank-feed", "event": "tx-posted"}]))
    assert any("own" in e.lower() for e in errs)


# --- duplicates ------------------------------------------------------------

def test_duplicate_plugin_event_pair_refused():
    _, errs = parse_and_validate_subscribes(
        "reporting", _subs([{"plugin": "finance", "event": "a"},
                            {"plugin": "finance", "event": "a"}]))
    assert any("duplicate" in e.lower() for e in errs)


def test_same_event_different_emitter_not_duplicate():
    entries, errs = parse_and_validate_subscribes(
        "reporting", _subs([{"plugin": "finance", "event": "a"},
                            {"plugin": "hr", "event": "a"}]))
    assert errs == []
    assert len(entries) == 2


# --- event naming ----------------------------------------------------------

def test_subscribe_bad_event_charset_rejected():
    _, errs = parse_and_validate_subscribes(
        "p", _subs([{"plugin": "finance", "event": "has space"}]))
    assert errs


def test_subscribe_double_dash_in_event_rejected():
    _, errs = parse_and_validate_subscribes(
        "p", _subs([{"plugin": "finance", "event": "a--b"}]))
    assert any("--" in e for e in errs)


def test_subscribe_event_leading_dash_rejected():
    _, errs = parse_and_validate_subscribes(
        "p", _subs([{"plugin": "finance", "event": "-x"}]))
    assert any("start with '-'" in e for e in errs)


def test_subscribe_event_trailing_dash_rejected():
    """Critical-3 pin — subscribe side (mirrors the emit-side rail above)."""
    _, errs = parse_and_validate_subscribes(
        "p", _subs([{"plugin": "finance", "event": "x-"}]))
    assert any("end with '-'" in e for e in errs)


def test_subscribe_event_plg_prefixed_rejected():
    _, errs = parse_and_validate_subscribes(
        "p", _subs([{"plugin": "finance", "event": "plg-x"}]))
    assert errs


# --- counts -----------------------------------------------------------

def test_too_many_subscribes_rejected():
    many = [{"plugin": "finance", "event": f"e{i}"} for i in range(5)]
    _, errs = parse_and_validate_subscribes("reporting", _subs(many))
    assert any("4" in e or "too many" in e.lower() for e in errs)


# --- all-or-nothing ----------------------------------------------------

def test_subscribe_all_or_nothing():
    entries, errs = parse_and_validate_subscribes(
        "reporting", _subs([{"plugin": "finance", "event": "ok"},
                            {"plugin": "finance", "event": "bad event"}]))
    assert errs
    assert entries == []


# ---------------------------------------------------------------------------
# subscribe_declaration_digest
# ---------------------------------------------------------------------------


def test_subscribe_declaration_digest_deterministic():
    entry = {"plugin": "finance", "event": "a", "digest": "irrelevant"}
    d1 = subscribe_declaration_digest(entry)
    d2 = subscribe_declaration_digest(dict(entry))
    assert d1 == d2
    assert len(d1) == 64


def test_subscribe_declaration_digest_changes_with_event():
    d1 = subscribe_declaration_digest({"plugin": "finance", "event": "a"})
    d2 = subscribe_declaration_digest({"plugin": "finance", "event": "b"})
    assert d1 != d2


def test_subscribe_declaration_digest_changes_with_plugin():
    d1 = subscribe_declaration_digest({"plugin": "finance", "event": "a"})
    d2 = subscribe_declaration_digest({"plugin": "hr", "event": "a"})
    assert d1 != d2


def test_subscribe_declaration_digest_golden():
    import hashlib
    import json
    expected = hashlib.sha256(
        json.dumps({"event": "a", "plugin": "finance"}, sort_keys=True,
                   separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert subscribe_declaration_digest({"plugin": "finance", "event": "a"}) == expected


# ---------------------------------------------------------------------------
# ack_identity — round-2 consent pins
# ---------------------------------------------------------------------------


def _identity(**over):
    kw = dict(subscriber="reporting", subscriber_artifact_id="art-1",
              emitter="finance", event="invoice-created",
              digest="d" * 64, targets=["resident:assistant"])
    kw.update(over)
    return ack_identity(**kw)


def test_identity_is_stable_for_equal_inputs():
    assert _identity() == _identity()


def test_identity_is_a_hex_digest():
    ident = _identity()
    assert len(ident) == 64
    int(ident, 16)  # parses as hex


def test_identity_ignores_targets_order():
    assert _identity(targets=["a", "b"]) == _identity(targets=["b", "a"])


@pytest.mark.parametrize("over", [
    {"subscriber": "other"},
    {"subscriber_artifact_id": "art-2"},
    {"emitter": "hr"},
    {"event": "other-event"},
    {"digest": "e" * 64},
    {"targets": ["resident:butler"]},
    {"targets": []},
    {"targets": ["resident:assistant", "resident:butler"]},
])
def test_identity_changes_when_any_component_changes(over):
    assert _identity(**over) != _identity()
