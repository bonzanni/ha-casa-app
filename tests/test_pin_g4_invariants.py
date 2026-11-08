"""Pinning tests for configuration/trigger invariants (docs corpus).

Each test names the corpus invariant it pins and records, in its docstring, the
red case that was demonstrated: the code edit that made it fail. A pinning test
never shown red proves nothing.
"""
import json
from pathlib import Path

import jsonschema
import pytest

import reload as reload_mod


def test_pin_inv_cfg_001_exactly_eight_scopes_none_reading_options():
    """INV-CFG-001: exactly eight reload scopes exist, and none rereads the
    app manifest options.

    Red case demonstrated: registering a ninth scope
    (`register_handler("options", ...)`) fails the set equality.
    """
    import inspect

    assert set(reload_mod._HANDLERS) == {
        "triggers", "agent", "policies", "plugin_env",
        "agents", "executors", "config_sync", "full",
    }
    # "Rereads the app manifest options" means the Supervisor options store —
    # /data/options.json or a bashio read — not os.environ generally (the
    # plugin_env scope legitimately WRITES env when re-sourcing its conf).
    for handler in reload_mod._HANDLERS.values():
        source = inspect.getsource(handler)
        assert "options.json" not in source
        assert "bashio" not in source


def test_pin_inv_trig_002_plugin_namespace_reserved_in_user_schema():
    """INV-TRIG-002 (namespace half): the user trigger schema refuses the
    plugin prefix, and a plugin declaration produces exactly that prefix, so
    the namespaces cannot collide. Uniqueness is pinned by the trigger
    registry's duplicate/cross-role tests.

    Red case demonstrated: removing the `(?!plg-)` guard from the schema's
    name pattern accepts the plugin-shaped user name and this test fails.
    """
    from plugin_triggers import parse_and_validate

    schema = json.loads(Path(
        "casa/rootfs/opt/casa/defaults/schema/triggers.v1.json"
    ).read_text())
    name_pattern = (
        schema["properties"]["triggers"]["items"]["properties"]["name"]["pattern"]
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate("plg-p--x", {"type": "string", "pattern": name_pattern})

    triggers, errors = parse_and_validate("p", {"casa": {"triggers": [{
        "name": "x", "type": "webhook", "target": "resident:assistant",
        "auth": {"mode": "static_header"},
    }]}})
    assert errors == []
    assert triggers[0]["effective"] == "plg-p--x"
