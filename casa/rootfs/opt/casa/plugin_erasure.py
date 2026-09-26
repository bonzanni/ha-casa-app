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
import logging
import threading
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

logger = logging.getLogger(__name__)

Verdict = Literal["complete", "incomplete", "unreadable"]
# What an episode can end with: the eraser's own verdict, or why it gave none.
Outcome = Literal["complete", "incomplete", "unreadable", "error", "no_call",
                  "timed_out", "not_dispatched"]

# How long an episode waits for the eraser's result. An eraser withdraws a
# consent per provider, so this is generous; a turn that ends without calling
# the eraser reports at once (``turn_ended``) and never waits this out.
ERASE_WAIT_S = 900.0

NO_CALL_REPORT = "The erase turn ended without a result from the plugin's eraser."
TIMED_OUT_REPORT = "The plugin's eraser did not report within the time allowed."
NOT_DISPATCHED_REPORT = ("Casa could not start the erase turn (no agent to run "
                         "the plugin's eraser, or the operator's Telegram chat "
                         "is not available).")

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

    def resolve_turn_end(self, artifact_id: str) -> None:
        """The erase turn for *artifact_id* ended: every key it armed that is
        still unanswered resolves as "no call"."""
        with self._lock:
            keys = [k for k in self._futures if k[0] == artifact_id]
        for key in keys:
            self.resolve(*key, error=None, text=None, _no_call=True)

    def is_armed_name(self, tool_name: str) -> bool:
        with self._lock:
            return any(name == tool_name for _a, name in self._futures)

    def resolve(self, artifact_id: str, tool_name: str, *,
                text: str | None = None, error: str | None = None,
                _no_call: bool = False) -> bool:
        """Hand the result to the waiting episode. False when the key is not
        armed or already resolved."""
        with self._lock:
            fut = self._futures.get((artifact_id, tool_name))
        if fut is None or fut.done():
            return False
        payload = {"text": text, "error": error}
        if _no_call:
            payload["no_call"] = True
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

    def has_complete(self, subject: str, artifact_id: str) -> bool:
        with self._lock:
            row = self._rows.get((subject, artifact_id))
            return row is not None and row[0] == "complete"

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


def turn_ended(context: dict | None) -> None:
    """Called from the agent's turn ``finally`` for every turn (#1046):
    on an erase-marked turn, resolve whatever its eraser did not answer.
    Synchronous; never raises."""
    try:
        artifact = erase_turn_artifact(context)
        if artifact:
            WATCH.resolve_turn_end(artifact)
    except Exception:  # noqa: BLE001 — the turn's reply is already produced
        logger.exception("erase turn-end report failed")
    return None


@dataclass(frozen=True)
class EraseSpec:
    """One plugin to erase: its registry name, the artifact the operator's tap
    named, its registry ``targets``, the eraser's full tool name on each of
    its MCP servers, and whether the plugin declared it protected."""
    name: str
    artifact_id: str
    targets: tuple
    tool_names: tuple
    protected: bool
    tool: str = ""                  # the declared (bare) tool name
    summary: "str | None" = None    # its protected-tool summary, if any


@dataclass(frozen=True)
class ErasureOutcome:
    name: str
    artifact_id: str
    verdict: Outcome
    report: str


_dispatch: "Callable[[str, str, dict], Awaitable[bool]] | None" = None


def configure(*, dispatch: "Callable[[str, str, dict], Awaitable[bool]]") -> None:
    """Wire the turn seam (casa_core's setup dispatch: a Casa-authored
    Telegram-shaped turn addressed to the configured operator)."""
    global _dispatch
    _dispatch = dispatch


def _instruction(tool_name: str) -> str:
    return (f"[casa plugin erase] The operator chose to erase this plugin's data "
            f"before it is uninstalled. Call the tool `{tool_name}` now, exactly "
            "once, with no arguments, and reply with the text it returns. Do "
            "not call any other tool.")


def _mint_grant(spec: EraseSpec, role: str, operator: tuple[int, int]) -> None:
    """The operator's Erase tap is the authorization for exactly this call:
    one grant per server name, for the no-argument call, bound to the
    executing role and the tap's artifact. Single-use and TTL-bound like any
    grant; the eraser consumes it and no challenge is posted."""
    import authz_grants
    chat_id, user_id = operator
    args_hash = authz_grants.canonical_args_hash({})
    for tool_name in spec.tool_names:
        authz_grants.GRANTS.mint(authz_grants.GrantKey(
            operator_id=user_id, chat_id=chat_id, enforcement_role=role,
            artifact_id=spec.artifact_id, tool_name=tool_name,
            args_hash=args_hash, engagement_id=""))


def _outcome(spec: EraseSpec, payload: dict | None) -> ErasureOutcome:
    if payload is None:
        return ErasureOutcome(spec.name, spec.artifact_id, "timed_out",
                              TIMED_OUT_REPORT)
    if payload.get("no_call"):
        return ErasureOutcome(spec.name, spec.artifact_id, "no_call",
                              NO_CALL_REPORT)
    if payload.get("error") is not None:
        return ErasureOutcome(spec.name, spec.artifact_id, "error",
                              _bounded(str(payload["error"])))
    verdict, report = parse_erase_result(payload.get("text"))
    return ErasureOutcome(spec.name, spec.artifact_id, verdict, report)


async def _erase_one(spec: EraseSpec, operator: tuple[int, int]) -> ErasureOutcome:
    import plugin_dispatch
    entry = {"targets": list(spec.targets)}
    _tier, exec_role = plugin_dispatch.execution_target(entry)
    role, instruction = plugin_dispatch.compose(
        entry, _instruction(spec.tool_names[0]))
    if role is None or exec_role is None or _dispatch is None:
        return ErasureOutcome(spec.name, spec.artifact_id, "not_dispatched",
                              NOT_DISPATCHED_REPORT)
    futures = [WATCH.arm(spec.artifact_id, t) for t in spec.tool_names]
    try:
        if spec.protected:
            _mint_grant(spec, exec_role, operator)
        accepted = await _dispatch(role, instruction, {
            "synthetic": "plugin_erase", "plugin_erase_target": exec_role,
            "plugin_erase_artifact": spec.artifact_id})
        if not accepted:
            return ErasureOutcome(spec.name, spec.artifact_id, "not_dispatched",
                                  NOT_DISPATCHED_REPORT)
        done, _pending = await asyncio.wait(
            futures, timeout=ERASE_WAIT_S, return_when=asyncio.FIRST_COMPLETED)
        # Prefer a real result over a turn-end "no call" on another server.
        payloads = [f.result() for f in done]
        real = [p for p in payloads if not p.get("no_call")]
        return _outcome(spec, (real or payloads or [None])[0])
    finally:
        for t in spec.tool_names:
            WATCH.disarm(spec.artifact_id, t)


async def run_erase_episode(specs: list[EraseSpec],
                            operator: tuple[int, int]) -> list[ErasureOutcome]:
    """Run each plugin's eraser in turn and record what it reported,
    stopping at the first erasure that is not complete: an uninstall
    proceeds only when every one completed, so running the rest would erase
    data the operator then keeps a plugin for. Returns the outcomes of the
    plugins that ran."""
    outcomes: list[ErasureOutcome] = []
    for spec in specs:
        out = await _erase_one(spec, operator)
        verdict = out.verdict if out.verdict in ("complete", "incomplete") \
            else "unreadable"
        RECORDS.put(f"plugin:{spec.name}", spec.artifact_id, verdict, out.report)
        outcomes.append(out)
        if out.verdict != "complete":
            break
    return outcomes
