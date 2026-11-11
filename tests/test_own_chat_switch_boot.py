"""The boot step that applies an agent's pending bot switch.

A switch retires the agent's old bot and lets the new one bind, so it must
run once every durable store it reads is loaded and the tools are wired,
and before anything can start a bot, a worker, the HTTP server, the channels
or the scheduler.
"""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap

import own_voice


def _call_lines(func) -> dict[str, list[int]]:
    """Every call in *func*'s body, by dotted name, with its line numbers."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    lines: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            lines.setdefault(ast.unparse(node.func), []).append(node.lineno)
    return lines


def test_switches_are_applied_after_the_stores_and_tools_and_before_any_bot_or_worker_starts():
    import casa_core

    calls = _call_lines(casa_core.main)
    assert calls.get("_apply_own_chat_switches"), "main() never applies the switches"
    assert len(calls["_apply_own_chat_switches"]) == 1
    (switch,) = calls["_apply_own_chat_switches"]

    for earlier in ("engagement_registry.load", "init_tools"):
        assert calls.get(earlier), f"{earlier} not found in main()"
        assert switch > max(calls[earlier]), f"switch applied before {earlier}"

    for later in ("_build_telegram_transport", "_pse.start_worker", "site.start",
                  "channel_manager.start_all", "_reconcile_own_chats",
                  "scheduler.start"):
        assert calls.get(later), f"{later} not found in main()"
        assert switch < min(calls[later]), f"switch applied after {later}"


async def test_a_failing_switch_step_never_stops_the_boot(monkeypatch, caplog):
    import casa_core

    applied: list[str] = []

    async def _apply():
        applied.append("applied")
    monkeypatch.setattr(own_voice, "apply_switches_at_boot", _apply, raising=False)
    await casa_core._apply_own_chat_switches()
    assert applied == ["applied"]

    async def _boom():
        raise RuntimeError("store unreadable")
    monkeypatch.setattr(own_voice, "apply_switches_at_boot", _boom, raising=False)
    with caplog.at_level(logging.ERROR):
        await casa_core._apply_own_chat_switches()
    assert "own chats' bot switches not applied at boot" in caplog.text
