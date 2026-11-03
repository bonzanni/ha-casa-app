"""Guard: residents can actually reach long-term memory on-demand.

Regression — a memory read tool was once missing from the resident
`tools.allowed` lists (2026-07-09 diagnosis), so a resident on a resumed turn
and the voice channel had no path to what was said. No turn recalls on its
own: the served view holds a ledger, and the saved conversation is reached
through the memory tools. So every resident that serves a person holds
`search_memory`, every prompt that names a memory tool belongs to a role that
holds it, and the tools' `unavailable` message says WHEN to tell (#1116).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import tools
from memory import capture
from memory_tools_support import new_store

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[1]
AGENTS = REPO / "casa" / "rootfs" / "opt" / "casa" / "defaults" / "agents"
PREFIX = "mcp__casa-framework__"
MEMORY_TOOLS = ("search_memory", "browse_memory_day", "open_memory",
                "correct_memory")


def _allowed(role: str) -> list[str]:
    data = yaml.safe_load((AGENTS / role / "runtime.yaml").read_text(encoding="utf-8"))
    return (data.get("tools") or {}).get("allowed") or []


def test_assistant_holds_every_memory_tool() -> None:
    assert {PREFIX + t for t in MEMORY_TOOLS} <= set(_allowed("assistant"))


def test_butler_holds_the_memory_reads() -> None:
    """The voice channel routes to the butler; voice captures nothing, so it
    has no message of his to mark and no correct_memory."""
    allowed = set(_allowed("butler"))
    assert {PREFIX + t for t in MEMORY_TOOLS[:3]} <= allowed
    assert PREFIX + "correct_memory" not in allowed


def test_butler_prompt_guides_memory_use() -> None:
    sys_md = (AGENTS / "butler" / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "search_memory" in sys_md
    assert "long-term memory" in sys_md.lower()


def test_a_prompt_names_only_memory_tools_its_role_holds() -> None:
    offenders = []
    for runtime in AGENTS.rglob("runtime.yaml"):
        role_dir = runtime.parent
        prompts = (list((role_dir / "prompts").glob("*.md"))
                   if (role_dir / "prompts").is_dir() else [])
        text = "".join(p.read_text(encoding="utf-8") for p in prompts)
        data = yaml.safe_load(runtime.read_text(encoding="utf-8"))
        allowed = (data.get("tools") or {}).get("allowed") or []
        offenders += [(role_dir.name, t) for t in MEMORY_TOOLS
                      if t in text and PREFIX + t not in allowed]
    assert offenders == []


_UNAVAILABLE_POLICY = (
    "Do NOT say the information doesn't exist or that you don't have it.",
    "If a person asked for something that needs memory, tell them memory "
    "couldn't be checked.",
    "On a check nobody asked for, this alone is not a reason to send "
    "anything; still report what the task itself requires.",
)


@pytest.mark.parametrize("tool,args", [
    ("search_memory", {"query": "What must I act on today?"}),
    ("browse_memory_day", {"date": "yesterday"}),
    ("open_memory", {"ids": ["o0000000000000000"]}),
])
@pytest.mark.parametrize("arm", ["no_store", "store_down"])
async def test_unavailable_memory_qualifies_when_to_tell(
        monkeypatch, tmp_path, tool, args, arm) -> None:
    """#1116: the payload is exactly {status, message}, never an empty
    result, and the message says when the user is told."""
    store = None
    if arm == "store_down":
        store = new_store(tmp_path)
        store._unavailable_reason = "a simulated outage"
    monkeypatch.setitem(capture._wiring, "store", store)
    monkeypatch.setattr(tools, "_snapshot_origin",
                        lambda: {"role": "assistant", "channel": "telegram"})
    result = await getattr(tools, tool).handler(args)
    payload = json.loads(result["content"][0]["text"])
    assert sorted(payload) == ["message", "status"]
    assert payload["status"] == "unavailable"
    message = payload["message"]
    assert message.count("could not be checked") == 1
    assert [message.count(c) for c in _UNAVAILABLE_POLICY] == [1, 1, 1]
