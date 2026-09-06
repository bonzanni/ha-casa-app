"""casa.triggers manifest parse + intrinsic validation (Release B, Task 1)."""
from __future__ import annotations

import pytest

import plugin_triggers
from plugin_triggers import effective_name, parse_and_validate

pytestmark = pytest.mark.unit


def _m(triggers):
    return {"casa": {"triggers": triggers}}


def _wh(**over):
    t = {"name": "voicemail", "type": "webhook", "target": "resident:assistant",
         "auth": {"mode": "static_header"}}
    t.update(over)
    return t


# --- happy path ------------------------------------------------------------

def test_valid_static_header_trigger():
    trig, errs = parse_and_validate("elevenlabs", _m([_wh(
        auth={"mode": "static_header", "header": "X-API-Key"})]))
    assert errs == []
    assert trig[0]["effective"] == "plg-elevenlabs--voicemail"
    assert trig[0]["target"] == "resident:assistant"
    assert trig[0]["auth"]["mode"] == "static_header"
    assert trig[0]["auth"]["header"] == "X-API-Key"
    assert trig[0]["auth"]["secret_owner"] == "casa"
    assert trig[0]["clearance"] == "public"


def test_timestamped_hmac_casa_defaults():
    trig, errs = parse_and_validate("p", _m([_wh(auth={"mode": "timestamped_hmac"})]))
    assert errs == []
    assert len(trig) == 1
    assert trig[0]["auth"]["header"] == "X-Webhook-Signature"
    assert trig[0]["auth"]["tolerance_secs"] == 300
    # No entry in the table names a vendor (#655), and it is the same table the
    # resident loader carries.
    assert plugin_triggers._DEFAULT_HEADER == {
        "hmac_body": "X-Webhook-Signature",
        "static_header": "X-API-Key",
        "timestamped_hmac": "X-Webhook-Signature",
    }


def test_timestamped_hmac_bad_header_recovers_the_neutral_default():
    """An invalid header token is an error AND falls back to the default, so
    the recovery value must be neutral too (#655)."""
    trig, errs = parse_and_validate(
        "p", _m([_wh(auth={"mode": "timestamped_hmac", "header": "bad header!"})]))
    assert len(errs) == 1
    assert "auth.header" in errs[0]
    assert len(trig) == 1
    assert trig[0]["auth"]["header"] == "X-Webhook-Signature"


def test_hmac_body_and_static_header_defaults_are_unchanged():
    trig, errs = parse_and_validate("p", _m([_wh(auth={"mode": "hmac_body"})]))
    assert errs == []
    assert trig[0]["auth"]["header"] == "X-Webhook-Signature"
    trig, errs = parse_and_validate("p", _m([_wh(auth={"mode": "static_header"})]))
    assert errs == []
    assert trig[0]["auth"]["header"] == "X-API-Key"


def test_an_explicit_timestamped_hmac_header_is_preserved():
    trig, errs = parse_and_validate(
        "p", _m([_wh(auth={"mode": "timestamped_hmac",
                           "header": "ElevenLabs-Signature"})]))
    assert errs == []
    assert trig[0]["auth"]["header"] == "ElevenLabs-Signature"


def test_effective_name_helper():
    assert effective_name("el", "vm") == "plg-el--vm"


# --- absent / malformed casa ----------------------------------------------

def test_absent_triggers_is_empty_not_error():
    assert parse_and_validate("p", {"casa": {}}) == ([], [])
    assert parse_and_validate("p", {}) == ([], [])
    assert parse_and_validate("p", {"casa": "nonsense"}) == ([], [])


def test_non_list_triggers_rejected():
    _, errs = parse_and_validate("p", {"casa": {"triggers": {"name": "x"}}})
    assert errs


def test_non_dict_entry_rejected():
    _, errs = parse_and_validate("p", _m(["not-a-dict"]))
    assert errs


def test_non_dict_auth_rejected():
    _, errs = parse_and_validate("p", _m([_wh(auth="nope")]))
    assert errs


def test_unknown_top_level_key_rejected():
    _, errs = parse_and_validate("p", _m([_wh(bogus=1)]))
    assert any("bogus" in e or "unknown" in e.lower() for e in errs)


# --- target ----------------------------------------------------------------

def test_non_resident_target_rejected():
    _, errs = parse_and_validate("p", _m([_wh(target="specialist:finance")]))
    assert any("resident:" in e for e in errs)


def test_missing_target_rejected():
    t = _wh()
    del t["target"]
    _, errs = parse_and_validate("p", _m([t]))
    assert errs


# --- auth mode / owner -----------------------------------------------------

def test_provider_secret_owner_rejected_this_release():
    _, errs = parse_and_validate("p", _m([_wh(
        auth={"mode": "timestamped_hmac", "secret_owner": "provider"})]))
    assert any("provider" in e for e in errs)


def test_unknown_mode_rejected():
    _, errs = parse_and_validate("p", _m([_wh(auth={"mode": "nonsense"})]))
    assert errs


def test_bad_header_token_rejected():
    _, errs = parse_and_validate("p", _m([_wh(
        auth={"mode": "static_header", "header": "bad header!"})]))
    assert any("header" in e.lower() for e in errs)


def test_tolerance_bool_or_out_of_range_rejected():
    for bad in (True, 10, 5000, "300"):
        _, errs = parse_and_validate("p", _m([_wh(
            auth={"mode": "timestamped_hmac", "tolerance_secs": bad})]))
        assert errs, f"tolerance {bad!r} should be rejected"


# --- naming ----------------------------------------------------------------

def test_double_dash_in_declared_rejected():
    _, errs = parse_and_validate("p", _m([_wh(name="a--b")]))
    assert any("--" in e for e in errs)


def test_double_dash_in_plugin_name_rejected():
    _, errs = parse_and_validate("a--b", _m([_wh(name="x")]))
    assert errs


def test_plg_prefixed_declared_rejected():
    _, errs = parse_and_validate("p", _m([_wh(name="plg-x")]))
    assert errs


def test_bad_name_charset_rejected():
    _, errs = parse_and_validate("p", _m([_wh(name="has space")]))
    assert errs


def test_effective_name_length_bound():
    _, errs = parse_and_validate("p", _m([_wh(name="z" * 70)]))
    assert any("64" in e or "long" in e.lower() for e in errs)


# --- counts / duplicates ---------------------------------------------------

def test_too_many_triggers_rejected():
    many = [_wh(name=f"t{i}") for i in range(9)]
    _, errs = parse_and_validate("p", _m(many))
    assert any("8" in e or "too many" in e.lower() for e in errs)


def test_duplicate_declared_names_rejected():
    _, errs = parse_and_validate("p", _m([_wh(name="d"), _wh(name="d")]))
    assert any("duplicate" in e.lower() for e in errs)


def test_non_webhook_type_rejected():
    _, errs = parse_and_validate("p", _m([_wh(type="interval")]))
    assert errs


# ---------------------------------------------------------------------------
# Sol shipB-r2 P1-3: injectivity requires banning the ambiguous dash edges —
# plugin "a-" + declared "x" and plugin "a" + declared "-x" would BOTH yield
# "plg-a---x" (colliding routes; a's revoke prefix would sweep a-'s secrets).
# ---------------------------------------------------------------------------


def test_plugin_name_trailing_dash_rejected():
    _, errs = parse_and_validate("a-", _m([
        {"name": "x", "type": "webhook", "target": "resident:assistant",
         "auth": {"mode": "static_header"}}]))
    assert any("end with '-'" in e for e in errs)


def test_declared_name_leading_dash_rejected():
    _, errs = parse_and_validate("a", _m([
        {"name": "-x", "type": "webhook", "target": "resident:assistant",
         "auth": {"mode": "static_header"}}]))
    assert any("start with '-'" in e for e in errs)


def test_dash_edge_collision_pair_is_fully_rejected():
    """Neither producer of the ambiguous effective name survives."""
    t1, e1 = parse_and_validate("a-", _m([
        {"name": "x", "type": "webhook", "target": "resident:assistant",
         "auth": {"mode": "static_header"}}]))
    t2, e2 = parse_and_validate("a", _m([
        {"name": "-x", "type": "webhook", "target": "resident:assistant",
         "auth": {"mode": "static_header"}}]))
    assert e1 and e2
