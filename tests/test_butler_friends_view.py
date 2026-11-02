"""The butler's view: a ``friends`` reader is served the ``friends`` ledger.

The butler speaks over voice, which reads at ``friends``. While a role that
reads at ``friends`` has a served-view budget, Casa maintains a ``friends``
ledger (``memory.ledgers.configure_roles`` with
``sensitivity.clearances_served``, as Casa's boot calls it), and a
voice turn's ``_build_options`` serves the butler that ledger: a house fact
filed at ``friends`` reaches its prompt, a ``private`` one never does. Voice
still captures nothing. Real store and the real ``Agent._process`` with the
CLI client stubbed.
"""
from __future__ import annotations

import secrets
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from memory import ledgers
from memory_serving_support import add, fact, ledger_store, wire
from session_reg_helpers import (
    RESIDENT_DIGEST, resident_prov, resident_role_id,
)
from session_registry import SessionRegistry
from test_memory_frozen_view import _Client

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.unit]

AGENTS = (Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt"
          / "casa" / "defaults" / "agents")


def _shipped(role: str) -> SimpleNamespace:
    raw = yaml.safe_load((AGENTS / role / "runtime.yaml").read_text())
    return SimpleNamespace(
        channels=list(raw.get("channels") or []),
        memory=SimpleNamespace(token_budget=raw["memory"]["token_budget"]))


def test_the_shipped_fleet_maintains_a_friends_ledger_for_the_butler(
        monkeypatch):
    from sensitivity import clearances_served
    monkeypatch.setattr(ledgers, "_served", frozenset())
    fleet = [_shipped(r) for r in ("assistant", "butler", "concierge")]
    assert clearances_served(fleet[1].channels) == ["friends"]
    assert sorted(clearances_served(fleet[0].channels)) == [
        "private", "private"]
    assert ledgers.configure_roles(fleet, clearances_served) == (
        "private", "friends")
    # A butler served no view leaves friends unmaintained: the concierge
    # also speaks over voice, but its budget is 0.
    fleet[1].memory.token_budget = 0
    assert ledgers.configure_roles(fleet, clearances_served) == ("private",)


def _butler(tmp_path) -> Agent:
    cfg = AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT, role="butler",
        model="claude-haiku-4-5", system_prompt="You are the butler.",
        character=CharacterConfig(name="Butler"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=_shipped("butler").memory.token_budget,
                            read_strategy="cached"),
        channels=["ha_voice"],
        role_id=resident_role_id("butler"), kind="resident",
        binding_digest=RESIDENT_DIGEST,
        speaker_provenance=resident_prov("butler"),
    )
    return Agent(config=cfg,
                 session_registry=SessionRegistry(str(tmp_path / "s.json")),
                 mcp_registry=McpServerRegistry(),
                 channel_manager=ChannelManager())


def _voice() -> BusMessage:
    return BusMessage(type=MessageType.REQUEST, source="voice",
                      target="butler", content="when do the bins go out?",
                      channel="voice", context={"chat_id": "kitchen"})


@pytest.mark.parametrize("maintained", [True, False])
async def test_a_friends_fact_reaches_the_butler_and_a_private_one_does_not(
        tmp_path, monkeypatch, maintained):
    house, secret = secrets.token_hex(8), secrets.token_hex(8)
    store = ledger_store(tmp_path, [fact("bins", f"Tuesday {house}")],
                         ledger="friends")
    add(store, [fact("safe", f"code {secret}")])
    wire(monkeypatch, store)
    monkeypatch.setattr(ledgers, "_served", frozenset(
        {"friends"} if maintained else ()))
    _Client.options = []
    with patch("sdk_client_pool._default_make_client", _Client):
        butler = _butler(tmp_path)
        await butler._process(_voice())
        await butler.aclose()
    [options] = _Client.options
    assert (house in options.system_prompt) is maintained
    assert secret not in options.system_prompt
    # The voice turn stored nothing.
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
