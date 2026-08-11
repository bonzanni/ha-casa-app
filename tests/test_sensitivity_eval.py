"""Sensitivity eval set: schema (unit) + prompt-accuracy regression (slow)."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from sensitivity import TIERS

EVAL_PATH = Path(__file__).parent / "fixtures" / "sensitivity_eval.jsonl"


def _load_eval() -> list[dict]:
    rows = []
    for line in EVAL_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


@pytest.mark.unit
def test_eval_set_schema_and_values():
    rows = _load_eval()
    assert len(rows) >= 20, "seed eval set should have a reasonable spread"
    for r in rows:
        assert set(r) >= {"fact", "channel", "expected_tier", "note"}
        assert isinstance(r["fact"], str) and r["fact"].strip()
        assert r["expected_tier"] in TIERS
    seen = {r["expected_tier"] for r in rows}
    assert seen == set(TIERS), f"eval set must cover all tiers; missing {set(TIERS) - seen}"


async def _classify_one(fact: str) -> str | None:
    """Run SENSITIVITY_PROMPT over one fact via the one-shot SDK query -> parsed tier."""
    import claude_agent_sdk as sdk
    from sensitivity import SENSITIVITY_PROMPT, parse_tier

    opts = sdk.ClaudeAgentOptions(
        # max_turns=8 / tools=[] / disallowed Bash,Task,Agent mirrors the
        # production classifier (#497 reopen) so the eval measures the
        # shipped configuration.
        system_prompt=SENSITIVITY_PROMPT, max_turns=8,
        tools=[], allowed_tools=[],
        disallowed_tools=["Bash", "Task", "Agent"],
        permission_mode="bypassPermissions",
    )
    text = ""
    async for msg in sdk.query(prompt=fact, options=opts):
        if isinstance(msg, sdk.AssistantMessage):
            for block in getattr(msg, "content", []) or []:
                # text blocks expose a `.text`; tolerate other block kinds
                t = getattr(block, "text", None)
                if isinstance(t, str):
                    text += t
    return parse_tier(text)


@pytest.mark.slow
def test_prompt_accuracy_meets_threshold():
    """Live-LLM regression gate: the prompt must classify the eval set accurately.
    Skipped without credentials (run manually / in a creds-bearing tier)."""
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        pytest.skip("no CLAUDE_CODE_OAUTH_TOKEN; live classifier eval skipped")
    rows = _load_eval()
    correct = 0
    misses = []
    for r in rows:
        got = asyncio.run(_classify_one(r["fact"]))
        if got == r["expected_tier"]:
            correct += 1
        else:
            misses.append((r["fact"][:60], r["expected_tier"], got))
    acc = correct / len(rows)
    assert acc >= 0.90, f"accuracy {acc:.2f} < 0.90; misses={misses}"
