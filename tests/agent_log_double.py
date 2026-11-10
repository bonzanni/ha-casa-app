"""A stand-in for ``agent_logs.AgentLogs`` that records every line.

Its ``log`` is synchronous, like the real sink's: a producer that awaited it
would fail with a TypeError on ``await None``, so a test using this double
also pins that producers never await the log."""

from __future__ import annotations

from typing import Any


class RecordingLogs:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.lines: list[tuple[Any, str, str]] = []

    def log(self, key: Any, run: str, text: str) -> None:
        if self.enabled:
            self.lines.append((key, run, text))

    def texts(self, key: Any = None) -> list[str]:
        """The logged texts, of one key when given."""
        return [t for k, _r, t in self.lines if key is None or k == key]

    def words(self) -> list[str]:
        """The logged texts that are neither a step nor a marker line."""
        marks = ("🔧 ", "▶ ", "✓ ", "⚠️ ", "✅ ", "🛑 ", "❌ ", "📊 ", "↻ ")
        return [t for t in self.texts() if not t.startswith(marks)]
