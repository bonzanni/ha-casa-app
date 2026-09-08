# tests/test_mcp_envelope.py
"""Unit tests for mcp_envelope.py (extracted from test_mcp_bridge.py)."""
from __future__ import annotations


def test_jsonrpc_ok_shape() -> None:
    from mcp_envelope import _jsonrpc_ok
    resp = _jsonrpc_ok(42, {"foo": "bar"})
    body = resp.body.decode("utf-8")
    import json
    parsed = json.loads(body)
    assert parsed == {"jsonrpc": "2.0", "id": 42, "result": {"foo": "bar"}}
    assert resp.status == 200


def test_jsonrpc_error_shape() -> None:
    from mcp_envelope import _jsonrpc_error
    resp = _jsonrpc_error(7, -32601, "Method not found: foo")
    import json
    parsed = json.loads(resp.body.decode("utf-8"))
    assert parsed == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32601, "message": "Method not found: foo"},
    }
    assert resp.status == 200


def test_jsonrpc_ok_null_id() -> None:
    """JSON-RPC permits null id (notifications + parse errors)."""
    from mcp_envelope import _jsonrpc_ok
    resp = _jsonrpc_ok(None, {})
    import json
    parsed = json.loads(resp.body.decode("utf-8"))
    assert parsed["id"] is None


def test_py_type_to_json_primitives() -> None:
    from mcp_envelope import _py_type_to_json
    assert _py_type_to_json(str) == {"type": "string"}
    assert _py_type_to_json(int) == {"type": "integer"}
    assert _py_type_to_json(float) == {"type": "number"}
    assert _py_type_to_json(bool) == {"type": "boolean"}
    assert _py_type_to_json(list) == {"type": "array"}
    assert _py_type_to_json(dict) == {"type": "object"}


def test_py_type_to_json_unknown_falls_back_to_permissive() -> None:
    from mcp_envelope import _py_type_to_json
    class Foo: ...
    assert _py_type_to_json(Foo) == {}


def test_tool_schema_dict_input() -> None:
    """The @tool(...) dict-of-types path: {"message": str} → JSON Schema."""
    from mcp_envelope import _tool_schema

    class _FakeTool:
        name = "t1"
        description = "Test tool"
        input_schema = {"message": str, "count": int}
        handler = None  # not used by _tool_schema

    schema = _tool_schema(_FakeTool())
    assert schema == {
        "name": "t1",
        "description": "Test tool",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "count": {"type": "integer"},
            },
        },
    }


def test_tool_schema_passthrough_dict() -> None:
    """If input_schema is already a JSON Schema dict (TypedDict-baked or
    pre-built), pass it through unchanged."""
    from mcp_envelope import _tool_schema

    class _FakeTool:
        name = "t2"
        description = ""
        input_schema = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        }
        handler = None

    schema = _tool_schema(_FakeTool())
    # Note: the passthrough branch doesn't normalize — it returns the dict
    # AS-IS for the inputSchema field if it looks pre-built.
    assert schema["name"] == "t2"
    assert schema["inputSchema"]["properties"]["x"] == {"type": "string"}


def test_tool_schema_empty_dict_input() -> None:
    """O-1 regression: no-arg tools declared with @tool(name, desc, {})
    must emit `inputSchema = {"type":"object","properties":{}}`.
    Empty dict is falsy, so prior code short-circuited the dict-of-types
    branch and passed {} through unchanged; CC v2.1.119 strict-validates
    and rejects the entire tools/list payload when `inputSchema.type`
    is missing. Affects casa_reload + marketplace_list_plugins."""
    from mcp_envelope import _tool_schema

    class _FakeTool:
        name = "no_arg_tool"
        description = "Takes no arguments."
        input_schema = {}
        handler = None

    schema = _tool_schema(_FakeTool())
    assert schema == {
        "name": "no_arg_tool",
        "description": "Takes no arguments.",
        "inputSchema": {"type": "object", "properties": {}},
    }


def test_envelope_module_exports_constants() -> None:
    """VERSION + PROTOCOL_VERSION are exported (svc_casa_mcp + the public
    fallback both consume them)."""
    import mcp_envelope
    assert hasattr(mcp_envelope, "VERSION")
    assert hasattr(mcp_envelope, "PROTOCOL_VERSION")
    assert isinstance(mcp_envelope.VERSION, str)
    assert isinstance(mcp_envelope.PROTOCOL_VERSION, str)
    # Sanity: PROTOCOL_VERSION matches the MCP spec we target (per v0.13.1).
    assert mcp_envelope.PROTOCOL_VERSION == "2025-06-18"


class TestNoArgToolsEmitObjectSchema:
    """Tools with empty input_schema must emit
    {"type": "object", "properties": {}} on the wire — bare {} is rejected
    by CC v2.1.119+ strict validation (Bug O-1).

    This class verifies the live tool objects (not a synthetic FakeTool)
    declared elsewhere in the codebase. Pairs with
    ``test_tool_schema_empty_dict_input`` above (which uses a fake) for
    defense-in-depth.
    """

    def test_casa_restart_supervised_schema(self):
        from mcp_envelope import _tool_schema
        from tools import casa_restart_supervised
        schema = _tool_schema(casa_restart_supervised)
        assert schema["inputSchema"] == {
            "type": "object", "properties": {}
        }, (
            f"casa_restart_supervised emitted {schema['inputSchema']!r}; "
            "expected {'type': 'object', 'properties': {}} per O-1 fix"
        )

    def test_plugin_list_schema(self):
        from mcp_envelope import _tool_schema
        from tools import plugin_list
        schema = _tool_schema(plugin_list)
        assert schema["inputSchema"] == {
            "type": "object", "properties": {}
        }
