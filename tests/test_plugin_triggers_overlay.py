"""TriggerRegistry plugin overlay: atomic, isolated from resident triggers
(Release B, Task 3)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web

pytestmark = pytest.mark.unit


def _mk_registry():
    from trigger_registry import TriggerRegistry
    sched = MagicMock()
    app = web.Application()
    bus = MagicMock()
    return TriggerRegistry(scheduler=sched, app=app, bus=bus)


def _overlay_entry(role="assistant", clearance="public", mode="static_header"):
    return {"role": role, "clearance": clearance,
            "auth": {"mode": mode, "header": "X-API-Key",
                     "tolerance_secs": 300, "secret_owner": "casa"}}


def test_replace_overlay_registers_reads():
    reg = _mk_registry()
    reg.replace_plugin_overlay({
        "plg-elevenlabs--voicemail": _overlay_entry(clearance="family")})
    assert reg.get_webhook_target("plg-elevenlabs--voicemail") == "assistant"
    route = reg.webhook_route("plg-elevenlabs--voicemail")
    assert route["clearance"] == "family"
    assert route["auth"]["mode"] == "static_header"


def test_replace_is_atomic_full_swap_stale_swept():
    """Pins INV-TRIG-005. Red case demonstrated: replacing the overlay rebind with an update() merge fails this test."""
    reg = _mk_registry()
    reg.replace_plugin_overlay({"plg-a--x": _overlay_entry()})
    reg.replace_plugin_overlay({"plg-b--y": _overlay_entry()})
    # the first plugin's trigger is gone (stale-swept), the second is live
    assert reg.get_webhook_target("plg-a--x") is None
    assert reg.get_webhook_target("plg-b--y") == "assistant"


def test_empty_replace_clears_all():
    reg = _mk_registry()
    reg.replace_plugin_overlay({"plg-a--x": _overlay_entry()})
    reg.replace_plugin_overlay({})
    assert reg.get_webhook_target("plg-a--x") is None


def test_overlay_survives_resident_reregister():
    reg = _mk_registry()
    reg.replace_plugin_overlay({"plg-p--a": _overlay_entry(role="assistant")})
    # a resident reregister for the SAME role must not touch the plugin overlay
    reg.reregister_for("assistant", [], channels=["webhook"])
    assert reg.get_webhook_target("plg-p--a") == "assistant"


def test_resident_and_plugin_names_are_disjoint():
    # Resident names can't start with plg- (schema), plugin effective names
    # always do — so a resident trigger and a plugin trigger never collide.
    from config import TriggerSpec
    reg = _mk_registry()
    reg.register_agent(
        "assistant",
        [TriggerSpec(name="doorbell", type="webhook")],
        channels=["webhook"],
    )
    reg.replace_plugin_overlay({"plg-p--doorbell": _overlay_entry()})
    assert reg.get_webhook_target("doorbell") == "assistant"          # resident
    assert reg.get_webhook_target("plg-p--doorbell") == "assistant"   # plugin
    # resident reregister clears the resident one, not the plugin one
    reg.reregister_for("assistant", [], channels=["webhook"])
    assert reg.get_webhook_target("doorbell") is None
    assert reg.get_webhook_target("plg-p--doorbell") == "assistant"


def test_overlay_names_listed():
    reg = _mk_registry()
    reg.replace_plugin_overlay({"plg-a--x": _overlay_entry(),
                                "plg-b--y": _overlay_entry()})
    assert set(reg.plugin_overlay_names()) == {"plg-a--x", "plg-b--y"}
