"""#1015: the grant identity of a Casa-dispatched plugin-setup turn
(INV-PLUG-027).

An origin carrying Casa's ``plugin_setup`` marker yields a grant identity only
when it is Telegram-shaped, addressed to the operator as configured NOW, read
on a direct or delegated turn, and its Casa-stamped ``plugin_setup_target``
equals the executing role; read from an engagement record it yields none;
every other synthetic marker yields none; the marker and the target cannot be
supplied from outside Casa.

The dispatch path is tested END TO END: a ``BusMessage`` shaped exactly as
``casa_core._setup_dispatch`` composes it runs through the real
``Agent._process`` (the ``_make_agent``/``_msg`` harness), intercepting
``compose_time_envelope`` after ``origin_var`` is set and before any SDK
client, and the identity is resolved INSIDE the interception. A hand-built
origin fixture passes on a tree that never copies the stamp; this does not.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agent as agent_mod
import tools as tools_mod
from authz_grants import GrantIdentity, resolve_grant_identity
from bus import BusMessage, MessageType
from provenance import sanitize_external_context

from test_agent_process import (
    ScriptedToolClient, _capture_reports, _make_agent, _mk_assistant, _mk_result,
)
from test_result_broker import _Origin

OP = 42            # the configured operator: a private chat's id equals the user id


class _Channel:
    def __init__(self, chat_id="42"):
        self.chat_id = chat_id


class _Manager:
    def __init__(self, channel):
        self._channel = channel

    def get(self, name):
        return self._channel if name == "telegram" else None


@pytest.fixture
def live_operator(monkeypatch):
    """A Telegram channel configured for operator 42, where the resolver
    looks for it."""
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(_Channel("42")),
                        raising=False)


def _setup_origin(role="assistant", target="assistant", **over) -> dict:
    """The origin ``_process`` derives from a ``_setup_dispatch`` message —
    only used where the seam under test is the resolver, not the copy."""
    base = {"role": role, "channel": "telegram", "chat_id": OP, "user_id": OP,
            "cid": "c", "user_text": "[casa plugin setup]",
            "message_type": "channel_in", "source": "telegram",
            "execution_role": role, "synthetic": "plugin_setup",
            "plugin_setup_target": target}
    base.update(over)
    return base


# --- the real dispatch path -----------------------------------------------------

def _dispatch_msg(target="assistant", context_extra=None) -> BusMessage:
    """Exactly ``casa_core._setup_dispatch``'s composition: the operator's
    ids, a cid, and the worker's context (marker, episode, target)."""
    context = {"chat_id": OP, "user_id": OP, "cid": "cid-1",
               "synthetic": "plugin_setup", "setup_episode": "ep-1015",
               "plugin_setup_target": target}
    context.update(context_extra or {})
    return BusMessage(type=MessageType.CHANNEL_IN, source="telegram",
                      target="assistant", content="[casa plugin setup] run it",
                      channel="telegram", context=context)


async def _run_dispatch(tmp_path, msg, role="assistant"):
    """Run *msg* through the real ``Agent._process``; capture the origin and
    the resolver's answer at the ``compose_time_envelope`` seam."""
    import plugin_setup_episodes as pse
    agent = _make_agent(tmp_path, role=role)
    captured: list[tuple[dict, tuple]] = []
    real = agent_mod.compose_time_envelope

    def intercept(now):
        captured.append((dict(agent_mod.origin_var.get() or {}),
                         resolve_grant_identity(role)))
        return real(now)

    ScriptedToolClient.reset([_mk_assistant("done"), _mk_result("sid-1015")])
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient), \
            patch.object(agent_mod, "compose_time_envelope", intercept), \
            patch.object(pse, "dispatch_still_owed", lambda _e: True), \
            _capture_reports():
        await agent._process(msg)
    assert len(captured) == 1
    return captured[0]


@pytest.mark.asyncio
async def test_the_dispatched_setup_turn_carries_the_stamp_and_has_an_identity(
        tmp_path, live_operator):
    origin, (identity, why) = await _run_dispatch(tmp_path, _dispatch_msg())
    assert origin["synthetic"] == "plugin_setup"
    assert origin["plugin_setup_target"] == "assistant"      # the COPY under test
    assert why is None
    assert identity == GrantIdentity(operator_id=OP, chat_id=OP,
                                     enforcement_role="assistant",
                                     artifact_id="", engagement_id="")
    assert identity.target_role == "assistant"


@pytest.mark.asyncio
async def test_a_courier_turn_has_no_identity_of_its_own(tmp_path, live_operator):
    """A specialist-target row is dispatched to the resident as a courier:
    the stamp names the specialist, so the resident's own turn is refused
    and only the specialist's sync-delegated turn matches."""
    origin, (identity, why) = await _run_dispatch(tmp_path, _dispatch_msg(target="finance"))
    assert origin["plugin_setup_target"] == "finance"
    assert (identity, why) == (None, "setup_target_mismatch")
    # the delegated child origin, exactly as _run_delegated_agent derives it
    child = {**origin, "delegation_depth": 1, "execution_role": "finance"}
    with _Origin(child):
        identity, why = resolve_grant_identity("finance", artifact_id="a" * 64)
        assert why is None and identity.chat_id == OP
        assert identity.enforcement_role == "finance"
        # a sync delegation to any OTHER role: no identity
        other = {**origin, "delegation_depth": 1, "execution_role": "weather"}
    with _Origin(other):
        assert resolve_grant_identity("weather") == (None, "setup_target_mismatch")


@pytest.mark.asyncio
async def test_a_dispatch_whose_ids_are_not_the_live_operator_is_refused(
        tmp_path, monkeypatch):
    """The gate compares the copied ids to the operator as configured at
    the time of the call — through a delegated child origin too — and
    refuses when no Telegram channel is up to say who that is."""
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(_Channel("43")),
                        raising=False)
    origin, (identity, why) = await _run_dispatch(tmp_path, _dispatch_msg())
    assert (identity, why) == (None, "setup_operator_changed")
    child = {**origin, "delegation_depth": 1, "execution_role": "finance",
             "plugin_setup_target": "finance"}
    with _Origin(child):
        assert resolve_grant_identity("finance") == (None, "setup_operator_changed")
    monkeypatch.setattr(tools_mod, "_channel_manager", _Manager(None), raising=False)
    with _Origin(origin):
        assert resolve_grant_identity("assistant") == (None, "setup_operator_changed")
    monkeypatch.setattr(tools_mod, "_channel_manager", None, raising=False)
    with _Origin(origin):
        assert resolve_grant_identity("assistant") == (None, "setup_operator_changed")


@pytest.mark.asyncio
async def test_a_target_the_ingress_could_supply_is_stripped(tmp_path, live_operator):
    """``plugin_setup_target`` is reserved: an external context carrying it
    loses it at ingress, and a dispatched turn without one has no identity."""
    assert "plugin_setup_target" not in sanitize_external_context(
        {"plugin_setup_target": "assistant", "synthetic": "plugin_setup", "k": 1})
    origin, (identity, why) = await _run_dispatch(
        tmp_path, _dispatch_msg(context_extra={"plugin_setup_target": ""}))
    assert origin["plugin_setup_target"] == ""
    assert (identity, why) == (None, "setup_target_mismatch")


# --- the resolver's own branches ----------------------------------------------------

def test_other_markers_still_yield_nothing(live_operator):
    for marker in ("callback_nudge", "button_x", "scheduled", "plugin_setup_"):
        with _Origin(_setup_origin(synthetic=marker)):
            assert resolve_grant_identity("assistant") == (None, "unsupported_origin")
    # the target alone, without the marker, is an ordinary DM origin
    with _Origin(_setup_origin(synthetic=None)):
        identity, why = resolve_grant_identity("assistant")
        assert why is None and identity.chat_id == OP


def test_the_target_absent_or_mismatched_yields_nothing(live_operator):
    with _Origin(_setup_origin(target=None)):
        assert resolve_grant_identity("assistant") == (None, "setup_target_mismatch")
    o = _setup_origin()
    del o["plugin_setup_target"]
    with _Origin(o):
        assert resolve_grant_identity("assistant") == (None, "setup_target_mismatch")
    with _Origin(_setup_origin(target="finance")):
        assert resolve_grant_identity("assistant") == (None, "setup_target_mismatch")
    # the gate precedes the role-mismatch deny: the closure's role is checked
    # against the stamp first, then against execution_role as before
    with _Origin(_setup_origin(role="assistant", target="finance", execution_role="assistant")):
        assert resolve_grant_identity("finance") == (None, "role_mismatch")


def test_a_non_telegram_shaped_setup_origin_is_unsupported(live_operator):
    with _Origin(_setup_origin(source="voice")):
        assert resolve_grant_identity("assistant") == (None, "unsupported_origin")
    with _Origin(_setup_origin(chat_id=-100123)):
        assert resolve_grant_identity("assistant") == (None, "unsupported_origin")


def _engagement(origin, **over):
    base = dict(id="eng-9", kind="specialist", status="active", topic_id=555,
                role_or_type="finance", origin=origin)
    base.update(over)
    return SimpleNamespace(**base)


def test_an_engagement_whose_stored_origin_carries_the_marker_yields_nothing(live_operator):
    """At launch and on resume, with an empty ambient origin and with an
    unrelated one, whatever the stamp says."""
    stored = _setup_origin(target="finance")
    for ambient in (None, {},
                    {"role": "finance", "execution_role": "finance",
                     "channel": "telegram", "chat_id": -100123, "user_id": OP,
                     "message_type": "channel_in", "source": "telegram"}):
        with _Origin(ambient, engagement=_engagement(stored)):
            assert resolve_grant_identity("finance") == (None, "setup_engagement")
        with _Origin(ambient, engagement=_engagement(stored, id="resumed-1")):
            assert resolve_grant_identity("finance") == (None, "setup_engagement")
    # an engagement under a setup-marked AMBIENT origin is refused too
    plain = dict(_setup_origin()); plain.pop("synthetic"); plain.pop("plugin_setup_target")
    with _Origin(_setup_origin(target="finance"), engagement=_engagement(plain)):
        assert resolve_grant_identity("finance") == (None, "setup_engagement")
    # and an ordinary engagement's identity is unchanged: the origin chat
    with _Origin(None, engagement=_engagement(plain)):
        identity, why = resolve_grant_identity("finance")
        assert why is None and (identity.chat_id, identity.engagement_id) == (OP, "eng-9")
