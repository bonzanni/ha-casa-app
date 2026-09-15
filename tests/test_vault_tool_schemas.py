"""The configurator's vault tools accept an omitted ``vault``.

#535 shipped the handler-side fallback to ``onepassword_default_vault`` — but
both tools were declared with the SDK's dict shorthand, which the SDK compiles
to ``required = every key`` (``claude_agent_sdk.create_sdk_mcp_server``'s
``_build_schema``), so the MCP input validator rejected every call that omitted
``vault`` BEFORE the handler ran. Measured on the N150 on 2026-09-15:
``list_vault_items(query="gmail")`` → ``Input validation error: 'vault' is a
required property``; the configurator gave up on wiring the plugin's secrets.

These tests pin the schema the SDK will actually serve: an explicit JSON
Schema object (the SDK's verbatim branch) in which ``vault`` is optional and
the search key stays required.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import jsonschema
import pytest

import tools


def _served_schema(tool_def) -> dict:
    """Reproduce the SDK's schema rule so the test fails if the tool ever
    reverts to the shorthand: a dict WITHOUT ``type``+``properties`` is
    compiled with every key required."""
    schema = tool_def.input_schema
    assert isinstance(schema, dict)
    if "type" in schema and "properties" in schema:
        return schema
    return {"type": "object",
            "properties": {k: {"type": "string"} for k in schema},
            "required": list(schema)}


@pytest.mark.parametrize("tool_def, required, omitted_call", [
    (tools.list_vault_items, ["query"], {"query": "gmail"}),
    (tools.get_item_fields, ["item"], {"item": "Gmail"}),
], ids=["list_vault_items", "get_item_fields"])
def test_vault_omitted_is_accepted_by_the_served_schema(
        tool_def, required, omitted_call):
    schema = _served_schema(tool_def)
    assert schema["required"] == required
    assert "vault" in schema["properties"]
    # The MCP validator's own verdict on the exact call the N150 refused.
    jsonschema.validate(omitted_call, schema)


@pytest.mark.parametrize("tool_def, bad_call", [
    (tools.list_vault_items, {"vault": "Casa"}),
    (tools.get_item_fields, {"vault": "Casa"}),
], ids=["list_vault_items", "get_item_fields"])
def test_the_search_key_stays_required(tool_def, bad_call):
    """``query`` / ``item`` remain required: the recipe's "never enumerate the
    whole vault" is enforced by the schema, not by prose."""
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_call, _served_schema(tool_def))


class _Result:
    returncode = 0
    stderr = ""
    stdout = json.dumps([])


async def test_list_vault_items_handler_runs_without_vault(monkeypatch):
    """End to end through the decorated tool: an omitted vault reaches the
    handler and resolves to the configured default."""
    captured: list[list[str]] = []

    def _fake_run(cmd, **_kw):
        captured.append(list(cmd))
        return _Result()

    monkeypatch.setenv("ONEPASSWORD_DEFAULT_VAULT", "Casa")
    with patch.object(tools.subprocess, "run", _fake_run):
        r = await tools.list_vault_items.handler({"query": "gmail"})
    payload = json.loads(r["content"][0]["text"])
    assert payload == {"items": []}
    assert captured and "--vault" in captured[0]
    assert captured[0][captured[0].index("--vault") + 1] == "Casa"


# --- the same class on the two tools every recipe's canonical order calls ----
#
# `casa_reload(scope="plugin_env")` and `emit_completion(status=..., text=...)`
# are the literal calls in recipes/plugin/secrets.md; both were rejected by the
# validator under the shorthand schema (Astra, batch-1 diff round 1, D5).

@pytest.mark.parametrize("tool_def, required, recipe_call", [
    (tools.casa_reload, ["scope"], {"scope": "plugin_env"}),
    (tools.emit_completion, ["text"], {"status": "ok", "text": "Wired X"}),
], ids=["casa_reload", "emit_completion"])
def test_recipe_literal_calls_pass_the_served_schema(tool_def, required, recipe_call):
    schema = _served_schema(tool_def)
    assert schema["required"] == required
    jsonschema.validate(recipe_call, schema)
