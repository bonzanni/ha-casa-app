"""#1046: erasing a plugin's data at uninstall, through the eraser the plugin
declares (``casa.eraseTool``).

Casa never erases anything itself: only the plugin knows what its data is and
what must be revoked at its providers. Casa *sequences* the eraser — it runs
it on the operator's Erase tap, before removal, while the plugin's server
still runs — and removes the plugin only when the eraser reported a complete
erasure. This module holds the result convention, the capture watch the
result broker's hooks resolve, and the single-use erasure records the
finishing ``plugin_remove`` / ``specialist_uninstall`` call consumes.

Everything here is in memory by design: a restart mid-erasure leaves the
plugin installed, which is the safe side, and the operator's next uninstall
asks again.
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Literal

Verdict = Literal["complete", "incomplete", "unreadable"]

# The report is relayed verbatim to the operator; bound it so a runaway
# eraser cannot flood the configurator's context or a Telegram message.
MAX_REPORT_CHARS = 4000
TRUNCATED = " [truncated]"


def _bounded(text: str) -> str:
    if len(text) > MAX_REPORT_CHARS:
        return text[:MAX_REPORT_CHARS] + TRUNCATED
    return text


def parse_erase_result(text: str | None) -> tuple[Verdict, str]:
    """The eraser's result, folded to text by the result broker, read as
    ``{"erasure": "complete"|"incomplete", "report": str}``. Any other shape
    is ``unreadable`` — never complete — and its raw text is the report."""
    if text is None:
        return "unreadable", ""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("report"), str):
        verdict = parsed.get("erasure")
        if verdict in ("complete", "incomplete"):
            return verdict, _bounded(parsed["report"])
    return "unreadable", _bounded(text)


class EraseWatch:
    """The erase results an episode is waiting for, keyed by the exact
    ``(artifact_id, full tool name)`` it armed. The result broker's hooks
    resolve an armed key from an erase-marked turn; nothing else does."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._futures: dict[tuple[str, str], asyncio.Future] = {}

    def arm(self, artifact_id: str, tool_name: str) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        with self._lock:
            self._futures[(artifact_id, tool_name)] = fut
        return fut

    def disarm(self, artifact_id: str, tool_name: str) -> None:
        with self._lock:
            self._futures.pop((artifact_id, tool_name), None)

    def is_armed(self, artifact_id: str, tool_name: str) -> bool:
        with self._lock:
            return (artifact_id, tool_name) in self._futures

    def is_armed_name(self, tool_name: str) -> bool:
        with self._lock:
            return any(name == tool_name for _a, name in self._futures)

    def resolve(self, artifact_id: str, tool_name: str, *,
                text: str | None = None, error: str | None = None) -> bool:
        """Hand the result to the waiting episode. False when the key is not
        armed or already resolved."""
        with self._lock:
            fut = self._futures.get((artifact_id, tool_name))
        if fut is None or fut.done():
            return False
        payload = {"text": text, "error": error}
        loop = fut.get_loop()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fut.set_result(payload)
        else:
            loop.call_soon_threadsafe(
                lambda: fut.done() or fut.set_result(payload))
        return True


class ErasureRecords:
    """What each finished erasure reported, keyed ``(subject, artifact_id)``
    where subject is ``plugin:<name>``. A complete record is taken once, by
    the finishing call, for the same artifact the eraser ran against."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], tuple[Verdict, str]] = {}

    def put(self, subject: str, artifact_id: str, verdict: Verdict,
            report: str) -> None:
        with self._lock:
            self._rows[(subject, artifact_id)] = (verdict, report)

    def take_complete(self, subject: str, artifact_id: str) -> str | None:
        """The report of a complete erasure of *subject* at *artifact_id*,
        consumed; ``None`` when there is none."""
        with self._lock:
            row = self._rows.get((subject, artifact_id))
            if row is None or row[0] != "complete":
                return None
            del self._rows[(subject, artifact_id)]
            return row[1]


WATCH = EraseWatch()
RECORDS = ErasureRecords()


def erase_turn_artifact(origin: dict | None) -> str | None:
    """The artifact the operator's tap named, on an erase-marked turn;
    ``None`` on any other turn."""
    if not isinstance(origin, dict) or origin.get("synthetic") != "plugin_erase":
        return None
    artifact = origin.get("plugin_erase_artifact")
    return artifact if isinstance(artifact, str) and artifact else ""
