"""#336: per-sender Telegram identity at the channel boundary.

The `telegram_sender` peer strategy used to fix ``user_peer`` to a hardcoded
operator constant for every accepted sender. With ``telegram_chat_id`` empty
("accept all chats") that attributed any Telegram user's turns to the operator
AND ran them at the operator's private recall clearance. `_handle` now resolves
the identity per sender and stamps the reserved origin markers so the recall
gate reads the per-sender clearance.

Since the operator-peer collapse, EVERY sender — the operator included — is
named ``telegram:<id>``, and clearance is the only thing that distinguishes the
operator. So the operator assertions below pin a derived peer, not a literal:
a regression to any route-wide constant fails them.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from bus import BusMessage, MessageBus
from channels.telegram import TelegramChannel
from ingress_identity import IngressIdentityError


class _FakeBot:
    async def send_message(self, **kwargs: Any) -> Any:
        return types.SimpleNamespace(message_id=1)


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _FakeBot()


def _fake_update(
    chat_id: str = "42", text: str = "hi", user_id: int | None = 7,
) -> Any:
    user = (
        types.SimpleNamespace(first_name="User", id=user_id)
        if user_id is not None else None
    )
    message = types.SimpleNamespace(text=text, message_id=42)
    chat = types.SimpleNamespace(id=chat_id)
    return types.SimpleNamespace(
        message=message, effective_chat=chat, effective_user=user)


async def _drain(bus: MessageBus, target: str = "assistant") -> list[BusMessage]:
    q = bus.queues.get(target)
    if q is None:
        return []
    out = []
    while not q.empty():
        _p, _s, msg = q.get_nowait()
        out.append(msg)
        q.task_done()
    return out


async def _noop_handler(_msg: BusMessage) -> None:
    return None


def _channel(configured_chat_id: str):
    bus = MessageBus()
    bus.register("assistant", _noop_handler)
    channel = TelegramChannel(
        bot_token="T", chat_id=configured_chat_id,
        default_agent="assistant", bus=bus,
    )
    channel._start_typing = lambda *a, **k: None  # type: ignore[assignment]
    channel._app = _FakeApp()  # type: ignore[assignment]
    return channel, bus


class TestOperatorSender:
    async def test_operator_sender_is_its_own_peer_at_private(self):
        # In the standard DM setup the configured chat id IS the operator's
        # user id. The operator is named by that id like anyone else — what
        # marks them out is the private clearance below.
        channel, bus = _channel("7")
        await channel._handle(_fake_update(chat_id="7", user_id=7), None)
        (msg,) = await _drain(bus)
        assert msg.trusted_user_origin.user_peer == "telegram:7"
        assert msg.trusted_user_origin.server_origin.clearance == "private"
        assert msg.context["_origin_route"] == "telegram"
        assert msg.context["_origin_clearance"] == "private"
        # #283: a live operator message is the ONE ingress that carries the
        # spawn-cap exemption marker.
        assert msg.context["_operator_turn"] is True


class TestNonOperatorSender:
    async def test_non_operator_gets_per_sender_peer_at_public(self):
        # Pre-#336 red case: this sender resolved to the operator's constant
        # peer with private clearance.
        channel, bus = _channel("0")
        await channel._handle(_fake_update(chat_id="42", user_id=7), None)
        (msg,) = await _drain(bus)
        assert msg.trusted_user_origin.user_peer == "telegram:7"
        assert msg.trusted_user_origin.server_origin.clearance == "public"
        assert msg.context["_origin_clearance"] == "public"
        # #283: a non-operator sender never carries the spawn-cap exemption.
        assert "_operator_turn" not in msg.context

    async def test_accept_all_mode_has_no_operator(self):
        # telegram_chat_id empty = accept-all: there is no configured
        # operator identity, so no sender resolves to the operator peer.
        channel, bus = _channel("")
        await channel._handle(_fake_update(chat_id="7", user_id=7), None)
        (msg,) = await _drain(bus)
        assert msg.trusted_user_origin.user_peer == "telegram:7"
        assert msg.trusted_user_origin.server_origin.clearance == "public"

    async def test_sender_less_update_fails_loudly_not_as_operator(self):
        # An anonymous group/channel post (no effective_user) used to be
        # silently attributed to the operator; #203 doctrine says an
        # unattributable ingress turn dies loudly instead.
        channel, bus = _channel("")
        with pytest.raises(IngressIdentityError):
            await channel._handle(_fake_update(chat_id="42", user_id=None), None)
        assert await _drain(bus) == []


class TestAcceptAllBootWarning:
    """#368: an empty ``telegram_chat_id`` makes the option a security
    control with real consequences (no operator attribution, protected tools
    always denied) — the channel must say so loudly at construction."""

    def test_empty_chat_id_warns_at_construction(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="channels.telegram"):
            _channel("")
        assert any(
            "accept-all" in r.message and "protected" in r.message
            for r in caplog.records
        ), caplog.records

    def test_configured_chat_id_does_not_warn(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="channels.telegram"):
            _channel("7")
        assert not any("accept-all" in r.message for r in caplog.records)


class TestOperatorDetermination:
    def test_group_configured_chat_never_names_an_operator(self):
        # A supergroup id is negative and can never equal a user id — group
        # members are not the operator.
        channel, _ = _channel("-100123")
        assert not channel._sender_is_operator(
            types.SimpleNamespace(id=100123))

    def test_operator_match_is_exact(self):
        channel, _ = _channel("7")
        assert channel._sender_is_operator(types.SimpleNamespace(id=7))
        assert not channel._sender_is_operator(types.SimpleNamespace(id=77))
        assert not channel._sender_is_operator(None)

    def test_operator_user_id_accessor_single_home(self):
        """#374: operator_user_id() answers "who is the configured operator"
        under exactly the _user_id_is_operator rules — a positive DM chat id
        IS the operator's user id; empty (accept-all), a group id (negative),
        and a non-numeric value all name NOBODY (None, fail-closed)."""
        channel, _ = _channel("7")
        assert channel.operator_user_id() == 7
        for configured in ("", "  ", "-100123", "0", "not-a-number"):
            channel, _ = _channel(configured)
            assert channel.operator_user_id() is None, configured

    def test_operator_user_id_never_disagrees_with_sender_rule(self):
        """Sol r1 S1 red case: a non-canonical numeric configuration ("007")
        must not name an operator here that _user_id_is_operator rejects —
        otherwise a sender classified non-operator at ingress could approve
        gated tools. The accessor answers None unless the exact string rule
        would also accept the id."""
        channel, _ = _channel("007")
        assert not channel._user_id_is_operator(7)
        assert channel.operator_user_id() is None


class TestButtonTapIdentity:
    """Sol r2: a tap is a turn — it must carry the tapper's identity, not just
    their clearance. Without ``trusted_user_origin`` the continuation was
    persisted as the unattributed ``system`` speaker."""

    async def test_non_operator_tap_is_attributed_to_that_sender(self):
        channel, bus = _channel("")
        await channel._dispatch_button_continuation(
            chat_id=999, user_id=999, target_role="assistant",
            request_id="r1", text="yes")
        (msg,) = await _drain(bus)
        assert msg.trusted_user_origin is not None
        assert msg.trusted_user_origin.user_peer == "telegram:999"
        assert msg.trusted_user_origin.server_origin.clearance == "public"
        assert msg.context["_origin_clearance"] == "public"

    async def test_operator_tap_keeps_the_operator_clearance(self):
        channel, bus = _channel("7")
        await channel._dispatch_button_continuation(
            chat_id=7, user_id=7, target_role="assistant",
            request_id="r1", text="yes")
        (msg,) = await _drain(bus)
        assert msg.trusted_user_origin.user_peer == "telegram:7"
        assert msg.context["_origin_clearance"] == "private"

    async def test_tap_provenance_is_a_user_not_the_system_speaker(self):
        from speaker_provenance import UserProvenance
        channel, bus = _channel("")
        await channel._dispatch_button_continuation(
            chat_id=999, user_id=999, target_role="assistant",
            request_id="r1", text="yes")
        (msg,) = await _drain(bus)
        o = msg.trusted_user_origin
        prov = UserProvenance.from_origin(
            surface=o.surface, server_origin=o.server_origin,
            authenticated_user=o.authenticated_user, user_peer=o.user_peer)
        assert prov.speaker_kind == "user"
        assert prov.user_peer == "telegram:999"


class TestEngagementInheritsItsOriginClearance:
    """Terra r2: an engagement's tool calls bind ``engagement_var`` but no
    ambient origin, so a clearance keyed off the ambient origin fell through
    to the telegram channel default (private) — letting an engagement started
    by a non-operator recall the operator's private memory."""

    def _clearance(self, eng_origin, ambient=None):
        import tools as tools_mod
        from sensitivity import clearance_for_origin

        class _Rec:
            def __init__(self, origin):
                self.origin = origin

        token = tools_mod.engagement_var.set(
            _Rec(eng_origin) if eng_origin is not None else None)
        try:
            route, clearance = tools_mod._origin_clearance_markers(ambient or {})
        finally:
            tools_mod.engagement_var.reset(token)
        return clearance_for_origin(route, clearance, "telegram")

    def test_engagement_started_by_a_non_operator_reads_public(self):
        assert self._clearance(
            {"_origin_route": "telegram", "_origin_clearance": "public"}
        ) == "public"

    def test_engagement_started_by_the_operator_still_reads_private(self):
        assert self._clearance(
            {"_origin_route": "telegram", "_origin_clearance": "private"}
        ) == "private"

    def test_record_without_markers_keeps_channel_behaviour(self):
        # Non-regressive: engagements created before this release, and origins
        # that stamp no route, resolve exactly as they did.
        assert self._clearance({}) == "private"
        assert self._clearance(None) == "private"

    def test_an_ambient_origin_is_never_overridden_by_the_record(self):
        # A delegated IN-PROCESS turn carries its own origin; the engagement
        # record must not displace it.
        assert self._clearance(
            {"_origin_route": "telegram", "_origin_clearance": "public"},
            ambient={"_origin_route": "telegram", "_origin_clearance": "private"},
        ) == "private"
