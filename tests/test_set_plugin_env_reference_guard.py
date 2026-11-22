"""set_plugin_env_reference refuses the env names Casa sets from its own app
options: a plugin-level entry for one of them would silently override the
value Casa chose at start."""
from __future__ import annotations

import json

import pytest

import plugin_env_conf
import plugin_store
import tools as tools_mod


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(plugin_env_conf, "set_entry",
                        lambda *a, **k: seen.append((a, k)))
    return seen


@pytest.mark.parametrize("name,option",
                         sorted(plugin_store.CASA_OWNED_ENV_OPTIONS.items()))
def test_casa_owned_names_are_refused(calls, name, option):
    got = tools_mod._tool_set_plugin_env_reference(
        plugin="gmail", var_name=name, op_ref_or_value="op://v/i/f")
    assert got["ok"] is False
    assert option in got["error"]
    assert name in got["error"]
    assert calls == []


def test_other_names_are_written_as_before(calls):
    got = tools_mod._tool_set_plugin_env_reference(
        plugin="gmail", var_name="GMAIL_TOKEN", op_ref_or_value="op://v/i/f")
    assert got == {"ok": True}
    assert calls == [(("GMAIL_TOKEN", "op://v/i/f"), {})]


async def test_the_tool_wrapper_returns_the_refusal(calls, monkeypatch):
    monkeypatch.setattr(tools_mod, "_record_plugin_installer", lambda *a, **k: None)
    out = await tools_mod.set_plugin_env_reference.handler(
        {"plugin": "gmail", "var_name": "OP_SERVICE_ACCOUNT_TOKEN",
         "op_ref_or_value": "op://v/i/f"})
    payload = json.loads(out["content"][0]["text"])
    assert payload["ok"] is False
    assert "onepassword_service_account_token" in payload["error"]
    assert calls == []
