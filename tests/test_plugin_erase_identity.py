"""#1046: the grant identity of a Casa-dispatched plugin-erase turn.

An erase turn carries Casa's ``plugin_erase`` marker, the role it is FOR
(``plugin_erase_target``) and the artifact the operator's tap named
(``plugin_erase_artifact``). It is gated exactly like a setup turn
(INV-PLUG-027): an identity only on a Telegram-shaped direct or delegated turn
addressed to the operator as configured now, whose stamped target is the
executing role; none from an engagement; the stamps cannot come from outside
Casa; and it may delegate only in ``sync`` mode.

The dispatch path runs through the real ``Agent._process`` (the harness of
``test_authz_grants_setup_identity``), so a tree that never copies the stamps
fails here.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agent as agent_mod
from authz_grants import GrantIdentity, resolve_grant_identity
from bus import BusMessage, MessageType
from provenance import sanitize_external_context, turn_provenance

from test_agent_process import (
    ScriptedToolClient, _capture_reports, _make_agent, _mk_assistant, _mk_result,
)
from test_authz_grants_setup_identity import OP, _Channel, _Manager, live_operator  # noqa: F401
from test_delegate_setup_mode_gate import _delegate, _wire
from test_result_broker import _Origin

ART = "b" * 64


def _erase_msg(target="assistant", context_extra=None) -> BusMessage:
    context = {"chat_id": OP, "user_id": OP, "cid": "cid-e",
               "synthetic": "plugin_erase", "plugin_erase_target": target,
               "plugin_erase_artifact": ART}
    context.update(context_extra or {})
    return BusMessage(type=MessageType.CHANNEL_IN, source="telegram",
                      target="assistant", content="[casa plugin erase] run it",
                      channel="telegram", context=context)


def _erase_origin(role="assistant", target="assistant", **over) -> dict:
    base = {"role": role, "channel": "telegram", "chat_id": OP, "user_id": OP,
            "cid": "c", "user_text": "[casa plugin erase]",
            "message_type": "channel_in", "source": "telegram",
            "execution_role": role, "synthetic": "plugin_erase",
            "plugin_erase_target": target, "plugin_erase_artifact": ART}
    base.update(over)
    return base


async def _run(tmp_path, msg, role="assistant"):
    agent = _make_agent(tmp_path, role=role)
    captured: list = []
    real = agent_mod.compose_time_envelope

    def intercept(now):
        captured.append((dict(agent_mod.origin_var.get() or {}),
                         resolve_grant_identity(role), turn_provenance()))
        return real(now)

    ScriptedToolClient.reset([_mk_assistant("done"), _mk_result("sid-e")])
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient), \
            patch.object(agent_mod, "compose_time_envelope", intercept), \
            _capture_reports():
        await agent._process(msg)
    assert len(captured) == 1
    return captured[0]


@pytest.mark.asyncio
async def test_the_dispatched_erase_turn_carries_its_stamps_and_has_an_identity(
        tmp_path, live_operator):
    origin, (identity, why), prov = await _run(tmp_path, _erase_msg())
    assert origin["synthetic"] == "plugin_erase"
    assert origin["plugin_erase_target"] == "assistant"
    assert origin["plugin_erase_artifact"] == ART
    assert prov.transport == "setup"
    assert why is None
    assert identity == GrantIdentity(operator_id=OP, chat_id=OP,
                                     enforcement_role="assistant",
                                     artifact_id="", engagement_id="")


@pytest.mark.asyncio
async def test_a_courier_erase_turn_gives_only_the_named_specialist_an_identity(
        tmp_path, live_operator):
    origin, (identity, why), _ = await _run(tmp_path, _erase_msg(target="finance"))
    assert (identity, why) == (None, "setup_target_mismatch")
    child = {**origin, "delegation_depth": 1, "execution_role": "finance"}
    with _Origin(child):
        identity, why = resolve_grant_identity("finance", artifact_id=ART)
        assert why is None and identity.enforcement_role == "finance"
    other = {**origin, "delegation_depth": 1, "execution_role": "weather"}
    with _Origin(other):
        assert resolve_grant_identity("weather") == (None, "setup_target_mismatch")


def test_an_erase_turn_for_another_operator_is_refused(monkeypatch):
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(_Channel("43")),
                        raising=False)
    with _Origin(_erase_origin()):
        assert resolve_grant_identity("assistant") == (None, "setup_operator_changed")


def test_the_erase_stamps_are_reserved():
    cleaned = sanitize_external_context({
        "synthetic": "plugin_erase", "plugin_erase_target": "assistant",
        "plugin_erase_artifact": ART, "k": 1})
    assert "plugin_erase_target" not in cleaned
    assert "plugin_erase_artifact" not in cleaned


def test_an_erase_marked_engagement_yields_nothing(live_operator):
    stored = _erase_origin(target="finance")
    rec = SimpleNamespace(id="eng-9", kind="specialist", status="active",
                          topic_id=555, role_or_type="finance", origin=stored)
    with _Origin(None, engagement=rec):
        assert resolve_grant_identity("finance") == (None, "setup_engagement")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interactive", "async"])
async def test_an_erase_turn_delegates_only_in_sync_mode(monkeypatch, mode):
    tm, tch, eng_reg = _wire(monkeypatch)
    origin = _erase_origin(target="finance")
    payload, is_error = await _delegate(tm, origin, mode)
    assert payload["kind"] == "mode_unsupported_on_setup_turn" and is_error is True
    assert eng_reg.create.await_count == 0
    assert tch.open_engagement_topic.await_count == 0
    payload, is_error = await _delegate(tm, origin, "sync")
    assert payload["status"] == "ok" and not is_error
