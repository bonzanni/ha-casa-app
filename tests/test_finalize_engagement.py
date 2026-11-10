"""Tests for the shared _finalize_engagement helper in tools.py."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


def _driver_double():
    """A driver double whose ``cancel`` is awaitable, like ``InCasaDriver``'s."""
    d = MagicMock()
    d.cancel = AsyncMock()
    return d


class TestFinalizeEngagement:
    async def test_happy_path_notifies_ellen(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t",
            origin={"role": "assistant", "channel": "telegram", "chat_id": "12345"},
        )

        telegram = MagicMock()
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        driver = _driver_double()

        await _finalize_engagement(
            rec, outcome="completed", text="summary",
            next_steps=[], driver=driver,
        )

        # NOTIFICATION sent to Ellen
        bus.notify.assert_awaited_once()
        # Driver cancelled
        driver.cancel.assert_awaited_once_with(rec)
        # Record status is completed
        assert rec.status == "completed"
        assert rec.completed_at is not None

    async def test_cancel_outcome_uses_cancel_path(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
        )

        cm = MagicMock()
        cm.get.return_value = MagicMock()
        bus = MagicMock()
        bus.notify = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        driver = _driver_double()

        await _finalize_engagement(
            rec, outcome="cancelled", text="user cancelled",
            next_steps=[], driver=driver,
        )
        assert rec.status == "cancelled"
        driver.cancel.assert_awaited_once_with(rec)


# --- G4 D5 (v0.96.0): typed finalize result --------------------------------


@pytest.mark.parametrize("outcome", ["cancelled", "error"])
async def test_a_job_s_cancelled_or_failed_end_carries_its_last_progress_once(
        tmp_path, outcome):
    """INV-BGJOB-022: a job's ``cancelled`` or ``error`` end tells what it had
    done — the funnel appends ``Last progress: <summary>`` to the text the
    resident receives, once; a completed end does not."""
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    texts = []
    for out in (outcome, "completed"):
        reg = EngagementRegistry(tombstone_path=str(tmp_path / f"{out}.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t",
            origin={"role": "assistant", "channel": "telegram",
                    "job": {"title": "Rows", "last_summary": "12 of 40 rows"}})
        cm = MagicMock()
        cm.get.return_value = MagicMock()
        bus = MagicMock()
        bus.notify = AsyncMock()
        init_tools(channel_manager=cm, bus=bus, specialist_registry=MagicMock(),
                   mcp_registry=MagicMock(), trigger_registry=MagicMock(),
                   engagement_registry=reg)
        await _finalize_engagement(rec, outcome=out, text="Stopped.",
                                   next_steps=[], driver=_driver_double())
        texts.append(bus.notify.await_args.args[0].content.text)
    told, done_told = texts
    assert told.count("Last progress: 12 of 40 rows") == 1
    assert "Last progress" not in done_told


def test_finalize_result_enum_exists():
    from tools import FinalizeResult
    assert {r.name for r in FinalizeResult} >= {
        "FINALIZED", "ALREADY_TERMINAL", "PRECONDITION_FAILED",
        "PERSIST_FAILED"}
    # Truthiness contract preserved for existing boolean callers: only a
    # won finalize is truthy.
    assert bool(FinalizeResult.FINALIZED) is True
    assert bool(FinalizeResult.ALREADY_TERMINAL) is False
    assert bool(FinalizeResult.PERSIST_FAILED) is False
    assert bool(FinalizeResult.PRECONDITION_FAILED) is False


def test_terminal_hook_abort_raises_and_leaves_live():
    """G4 D2: the registry evaluates the terminal hook INSIDE the mutation
    critical section; an abort leaves the record live."""
    import asyncio
    from engagement_registry import (
        EngagementRegistry, TerminalPreconditionFailed)

    async def run(tmp_path="/tmp"):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            reg = EngagementRegistry(tombstone_path=td + "/e.json")
            rec = await reg.create(
                kind="executor", role_or_type="configurator",
                driver="in_casa", task="t",
                origin={"role": "assistant"})
            calls = []
            def hook():
                calls.append(1)
                return "unread_inbound depth=1"
            try:
                await reg.try_transition_terminal(
                    rec.id, "completed", strict=True, terminal_hook=hook)
            except TerminalPreconditionFailed as exc:
                assert "unread_inbound" in str(exc)
            else:
                raise AssertionError("expected TerminalPreconditionFailed")
            assert calls == [1]
            assert rec.status not in ("completed", "cancelled", "error")
            # hook returning None proceeds
            won = await reg.try_transition_terminal(
                rec.id, "completed", strict=True, terminal_hook=lambda: None)
            assert won is True
            assert rec.status == "completed"
    asyncio.get_event_loop().run_until_complete(run()) if False else None
    asyncio.run(run())


def _repo_root():
    return Path(__file__).resolve().parent.parent


def _module_tree(rel):
    src = (_repo_root() / rel).read_text(encoding="utf-8")
    return src, ast.parse(src)


def test_the_finalization_citations_that_are_not_about_the_rule_stay():
    """Regression, green at base (not a red case): the #632 suite's comment
    cites engagement-finalization.md correctly for INV-ENG-010 and must not
    be swept up in a retarget of the rule pointers."""
    _src, tree = _module_tree("tests/test_emit_completion_tool.py")
    classes = [
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "TestEmitCompletionCancellation"
    ]
    assert len(classes) == 1
    assert "docs/architecture/engagement-finalization.md" in ast.get_docstring(classes[0])
