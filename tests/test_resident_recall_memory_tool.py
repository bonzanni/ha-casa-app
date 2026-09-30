"""Guard: residents can actually reach long-term memory on-demand.

Regression — the `recall_memory` pull tool was missing from the resident
`tools.allowed` lists (2026-07-09 diagnosis). Because `_plan_load` auto-injects
a recall ONLY on a fresh non-voice session, a resident on a RESUMED turn (the
steady-state hourly heartbeat, which resumes inside the 12 h freshness window)
and the VOICE channel (which never auto-recalls and, at `friends` clearance,
gets no overlay) had NO memory-read path at all. Being scheduled does not by
itself mean resumed: the weekday morning briefing fires >= 24 h apart, so it
starts fresh and DOES receive auto-recall on its own prompt text. See memory
`recall-memory-tool-missing-bug`.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
AGENTS = REPO / "casa" / "rootfs" / "opt" / "casa" / "defaults" / "agents"
RECALL_TOOL = "mcp__casa-framework__recall_memory"


def _allowed(role: str) -> list[str]:
    data = yaml.safe_load((AGENTS / role / "runtime.yaml").read_text(encoding="utf-8"))
    return (data.get("tools") or {}).get("allowed") or []


def test_assistant_allows_recall_memory() -> None:
    """Ellen's steady-state heartbeat runs on a resumed telegram session (no
    auto-inject), so `recall_memory` is her only memory-read path there."""
    assert RECALL_TOOL in _allowed("assistant")


def test_butler_allows_recall_memory() -> None:
    """The voice channel routes to butler (casa_core default_agent) and voice
    never auto-recalls — the pull tool is its only long-term-memory path."""
    assert RECALL_TOOL in _allowed("butler")


def test_butler_prompt_guides_recall_usage() -> None:
    """X1 (2026-07-09): butler holds recall_memory (v0.59.2) but was memory-blind
    on voice because its prompt never told it to use the tool. Its system prompt
    must now guide recall (or voice stays memory-blind despite the grant)."""
    sys_md = (AGENTS / "butler" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "recall_memory" in sys_md, "butler prompt must reference recall_memory"
    low = sys_md.lower()
    assert "long-term memory" in low or "household" in low, (
        "butler prompt must tell it it can read long-term/household memory"
    )


def test_prompt_referenced_recall_memory_is_allowed() -> None:
    """Invariant: any agent whose prompt text names `recall_memory` must have
    the tool in its allowed list, or the instruction is unfulfillable."""
    offenders = []
    for runtime in AGENTS.rglob("runtime.yaml"):
        role_dir = runtime.parent
        prompts = list((role_dir / "prompts").glob("*.md")) if (role_dir / "prompts").is_dir() else []
        mentions = any("recall_memory" in p.read_text(encoding="utf-8") for p in prompts)
        if not mentions:
            continue
        data = yaml.safe_load(runtime.read_text(encoding="utf-8"))
        allowed = (data.get("tools") or {}).get("allowed") or []
        if RECALL_TOOL not in allowed:
            offenders.append(role_dir.name)
    assert not offenders, (
        f"agents reference recall_memory in prompts but do not allow it: {offenders}"
    )


# ---------------------------------------------------------------------------
# #1116 red case (c): both `recall_memory` `unavailable` arms say WHEN to tell.
#
# At the base both arms told the model, unconditionally, to tell the user that
# memory could not be checked — so a scheduled check nobody asked for (the
# weekday briefing) turned a memory outage into a message. The payload shape is
# the three-outcome contract (INV-MEM-001/-010) and does not change: exactly
# {status, message}, status "unavailable", never a `memory` key, never "no
# record". Only the message qualifies when the user is told. This pins the TEXT
# the model receives, not that it obeys it.
# ---------------------------------------------------------------------------

_UNAVAILABLE_POLICY = (
    "Do NOT say the information doesn't exist or that you don't have it.",
    "If a person asked for something that needs memory, tell them memory "
    "couldn't be checked.",
    "On a check nobody asked for, this alone is not a reason to send "
    "anything; still report what the task itself requires.",
)

_UNAVAILABLE_REASONS = {
    "no_backend":
        "Long-term memory could not be checked (no memory backend).",
    "backend_down":
        "Long-term memory could not be checked (backend unavailable).",
}


@pytest.mark.parametrize("arm", ["no_backend", "backend_down"])
async def test_unavailable_recall_qualifies_when_to_tell(monkeypatch, arm) -> None:
    import json
    from unittest.mock import AsyncMock

    import agent as agent_mod
    import tools
    from recall_health import reset_recall_breakers

    reset_recall_breakers()
    recall = AsyncMock(side_effect=RuntimeError("probe_down"))

    class _Down:
        recall_items = recall

    monkeypatch.setattr(
        agent_mod, "active_semantic_memory",
        None if arm == "no_backend" else _Down(), raising=False,
    )
    monkeypatch.setattr(
        tools, "_snapshot_origin",
        lambda: {"role": "assistant", "channel": "telegram"},
    )
    try:
        result = await tools.recall_memory.handler(
            {"query": "What must I act on today?"})
    finally:
        reset_recall_breakers()

    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "text"
    payload = json.loads(result["content"][0]["text"])

    assert sorted(payload) == ["message", "status"]
    assert [payload["status"]].count("unavailable") == 1

    message = payload["message"]
    reason = _UNAVAILABLE_REASONS[arm]
    assert message.count(reason) == 1
    assert message.count("could not be checked") == 1
    assert [message.count(c) for c in _UNAVAILABLE_POLICY] == [1, 1, 1]
    assert message == reason + " " + " ".join(_UNAVAILABLE_POLICY)

    assert recall.await_count == int(arm == "backend_down")
