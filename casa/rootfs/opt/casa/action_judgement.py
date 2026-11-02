"""Casa Browser action model (design §2.1–§2.3): the check of one browsing
action, asked by a plugin's own code during one of its ``callsCasa`` calls.

``POST /internal/broker/action-judgement`` takes ``{"client", "kind",
"packet", "spec_digest"}``. The packet is the app's own canonical JSON text,
relayed untouched as a JSON string; Casa hashes exactly the UTF-8 bytes it
received (``packet_sha256``) and the app binds the verdict to its frozen
action by that digest, so nothing on the way can change what was judged.

One clean-context model call judges it: Claude Opus 5.5 at effort high, fixed
whatever ``MODEL_MAP`` says (operator ruling 5), the measured rulebook for the
kind as the system prompt (``rulebooks/``, byte for byte), no tools, no
settings sources, no session. The model sees the measured body: for
``action`` ``{"context": {"operator_words": …}, "packet": …}``, for
``rejudge`` the packet alone. Slice 5 adds two kinds the app starts itself:
``login_item`` (which vault items sign in to a site: 0–5 indexes) and
``signin`` (did a live-view sign-in attempt sign the person in), each judged
on its packet alone. The operator's own words travel outside the
packet and its digest (ruling 3); Casa keeps them, memory-only, per spec
digest when run admission reads them (``WORDS``).

A failure, a timeout, a refusal, an unparsable reply or one outside the
bounds is ``{"error": "check_unavailable"}`` — never a verdict, and Casa
never answers ``go`` in the check's place (F1). Bounds are checked, not
clipped: clipping could cut a material fact out of the operator's question."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

JUDGE_MODEL = "claude-opus-5-5"     # ruling 5: the judge is fixed, not MODEL_MAP's
JUDGE_EFFORT = "high"
CHECK_TIMEOUT_S = 90.0              # measured max 15.3 s, the CLI start included
MAX_PACKET_BYTES = 512 * 1024
MAX_DIGEST_CHARS = 128
MAX_WORDS_CHARS = 4000
WORDS_MAX_ENTRIES = 500
WORDS_KEEP_S = 24 * 3600.0

KINDS = ("action", "rejudge", "login_item", "signin")
VERDICTS = {"action": ("go", "ask", "look_first"), "rejudge": ("same", "changed"),
            "signin": ("signed_in", "not_signed_in", "unsure")}
CATEGORIES = ("reading", "local", "account_change", "commitment", "payment",
              "personal_data", "login", "software", "unclear")
SCREENSHOTS = ("required", "useful", "none")
# Bounds sit above every measured reply (767 parsed: reason ≤ 326, an extra option ≤ 33,
# confirm label ≤ 32), so a well-formed check never costs a retry; they are checked, never
# clipped.
MAX_REASON_CHARS = 600
MAX_TEXT_CHARS = 600
MAX_LABEL_CHARS = 32                # the confirm button's label
MAX_OPTION_CHARS = 60               # each extra option's label
MAX_EXTRA_OPTIONS = 3
MAX_HIGHLIGHTS = 6
MAX_HIGHLIGHT_CHARS = 200
MAX_LOGIN_ITEMS = 5                 # a login_item reply: at most five candidates (S5-2)
MAX_EVIDENCE_BLOCKS = 50            # a signin reply's evidence block numbers

RULEBOOK_DIR = Path(__file__).resolve().with_name("rulebooks")
RULEBOOK_FILES = {"action": "action_v1.txt", "rejudge": "rejudge_v1.txt",
                  "login_item": "login_item_v1.txt", "signin": "signin_v1.txt"}
# Read as bytes and decoded: no newline translation, the measured text exactly.
RULEBOOKS: dict[str, str] = {
    kind: (RULEBOOK_DIR / name).read_bytes().decode("utf-8")
    for kind, name in RULEBOOK_FILES.items()}


# The terminal facts of a check that finished its answer. ALLOW-lists, as in
# ``tools._COMPLETED_TERMINAL_REASONS``: the SDK passes these open namespaces
# through verbatim, so an unheard-of value (a cancellation, a cut answer) must
# read as unfinished. ``None`` is an older CLI that reports none. Every measured
# call (765) ended ``success`` / ``end_turn`` / ``completed``.
_COMPLETED_SUBTYPES = frozenset({"success"})
_COMPLETED_STOP_REASONS = frozenset({None, "end_turn", "stop_sequence"})
_COMPLETED_TERMINAL_REASONS = frozenset({None, "completed"})


class CheckFailed(Exception):
    """The model call did not finish its answer: an error, a refusal, a
    cancelled or cut turn, or no terminal result at all."""


# ---------------------------------------------------------------------------
# The operator's words, per admitted spec digest (memory-only)
# ---------------------------------------------------------------------------

class OperatorWords:
    """``(plugin, operator_id, spec_digest) -> words``, at most
    ``WORDS_MAX_ENTRIES`` for ``WORDS_KEEP_S`` each. Memory-only on purpose:
    nothing durable keeps the operator's words (the binding store's rule); a
    restart leaves the check the task text alone, as a continuation's has."""

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._items: "collections.OrderedDict[tuple, tuple[str, float]]" = (
            collections.OrderedDict())

    def _sweep_locked(self) -> None:
        now = self._now()
        for key, (_w, expires) in list(self._items.items()):
            if expires <= now:
                del self._items[key]

    def record(self, *, plugin: str, operator_id: Any, spec_digest: Any, words: Any) -> None:
        if not isinstance(words, str) or not isinstance(spec_digest, str) or not spec_digest:
            return
        key = (plugin, int(operator_id), spec_digest)
        with self._lock:
            self._sweep_locked()
            self._items.pop(key, None)
            self._items[key] = (words[:MAX_WORDS_CHARS], self._now() + WORDS_KEEP_S)
            while len(self._items) > WORDS_MAX_ENTRIES:
                self._items.popitem(last=False)

    def get(self, *, plugin: str, operator_id: Any, spec_digest: Any) -> "str | None":
        if not isinstance(spec_digest, str) or not spec_digest:
            return None
        with self._lock:
            self._sweep_locked()
            hit = self._items.get((plugin, int(operator_id), spec_digest))
        return hit[0] if hit is not None else None


WORDS = OperatorWords()


# ---------------------------------------------------------------------------
# The reply
# ---------------------------------------------------------------------------

def _str_within(value: Any, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit


def _labels_within(value: Any, count: int, limit: int) -> bool:
    return (isinstance(value, list) and len(value) <= count
            and all(_str_within(v, limit) for v in value))


def _question(q: Any) -> "dict | None":
    if not isinstance(q, dict) or not isinstance(q.get("text_incomplete"), bool):
        return None
    if not (_str_within(q.get("text"), MAX_TEXT_CHARS)
            and _str_within(q.get("confirm_label"), MAX_LABEL_CHARS)
            and _labels_within(q.get("extra_options", []), MAX_EXTRA_OPTIONS, MAX_OPTION_CHARS)
            and q.get("screenshot") in SCREENSHOTS
            and _labels_within(q.get("highlight", []), MAX_HIGHLIGHTS, MAX_HIGHLIGHT_CHARS)):
        return None
    return {"text": q["text"], "confirm_label": q["confirm_label"],
            "extra_options": list(q.get("extra_options", [])),
            "screenshot": q["screenshot"], "highlight": list(q.get("highlight", [])),
            "text_incomplete": q["text_incomplete"]}


def _index(v: Any) -> bool:
    return type(v) is int and v >= 0


def _login_item_reply(d: dict) -> "dict | None":
    """``{"items": [index, …], "reason"}``: at most five distinct indexes, best first, and a
    one-line reason. The app checks each index against its own packet."""
    items, reason = d.get("items"), d.get("reason")
    if not (isinstance(items, list) and len(items) <= MAX_LOGIN_ITEMS
            and all(_index(i) for i in items) and len(set(items)) == len(items)
            and _str_within(reason, MAX_REASON_CHARS)):
        return None
    return {"items": list(items), "reason": reason}


def parse_reply(kind: str, text: Any) -> "dict | None":
    """The verdict a reply states, or None (no verdict). The first ``{…}``
    JSON object of the text (the measurement harness's parser), its verdict in
    the kind's set, and every bound checked — never clipped."""
    m = re.search(r"\{.*\}", text if isinstance(text, str) else "", re.S)
    if m is None:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    if kind == "login_item":
        return _login_item_reply(d)
    if d.get("verdict") not in VERDICTS.get(kind, ()):
        return None
    reason = d.get("reason", "")
    if not _str_within(reason, MAX_REASON_CHARS):
        return None
    if kind == "signin":
        blocks = d.get("evidence_blocks")
        if not (isinstance(blocks, list) and len(blocks) <= MAX_EVIDENCE_BLOCKS
                and all(_index(b) and b >= 1 for b in blocks)):
            return None
        return {"verdict": d["verdict"], "evidence_blocks": list(blocks), "reason": reason}
    if kind == "rejudge":
        if not isinstance(d.get("text_incomplete"), bool):
            return None
        return {"verdict": d["verdict"], "reason": reason,
                "text_incomplete": d["text_incomplete"]}
    if d.get("category") not in CATEGORIES:
        return None
    note = d.get("look_first") or ""
    if not isinstance(note, str):
        return None
    question = None
    if d["verdict"] == "ask":
        question = _question(d.get("question"))
        if question is None:
            return None
    return {"verdict": d["verdict"], "category": d["category"], "reason": reason,
            "look_first": note, "question": question}


# ---------------------------------------------------------------------------
# The model call
# ---------------------------------------------------------------------------

async def _ask_check(rulebook: str, body: str) -> str:
    """One clean-context call: Opus 5.5 at high, the rulebook as the system
    prompt, no tools, no settings sources, no session persisted — run
    admission's posture with the measured options. Only *body* is sent."""
    import claude_agent_sdk as sdk
    from claude_runtime import (
        CLAUDE_CLI_PATH, SDK_MAX_BUFFER_SIZE, cli_session_settings,
        ephemeral_extra_args, ephemeral_setting_sources,
        with_cross_session_tools_denied,
    )

    opts = sdk.ClaudeAgentOptions(
        model=JUDGE_MODEL, effort=JUDGE_EFFORT, cli_path=CLAUDE_CLI_PATH,
        max_buffer_size=SDK_MAX_BUFFER_SIZE, system_prompt=rulebook,
        max_turns=4, tools=[], allowed_tools=[],
        disallowed_tools=with_cross_session_tools_denied(["Bash", "Task", "Agent"]),
        settings=cli_session_settings(), setting_sources=ephemeral_setting_sources(),
        extra_args=ephemeral_extra_args(), permission_mode="acceptEdits",
    )
    import sdk_logging
    reply = ""
    finished = False
    # The SDK logs raw CLI stdout (a reply that may quote the page) in its own malformed-frame
    # diagnostics; nothing a check reads or answers is stored, so those records are dropped for
    # the call (slice 5, r6).
    with sdk_logging.suppress_structured_voice_sdk_payload_logs():
        async for msg in sdk.query(prompt=body, options=opts):
            if isinstance(msg, sdk.AssistantMessage):
                # The CLI reports an API fault or a refusal as an ordinary
                # assistant message carrying its own text (error_kinds).
                if getattr(msg, "error", None) or getattr(msg, "stop_reason", None) == "refusal":
                    raise CheckFailed("assistant_error")
                for block in getattr(msg, "content", []) or []:
                    t = getattr(block, "text", None)
                    if isinstance(t, str):
                        reply += t
            elif isinstance(msg, sdk.ResultMessage):
                if (getattr(msg, "is_error", True) is not False
                        or getattr(msg, "subtype", None) not in _COMPLETED_SUBTYPES
                        or getattr(msg, "stop_reason", None) not in _COMPLETED_STOP_REASONS
                        or getattr(msg, "terminal_reason", None)
                        not in _COMPLETED_TERMINAL_REASONS):
                    raise CheckFailed("result_not_completed")
                finished = True
    if not finished:
        raise CheckFailed("no_result")
    return reply


# ---------------------------------------------------------------------------
# The judgement
# ---------------------------------------------------------------------------

def _no_duplicate_keys(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def parse_packet(packet: Any) -> "tuple[bytes, dict] | None":
    """The packet's UTF-8 bytes and its object, or None: a str of at most
    ``MAX_PACKET_BYTES`` bytes that is one JSON object, no key repeated (what
    the model reads is then exactly what was hashed)."""
    if not isinstance(packet, str):
        return None
    try:
        raw = packet.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(raw) > MAX_PACKET_BYTES:
        return None
    try:
        parsed = json.loads(packet, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    return raw, parsed


def model_body(kind: str, packet: dict, words: "str | None") -> str:
    """The body the rulebooks were measured with: the operator's words
    beside an action packet, every other kind's packet alone."""
    if kind == "action":
        return json.dumps({"context": {"operator_words": words}, "packet": packet},
                          ensure_ascii=False)
    return json.dumps(packet, ensure_ascii=False)


async def judge(call, kind: Any, packet: Any, spec_digest: Any) -> dict:
    """The check for *call* (an ``operator_calls.OperatorCall``):
    ``{"verdict", "packet_sha256", "latency_ms"}``, ``{"error":
    "check_unavailable"}``, or a typed request error before any model call."""
    if kind not in KINDS:
        return {"error": "bad_kind"}
    if spec_digest is not None and (
            not _str_within(spec_digest, MAX_DIGEST_CHARS)
            or not spec_digest or not spec_digest.isprintable()):
        return {"error": "bad_digest"}
    got = parse_packet(packet)
    if got is None:
        return {"error": "bad_packet"}
    raw, parsed = got
    digest = hashlib.sha256(raw).hexdigest()
    words = None
    if kind == "action":
        words = WORDS.get(plugin=call.plugin, operator_id=call.identity.operator_id,
                          spec_digest=spec_digest)
    body = model_body(kind, parsed, words)
    started = time.monotonic()
    verdict, why = None, "unparsable"
    try:
        verdict = parse_reply(kind, await asyncio.wait_for(
            _ask_check(RULEBOOKS[kind], body), CHECK_TIMEOUT_S))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — any failure is no verdict (F1)
        why = type(exc).__name__
    latency_ms = int((time.monotonic() - started) * 1000)
    if verdict is None:
        # Never the packet, the words, the reply or the exception's text: a
        # packet holds masked page text of the operator's accounts.
        logger.warning("action judgement: kind=%s check_unavailable (%s) latency_ms=%d "
                       "packet=%s", kind, why, latency_ms, digest[:12])
        return {"error": "check_unavailable"}
    logger.info("action judgement: kind=%s verdict=%s category=%s latency_ms=%d packet=%s",
                kind, verdict.get("verdict", f"{len(verdict.get('items', ()))}_items"),
                verdict.get("category", "-"), latency_ms, digest[:12])
    return {"verdict": verdict, "packet_sha256": digest, "latency_ms": latency_ms}
