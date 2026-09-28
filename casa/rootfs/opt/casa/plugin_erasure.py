"""#1046: erasing a plugin's data at uninstall, through the eraser the plugin
declares (``casa.eraseTool``) — or, since #1067, its data-only eraser
(``casa.eraseDataOnlyTool``), which keeps what a reinstall needs to carry on
without re-authenticating.

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

# The two eraser kinds (#1067): the operator's "Erase everything" runs the
# plugin's casa.eraseTool; "Erase data, keep sign-ins" its casa.eraseDataOnlyTool.
EVERYTHING = "everything"
DATA_ONLY = "data_only"
Kind = Literal["everything", "data_only"]

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


# #1073: vault items an eraser left because nothing records the plugin
# creating them, so they cannot be told from items the operator made. Each is
# ``(title, found)``: ``True`` when the vault shows it, ``None`` when the vault
# could not be checked. Bounded like the report, for the same reason.
MAX_UNRECORDED_ITEMS = 50
MAX_TITLE_CHARS = 200


def parse_unrecorded_vault_items(text: str | None) -> tuple:
    """The optional ``unrecorded_vault_items`` beside ``erasure`` and
    ``report`` — a list of ``{"title": str, "found": true|null}`` — as
    ``((title, found), ...)``. An entry of any other shape, or one the vault
    says is absent (``found: false``), is left out; no key is ``()``."""
    try:
        parsed = json.loads(text) if text is not None else None
    except ValueError:
        return ()
    items = parsed.get("unrecorded_vault_items") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return ()
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title, found = item.get("title"), item.get("found", False)
        if isinstance(title, str) and title and (found is True or found is None):
            out.append((title[:MAX_TITLE_CHARS], found))
    return tuple(out[:MAX_UNRECORDED_ITEMS])


class EraseWatch:
    """The erase results the episodes are waiting for, keyed by the exact
    ``(run id, full tool name)`` each run armed. A run id is unique to one
    dispatched erase turn and travels on it as ``plugin_erase_episode``, so a
    late result of one run can never answer another, even for the same
    artifact and tool. The result broker's hooks resolve an armed key from an
    erase-marked turn of that run; nothing else does."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._futures: dict[tuple[str, str], asyncio.Future] = {}

    def arm(self, run_id: str, tool_name: str) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        with self._lock:
            self._futures[(run_id, tool_name)] = fut
        return fut

    def disarm(self, run_id: str, tool_name: str) -> None:
        with self._lock:
            self._futures.pop((run_id, tool_name), None)

    def is_armed(self, run_id: str, tool_name: str) -> bool:
        with self._lock:
            return (run_id, tool_name) in self._futures

    def resolve_turn_end(self, run_id: str) -> None:
        """The erase turn of *run_id* ended: every key it armed that is still
        unanswered resolves as "no call"."""
        with self._lock:
            keys = [k for k in self._futures if k[0] == run_id]
        for key in keys:
            self.resolve(*key, error=None, text=None, _no_call=True)

    def resolve(self, run_id: str, tool_name: str, *,
                text: str | None = None, error: str | None = None,
                _no_call: bool = False) -> bool:
        """Hand the result to the waiting episode. False when the key is not
        armed or already resolved."""
        with self._lock:
            fut = self._futures.get((run_id, tool_name))
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


class QuestionIds:
    """The uninstall question currently open for each subject
    (``plugin:<name>`` / ``specialist:<slug>``). Every Erase tap, erase run
    and erasure record carries the id of the question it answers, and only
    the CURRENT question's can authorize or finish an uninstall: asking again
    — or answering Keep or Cancel — replaces it, which voids every older tap,
    run and record at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: dict[str, str] = {}

    def open(self, subject: str) -> str:
        import uuid
        qid = uuid.uuid4().hex
        with self._lock:
            self._current[subject] = qid
        return qid

    def current(self, subject: str) -> "str | None":
        with self._lock:
            return self._current.get(subject)

    def close(self, subject: str, qid: "str | None" = None) -> None:
        """Void the open question (only if it is still *qid*, when given)."""
        with self._lock:
            if qid is None or self._current.get(subject) == qid:
                self._current.pop(subject, None)


class ErasureRecords:
    """What each finished erasure reported, keyed ``(plugin subject,
    artifact_id)`` and stamped with the question its run answered and the
    eraser kind it ran. A complete record is taken once, by the finishing
    call, for the same artifact and the same, still current, question."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str],
                         tuple[Verdict, str, str, str, tuple]] = {}

    def put(self, subject: str, artifact_id: str, verdict: Verdict,
            report: str, question: str, kind: Kind = EVERYTHING,
            unrecorded: tuple = ()) -> None:
        with self._lock:
            self._rows[(subject, artifact_id)] = (verdict, report, question, kind,
                                                  unrecorded)

    def complete_kind(self, subject: str, artifact_id: str,
                      question: str) -> "Kind | None":
        """The eraser kind of a complete erasure of *subject* at
        *artifact_id* for *question*, not consumed; ``None`` when there is
        none."""
        with self._lock:
            row = self._rows.get((subject, artifact_id))
            if row is None or row[0] != "complete" or row[2] != question:
                return None
            return row[3]

    def has_complete(self, subject: str, artifact_id: str,
                     question: str) -> bool:
        with self._lock:
            row = self._rows.get((subject, artifact_id))
            return (row is not None and row[0] == "complete"
                    and row[2] == question)

    def take_complete(self, subject: str, artifact_id: str,
                      question: str) -> "tuple[str, tuple] | None":
        """The report and the unrecorded vault items (#1073) of a complete
        erasure of *subject* at *artifact_id* for *question*, consumed;
        ``None`` when there is none."""
        with self._lock:
            row = self._rows.get((subject, artifact_id))
            if row is None or row[0] != "complete" or row[2] != question:
                return None
            del self._rows[(subject, artifact_id)]
            return row[1], row[4]


WATCH = EraseWatch()
QUESTIONS = QuestionIds()
RECORDS = ErasureRecords()


def erase_turn(origin: dict | None) -> "tuple[str, str] | None":
    """``(tapped artifact, run id)`` on an erase-marked turn — either may be
    "" when missing, which no armed key matches — or ``None`` on any other
    turn."""
    if not isinstance(origin, dict) or origin.get("synthetic") != "plugin_erase":
        return None
    artifact = origin.get("plugin_erase_artifact")
    run_id = origin.get("plugin_erase_episode")
    return (artifact if isinstance(artifact, str) else "",
            run_id if isinstance(run_id, str) else "")


def erase_turn_question_open(origin: dict | None) -> bool:
    """Whether the uninstall question an erase-marked turn answers is still
    the open one. A new question, a Keep or a Cancel replaces or closes it,
    and then an erase still queued behind it must not run (operator ruling,
    #1046 diff r5)."""
    if not isinstance(origin, dict):
        return False
    subject = origin.get("plugin_erase_subject")
    question = origin.get("plugin_erase_question")
    return (isinstance(subject, str) and isinstance(question, str)
            and bool(question) and QUESTIONS.current(subject) == question)


def turn_ended(context: dict | None) -> None:
    """Called from the agent's turn ``finally`` for every turn (#1046):
    on an erase-marked turn, resolve whatever its eraser did not answer.
    Synchronous; never raises."""
    try:
        turn = erase_turn(context)
        if turn and turn[1]:
            WATCH.resolve_turn_end(turn[1])
    except Exception:  # noqa: BLE001 — the turn's reply is already produced
        logger.exception("erase turn-end report failed")
    return None


@dataclass(frozen=True)
class EraseSpec:
    """One plugin to erase: its registry name, the artifact the operator's tap
    named, its registry ``targets``, the eraser's full tool name on each of
    its MCP servers, and whether the plugin declared it protected.

    The ``tool`` fields describe the plugin's ``casa.eraseTool`` ("" when it
    declares none); the ``data_`` fields its ``casa.eraseDataOnlyTool``
    (#1067). An episode runs a spec projected onto one kind
    (:meth:`for_kind`), whose ``tool`` fields are that kind's eraser."""
    name: str
    artifact_id: str
    targets: tuple
    tool_names: tuple
    protected: bool
    tool: str = ""                  # the declared (bare) tool name
    summary: "str | None" = None    # its protected-tool summary, if any
    data_tool: str = ""
    data_tool_names: tuple = ()
    data_protected: bool = False
    data_summary: "str | None" = None

    def offers(self, kind: Kind) -> bool:
        return bool(self.data_tool if kind == DATA_ONLY else self.tool)

    def for_kind(self, kind: Kind) -> "EraseSpec":
        """This plugin with *kind*'s eraser in the ``tool`` fields."""
        if kind == DATA_ONLY:
            return EraseSpec(self.name, self.artifact_id, self.targets,
                             self.data_tool_names, self.data_protected,
                             tool=self.data_tool, summary=self.data_summary)
        return EraseSpec(self.name, self.artifact_id, self.targets,
                         self.tool_names, self.protected, tool=self.tool,
                         summary=self.summary)


@dataclass(frozen=True)
class ErasureOutcome:
    name: str
    artifact_id: str
    verdict: Outcome
    report: str
    unrecorded: tuple = ()


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
    return ErasureOutcome(spec.name, spec.artifact_id, verdict, report,
                          parse_unrecorded_vault_items(payload.get("text")))


async def _erase_one(spec: EraseSpec, operator: tuple[int, int],
                     question: str, subject: str) -> ErasureOutcome:
    import uuid
    import plugin_dispatch
    entry = {"targets": list(spec.targets)}
    _tier, exec_role = plugin_dispatch.execution_target(entry)
    role, instruction = plugin_dispatch.compose(
        entry, _instruction(spec.tool_names[0] if spec.tool_names else ""))
    if (role is None or exec_role is None or _dispatch is None
            or not spec.tool_names):
        return ErasureOutcome(spec.name, spec.artifact_id, "not_dispatched",
                              NOT_DISPATCHED_REPORT)
    run_id = uuid.uuid4().hex
    futures = [WATCH.arm(run_id, t) for t in spec.tool_names]
    try:
        accepted = await _dispatch(role, instruction, {
            "synthetic": "plugin_erase", "plugin_erase_target": exec_role,
            "plugin_erase_artifact": spec.artifact_id,
            "plugin_erase_episode": run_id,
            "plugin_erase_subject": subject,
            "plugin_erase_question": question})
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
            WATCH.disarm(run_id, t)


async def run_erase_episode(specs: list[EraseSpec], operator: tuple[int, int],
                            question: str, subject: str,
                            kind: Kind = EVERYTHING) -> list[ErasureOutcome]:
    """Run each plugin's eraser in turn and record what it reported,
    stopping at the first erasure that is not complete: an uninstall
    proceeds only when every one completed, so running the rest would erase
    data the operator then keeps a plugin for. Returns the outcomes of the
    plugins that ran. Every record is stamped with *question*, the uninstall
    question whose Erase tap started this episode, and with *kind*, the
    eraser the tap chose — *specs* are already projected onto it."""
    outcomes: list[ErasureOutcome] = []
    for spec in specs:
        out = await _erase_one(spec, operator, question, subject)
        verdict = out.verdict if out.verdict in ("complete", "incomplete") \
            else "unreadable"
        RECORDS.put(f"plugin:{spec.name}", spec.artifact_id, verdict,
                    out.report, question, kind, out.unrecorded)
        outcomes.append(out)
        if out.verdict != "complete":
            break
    return outcomes
