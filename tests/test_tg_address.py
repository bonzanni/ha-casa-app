"""Telegram addresses, routes and broker scopes (channels/tg_address.py).

An address names a message as one bot saw it, so its bot is always a real bot
id: the reserved route value ``CASA`` must never reach an address or a key."""

from __future__ import annotations

import pytest

from channels.tg_address import (
    CASA,
    AddressError,
    DuplicateEndpoint,
    EndpointUnavailable,
    RouteMissing,
    TgAddress,
    is_route,
    scope,
)


def test_round_trip():
    addr = TgAddress(111, 100, 5)
    assert addr.to_json() == [111, 100, 5]
    assert TgAddress.from_json(addr.to_json()) == addr
    chat = TgAddress(111, -1001)
    assert chat.message_id is None
    assert TgAddress.from_json(chat.to_json()) == chat


@pytest.mark.parametrize("stored", [
    [None, 1, 2],        # no bot
    [1, 2],              # the pre-bot shape: (chat, message)
    ["1", 2, 3],         # a bot that is not an int
    [True, 2, 3],        # a bool is never an id
    [CASA, 2, 3],        # a route, not an address
    [1, None, 3],        # no chat
    [1, 2, "3"],         # a message id that is not an int
    {"bot_id": 1, "chat_id": 2, "message_id": 3},
    None,
])
def test_from_json_is_strict(stored):
    with pytest.raises(AddressError):
        TgAddress.from_json(stored)


@pytest.mark.parametrize("bot", [CASA, True, None, 0, -5, "111", 1.0])
def test_an_address_refuses_a_bot_that_is_not_a_bot_id(bot):
    with pytest.raises(AddressError):
        TgAddress(bot, 100)


def test_address_error_is_a_value_error():
    assert issubclass(AddressError, ValueError)


def test_scope_carries_the_bot():
    assert scope("dm", 111, 100) == "dm:111:100"
    assert scope("authz", 222, 100) == "authz:222:100"
    assert scope("ask", 111, 100) == "ask:111:100"
    assert scope("proposal", 111, 100) == "proposal:111:100"
    assert scope("dm", 111, 100) != scope("dm", 222, 100)


@pytest.mark.parametrize("bot", [CASA, True, None, "111"])
def test_scope_refuses_a_bot_that_is_not_a_bot_id(bot):
    with pytest.raises(AddressError):
        scope("dm", bot, 100)


def test_scope_refuses_an_unknown_kind():
    with pytest.raises(ValueError):
        scope("desk", 111, 100)


@pytest.mark.parametrize("value,expected", [
    (111, True), (CASA, True),
    (None, False), (True, False), (False, False), (0, False), (-1, False),
    ("111", False), ("CASA", False), (1.0, False),
])
def test_is_route(value, expected):
    assert is_route(value) is expected


def test_typed_errors_carry_what_failed():
    assert EndpointUnavailable(333).bot_id == 333
    assert DuplicateEndpoint(111).bot_id == 111
    assert RouteMissing("send").site == "send"
