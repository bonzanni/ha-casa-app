"""S5 §2: the stored call — the argument grammar, the canonical form and the
one predicate (``stored_call_ok``) that decides which of a plugin's tools a
proposal button may name, reused verbatim at tap time (INV-PROP-003).
"""
from __future__ import annotations

import json

import pytest

import stored_calls as sc
from plugin_grants import PluginContract, ResultContractMap, ToolContract

ART = "a" * 64
SEG = "probe"
SRV = "probe"


def _tool(name, kind="safe", *, provides=(), consumes=None, delivers=None,
          servers=(SRV,), transport="stdio"):
    return ToolContract(ART, SEG, kind, tuple(provides), dict(consumes or {}),
                        delivers=dict(delivers or {}), servers=tuple(servers),
                        wire_name=name, transport=transport)


def _runtime(name, server=SRV):
    return f"mcp__plugin_{SEG}_{server}__{name}"


def _map(tools, *, setup=()):
    return ResultContractMap(
        tools={_runtime(t.wire_name, t.servers[0]): t for t in tools},
        plugins={SEG: PluginContract(ART, True, frozenset(_runtime(s) for s in setup), name="probe")})


# --- canonical form --------------------------------------------------------------

def test_canonical_json_is_sorted_compact_and_unicode():
    assert sc.canonical_json({"b": 1, "a": [True, None, "é"]}) == '{"a":[true,null,"é"],"b":1}'
    assert sc.canonical_json({"a": {"y": 2, "x": 1}}) == sc.canonical_json({"a": {"x": 1, "y": 2}})


# --- the argument grammar ------------------------------------------------------------

@pytest.mark.parametrize("arguments, reason", [
    ([], "not_object"),
    ({"n": 1.0}, "float"),
    ({"n": 2 ** 53}, "unsafe_integer"),
    ({"n": -(2 ** 53)}, "unsafe_integer"),
    ({"_n": 1}, "key"),
    ({"__consentNonce": "x"}, "key"),
    ({"1n": 1}, "key"),
    ({"a-b": 1}, "key"),
    ({"s": "a<b"}, "angle_bracket"),
    ({"s": "</render_id></invoke>"}, "angle_bracket"),
    ({"s": "x" * 2001}, "string_length"),
    ({"r": "casa-cap-" + "0" * 32}, "reference"),
    ({"deep": {"inner": {"n": 1.5}}}, "float"),
    ({"list": [1, "ok", {"k": 2 ** 60}]}, "unsafe_integer"),
])
def test_the_grammar_refuses_what_a_cli_transformation_or_a_reference_would_change(arguments, reason):
    assert sc.arguments_ok(arguments) == reason


def test_the_grammar_admits_strings_bools_null_safe_integers_lists_and_objects():
    ok = {"match_id": 17, "expected_revision": 9, "render_id": "r-1", "labels": ["clean", "x"],
          "flag": True, "none": None, "nested": {"k": [1, 2, {"z": "y"}]}, "big": 2 ** 53 - 1,
          "neg": -(2 ** 53 - 1)}
    assert sc.arguments_ok(ok) is None
    assert sc.arguments_ok({}) is None
    assert json.loads(sc.canonical_json(ok)) == ok


def test_the_grammar_bounds_the_serialised_size():
    assert sc.arguments_ok({"s": "x" * 1500, "t": "y" * 1500, "u": "z" * 1500}) == "size"
    assert sc.ARGUMENTS_MAX_BYTES == 4096 and sc.STRING_MAX_CHARS == 2000


# --- the predicate: which tools a button may name ----------------------------------

def test_a_safe_tool_of_the_same_server_with_no_consumes_is_an_ordinary_stored_call():
    cmap = _map([_tool("confirm_match")])
    call, reason = sc.resolve_stored_call("confirm_match", contract_map=cmap, protected={},
                                          seg=SEG, server=SRV)
    assert reason is None
    assert (call.server, call.wire_name, call.runtime_name, call.proposal) == (
        SRV, "confirm_match", _runtime("confirm_match"), False)


def test_the_more_exception_is_a_capability_whose_only_slot_is_its_own_proposal():
    more = _tool("more", "capability", provides=("proposal",),
                 delivers={"proposal": "operator_proposal"})
    call, reason = sc.resolve_stored_call("more", contract_map=_map([more]), protected={},
                                          seg=SEG, server=SRV)
    assert reason is None and call.proposal is True


@pytest.mark.parametrize("tool, protected, setup, reason", [
    (None, {}, (), "undeclared"),
    (_tool("x", "safe", consumes={"doc": "document"}), {}, (), "consumes"),
    (_tool("x", "capability", provides=("link",), delivers={"link": "operator_link"}), {}, (), "capability"),
    (_tool("x", "capability", provides=("proposal", "other"), delivers={"proposal": "operator_proposal"}), {}, (), "capability"),
    (_tool("x", "capability", provides=("proposal",)), {}, (), "capability"),
    (_tool("x", "capability", provides=("proposal",), consumes={"d": "s"}, delivers={"proposal": "operator_proposal"}), {}, (), "consumes"),
    (_tool("x"), {_runtime("x"): object()}, (), "protected"),
    (_tool("x"), {}, ("x",), "setup"),
    (_tool("x", transport="http"), {}, (), "transport"),
    (_tool("x", servers=(SRV, "probe-2")), {}, (), "ambiguous_server"),
])
def test_every_other_shape_is_refused_with_its_reason(tool, protected, setup, reason):
    cmap = _map([tool] if tool is not None else [], setup=setup)
    call, why = sc.resolve_stored_call("x", contract_map=cmap, protected=protected,
                                       seg=SEG, server=SRV)
    assert call is None and why == reason


def test_a_bare_name_never_reaches_another_plugin_or_server():
    other = ToolContract(ART, "other", "safe", (), {}, servers=("other",), wire_name="x",
                         transport="stdio")
    cmap = ResultContractMap(tools={"mcp__plugin_other_other__x": other},
                             plugins={"other": PluginContract(ART, True, frozenset(), name="other"),
                                      SEG: PluginContract(ART, True, frozenset(), name="probe")})
    call, why = sc.resolve_stored_call("x", contract_map=cmap, protected={}, seg=SEG, server=SRV)
    assert call is None and why == "undeclared"
    # a qualified or path-like name is not a bare name
    for bad in ("mcp__plugin_probe_probe__x", "other/x", "", "x y"):
        call, why = sc.resolve_stored_call(bad, contract_map=cmap, protected={}, seg=SEG, server=SRV)
        assert call is None and why == "bare_name"


def test_the_tap_time_recheck_uses_the_same_predicate_by_runtime_name():
    cmap = _map([_tool("confirm_match")])
    assert sc.stored_call_still_ok(_runtime("confirm_match"), contract_map=cmap, protected={}) is None
    assert sc.stored_call_still_ok(_runtime("confirm_match"), contract_map=_map([]), protected={}) == "undeclared"
    assert sc.stored_call_still_ok(_runtime("confirm_match"), contract_map=cmap,
                                   protected={_runtime("confirm_match"): 1}) == "protected"
