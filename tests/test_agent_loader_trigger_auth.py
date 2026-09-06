"""_build_triggers normalizes webhook auth + clearance (Release A, Task 3)."""
from __future__ import annotations

import logging

import agent_loader
from agent_loader import _build_triggers


def _webhook(**over):
    t = {"name": "vm", "type": "webhook"}
    t.update(over)
    return _build_triggers({"triggers": [t]}, agent_dir="/nonexistent")[0]


def test_webhook_without_auth_defaults_hmac_body():
    spec = _webhook()
    assert spec.auth["mode"] == "hmac_body"
    assert spec.auth["header"] == "X-Webhook-Signature"
    assert spec.auth["secret_owner"] == "casa"
    assert spec.clearance == "public"


def test_static_header_defaults_header_and_owner():
    spec = _webhook(auth={"mode": "static_header"})
    assert spec.auth["mode"] == "static_header"
    assert spec.auth["header"] == "X-API-Key"
    assert spec.auth["secret_owner"] == "casa"


def test_timestamped_hmac_defaults():
    spec = _webhook(auth={"mode": "timestamped_hmac"})
    assert spec.auth["header"] == "X-Webhook-Signature"
    assert spec.auth["tolerance_secs"] == 300
    # No entry in the table names a vendor (#655).
    assert agent_loader._DEFAULT_AUTH_HEADER == {
        "hmac_body": "X-Webhook-Signature",
        "static_header": "X-API-Key",
        "timestamped_hmac": "X-Webhook-Signature",
    }


def test_timestamped_hmac_provider_owned_defaults_neutral_header():
    """The default is keyed on MODE alone: a provider-owned entry that omits
    `header` gets the same neutral name, not a vendor's (#655)."""
    spec = _webhook(auth={"mode": "timestamped_hmac",
                          "secret_owner": "provider"})
    assert spec.auth == {
        "mode": "timestamped_hmac",
        "header": "X-Webhook-Signature",
        "tolerance_secs": 300,
        "secret_owner": "provider",
    }


def test_explicit_values_preserved():
    spec = _webhook(auth={"mode": "timestamped_hmac", "header": "X-Sig",
                          "tolerance_secs": 120, "secret_owner": "provider"},
                    clearance="family")
    assert spec.auth["header"] == "X-Sig"
    assert spec.auth["tolerance_secs"] == 120
    assert spec.auth["secret_owner"] == "provider"
    assert spec.clearance == "family"


def test_path_on_webhook_emits_migration_warning(caplog):
    with caplog.at_level(logging.WARNING):
        _webhook(path="/hooks/legacy")
    assert any("deprecated" in r.message.lower() or "path" in r.message.lower()
               for r in caplog.records)


def test_interval_trigger_has_no_auth():
    spec = _build_triggers(
        {"triggers": [{"name": "hb", "type": "interval", "minutes": 5,
                       "channel": "telegram", "prompt": "ping"}]},
        agent_dir="/nonexistent")[0]
    assert spec.auth is None
