"""Authenticated Home Assistant voice-agent catalog tests."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bus import MessageBus
from channels.voice.channel import VoiceChannel
from channels.voice.catalog import (
    VoiceAgentCatalogError,
    build_voice_agent_catalog,
)


pytestmark = pytest.mark.unit


def _cfg(
    role: str,
    name: str,
    channels: list[str],
    *,
    enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        role=role,
        channels=channels,
        character=SimpleNamespace(name=name),
        system_prompt="PRIVATE_PROMPT_CANARY",
        tools={"private": "PRIVATE_TOOLS_CANARY"},
        delegates=["PRIVATE_DELEGATE_CANARY"],
        secret="PRIVATE_SECRET_CANARY",
    )


def test_catalog_filters_sorts_and_limits_output_fields():
    configs = {
        "concierge": _cfg("concierge", " Gary ", ["ha_voice"]),
        "assistant": _cfg(
            "assistant", "Ellen", ["telegram", "webhook"],
        ),
        "butler": _cfg("butler", "Tina", ["ha_voice"]),
        "disabled": _cfg(
            "disabled", "Hidden", ["ha_voice"], enabled=False,
        ),
    }

    catalog = build_voice_agent_catalog(configs)

    assert catalog == {
        "schema_version": 1,
        "agents": [
            {"role": "butler", "name": "Tina"},
            {"role": "concierge", "name": "Gary"},
        ],
    }
    serialized = json.dumps(catalog)
    for canary in (
        "PRIVATE_PROMPT_CANARY",
        "PRIVATE_TOOLS_CANARY",
        "PRIVATE_DELEGATE_CANARY",
        "PRIVATE_SECRET_CANARY",
    ):
        assert canary not in serialized


@pytest.mark.parametrize(
    ("role", "name"),
    [
        ("Upper", "Name"),
        ("x" * 65, "Name"),
        ("valid", ""),
        ("valid", "x" * 129),
        ("valid", "bad\nname"),
        # #357: U+2028 LINE SEPARATOR (Zl) / U+2029 PARAGRAPH SEPARATOR (Zp)
        # are not category C, but let a configured name render a forged
        # second line in the agent picker.
        ("valid", "Kitchen\u2028Attacker"),
        ("valid", "Kitchen\u2029Attacker"),
    ],
)
def test_catalog_rejects_invalid_identity(role, name):
    with pytest.raises(VoiceAgentCatalogError):
        build_voice_agent_catalog({
            role: _cfg(role, name, ["ha_voice"]),
        })


def test_catalog_rejects_mapping_role_mismatch():
    with pytest.raises(VoiceAgentCatalogError, match="invalid_role"):
        build_voice_agent_catalog({
            "butler": _cfg("concierge", "Gary", ["ha_voice"]),
        })


def test_catalog_accepts_empty_catalog():
    assert build_voice_agent_catalog({}) == {
        "schema_version": 1,
        "agents": [],
    }


def test_catalog_accepts_exactly_twenty_entries():
    configs = {
        f"role_{index}": _cfg(
            f"role_{index}", f"Agent {index}", ["ha_voice"],
        )
        for index in range(20)
    }

    catalog = build_voice_agent_catalog(configs)

    assert len(catalog["agents"]) == 20


def test_catalog_rejects_overflow_without_truncating():
    """Pins INV-VOICE-003. Red case demonstrated: truncating to the cap instead of raising fails this test."""
    configs = {
        f"role_{index}": _cfg(
            f"role_{index}", f"Agent {index}", ["ha_voice"],
        )
        for index in range(21)
    }

    with pytest.raises(VoiceAgentCatalogError, match="too_many_agents"):
        build_voice_agent_catalog(configs)


def _catalog_channel(
    configs: dict[str, SimpleNamespace],
    secret: str,
    *,
    sse_enabled: bool = True,
    ws_enabled: bool = False,
) -> VoiceChannel:
    return VoiceChannel(
        bus=MessageBus(),
        default_agent="butler",
        webhook_secret=secret,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs=configs,
        idle_timeout=300,
        sse_enabled=sse_enabled,
        ws_enabled=ws_enabled,
    )


def _signed_headers(secret: str) -> dict[str, str]:
    signature = hmac.new(
        secret.encode(), b"", hashlib.sha256,
    ).hexdigest()
    return {"X-Webhook-Signature": signature}


@pytest.fixture
async def catalog_client():
    secret = "catalog-test-secret"
    configs = {
        "butler": _cfg("butler", "Tina", ["ha_voice"]),
    }
    channel = _catalog_channel(configs, secret)
    app = web.Application()
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client, secret, configs


@pytest.mark.asyncio
async def test_catalog_requires_secret_and_valid_empty_body_hmac(
    catalog_client,
):
    client, secret, _configs = catalog_client

    ok = await client.get(
        "/api/voice/agents",
        headers=_signed_headers(secret),
    )
    assert ok.status == 200
    assert ok.headers["Cache-Control"] == "no-store"
    assert await ok.json() == {
        "schema_version": 1,
        "agents": [{"role": "butler", "name": "Tina"}],
    }

    missing = await client.get("/api/voice/agents")
    wrong = await client.get(
        "/api/voice/agents",
        headers={"X-Webhook-Signature": "0" * 64},
    )
    assert missing.status == wrong.status == 401
    assert await missing.json() == await wrong.json() == {
        "error": "invalid signature",
    }


@pytest.mark.asyncio
async def test_catalog_rejects_non_ascii_signature_as_generic_401(
    catalog_client,
):
    client, _secret, _configs = catalog_client

    response = await client.get(
        "/api/voice/agents",
        headers={"X-Webhook-Signature": "é" * 64},
    )

    assert response.status == 401
    assert await response.json() == {"error": "invalid signature"}


@pytest.mark.asyncio
async def test_catalog_fails_closed_when_server_secret_is_empty():
    channel = _catalog_channel(
        {"butler": _cfg("butler", "Tina", ["ha_voice"])},
        "",
    )
    app = web.Application()
    channel.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/api/voice/agents",
            headers=_signed_headers("any-client-secret"),
        )
        body = await response.json()

    assert response.status == 401
    assert body == {"error": "invalid signature"}


@pytest.mark.asyncio
async def test_invalid_live_metadata_returns_generic_unavailable(
    caplog,
):
    secret = "catalog-test-secret"
    private_canary = "PRIVATE_INVALID_NAME_CANARY"
    channel = _catalog_channel(
        {
            "butler": _cfg(
                "butler", f"{private_canary}\nprivate", ["ha_voice"],
            ),
        },
        secret,
    )
    app = web.Application()
    channel.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/api/voice/agents",
            headers=_signed_headers(secret),
        )
        body = await response.json()

    assert response.status == 503
    assert body == {"error": "voice catalog unavailable"}
    assert private_canary not in caplog.text


@pytest.mark.asyncio
async def test_catalog_reads_live_mapping_on_every_request(catalog_client):
    client, secret, configs = catalog_client
    headers = _signed_headers(secret)

    first = await client.get("/api/voice/agents", headers=headers)
    assert await first.json() == {
        "schema_version": 1,
        "agents": [{"role": "butler", "name": "Tina"}],
    }

    configs["butler"].character.name = "Tina Two"
    configs["concierge"] = _cfg(
        "concierge", "Gary", ["ha_voice"],
    )

    second = await client.get("/api/voice/agents", headers=headers)
    assert await second.json() == {
        "schema_version": 1,
        "agents": [
            {"role": "butler", "name": "Tina Two"},
            {"role": "concierge", "name": "Gary"},
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sse_enabled", "ws_enabled"),
    [(True, False), (False, True)],
)
async def test_catalog_mounts_when_either_voice_transport_is_enabled(
    sse_enabled,
    ws_enabled,
):
    secret = "catalog-test-secret"
    channel = _catalog_channel(
        {"butler": _cfg("butler", "Tina", ["ha_voice"])},
        secret,
        sse_enabled=sse_enabled,
        ws_enabled=ws_enabled,
    )
    app = web.Application()
    channel.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/api/voice/agents",
            headers=_signed_headers(secret),
        )

    assert response.status == 200


@pytest.mark.asyncio
async def test_catalog_is_not_mounted_when_voice_is_disabled():
    channel = _catalog_channel(
        {"butler": _cfg("butler", "Tina", ["ha_voice"])},
        "catalog-test-secret",
        sse_enabled=False,
        ws_enabled=False,
    )
    app = web.Application()
    channel.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/voice/agents")

    assert response.status == 404


@pytest.mark.asyncio
async def test_catalog_rejects_post(catalog_client):
    client, secret, _configs = catalog_client

    response = await client.post(
        "/api/voice/agents",
        headers=_signed_headers(secret),
    )

    assert response.status == 405
