"""Telegram channel implementation using python-telegram-bot v20+.

Supports two transport modes and two delivery modes:

Transport:
- **Polling** (default): long-polls Telegram for updates.
- **Webhook**: Telegram pushes updates. Set ``webhook_url``.

Delivery:
- **stream**: send first token immediately, edit message in-place as
  more tokens arrive (~1 s throttle). Feels real-time.
- **block**: wait for the full response before sending (classic).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import time

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any, Awaitable, Callable

from telegram import InputFile, Update
from telegram.constants import ChatAction
from telegram.request import HTTPXRequest
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from channels.tg_richtext import (
    missing_link_targets, plain_text, plain_with_link_targets, render,
    render_paged,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bus import BusMessage, MessageBus, MessageType
from ingress_identity import (
    TELEGRAM_NON_OPERATOR_CLEARANCE,
    TELEGRAM_OPERATOR_CLEARANCE,
    ingress_identity,
)
from channels import Channel, DeliveryOutcome, UnconfirmedDelivery
from output_boundary import Admitted, UnadmittedText
from media_policies import MEDIA_POLICIES
from text_util import replace_lone_surrogates, utf16_len, utf16_prefix_end
from channels.telegram_supervisor import ReconnectSupervisor
from log_cid import cid_var, new_cid
from provenance import (
    ANSWERED_BY_OPERATOR,
    sanitize_external_context,
    scheduled_delivery_markers,
    strict_positive_id,
)
from rate_limit import RateLimiter
from timekeeping import resolve_tz

logger = logging.getLogger(__name__)


def _unadmitted(method: str, text: Any) -> bool:
    """#1038 INV-OUT-001: model text reaches this transport only as an
    ``Admitted`` minted by the authoring turn's scope. A bare string is
    refused here — loudly, with zero Bot API calls — and the caller sees the
    method's OWN failure value (``NOT_DELIVERED``, ``None``, or a raise), so
    every existing consumer already handles it. Casa-composed text comes
    through ``output_boundary.casa_text``."""
    if isinstance(text, Admitted):
        return False
    logger.error("unadmitted model text refused: %s (%s)", method,
                 type(text).__name__)
    return True


def _proposal_keyboard(labels: list, rid: str, urls: "list | None" = None):
    """A proposal's keyboard: one button per label, ``callback_data = v1|proposal|<rid>|<i>``,
    or (slice 5f) the link button's https address where *urls* has one at that index."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    urls = list(urls or [])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text=str(label), url=urls[i])
         if i < len(urls) and urls[i] else
         InlineKeyboardButton(text=str(label), callback_data=f"v1|proposal|{rid}|{i}")]
        for i, label in enumerate(labels)])


def _plain(text):
    """#1330: ``plain_text`` for a send without entities, keeping the
    admission of model text — the plain transports refuse a bare string, and
    consuming an escape is rendering, not new text."""
    shown = plain_text(text)
    if shown is not text and isinstance(text, Admitted):
        return text.with_text(shown)
    return shown


def _overflow_head(text: Any, outcome: DeliveryOutcome) -> str:
    """#1038 §6.6 (as amended in plan round 3): when the page-1 unit of a
    streamed reply did not land, the admission's lines go out once, as their
    own message, before the overflow pages — otherwise the operator can see the
    claims on pages 2+ with no line before them. Empty when the head landed or
    nothing was annotated. ``UNKNOWN`` is an unconfirmed head — the edit may
    have applied with its acknowledgement lost — and deliberately gets the line
    message too: a line shown twice costs less than claims shown with none."""
    if outcome is DeliveryOutcome.DELIVERED:
        return ""
    lines = getattr(text, "annotations", ()) or ()
    return "\n\n".join(lines) if lines else ""

# Telegram typing indicator lasts ~5 s; resend every 4 s to keep it alive.
_TYPING_INTERVAL = 4.0

# Typing 401 backoff: initial delay, multiplier, max delay, max consecutive
# failures before circuit-breaking all typing. (Orthogonal to reconnect —
# spec 5.2 §4.3. Do not fold into the supervisor.)
_TYPING_BACKOFF_INIT = 1.0
_TYPING_BACKOFF_FACTOR = 2.0
_TYPING_BACKOFF_MAX = 60.0
_TYPING_CIRCUIT_BREAK = 10

# r1-2 (Sol): every typing lease carries this TTL, enforced by the loop
# itself, so the indicator is bounded even when no teardown ever runs. A
# turn's delivery normally releases its lease; but an accepted-but-never-
# consumed message (bus.py: a queue can be accepted then unregistered, or
# lack a live consumer) would otherwise keep the loop alive forever. With a
# TTL the loop drops the stale lease on its next pass — a >5-min turn merely
# stops showing "typing…", which is strictly safer than the old unbounded
# loop. Button-originated leases get this watchdog for free.
_TYPING_LEASE_TTL_S = 300.0

# One-liner sent when a chat exceeds TELEGRAM_RATE_PER_MIN. Only the
# FIRST rejection in a streak triggers this reply (spec 5.2 §8.2);
# subsequent rejects drop silently. Phrasing intentionally kept short
# and non-alarmist — the bucket refills within a minute.
_RATE_LIMIT_REPLY = "Slow down — try again in a minute."

# Stream edit throttle (seconds between editMessageText calls).
_STREAM_THROTTLE = 1.0


def _resolve_chat_id(context: dict[str, Any], default: int | str) -> int | str:
    """Pick a deliverable Telegram chat id from ``context``.

    The ``context["chat_id"]`` slot is overloaded — for user-initiated
    messages it carries a real Telegram chat id (signed integer); for
    scheduled triggers and voice scope probes it carries a session-keying
    label like ``"interval:heartbeat"`` or ``"probe-scope"``. The Telegram
    API rejects non-numeric values with ``BadRequest: Chat not found``,
    which used to bubble through ``finalize_stream → send`` and surface
    as a full traceback at the bus dispatcher. Fall back to the channel's
    registered default when the value isn't a valid chat id.
    """
    raw = context.get("chat_id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return default
    return default


class _NormalisedRequestData:
    """Read-only view of a PTB ``RequestData`` whose serialized form values carry
    no surrogate code point. Exposes exactly the two properties
    ``HTTPXRequest.do_request`` reads (PTB 22.7: ``multipart_data`` and
    ``json_parameters``); nothing else is consulted on that path."""

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def multipart_data(self) -> Any:
        # File names and bytes are not form VALUES; they pass through untouched.
        return self._inner.multipart_data

    @property
    def json_parameters(self) -> dict[str, str]:
        # A ``str`` parameter reaches here RAW (text, caption, ...); every other
        # value was JSON-encoded with ``ensure_ascii``, so a surrogate nested in
        # an entity url or a button label is already the six-character ASCII
        # escape and is left for the platform to judge. Only an actual
        # surrogate code point is replaced, one for one.
        return {key: replace_lone_surrogates(value)
                for key, value in self._inner.json_parameters.items()}


class _SurrogateSafeRequest(HTTPXRequest):
    """The one request class Casa's bot sends through (#846, INV-TG-009).

    PTB sends every request as UTF-8 FORM data, so a string parameter still
    carrying a lone surrogate raises ``NetworkError(UnicodeEncodeError)``
    before any request leaves the process — the whole reply vanishes in block
    mode, page 1 in stream mode, and the loss is classed unknown so the health
    notice is not re-offered. Normalising here, below every sender (plain,
    rich, streamed edits, narration, captions) and above the encoder, is what
    "replace it once where the text leaves for the transport" means: no send
    site can forget it and no sender changes. Replacement is one code point
    for one, so entities computed upstream stay valid. Installed by
    ``_rebuild`` on the ordinary pool only — ``getUpdates`` carries no
    Casa-authored text. A bot injected through ``bot=`` is outside this
    guarantee; ``tests/test_telegram_surrogate_boundary.py`` pins it against
    the real library."""

    async def do_request(  # type: ignore[override]
        self, url: str, method: str, request_data: Any = None, **timeouts: Any,
    ) -> tuple[int, bytes]:
        if request_data is not None:
            request_data = _NormalisedRequestData(request_data)
        return await super().do_request(
            url, method, request_data=request_data, **timeouts)


def _peek_stream_message_id(on_token: "OnTokenCallback") -> int | None:
    """Recover the streamed message_id from the on_token closure state.

    Mirrors the closure-peek in ``finalize_stream``; ``None`` means streaming
    never sent a message (block mode, or an empty/suppressed stream).
    """
    if hasattr(on_token, "__closure__") and on_token.__closure__:
        for cell in on_token.__closure__:
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if isinstance(val, dict) and "message_id" in val:
                return val["message_id"]
    return None

# M11 (v0.52.0): MarkdownV2 escaping for permission-relay text. Telegram
# rejects a message with parse_mode="MarkdownV2" if reserved chars in the
# dynamic text are unescaped ("can't parse entities", HTTP 400) — and the
# engagement_permission_relay hook treats that failure as an auto-DENY. We
# escape locally (rather than importing telegram.helpers.escape_markdown) so
# the code is stub-independent under tests; the char sets match PTB 22.7's
# escape_markdown(version=2). Outside a code entity every reserved char must
# be backslash-escaped; inside a ``` / ` (pre/code) entity only backtick and
# backslash are special.
_MDV2_SPECIALS = frozenset("_*[]()~`>#+-=|{}.!\\")
_MDV2_PRE_SPECIALS = frozenset("`\\")


def _escape_mdv2(text: str) -> str:
    """Escape MarkdownV2 reserved characters in general (non-code) text."""
    return "".join("\\" + c if c in _MDV2_SPECIALS else c for c in text)


def _escape_mdv2_pre(text: str) -> str:
    """Escape MarkdownV2 reserved characters inside a pre/code entity."""
    return "".join("\\" + c if c in _MDV2_PRE_SPECIALS else c for c in text)


# ---------------------------------------------------------------------------
# v0.75.0 (W5/Sol B3,B4): inline-callback data — v1 broker format.
# ---------------------------------------------------------------------------

# Telegram hard-caps callback_data at 64 bytes.
_CALLBACK_DATA_MAX_BYTES = 64
_CALLBACK_NAMESPACES = ("resident_ask",
                        "proposal",       # S5: a stored-call keyboard (DM-scoped)
                        "run_stop")       # the Stop on a browsing request's card (DM-scoped)


async def _safe_answer(cq: Any, text: str) -> None:
    """Exactly one ``cq.answer(text)`` per callback path (r7-B2).

    ``CallbackQuery.answer`` is async; a transport failure clearing the
    Telegram client-side spinner is caught non-fatally so one failed answer
    never crashes the handler.
    """
    try:
        await cq.answer(text)
    except Exception as exc:  # noqa: BLE001 — defensive, never propagate
        logger.warning("callback_query.answer failed: %s", exc)


def _parse_callback_data(
    data: str,
) -> tuple[str | None, str | None, int | None, str | None]:
    """Parse inline-keyboard ``callback_data`` into ``(namespace, request_id,
    option_index, kind)``, or ``(None, None, None, None)`` on any malformed
    shape.

    Accepted shapes:
    - v1 (current): ``v1|<ns>|<request_id>|<option_index>``, ``ns`` one of
      ``_CALLBACK_NAMESPACES``, ``option_index`` an int. ``kind`` is
      always ``None`` (a single-select tap).

    Oversized payloads (> 64 bytes, Telegram's own cap) are rejected
    defensively even though nothing this process composes should ever
    generate one.
    """
    if len(data.encode("utf-8")) > _CALLBACK_DATA_MAX_BYTES:
        return None, None, None, None
    if data.startswith("v1|"):
        parts = data.split("|", 3)
        if len(parts) != 4:
            return None, None, None, None
        _, ns, request_id, idx_s = parts
        if not request_id:
            return None, None, None, None
        if ns not in _CALLBACK_NAMESPACES:
            return None, None, None, None
        try:
            idx = int(idx_s)
        except ValueError:
            return None, None, None, None
        return ns, request_id, idx, None
    return None, None, None, None


# A background job's buttons in the direct chat. They carry the engagement id
# (32 hex characters) and, for a question, its random id and the option's
# index, so a tap is resolved against the record alone and works after a
# restart. Matched BEFORE ``_parse_callback_data``, whose four-field shape
# they do not have.
_JOB_QUESTION_PREFIX = "v1|job_q|"
_JOB_CANCEL_PREFIX = "v1|job_cancel|"
_ENGAGEMENT_ID_RE = re.compile(r"[0-9a-f]{32}")
_QUESTION_ID_RE = re.compile(r"[A-Z2-7]{8}")
_QUESTION_GONE_TOAST = "This question is no longer active — reply in words"
_CANCEL_NOT_SAVED_LINE = ("The job could not be cancelled just now — "
                          "tap Cancel again.")


def job_question_callback(engagement_id: str, qid: str, index: int) -> str:
    return f"{_JOB_QUESTION_PREFIX}{engagement_id}|{qid}|{index}"


def job_cancel_callback(engagement_id: str) -> str:
    return f"{_JOB_CANCEL_PREFIX}{engagement_id}"


def _parse_job_callback(data: str) -> "tuple | None":
    """``None`` when *data* is not a job button; ``("bad",)`` when it has a
    job prefix but not its exact shape; else ``("q", eid, qid, index)`` or
    ``("cancel", eid)``."""
    if data.startswith(_JOB_QUESTION_PREFIX):
        parts = data.split("|")
        if (len(data.encode("utf-8")) > _CALLBACK_DATA_MAX_BYTES
                or len(parts) != 5
                or not _ENGAGEMENT_ID_RE.fullmatch(parts[2])
                or not _QUESTION_ID_RE.fullmatch(parts[3])
                or not parts[4].isdigit()):
            return ("bad",)
        return ("q", parts[2], parts[3], int(parts[4]))
    if data.startswith(_JOB_CANCEL_PREFIX):
        parts = data.split("|")
        if (len(data.encode("utf-8")) > _CALLBACK_DATA_MAX_BYTES
                or len(parts) != 3
                or not _ENGAGEMENT_ID_RE.fullmatch(parts[2])):
            return ("bad",)
        return ("cancel", parts[2])
    return None


# v0.81.0 (W-R3, Sol r1-5): button labels are DERIVED channel-side from the
# full option so they stay readable — Telegram truncates long labels to a few
# words, which made most choices unpickable. The full option text lives VERBATIM
# in the message body (``render_ask_body`` below); the button carries
# only a short summary. The button's IDENTITY stays the option INDEX in
# ``callback_data`` — the label is display-only, so nothing is lost by shortening.
#
# Round 4 (spec D2, Sol r1-3/r2-3/r2-7/r3-5): the v0.83.0 per-option elision
# ladder is GONE — it garbled labels ("1 · A —…MCP…MCPB"). Button labels are now MODEL-generated: the agent
# supplies an optional ``short`` per option, resolved with WHOLE-SET semantics by
# ``resolve_button_labels`` below. #1386: an option without a short offers its
# own label, so a set of short options ("Delete" / "Keep") renders its own
# words; the WHOLE set floors to ``Option 1``, ``Option 2``, … only when the
# words do not fit, collide or are blank — never mutated, never a rejection.
_ASK_BUTTON_CAPTION_CAP = 64
# #1386: an option's OWN label becomes its button when it is at most this many
# characters — about what a full-width button shows on a phone without cutting.
_ASK_BUTTON_WORDS_FIT = 32

# #1201: settled proposals remembered per chat for their tap toast — eight
# times result_broker.PROPOSAL_MAX_LIVE, since the live bound does not bound
# how many settle within the hour. A tap on one dropped from this bound takes
# the broker's path, as before: "already answered" while an answered
# proposal's tombstone lasts (verdict_broker._RETIRE_S), "expired" otherwise.
_PROPOSAL_SETTLED_PER_CHAT = 256

# A5 · F-MULTI checkbox glyphs (also the multi decoration prefix the resolver
# validates against — see ``_classify_button_labels``).
_MULTI_BOX_ON = "☑"


def _button_short(item: "str | dict") -> str | None:
    """The agent-supplied ``short`` of an options-list ITEM, iff it is a
    ``str`` (any string — blank or not; blankness is checked separately).
    ``None`` when absent, non-string, or the item is a bare ``str`` (a bare
    label never carries a short)."""
    if isinstance(item, dict):
        short = item.get("short")
        if isinstance(short, str):
            return short
    return None


def _button_label_text(item: "str | dict") -> str:
    """The full label text of an options-list ITEM (a bare ``str``, or a
    dict's ``"label"``)."""
    if isinstance(item, dict):
        return str(item.get("label", ""))
    return str(item)


def _floor_captions(n: int) -> list[str]:
    """The whole-set floor: numbered, content-free placeholders."""
    return [f"Option {i + 1}" for i in range(n)]


def _fit_captions(
    texts: list, own: list, numbered: bool, multi: bool,
) -> "tuple[str | None, list[str]]":
    """One whole-set attempt: ``(None, captions)`` when every text is non-blank,
    the set is pairwise-distinct (casefold) and every caption fits, else
    ``(reason, floor)``. ``own[i]`` marks a text that is the option's own label
    (it must fit ``_ASK_BUTTON_WORDS_FIT``); ``numbered`` prefixes ``n · ``."""
    n = len(texts)
    # wb1-4: ``strip()`` is used ONLY as the BLANK predicate (verbatim-or-
    # floor, never mutated): distinctness, captions and lengths read the RAW
    # text, so ``"  Setup  "`` renders ``"1 ·   Setup  "``.
    if any(t.strip() == "" for t in texts):
        return "blank", _floor_captions(n)
    if len({t.casefold() for t in texts}) != n:
        return "dup", _floor_captions(n)
    captions = [f"{i + 1} · {t}" if numbered else t for i, t in enumerate(texts)]
    for i, cap in enumerate(captions):
        decorated = f"{_MULTI_BOX_ON} {cap}" if multi else cap
        if (own[i] and len(texts[i]) > _ASK_BUTTON_WORDS_FIT) or (
                len(decorated) > _ASK_BUTTON_CAPTION_CAP):
            return "too_long", _floor_captions(n)
    return None, captions


def _classify_button_labels(
    options: list, multi: bool,
) -> "tuple[str | None, list[str]]":
    """Whole-set classification behind ``resolve_button_labels``
    (Sol r2-3/r2-7).

    #1386: each option offers its agent-supplied ``short`` when it has one (a
    ``str``), otherwise its OWN label. When no option has a short, the buttons
    are the labels verbatim (``Delete`` / ``Keep``); when any has one, every
    caption is ``n · <text>`` so a summary still points at its numbered full
    option in the body. The set floors to ``Option n`` only when a text is
    blank, two collide (casefold), an own label is wider than
    ``_ASK_BUTTON_WORDS_FIT``, or a DECORATED caption (``☑ `` prefix on a
    multi ask) exceeds the 64-char contract (Sol r3-5). If the shorts fail, the
    plain labels get one try of their own before the floor. Purely mechanical
    — length and distinctness, never content.

    Returns ``(floor_reason, captions)``: ``None`` and the stored captions
    (undecorated — the checkbox glyph is a render-time concern, D2 item 3), or
    one of ``blank|dup|too_long`` and the numbered floor. Pure; never mutates
    ``options``; never raises.
    """
    shorts = [_button_short(o) for o in options]
    labels = [_button_label_text(o) for o in options]
    has_short = [s is not None for s in shorts]
    texts = [s if s is not None else lab for s, lab in zip(shorts, labels)]
    own = [not h for h in has_short]
    reason, captions = _fit_captions(texts, own, any(has_short), multi)
    if reason is not None and any(has_short):
        alt_reason, alt = _fit_captions(labels, [True] * len(labels), False, multi)
        if alt_reason is None:
            return None, alt
    return reason, captions


def resolve_button_labels(options: list, multi: bool) -> list[str]:
    """THE pure whole-set button-label resolver (spec D2 item 1).

    ``options`` items are either a bare ``str`` (full label, no short) or
    ``{"label": str, "short": ...}``. Returns the final captions — the
    options' own words, or ``n · <short or label>`` when shorts are in play
    (see ``_classify_button_labels``) — or the whole-set floor
    ``["Option 1", "Option 2", …]``. Never mixed (some real, some floored),
    never mutated, never raises. Run BEFORE ``BROKER.register`` so the
    resolved captions can be placed in static broker meta (wiring: Task A5).
    """
    _reason, captions = _classify_button_labels(options, multi)
    return captions


def short_option_labels(
    labels: list, shorts: "list | None" = None,
) -> list[str]:
    """Derive button labels for a WHOLE option set (name/signature PINNED —
    resident ``ask_user`` calls this via
    ``post_dm_keyboard(short_labels=True)``, wired from parallel-owned
    ``tools.py``).

    Same whole-set rule as ``resolve_button_labels``: agent shorts VERBATIM,
    an option without one shows its own label, and the WHOLE set floors to the
    numbered placeholder only when the words do not fit. No elision anywhere
    (spec D2's ``short_option_labels()`` note) — this is a thin convenience
    wrapper that zips ``labels``/``shorts`` into ``resolve_button_labels``'s
    input shape for a single-select ask.
    """
    options = [
        {
            "label": str(lab),
            "short": (
                shorts[i] if shorts is not None and i < len(shorts) else None
            ),
        }
        for i, lab in enumerate(labels)
    ]
    return resolve_button_labels(options, multi=False)


def _canonical_question(question: str, number: int) -> str:
    """v0.79.0 §4 — the DISPLAYED question prefix is ALWAYS the allocated
    durable number: ``Q<number>: `` is PREPENDED to the question. v0.85.0
    (round 4, D4): the old leading ``Q<digits>:`` strip is REMOVED — the
    agent's question text is preserved VERBATIM (an agent-authored
    ``"Q7: which flavor?"`` renders ``"Q<n>: Q7: which flavor?"``); doctrine,
    not a regex, tells the agent not to self-number."""
    return f"Q{number}: {question}"


def buttons_show_options(options: list, captions: "list | None") -> bool:
    """#1392: True when the buttons are the options' own words verbatim
    (``captions`` is what the keyboard shows; ``None`` means no keyboard is
    known). Then the numbered list under the question only repeats them."""
    return bool(options) and captions is not None and (
        [str(c) for c in captions] == [str(o) for o in options])


def render_ask_body(
    number: "int | None", question: str, options: list,
    captions: "list | None" = None,
) -> str:
    """v0.81.0 (W-R3, Sol r1-5) — the SINGLE canonical rendered ask body.

    Used IDENTICALLY by the DM ask posters (``ask_user``, ``wipe_memory``)
    for the posted body and the finish-hook settlement base, so the two can
    never disagree.

    Format::

        Q<n>: <question>

        1. <opt0>
        2. <opt1>
        …

    EVERY option is rendered VERBATIM (no truncation, no ellipsis), 1-based
    numbered. A free-text anchor (``options == []``) renders the numbered
    question ALONE — no option list. ``number`` may be ``None`` (no durable
    number allocated / degraded boot), in which case the bare question is used
    without the ``Q<n>:`` prefix.

    v0.84.0 (round 4, D1): the LENGTH caps this docstring used to lean on for a
    "no overflow path" claim are gone (``_validate_ask_args`` no longer bounds
    question/label length; only option COUNT stays capped at
    ``_ASK_MAX_OPTIONS``). The real Telegram 4096-char body limit is now
    enforced by a dedicated per-ask render-and-measure lifecycle validator
    (Task A3, spec §D1 bullet 2) rather than by an invented length heuristic
    here — this function never truncates.

    #1392: when ``captions`` (the button captions the keyboard shows) are the
    options' own words verbatim, the list is left out — the buttons already
    carry every option in full. The list stays whenever a button shows a
    ``short`` or the ``Option n`` floor, and when no captions are passed.
    """
    base = _canonical_question(question, number) if number else question
    if not options or buttons_show_options(options, captions):
        return base
    numbered = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
    return f"{base}\n\n{numbered}"


# Reconnect supervisor backoff schedule (spec 5.2 §4.2): 1s, 2s, 4s, 8s,
# 16s, cap 60s, unbounded. Reuses retry.compute_backoff_ms via
# ReconnectSupervisor. Module-level so tests can monkey-patch short
# values; do not expose as env vars (spec §9.3).
_RECONNECT_INITIAL_MS = 1000
_RECONNECT_CAP_MS = 60_000

# Health probe: every _PROBE_INTERVAL seconds, call bot.get_me() with
# _PROBE_TIMEOUT. Replaces the v0.5.x _POLL_STALL_THRESHOLD placeholder
# (which only refreshed its own timestamp — no real detection).
_PROBE_INTERVAL = 45.0
_PROBE_TIMEOUT = 10.0

_TURN_INCOMPLETE_NOTICE_TIMEOUT_S = 5.0
"""The bound on one best-effort direct chat line about a job (a lost-message
notice, the cancel retry line, an answer mark) — attempted once, never
retried: a retry-until-posted on a job's path would hold the message's
completion veto forever (the INV-ENG-003 livelock class)."""

_SETTLED_STATUS_TIMEOUT_S = 5.0
"""The bound on the registry's settled-status read that words a lost-message
notice. The registry lock is held across a tombstone write; on timeout the
plain wording is used."""

_LOST_REPLY_NOTICE = "Your message could not be delivered — please resend it."
_LOST_REPLY_ENDED_NOTICE = "The job had just ended — not delivered, please resend."
_LOST_ANSWER_NOTICE = "Your answer could not be passed to the job — please answer again."
_LOST_ANSWER_ENDED_NOTICE = "The job had already ended; your answer wasn't used."
"""The one notice for an operator's words a job never took up, posted in the
direct chat as a reply to them: their typed reply, or the question or approval
card their tap answered. The ended form is chosen when the record's settled
status is terminal; Casa never forwards the words anywhere else."""

# Callback type for on_token streaming
OnTokenCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class _NoticeTarget:
    """Where an operator-triggered engagement turn's notices go: a reply to
    *message_id* in the operator's direct chat — their own message for a typed
    reply, the question or approval card for a tap (``answer``)."""

    chat_id: int
    message_id: int
    answer: bool = False


def _notice_dict(target: "_NoticeTarget | None") -> "dict | None":
    """A notice target as the job's message journal stores it."""
    if target is None:
        return None
    return {"chat_id": target.chat_id, "message_id": target.message_id,
            "answer": target.answer}


def _notice_target_from(value: Any) -> "_NoticeTarget | None":
    """A journaled notice target back as one; ``None`` for anything that is
    not one."""
    if not isinstance(value, dict):
        return None
    chat, mid = value.get("chat_id"), value.get("message_id")
    if type(chat) is not int or type(mid) is not int:
        return None
    return _NoticeTarget(chat_id=chat, message_id=mid,
                         answer=value.get("answer") is True)


_LOG_GROUP_REDIRECT = ("This group is a read-only log. Talk to me in our "
                       "direct chat.")
"""The one line a person who writes in the log group gets, once per boot."""



# ---------------------------------------------------------------------------
# #1036: inbound files. Arrival stores and acknowledges; it runs no agent turn.
# ---------------------------------------------------------------------------

#: What the operator is told for every kind the handler refuses. Human words,
#: keyed by the PTB attribute that identifies the kind; the fallback covers any
#: kind Telegram adds later, so a new kind is answered rather than dropped.
_INBOUND_KIND_WORDS = {
    "sticker": "sticker", "video": "video", "video_note": "video message",
    "voice": "voice message", "audio": "audio file", "animation": "GIF",
    "contact": "contact", "location": "location", "venue": "venue",
    "poll": "poll", "dice": "dice roll", "game": "game", "story": "story",
    "web_app_data": "Web App result", "invoice": "payment invoice",
}
#: Chat notices rather than messages. Answered like everything else (there is
#: no silent list in the operator's DM), with wording that does not call them
#: files.
_INBOUND_NOTICES = (
    "pinned_message", "new_chat_title", "new_chat_photo", "delete_chat_photo",
    "message_auto_delete_timer_changed", "new_chat_members", "left_chat_member",
    "write_access_allowed",
)
_ACCEPTED_KINDS_TEXT = "PDFs, images, and text or CSV files"


@dataclass(frozen=True)
class _InboundClass:
    """What a non-text message is, decided from the update alone."""

    accept: bool
    file_id: str = ""
    declared_size: int | None = None
    display_name: str = ""
    ext: str = ""
    refusal: str = ""


def _text_filter():
    """The filter the text handler is registered with — one definition, so the
    test that checks it against the real library checks THIS one.

    ``UpdateType.MESSAGE`` is load-bearing for the same reason as in
    `_non_text_filter`: a bare ``filters.TEXT`` also matches an edited message,
    a channel post and a business message, and an edited message must not
    start a second turn. ``handle_update``'s ``update.message`` check drops
    them too; this states it where the handler is declared."""
    return filters.UpdateType.MESSAGE & filters.TEXT


def _non_text_filter():
    """The filter the non-text handler is registered with — one definition,
    so the test that checks it against the real library checks THIS one.

    ``UpdateType.MESSAGE`` is load-bearing: PTB 22.7's ``BaseFilter`` also
    matches edited messages, channel posts and business messages, and an
    edited caption must not draw a reply. Disjoint from ``filters.TEXT``."""
    return filters.UpdateType.MESSAGE & ~filters.TEXT


def _carries_content(msg) -> bool:
    """A message a person sent with something in it — a file, or one of the
    content kinds above. Chat notices (a topic opened or closed, a member
    joining, a pin) carry none. Only consulted in the engagement supergroup: in
    the operator's DM every non-text message is answered."""
    if getattr(msg, "document", None) is not None or getattr(msg, "photo", None):
        return True
    return any(getattr(msg, attr, None) for attr in _INBOUND_KIND_WORDS)


def _human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{max(1, n // 1024)} KB"


def _classify_inbound(msg) -> _InboundClass:
    """Decide from the message alone: accept (with what to fetch), or refuse
    (with the exact words). Never touches the network or the filesystem."""
    import agent_inbox

    doc = getattr(msg, "document", None)
    if doc is not None:
        name = getattr(doc, "file_name", None) or ""
        ext = agent_inbox.extension_of(name)
        if ext is None:
            dot = name.rfind(".")
            if 0 < dot < len(name) - 1:
                # The operator's extension: bounded, since it is echoed back.
                shown = agent_inbox._bounded_display(name[dot:].lower())[:12]
            else:
                shown = "file like that"
            return _InboundClass(False, refusal=(
                f"I can't open a {shown}, so I haven't kept it. "
                f"{_ACCEPTED_KINDS_TEXT[0].upper()}{_ACCEPTED_KINDS_TEXT[1:]} all work."))
        return _InboundClass(True, file_id=doc.file_id,
                             declared_size=getattr(doc, "file_size", None),
                             display_name=name, ext=ext)
    photos = getattr(msg, "photo", None) or ()
    if photos:
        # Sizes of ONE image: take the largest, not each.
        largest = max(photos, key=lambda p: (p.width or 0) * (p.height or 0))
        return _InboundClass(True, file_id=largest.file_id,
                             declared_size=getattr(largest, "file_size", None),
                             display_name="", ext=".jpg")
    for attr in _INBOUND_NOTICES:
        if getattr(msg, attr, None):
            return _InboundClass(False, refusal=(
                f"I can't do anything with that — I only take {_ACCEPTED_KINDS_TEXT}."))
    for attr, words in _INBOUND_KIND_WORDS.items():
        if getattr(msg, attr, None):
            return _InboundClass(False, refusal=(
                f"I can't read a {words}. I can take {_ACCEPTED_KINDS_TEXT}."))
    return _InboundClass(False, refusal=(
        f"I can't read that kind of message. I can take {_ACCEPTED_KINDS_TEXT}."))


def caption_file_note(files: list[tuple[str, str, str, str]], *, album: bool) -> str:
    """#1378/#1379: Casa's note on the turn a caption starts — the facts of the
    files it covers as ``(name, kind, size, path)``, never the caption, which
    is the operator's message itself. An album's caption covers the album."""
    if len(files) == 1:
        name, kind, size, path = files[0]
        note = (f"The person sent a file with this message: {name} ({kind}, {size}). "
                f"It is in your inbox at {path}; that path is for your tools, not for "
                "your reply. Their message is the caption they wrote on the file.")
        if album:
            note += " It came in an album; it is the only file of the album you have."
        return note
    listed = "; ".join(f"{name} ({kind}, {size}) at {path}"
                       for name, kind, size, path in files)
    return (f"The person sent {len(files)} files together with this message: {listed}. "
            "They are in your inbox; those paths are for your tools, not for your "
            "reply. Their message is the caption they wrote on the files.")


def _inbound_reply(receipt, cls: _InboundClass) -> str:
    """The one line the operator sees for an upload's outcome."""
    import agent_inbox
    O = agent_inbox.Outcome
    shown = agent_inbox._bounded_display(cls.display_name)
    label = shown if shown else "your photo"
    kind = cls.ext.lstrip(".").upper() if cls.ext else "file"
    o = receipt.outcome
    if o is O.STORED:
        days = agent_inbox.RETENTION_S // 86400
        return (f"Got {label} — {_human_size(receipt.size)}. Ask me any time and "
                f"I'll read it. I'll keep it {days} days.")
    if o is O.TOO_LARGE:
        cap = agent_inbox.CAP_BYTES // (1024 * 1024)
        return (f"That {kind} is more than {cap} MB — more than I can open in "
                f"one go. {cap} MB is my limit.")
    if o is O.MISMATCH:
        return (f"That's named {label} but the contents aren't a {kind}, so "
                f"I've left it.")
    if o is O.FULL:
        days = agent_inbox.RETENTION_S // 86400
        mb = agent_inbox.MAX_BYTES // (1024 * 1024)
        return (f"I've no room for another file — I keep up to "
                f"{agent_inbox.MAX_FILES} files and {mb} MB in all. The oldest go "
                f"after {days} days; try again then, or ask me to read one I have.")
    if o is O.DOWNLOAD_FAILED:
        return "Telegram wouldn't give me that file — try sending it again."
    if o is O.LOCAL_PATH:
        return ("Your Telegram server handed back a local file path I can't "
                "safely import, so I've left the file.")
    if o is O.UNCERTAIN:
        return ("I couldn't confirm that file was saved — it may or may not "
                "still be there. Send it again to be sure.")
    return "I couldn't save that one. Nothing's been kept — try sending it again."


#: #1379: files whose events (an arrival, an outcome) fall within this many
#: seconds of the previous one in the chat — or that share a Telegram album —
#: share one acknowledgement, edited as each lands. Mechanical only.
ACK_WINDOW_S = 10.0
#: An album's size; with names shortened in a list, ten files' lines always fit
#: one Telegram message (design round d1, Astra).
ACK_MAX_FILES = 10


@dataclass(eq=False)
class _AckClaim:
    """#1379: a caption's claim on its album (or on its own file): the files it
    covers are left out of the acknowledgement while the claim is open, and for
    good once its turn was dispatched."""

    captions: list
    touched: float = 0.0
    sealed: bool = False             # its turn took its files and words: none join now
    done: bool = False


@dataclass(eq=False)
class _AckEntry:
    """#1379: one non-text message of the default agent's path, created on arrival."""

    name: str
    group: str | None
    line: str | None = None          # its own one-line outcome; None while pending
    stored: tuple | None = None      # (name, kind, size, path) once stored
    claim: _AckClaim | None = None
    covered: bool = False            # a dispatched caption turn acts on it


@dataclass(eq=False)
class _AckBurst:
    """#1379: the files sent together in one chat and their one message."""

    entries: list
    touched: float
    claims: dict
    message_id: int | None = None
    shown: str = ""


def _ack_text(burst: _AckBurst) -> str:
    """The acknowledgement for what has landed: one file's own line, byte for
    byte as before; several files as one list of what was kept, then a line for
    each file that was not. Files a caption turn covers are its reply's."""
    import agent_inbox
    shown = [e for e in burst.entries if e.line is not None and not e.covered
             and not (e.claim is not None and not e.claim.done)]
    if len(shown) == 1 and len(burst.entries) == 1:
        return shown[0].line
    def short(name: str) -> str:
        return name if len(name) <= 64 else name[:63] + "…"
    kept = [e for e in shown if e.stored is not None]
    lines = []
    if len(kept) == 1:
        lines.append(kept[0].line)
    elif kept:
        days = agent_inbox.RETENTION_S // 86400
        noun = "photos" if all(not e.name for e in kept) else "files"
        listed = ", ".join(f"{short(e.name) or 'a photo'} ({e.stored[2]})" for e in kept)
        lines.append(f"Got {len(kept)} {noun}: {listed}. Ask me any time and I'll "
                     f"read them. I'll keep them {days} days.")
    for e in shown:
        if e.stored is None:
            lines.append(f"{short(e.name)}: {e.line}" if e.name else e.line)
    return "\n".join(lines)


class TelegramChannel(Channel):
    """Bidirectional Telegram channel backed by python-telegram-bot."""

    name: str = "telegram"

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        default_agent: str = "",
        bus: MessageBus | None = None,
        webhook_url: str = "",
        webhook_path: str = "/telegram/update",
        webhook_secret: str = "",
        rate_limiter: RateLimiter | None = None,
        bot: Any = None,
        engagement_supergroup_id: int | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        if not str(chat_id or "").strip():
            # #368: empty telegram_chat_id is a SECURITY posture, not just a
            # convenience default — say so once, loudly, at construction.
            logger.warning(
                "telegram_chat_id is empty (accept-all mode): no sender is "
                "the operator, so no turn receives operator attribution or "
                "private clearance, and protected plugin tools are always "
                "denied. Set telegram_chat_id to the operator's chat id to "
                "restore operator authorization."
            )
        self.default_agent = default_agent
        self._bus = bus
        self._webhook_url = webhook_url
        self._webhook_path = webhook_path
        self._webhook_secret = webhook_secret
        # Rate limiter (spec 5.2 §8). None = unlimited; a limiter with
        # capacity=0 also admits every message.
        self._rate_limiter = rate_limiter
        self._app: Application | None = None
        # v0.83.0 (§A3(b), Sol r10-3): the CHANNEL-READINESS event the boot
        # open-question reconcilers await. SET at the first successful ``_rebuild``
        # (``self._app`` becomes usable) — NOT at ``start_all()`` return, since
        # Telegram deliberately survives a transient bring-up failure and lets its
        # supervisor retry. A boot reconciler snapshots pre-service but its confirmed
        # settle edits must not fire against a ``None`` bot (they would fail closed
        # and the ledger would never settle). Loop-agnostic since Py3.10.
        self._ready_event: asyncio.Event = asyncio.Event()
        # r1-1/r1-2 (Sol): turn-owned typing leases. ``_typing_leases`` maps a
        # chat_id to ``{lease_id: monotonic_expiry}``; ``_typing_loops`` holds
        # ONE loop task per chat, running while that chat's lease dict is
        # non-empty. Each lease is keyed by its turn's cid, so overlapping
        # same-chat turns (the fast-tap timeline: the original turn still
        # narrating while the button-continuation turn starts) don't cancel
        # each other's indicator — a turn releases only ITS lease.
        self._typing_leases: dict[str, dict[str, float]] = {}
        self._typing_loops: dict[str, asyncio.Task] = {}
        # Typing backoff state (shared across all chats) — item-E-orthogonal.
        self._typing_consecutive_failures: int = 0
        self._typing_suspended: bool = False
        # Reconnect supervisor + health probe task.
        self._supervisor: ReconnectSupervisor | None = None
        self._probe_task: asyncio.Task | None = None
        # Test-injection bot and the optional forum group the agent log
        # writes to.
        self._bot = bot
        self.engagement_supergroup_id = engagement_supergroup_id
        # H5 (v0.52.0): bounded LRU of recently-seen webhook update_ids so a
        # Telegram redelivery (retry after a slow ACK) is processed at most
        # once. OrderedDict used as an insertion-ordered ring buffer.
        self._seen_update_ids: "collections.OrderedDict[int, None]" = (
            collections.OrderedDict()
        )
        # #1201: rid -> (the proposal's own meta dict, its settled toast), kept
        # until the proposal's deadline, or until the per-chat bound
        # (_PROPOSAL_SETTLED_PER_CHAT) drops it first, so a tap on a settled
        # keyboard answers what happened after the broker's tombstone is gone.
        # In-process only.
        self._proposal_settled: "collections.OrderedDict[str, tuple[dict, str]]" = (
            collections.OrderedDict()
        )
        # M9 (v0.52.0): strong refs to in-flight background turn and
        # continuation tasks the handlers spawn; each task keeps a ref here +
        # a done-callback that discards it. Cancelled in stop().
        self._turn_tasks: set[asyncio.Task] = set()
        # S6 §2.4 / §5: the 📎 armings (one per chat, memory-only) and the per-(chat, specialist)
        # file-intake locks (FIFO) — a file is judged, stored and given its desk place under one
        self._armings: dict = {}
        self._intake_locks: dict = {}
        # Injectable collaborators (wired at startup by Task 22; tests assign AsyncMocks).
        self._engagement_registry = None
        # #649: producer-closing stop gate — once stop() began, no message is
        # handed to a job any more.
        self._stopping = False
        self._engagement_driver = None
        self._finalize_cancel = None
        self._session_registry = None   # wired in main() (Task 9); /new reset
        # The agent log sink (agent_logs.AgentLogs), wired in main(); its
        # check runs at every successful rebuild.
        self._agent_logs = None
        # The people already told, this boot, that the log group is read-only.
        self._log_group_redirect_seen: set[int] = set()
        # The per-engagement lifecycle locks every resume owner of a job's
        # session takes (``engagement_lifecycle_lock``). Keyed by engagement
        # id; never held by a Telegram handler.
        self._engagement_lifecycle_locks: dict[str, asyncio.Lock] = {}
        # #317: per-chat serialization for the DM path (_handle). Handlers run
        # with block=False (H5), so /new and an immediately-following message
        # dispatch concurrently — the follow-up could resume the mid-reset
        # session or have its fresh session erased by the reset's trailing
        # remove. Serializing /new (which awaits the whole reset) against
        # same-chat message ENQUEUE restores the ordering M29 assumed from
        # PTB's default serialized dispatch; other chats keep flowing. Keyed
        # by chat_id; entries are not pruned — bounded growth (DM chats),
        # cleared on add-on restart.
        self._chat_serial_locks: dict[str, asyncio.Lock] = {}
        # #1379: the files of the operator's chat being acknowledged together,
        # and the lock that keeps their one message's sends and edits in order.
        self._ack_bursts: dict[tuple[str, str | None], _AckBurst] = {}
        self._ack_locks: dict[str, asyncio.Lock] = {}
        # Jobs whose Cancel button started a cancellation that has not
        # finished: a second tap answers "already finishing" instead of
        # starting a second funnel run.
        self._job_cancels: set[str] = set()
        # Tasks retiring ended jobs' keyboards (strong references).
        self._job_ui_tasks: set[asyncio.Task] = set()
        # Default routing: forward to the existing PTB handler.
        self._route_to_assistant = self._route_to_assistant_default

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initial bring-up. Starts the supervisor, runs one rebuild,
        then launches the health probe loop.
        """
        self._supervisor = ReconnectSupervisor(
            rebuild_fn=self._rebuild,
            logger=logger,
            initial_ms=_RECONNECT_INITIAL_MS,
            cap_ms=_RECONNECT_CAP_MS,
        )
        self._supervisor.start()
        try:
            await self._rebuild()
        except (NetworkError, TimedOut) as exc:
            # Transient failure on first bring-up — hand off to supervisor.
            logger.error(
                "Telegram initial bring-up failed (%s); handing off to supervisor",
                exc,
            )
            self._supervisor.trigger(f"initial_bringup: {exc!r}")
        self._probe_task = asyncio.create_task(self._health_probe_loop())

    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        # #649: close the producers FIRST — both hand-off sites refuse to
        # create a delivery task once this is set, so the drain below cannot
        # be outrun by a handler that was already past its admission.
        self._stopping = True
        for task in self._typing_loops.values():
            task.cancel()
        self._typing_loops.clear()
        self._typing_leases.clear()

        # M9: cancel any in-flight engagement-turn delivery tasks.
        for task in list(self._turn_tasks):
            task.cancel()

        # Bounded, loop-until-stable drain of the cancelled tasks: shutdown
        # never wedges on a notice.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while True:
            pending = [t for t in list(self._turn_tasks) if not t.done()]
            if not pending or loop.time() >= deadline:
                break
            await asyncio.wait(
                pending, timeout=max(0.1, deadline - loop.time()))
        self._turn_tasks.clear()

        if self._probe_task and not self._probe_task.done():
            self._probe_task.cancel()
            try:
                await self._probe_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._supervisor is not None:
            await self._supervisor.stop()
            self._supervisor = None

        await self._teardown_app()
        logger.info("Telegram channel stopped")

    async def _teardown_app(self) -> None:
        """Best-effort teardown of the current Application.

        Called both on `stop()` and as the first step of `_rebuild()`.
        Swallows exceptions — a failing teardown must not block the
        rebuild that follows.
        """
        app = self._app
        if app is None:
            return
        # M8 (v0.52.0): each teardown step runs independently. A failing
        # first step (e.g. delete_webhook raising during the very network
        # outage that triggered the rebuild) must NOT skip app.stop()/
        # shutdown() — otherwise the started Application's _update_fetcher
        # task, JobQueue, and HTTPX pools leak on every reload. `except
        # Exception` deliberately does NOT catch asyncio.CancelledError
        # (BaseException), so cancellation still propagates.
        try:
            if self._webhook_url:
                try:
                    await app.bot.delete_webhook()
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.debug("Telegram teardown: delete_webhook failed: %s", exc)
            elif app.updater is not None:
                try:
                    await app.updater.stop()
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.debug("Telegram teardown: updater.stop failed: %s", exc)
            try:
                await app.stop()
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.debug("Telegram teardown: app.stop failed: %s", exc)
            try:
                await app.shutdown()
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.debug("Telegram teardown: app.shutdown failed: %s", exc)
        finally:
            self._app = None

    async def _rebuild(self) -> None:
        """Build (or rebuild) the Application, register handlers, start it.

        Idempotent — if called while an Application exists, it is torn
        down first. Raises on bring-up failure so the supervisor can
        catch and schedule a retry; callers on the happy path do not
        expect this to raise.
        """
        await self._teardown_app()

        _bot_api_base = os.environ.get("TELEGRAM_BOT_API_BASE", "").strip()
        builder = Application.builder().token(self.bot_token)
        if _bot_api_base:
            builder = builder.base_url(f"{_bot_api_base}/bot")
            builder = builder.base_file_url(f"{_bot_api_base}/file/bot")
        # #846 / INV-TG-009: the ordinary Bot API pool sends through the
        # surrogate-replacing request class. Not reassigned: the builder
        # mutates and returns itself, and the suite's fake builder chain does
        # not. A no-argument HTTPXRequest carries the same pool size, HTTP
        # version and timeouts the builder would have installed (PTB 22.7).
        builder.request(_SurrogateSafeRequest())
        app = builder.build()
        # E-13: dispatch to handle_update, which routes by chat: the
        # operator's direct chat to _handle (via _route_to_assistant), the
        # log group to its read-only pointer.
        #
        # H5 (v0.52.0): block=False dispatches each update via
        # Application.create_task rather than awaiting it inline, so one
        # long turn cannot stall PTB's sequential update fetcher
        # (default max_concurrent_updates=1) — which in polling mode would
        # otherwise freeze assistant DMs too. PTB routes task exceptions to
        # the registered error handler.
        app.add_handler(
            MessageHandler(_text_filter(), self.handle_update, block=False)
        )
        # #1036: every non-text message — files and everything else — so none
        # is dropped silently. See `_non_text_filter` for why it is not a bare
        # `~filters.TEXT`.
        app.add_handler(
            MessageHandler(_non_text_filter(), self._on_non_text_message,
                           block=False)
        )
        # E-12 (v0.37.0) Task 20: U1 permission verdicts arrive as inline-keyboard
        # callbacks. CallbackQueryHandler with no pattern matches all callback_data;
        # _on_inline_callback parses by prefix and ignores unknown shapes.
        # block=False (H5): a permission-verdict click must never queue
        # behind a long in-flight in_casa turn.
        app.add_handler(
            CallbackQueryHandler(self._on_inline_callback, block=False)
        )
        app.add_error_handler(self._on_ptb_error)
        # M8 (v0.52.0): roll back a half-started Application if any bring-up
        # step raises, so a started-but-unpublished app (fetcher task,
        # JobQueue, HTTPX pools) is not leaked. Re-raise so the supervisor
        # still schedules a retry.
        try:
            await app.initialize()
            await app.start()

            if self._webhook_url:
                full_url = self._webhook_url.rstrip("/") + self._webhook_path
                kwargs: dict[str, Any] = {"url": full_url}
                if self._webhook_secret:
                    kwargs["secret_token"] = self._webhook_secret
                    # #214: PTB logs Bot API call parameters (incl.
                    # secret_token) as a dict at DEBUG. RedactingFilter masks
                    # the secret_token value by key name; registering the exact
                    # value too covers any other path that echoes it verbatim —
                    # belt-and-suspenders on the leak path.
                    try:
                        import log_redact
                        log_redact.register_secret(self._webhook_secret)
                    except Exception:  # noqa: BLE001 — redaction is best-effort
                        pass
                await app.bot.set_webhook(**kwargs)
                logger.info(
                    "Telegram started (webhook, url=%s, secret=%s)",
                    full_url,
                    "yes" if self._webhook_secret else "no",
                )
            else:
                await app.updater.start_polling()  # type: ignore[union-attr]
                logger.info(
                    "Telegram started (polling, chat_id=%s)",
                    self.chat_id,
                )
        except BaseException:
            # PTB 22.7 ordering: stop() raises if not running (guard with
            # app.running); shutdown() raises while running (only after
            # stop()).
            #
            # #347: ``BaseException``, not ``Exception`` — a CancelledError
            # during set_webhook()/start_polling() used to skip this rollback
            # entirely, and since the app is not yet published to
            # ``self._app``, channel stop() could never tear it down either:
            # the running updater/fetcher leaked for the process lifetime.
            # Rollback here is already best-effort per step, then the original
            # exception (cancellation included) is re-raised.
            #
            # Updater FIRST, mirroring ``_teardown_app`` (Sol+Terra r1): a
            # cancellation landing inside ``start_polling()`` leaves
            # ``updater.running`` set, ``app.stop()`` does NOT stop the
            # updater, and ``app.shutdown()`` then raises (swallowed below) —
            # the polling task would survive unpublished.
            try:
                if app.updater is not None and getattr(
                        app.updater, "running", False):
                    await app.updater.stop()
            except Exception as exc:  # noqa: BLE001 — best-effort rollback
                logger.debug(
                    "Telegram rebuild rollback: updater.stop failed: %s", exc)
            try:
                if getattr(app, "running", False):
                    await app.stop()
            except Exception as exc:  # noqa: BLE001 — best-effort rollback
                logger.debug("Telegram rebuild rollback: app.stop failed: %s", exc)
            try:
                await app.shutdown()
            except Exception as exc:  # noqa: BLE001 — best-effort rollback
                logger.debug("Telegram rebuild rollback: app.shutdown failed: %s", exc)
            raise

        # Publish the rebuilt app atomically.
        self._app = app

        # v0.83.0 (§A3(b), Sol r10-3): channel readiness. A successful rebuild
        # means ``self._app``/``self.bot`` are usable, so the boot open-question
        # reconcilers may now run their confirmed settle edits. Idempotent (a
        # later rebuild re-sets an already-set event) — never cleared.
        self._ready_event.set()

        # L6 (v0.52.0): a successful rebuild proves token+transport are
        # healthy (initialize() performs getMe and raises InvalidToken on a
        # bad token), so heal the typing circuit breaker — a past outage (or
        # a since-fixed token) must not suppress typing for the process
        # lifetime.
        self._typing_suspended = False
        self._typing_consecutive_failures = 0

        # E-F (v0.30.0): the agent log's check MUST run after
        # `self._app = app` (it needs the bot). Tied to every successful
        # rebuild, so a first rebuild that failed on a transient network
        # blip cannot leave the log off for the life of the process.
        await self.check_agent_logs()

    async def check_agent_logs(self) -> bool:
        """Turn the agent log on when the forum group is configured and the
        bot may manage its topics, and start its writer; otherwise it stays
        off. Never raises."""
        logs = self._agent_logs
        if logs is None or self._app is None:
            return False
        try:
            if await logs.check_enabled(self.bot, self.engagement_supergroup_id):
                logs.start()
                return True
        except Exception as exc:  # noqa: BLE001 — the log is optional
            logger.warning("agent log check failed: %s", exc)
        return False

    # ------------------------------------------------------------------
    # Health probe + PTB error handler (spec 5.2 §4.2)
    # ------------------------------------------------------------------

    async def _health_probe_loop(self) -> None:
        """Periodic liveness probe against the Bot API.

        Every `_PROBE_INTERVAL` seconds, call `bot.get_me()` with a
        `_PROBE_TIMEOUT` ceiling. On success: quiet. On timeout or
        transport exception: trigger the supervisor. Replaces the
        v0.5.x `_poll_stall_watchdog` placeholder.
        """
        try:
            while True:
                await asyncio.sleep(_PROBE_INTERVAL)
                app = self._app
                supervisor = self._supervisor
                if app is None or supervisor is None:
                    continue  # reconnect in progress — supervisor owns it
                try:
                    await asyncio.wait_for(
                        app.bot.get_me(), timeout=_PROBE_TIMEOUT,
                    )
                except asyncio.CancelledError:
                    raise
                except (NetworkError, TimedOut, asyncio.TimeoutError) as exc:
                    supervisor.trigger(f"probe_failed: {exc!r}")
                except Exception as exc:  # noqa: BLE001 — diagnostic only
                    # Non-transport exceptions: log and keep probing.
                    # Supervisor rebuild would not help.
                    logger.debug(
                        "Telegram health probe non-transport error: %s", exc,
                    )
        except asyncio.CancelledError:
            return

    async def _on_ptb_error(
        self,
        update: object,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """PTB error handler. Routes transport errors to the supervisor.

        Registered via `Application.add_error_handler`. Non-transport
        errors (handler bugs, ValueError, etc.) are logged but do NOT
        trigger a reconnect — rebuilding the Application would not fix
        them.
        """
        exc = getattr(context, "error", None)
        if isinstance(exc, (NetworkError, TimedOut)):
            if self._supervisor is not None:
                self._supervisor.trigger(f"ptb_error: {type(exc).__name__}: {exc}")
            return
        if exc is not None:
            logger.warning("Telegram handler error (not retryable): %s", exc)

    async def process_webhook_update(self, payload: dict) -> str:
        """Enqueue a webhook update payload for PTB's fetcher (fast ACK).

        Returns the outcome — ``"accepted"`` (queued), ``"duplicate"``
        (redelivery absorbed by the LRU), or ``"ignored"`` (channel not
        started, or payload not parseable as an update) — which the webhook
        route surfaces as the ``X-Casa-Update`` response header (#428).

        H5 (v0.52.0): pre-fix this awaited ``Application.process_update``,
        which (default block=True handlers) ran the ENTIRE engagement SDK
        turn before the aiohttp route could return 200 — Telegram timed out
        and redelivered the update, duplicating turns. We now push the
        update onto ``Application.update_queue`` (drained by the internal
        fetcher started by ``app.start()`` in both transports — exactly what
        PTB's own webhook server does) so the route returns within
        milliseconds. A bounded update_id LRU drops redeliveries that were
        already in flight before the first ACK landed.
        """
        if self._app is None:
            return "ignored"
        update = Update.de_json(payload, self._app.bot)
        if not update:
            return "ignored"
        uid = getattr(update, "update_id", None)
        if uid is not None:
            if uid in self._seen_update_ids:
                logger.info("Dropping duplicate webhook update_id=%s", uid)
                return "duplicate"
            self._seen_update_ids[uid] = None
            while len(self._seen_update_ids) > 256:
                self._seen_update_ids.popitem(last=False)
        await self._app.update_queue.put(update)
        return "accepted"

    # ------------------------------------------------------------------
    # Typing indicator (with 401 backoff)
    # ------------------------------------------------------------------

    def _start_typing(
        self, chat_id: str, lease_id: str, ttl_s: float = _TYPING_LEASE_TTL_S,
    ) -> None:
        """Add/refresh a turn-owned typing lease and ensure the chat's loop.

        r1-1: the lease is keyed by *lease_id* (the turn's cid), so overlapping
        same-chat turns each own a lease and one turn's finalizer can't cancel
        another's indicator. r1-2: the lease carries an expiry (``ttl_s``,
        default ``_TYPING_LEASE_TTL_S``) the loop enforces, so the indicator is
        bounded even if no teardown ever runs. Suspended (401 circuit breaker)
        → no-op, exactly as before.
        """
        if self._typing_suspended:
            return
        key = str(chat_id)
        leases = self._typing_leases.setdefault(key, {})
        leases[lease_id] = time.monotonic() + ttl_s
        existing = self._typing_loops.get(key)
        if existing is None or existing.done():
            self._typing_loops[key] = asyncio.create_task(
                self._typing_loop(key)
            )

    def _stop_typing(self, chat_id: str, lease_id: str | None = None) -> None:
        """Release a typing lease (idempotent).

        With *lease_id* → discard ONLY that lease (r1-1: a turn releases its
        own). Without → release ALL leases for the chat (the release-all
        fallback for delivery paths that carry no cid, preserving today's
        semantics). Once the chat's last lease is gone the loop is cancelled so
        the indicator tears down promptly (matching the pre-lease behaviour);
        while any lease remains the loop keeps running.
        """
        key = str(chat_id)
        leases = self._typing_leases.get(key)
        if not leases:
            return
        if lease_id is None:
            leases.clear()
        else:
            leases.pop(lease_id, None)
        if not leases:
            self._typing_leases.pop(key, None)
            task = self._typing_loops.pop(key, None)
            if task and not task.done():
                task.cancel()

    def _clear_typing_loop(self, chat_id: str) -> None:
        """Identity-guarded teardown of a chat's typing state, called from
        inside its own loop on every terminal exit (natural empty-exit,
        app-gone, suspended, or breaker trip).

        Once the loop abandons its duty its leases are meaningless, so the
        chat's lease dict is dropped wholesale and the next turn's
        ``_start_typing`` begins from a clean slate. The ``_typing_loops``
        entry is popped ONLY when it is still THIS task — a concurrent
        ``_start_typing`` that already installed a fresh loop must not be
        evicted. Contains no ``await`` so the empty-check → return window
        stays atomic (T3).
        """
        self._typing_leases.pop(chat_id, None)
        if self._typing_loops.get(chat_id) is asyncio.current_task():
            self._typing_loops.pop(chat_id, None)

    async def _typing_loop(self, chat_id: str) -> None:
        """Send 'typing' chat action while the chat holds a live lease.

        r1-2: each pass drops EXPIRED leases and exits when none remain, so the
        loop is bounded even if a teardown never runs. The 401 circuit-breaker
        and transport backoff below are preserved verbatim.
        """
        backoff = _TYPING_BACKOFF_INIT
        try:
            while True:
                # r1-2: expire stale leases; exit (natural completion, not a
                # cancel) when the chat holds none.
                leases = self._typing_leases.get(chat_id)
                if leases:
                    now = time.monotonic()
                    for lease_id in [
                        lid for lid, exp in leases.items() if exp <= now
                    ]:
                        leases.pop(lease_id, None)
                if not leases:
                    self._clear_typing_loop(chat_id)
                    return
                if self._typing_suspended or self._app is None:
                    # Channel unavailable / breaker suspended: abandon the loop
                    # and clean both maps so a reconnect window can't leave a
                    # stale lease the next turn's _start_typing would revive.
                    self._clear_typing_loop(chat_id)
                    return
                try:
                    await self._app.bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                    # Success -- reset backoff
                    self._typing_consecutive_failures = 0
                    backoff = _TYPING_BACKOFF_INIT
                except (NetworkError, TimedOut) as exc:
                    # L6 (v0.52.0): transient transport failure — back off but
                    # do NOT count toward the 401 circuit breaker; the
                    # reconnect supervisor owns transport recovery (spec 5.2
                    # §4.2/§4.3). (Order matters: NetworkError/TimedOut
                    # subclass TelegramError, so this clause must precede it.
                    # Note in PTB 22.7 BadRequest subclasses NetworkError, so
                    # a persistent BadRequest no longer trips the breaker —
                    # bounded because _stop_typing cancels the loop at end of
                    # turn.)
                    logger.warning(
                        "Typing transport error: %s — backing off %.1fs",
                        exc, backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * _TYPING_BACKOFF_FACTOR, _TYPING_BACKOFF_MAX)
                    continue
                except TelegramError as exc:
                    self._typing_consecutive_failures += 1
                    if self._typing_consecutive_failures >= _TYPING_CIRCUIT_BREAK:
                        self._typing_suspended = True
                        logger.error(
                            "Typing suspended after %d failures. "
                            "Bot token may be invalid.",
                            self._typing_consecutive_failures,
                        )
                        self._clear_typing_loop(chat_id)
                        return
                    logger.warning(
                        "Typing failed (%d/%d): %s — backing off %.1fs",
                        self._typing_consecutive_failures,
                        _TYPING_CIRCUIT_BREAK,
                        exc,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * _TYPING_BACKOFF_FACTOR, _TYPING_BACKOFF_MAX)
                    continue
                await asyncio.sleep(_TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass

    def _release_typing(self, context: dict[str, Any], chat_id: str) -> None:
        """Release THIS turn's typing lease by its cid (r1-1).

        Falls back to releasing ALL leases for the chat when the delivery
        context carries no cid (legacy / synthetic paths), preserving today's
        release-all semantics.
        """
        cid = context.get("cid")
        self._stop_typing(
            str(chat_id), cid if isinstance(cid, str) and cid else None,
        )

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def _send_plain_line(self, chat_id: str, text: str) -> None:
        """One Casa line to *chat_id*; a failure is logged, never raised."""
        if self._app is None:
            return
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:  # noqa: BLE001 — a notice fault is logged
            logger.debug("line to chat_id=%s failed: %s", chat_id, exc)

    async def _send_rate_limit_reply(self, chat_id: str) -> None:
        """Send the one-shot 'slow down' reply for a rate-limited chat."""
        if self._app is None:
            return
        try:
            await self._app.bot.send_message(
                chat_id=chat_id, text=_RATE_LIMIT_REPLY,
            )
        except Exception as exc:  # noqa: BLE001
            # A 401/503/etc. on the reply itself is not fatal — the user
            # simply doesn't see the notice this time. Do NOT raise;
            # the rest of _handle has already chosen to drop the
            # message.
            logger.debug(
                "rate-limit reply to chat_id=%s failed: %s", chat_id, exc,
            )

    def _user_id_is_operator(self, user_id) -> bool:
        """#336: does this Telegram user id belong to the configured operator?

        The only operator identity Casa is configured with is
        ``telegram_chat_id`` — in the standard DM setup the chat id IS the
        operator's Telegram user id (a private chat's id equals the peer's
        user id), so a sender whose id matches it is the operator. With
        ``telegram_chat_id`` empty ("accept all chats") there is no
        configured operator identity, so NO sender is the operator — every
        turn is then attributed to a per-sender ``telegram:<id>`` peer at
        public clearance (fail-closed; set ``telegram_chat_id`` to retain
        operator attribution). A group-id configuration (negative id) can
        never match a user id, which is correct: group members are not the
        operator.

        The single home of this rule: both the message ingress and the
        button-tap continuation resolve identity through it, so the two
        entry points into a turn can never disagree about who the operator
        is.
        """
        configured = str(self.chat_id or "").strip()
        return bool(configured) and user_id is not None and str(user_id) == configured

    def _sender_is_operator(self, user) -> bool:
        """``_user_id_is_operator`` for a Telegram ``User`` object."""
        return user is not None and self._user_id_is_operator(user.id)

    def operator_user_id(self) -> int | None:
        """The configured operator's Telegram user id, or ``None`` when no
        operator identity exists (#374).

        Derived from ``telegram_chat_id`` and then verified through
        ``_user_id_is_operator`` — the single home of the operator rule —
        so the two can never disagree: a configuration whose canonical
        integer form the exact string match would reject (``"007"``) names
        NOBODY here too, rather than an id the identity system classifies
        as non-operator. Empty (accept-all), a group id (negative), and any
        non-positive/non-numeric value ⇒ ``None``. Callers binding an
        approval surface treat ``None`` fail-closed — NOBODY is the
        operator.
        """
        configured = str(self.chat_id or "").strip()
        try:
            user_id = int(configured)
        except ValueError:
            return None
        if user_id <= 0 or not self._user_id_is_operator(user_id):
            return None
        return user_id

    def _origin_clearance_for_user_id(self, user_id) -> str:
        """The per-sender read clearance stamped on a telegram turn (#336)."""
        return (
            TELEGRAM_OPERATOR_CLEARANCE if self._user_id_is_operator(user_id)
            else TELEGRAM_NON_OPERATOR_CLEARANCE
        )

    async def _handle(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle an incoming Telegram text message.

        #317: the body runs under the per-chat serialization lock — /new
        awaits its whole reset in here, so a follow-up message for the SAME
        chat cannot be enqueued (and thus cannot have its turn processed)
        until the reset finished. Normal messages hold the lock only for the
        brief enqueue; distinct chats never contend."""
        if update.message is None or update.message.text is None:
            return

        chat_id = str(update.effective_chat.id) if update.effective_chat else self.chat_id
        # #1379: a text in between ends the files sent together
        self._ack_bursts.pop((chat_id, None), None)
        lock = self._chat_serial_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            await self._handle_serialized(update, chat_id)

    async def _handle_serialized(self, update: Update, chat_id: str) -> None:
        """Body of ``_handle``, run under ``_chat_serial_locks[chat_id]`` (#317)."""
        # S4 §3: a swipe-reply on a retained specialist post is that
        # specialist's turn — judged FIRST, ahead of the /new interception, so
        # "/new, start over" on a Finance post is Finance's turn, as text: it
        # resets nothing, and the desk turn's context says so (#1198).
        # Everything that does not route takes the path below, byte for byte.
        if getattr(update.message, "reply_to_message", None) is not None:
            # A reply to a live job's post goes into that job, ahead of the
            # desk: the job is the one that is waiting on the operator.
            if await self._maybe_route_job_reply(update, chat_id):
                return
            # #1522: a reply to an ended job's post is the resident's turn,
            # with the job's end in its note — never the desk, which cannot
            # see the job.
            ended = self._ended_job_quoted(
                chat_id, update.effective_user, update.message.reply_to_message)
            if ended is None and await self._maybe_route_desk_reply(update, chat_id):
                return
        # /new reset — intercept before rate-limiting or bus dispatch (spec §4.2 #2, C2).
        text = (update.message.text or "").strip()
        # M10 (v0.52.0): tolerate a "/new@botname" mention suffix (defensive —
        # /new is a DM command and private clients don't append it, but a
        # forwarded/group-origin update could).
        _first = text.split()[0].lower() if text else ""
        if _first.split("@", 1)[0] == "/new":
            # v0.76.0 (W5b, r1-B4): cancel any pending resident_ask/authz
            # records and purge authorization grants for this chat
            # SYNCHRONOUSLY, before the first await below — a /new reset
            # must not race a still-pending question or a live grant. The
            # channel's chat_id is a STRING; the broker/grant store are
            # int-keyed, so convert via strict_positive_id first.
            _chat_int = strict_positive_id(chat_id)
            if _chat_int is not None:
                from authz_grants import GRANTS
                from verdict_broker import BROKER
                BROKER.cancel_scope(
                    namespace="resident_ask", scope=f"dm:{_chat_int}",
                    reason="new_session",
                )
                BROKER.cancel_scope(
                    namespace="resident_ask", scope=f"authz:{_chat_int}",
                    reason="new_session",
                )
                GRANTS.purge_chat(_chat_int)
            # M29: ack BEFORE the reset so the user gets instant feedback.
            # reset_channel is awaited; it only drops the pointer (live
            # capture already holds what the conversation said). Handlers
            # dispatch with block=False (H5), so PTB no longer
            # serializes updates; the per-chat lock held by _handle (#317) is
            # what guarantees no same-chat follow-up can resume the old
            # session mid-reset.
            if self._app is not None:
                try:
                    await self._app.bot.send_message(
                        chat_id=chat_id,
                        text="Starting fresh — I still remember what matters.",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("/new ack send to chat_id=%s failed: %s", chat_id, exc)
            if self._session_registry is not None:
                from session_registry import (
                    build_scoped_session_key, reset_channel,
                )
                channel_key = build_scoped_session_key(
                    "telegram", self.default_agent, chat_id,
                )
                await reset_channel(channel_key, self._session_registry)
            return  # do NOT feed /new to the agent

        await self._dispatch_dm_turn(update, chat_id, update.message.text,
                                     user=update.effective_user)

    async def _dispatch_dm_turn(self, update: Update, chat_id: str, content: str, *,
                                user: Any, received_file: dict | None = None) -> bool:
        """The direct chat's turn admission, run under ``_chat_serial_locks[chat_id]``:
        the rate decision, the typed-answer ask cancellation, the trusted identity,
        the typing lease and ONE bus message for the default agent. ``False`` when
        nothing was dispatched: the rate limiter refused the turn, or the memory
        wipe's restart is under way. #1378: a file's
        caption takes this same path, with ``received_file`` naming the stored file."""
        # The memory wipe's courtesy line: the Supervisor accepted the app
        # restart, so a turn now would only be cut off. A message nicety, not
        # a guarantee — one that slips past is handled, and wiped with the rest.
        from memory import maintenance as memory_maintenance
        if memory_maintenance.restart_pending():
            await self._send_plain_line(chat_id, memory_maintenance.courtesy_line())
            return False
        # Rate limit BEFORE typing indicator or bus dispatch (spec 5.2 §8) —
        # and BEFORE the typed-answer ask cancellation below (#347): a
        # rate-limited reply is dropped, so it must not expire the pending
        # question it will never answer. Pre-fix the cancel ran first and a
        # "Slow down"-rejected answer left the ask expired with the answer
        # lost; now the question stays live for the operator's next attempt.
        if self._rate_limiter is not None and self._rate_limiter.enabled:
            decision = self._rate_limiter.check(chat_id)
            if not decision.allowed:
                if decision.should_notify:
                    logger.info(
                        "Telegram rate limit hit for chat_id=%s; replying with one-shot notice",
                        chat_id,
                    )
                    await self._send_rate_limit_reply(chat_id)
                return False

        # v0.76.0 (W5b, r1-B2): a same-DM plain-text reply resolves any
        # pending resident_ask ("the text IS the answer") — cancel it BEFORE
        # normal dispatch so the finish hook edits the stale keyboard to
        # expired while this text still proceeds to the agent as a normal
        # turn. Only the plain-ask `dm:` scope is affected; `authz:`
        # challenges are untouched here (Task 5+).
        #
        # #648: HUMAN-raised asks only. Since v0.206.0 (#573) a SCHEDULED
        # question lives in this same `dm:` scope, and for it the rationale
        # above is false — its answer routes to the scheduled session that
        # asked it, which this turn's text can never reach, so ordinary
        # conversation was expiring a waiting question for nothing. The
        # selection is marker-bound AND scope-bound (cancel_where is
        # namespace-wide), and it stays here, AFTER the rate-limit return
        # (#347) and before dispatch.
        _chat_int = strict_positive_id(chat_id)
        if _chat_int is not None:
            import scheduled_asks
            scheduled_asks.cancel_non_scheduled_for_chat(
                _chat_int, "typed_answer")

        user_name = user.first_name if user else "unknown"

        inherited = cid_var.get()
        cid = inherited if inherited != "-" else new_cid()

        # Task 9: server-created trusted ingress identity (never decoded
        # from payload) — the sole source Agent._process reads to persist
        # this turn's user provenance for the session-resume identity gate.
        # Resolved through the declarative ingress table (#203) so this
        # route can never silently lose its identity. #336: identity is
        # per-SENDER — only the configured operator resolves to the operator
        # peer + private clearance; a sender-less update (anonymous group/
        # channel post, only reachable in accept-all mode) raises rather
        # than borrowing the operator's identity, and that failure kills
        # the turn loudly per #203.
        #
        # Resolved BEFORE the typing lease starts (Terra, review r1): that
        # raise produces no bus turn, so a lease taken first would never be
        # released and the chat would show "typing…" forever.
        trusted_origin = ingress_identity(
            "telegram",
            sender_id=str(user.id) if user is not None else None,
            sender_display_name=user_name if user is not None else None,
            sender_is_operator=self._sender_is_operator(user),
        )

        # r1-1: start this organic turn's typing lease keyed by its cid — the
        # SAME cid placed in the delivery context below, so send()/streaming
        # first-token/turn_finished release exactly this lease.
        self._start_typing(chat_id, cid)

        # This context dict is entirely Casa-owned (built from the Telegram
        # `Update`, not caller-supplied) — routed through
        # sanitize_external_context() for uniformity with the other
        # ingresses (A:§3.5); a no-op here since none of these keys are
        # reserved.
        context = sanitize_external_context({
            "chat_id": chat_id,
            # Bug 8 (v0.14.6): the sender's user id, for the attribution
            # and clearance decisions downstream. Stored as int when present
            # (Telegram user ids are ints).
            "user_id": user.id if user else None,
            "user_name": user_name,
            "message_id": str(update.message.message_id),
            "cid": cid,
        })
        # Release A origin markers, telegram edition (#336): stamped
        # server-side AFTER sanitization (the keys are reserved, so nothing
        # external can set them) — the recall gate reads the per-sender
        # clearance off these instead of the channel-wide private constant.
        context["_origin_route"] = "telegram"
        context["_origin_clearance"] = trusted_origin.server_origin.clearance
        # #283: live-operator marker — reserved (sanitize strips it from
        # every external context), stamped ONLY for the authenticated
        # operator sender. The agent-spawn cap exempts exactly these turns;
        # synthesized/scheduled/delegated turns never carry it.
        if self._sender_is_operator(user):
            context["_operator_turn"] = True
            # Browser executor §5.5: the operator's own words for this turn —
            # the text he typed (or a file's caption), on a reserved key
            # stamped only here, so run admission never reads anything else.
            context["_operator_words"] = content
        # #1314: a reply the desk did not take still tells the resident what
        # it answered — Casa's note, on a reserved key; the text stays raw.
        reply_note = self._reply_note_for(update, chat_id)
        if reply_note is not None:
            context["_reply_note"] = reply_note
        # #1378: the file a caption was written on — Casa's note of it and the
        # turn's own file — on a reserved key, stamped only here.
        if received_file is not None:
            context["_received_file"] = received_file

        msg = BusMessage(
            type=MessageType.CHANNEL_IN,
            source="telegram",
            target=self.default_agent,
            content=content,
            channel="telegram",
            context=context,
            trusted_user_origin=trusted_origin,
        )
        if received_file is None:
            await self._bus.send(msg)
            return True
        # #1378: a caption turn reports whether the agent's queue took it, so a
        # dropped turn falls back to the acknowledgement rather than to silence
        if await self._bus.send_checked(msg) == "accepted":
            return True
        self._stop_typing(chat_id, cid)
        return False

    def _reply_note_for(self, update: Update, chat_id: str) -> str | None:
        """#1314: the note of what a message the desk did not take replied to,
        or ``None`` for a message that is not a reply. Who posted it is decided
        from Casa's post map and Telegram's sender ids, never from its text: a
        message sent on behalf of a chat names no person (its ``from_user`` may
        be a placeholder shared by every anonymous admin)."""
        import result_broker
        import specialist_desk
        message = update.message
        quoted = getattr(message, "reply_to_message", None)
        if quoted is None:
            return None
        chat = strict_positive_id(chat_id)
        record = (result_broker.POST_MAP.get(chat, getattr(quoted, "message_id", None))
                  if chat is not None else None)
        quoted_from = getattr(quoted, "from_user", None)
        sender = getattr(message, "from_user", None) or update.effective_user
        bot = getattr(self._app, "bot", None) if self._app is not None else None
        try:
            bot_id = getattr(bot, "id", None)
        except Exception:  # noqa: BLE001 — an uninitialised bot has no id yet
            bot_id = None
        ended_job = self._ended_job_quoted(chat_id, sender, quoted)
        if ended_job is not None:
            # #1522: ahead of the post map, which names a specialist-hosted
            # job's plugin post as that specialist's
            who = self._ended_job_phrase(ended_job)
        elif record is not None:
            who = ("a message Casa posted on your behalf" if record.role == self.default_agent
                   else f"a message Casa posted for {specialist_desk.label_for(record.role)}")
        elif (getattr(quoted, "sender_chat", None) is not None
              or getattr(message, "sender_chat", None) is not None):
            who = "a message sent on behalf of a chat"
        elif quoted_from is not None and bot_id is not None and quoted_from.id == bot_id:
            # #1335: the post map never held the resident's own replies, so
            # nothing was lost; nor does its absence prove who wrote it (a
            # specialist's file, Casa's notices, a forgotten post)
            who = ("an earlier message from Casa with no record of who wrote it (Casa keeps "
                   "none for your own replies, so it is most likely one of yours)")
        elif quoted_from is not None and sender is not None and quoted_from.id == sender.id:
            who = "their own earlier message"
        else:
            who = "someone else's message"
        posted = None
        date = getattr(quoted, "date", None)
        if date is not None:
            try:
                posted = date.astimezone(resolve_tz()).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError, AttributeError):
                posted = None
        text = getattr(quoted, "text", None) or getattr(quoted, "caption", None)
        return specialist_desk.reply_note(who, posted, text)

    # ------------------------------------------------------------------
    # A reply to a live background job's post in the direct chat
    # ------------------------------------------------------------------

    async def _maybe_route_job_reply(self, update: Update, chat_id: str) -> bool:
        """The sender is the authenticated operator, the quoted message is a
        post the job record filed (its start line, a question, a plugin
        post), the record counts as live, and it is this operator's job in
        this chat — then the chat's rate decision, then the reply goes into
        the job (``deliver_operator_reply``). ``True`` when the message was
        consumed here; anything else falls through to the routes below
        unchanged. A reply the job does not take up is told, never sent
        anywhere else."""
        reg = self._engagement_registry
        owner_of = getattr(reg, "job_post_owner", None) if reg is not None else None
        user = update.effective_user
        chat = strict_positive_id(chat_id)
        if (owner_of is None or chat is None or user is None
                or not self._sender_is_operator(user)):
            return False
        quoted = update.message.reply_to_message
        rec = owner_of(getattr(quoted, "message_id", None))
        if not self._job_is_theirs(rec, user, chat):
            return False
        if self._rate_limiter is not None and self._rate_limiter.enabled:
            decision = self._rate_limiter.check(chat_id)
            if not decision.allowed:
                if decision.should_notify:
                    logger.info("Telegram rate limit hit for chat_id=%s (job reply); "
                                "replying with one-shot notice", chat_id)
                    await self._send_rate_limit_reply(chat_id)
                return True
        await self.deliver_operator_reply(
            rec, update.message.text, chat_id=chat,
            message_id=update.message.message_id,
            quoted_text=(getattr(quoted, "text", None)
                         or getattr(quoted, "caption", None)),
            user_name=getattr(user, "first_name", None) or "unknown",
            user_id=user.id)
        return True

    @staticmethod
    def _job_is_theirs(rec, user, chat: "int | None") -> bool:
        """The job record was started by *user* in *chat*: its origin names
        that user, and that chat when it names one."""
        origin = getattr(rec, "origin", None)
        if not isinstance(origin, dict) or user is None or chat is None:
            return False
        owner = strict_positive_id(origin.get("user_id"))
        job_chat = origin.get("chat_id")
        return owner is not None and owner == getattr(user, "id", None) and (
            job_chat is None or strict_positive_id(job_chat) == chat)

    def _ended_job_quoted(self, chat_id: Any, user: Any, quoted: Any):
        """#1522: the ended job record whose direct chat post *quoted* is,
        when the sender is the authenticated operator who started that job in
        this chat — the live route's own checks; otherwise ``None``. Such a
        reply is the resident's, with the job's end named, never the desk's:
        the desk cannot see the job."""
        reg = self._engagement_registry
        owner_of = getattr(reg, "ended_job_post_owner", None) if reg is not None else None
        chat = strict_positive_id(chat_id)
        if (owner_of is None or quoted is None or chat is None
                or not self._sender_is_operator(user)):
            return None
        rec = owner_of(chat, getattr(quoted, "message_id", None))
        return rec if self._job_is_theirs(rec, user, chat) else None

    def _ended_job_phrase(self, rec) -> str:
        """#1522: who posted a quoted message of an ended job, and how and when
        the job ended — every fact read from its record."""
        if rec.role_or_type == self.default_agent:
            whose = "your"
        else:
            whose = f"{self._job_agent_name(rec)}'s"
        plugin_job = (getattr(rec, "origin", None) or {}).get("plugin_job")
        plugin = plugin_job.get("plugin") if isinstance(plugin_job, dict) else None
        via = f" (run by the plugin {plugin})" if isinstance(plugin, str) and plugin else ""
        ended = f"which ended ({rec.status})"
        completed_at = getattr(rec, "completed_at", None)
        if isinstance(completed_at, (int, float)) and not isinstance(completed_at, bool):
            try:
                ended += " at " + datetime.fromtimestamp(
                    completed_at, tz=resolve_tz()).strftime("%Y-%m-%d %H:%M")
            except (OverflowError, OSError, ValueError):
                pass
        return (f"a message Casa posted for {whose} background job "
                f"«{self._job_title(rec)}»{via}, {ended}")

    # ------------------------------------------------------------------
    # S4: the specialist desk — a swipe-reply on a specialist's post
    # ------------------------------------------------------------------

    async def _maybe_route_desk_reply(self, update: Update, chat_id: str) -> bool:
        """INV-DESK-005: four positive conditions — the sender is the
        authenticated operator, the quoted message is retained in the post
        map for this chat and was posted for this operator, the poster is not
        this chat's resident, and the poster holds a desk (a specialist or the
        configurator) the resident may delegate to now — then the chat's rate decision, exactly as today's
        path takes it, then ONE tracked desk task. ``True`` when the message
        was consumed here (routed, or rate-limited with the notice)."""
        import result_broker
        import specialist_desk
        quoted = update.message.reply_to_message
        user = update.effective_user
        chat = strict_positive_id(chat_id)
        if chat is None or user is None or not self._sender_is_operator(user):
            return False
        record = result_broker.POST_MAP.get(chat, getattr(quoted, "message_id", None))
        if (record is None or record.operator_id != user.id
                or record.role == self.default_agent
                or not specialist_desk.desk_target_ok(self.default_agent, record.role)):
            return False
        if self._rate_limiter is not None and self._rate_limiter.enabled:
            decision = self._rate_limiter.check(chat_id)
            if not decision.allowed:
                if decision.should_notify:
                    logger.info("Telegram rate limit hit for chat_id=%s (desk reply); "
                                "replying with one-shot notice", chat_id)
                    await self._send_rate_limit_reply(chat_id)
                return True
        reservation = await self._reserve_desk_or_refuse(chat, record.role)
        if reservation is None:
            return True
        cid = new_cid()
        self._start_typing(chat_id, cid)
        self._spawn_desk_turn(
            chat_id=chat, user_id=user.id, user_name=user.first_name or "unknown",
            message_id=getattr(update.message, "message_id", None), cid=cid,
            text=update.message.text,
            quoted_text=(getattr(quoted, "text", None) or getattr(quoted, "caption", None)),
            record=record, desk_role=record.role, continuation=False,
            reservation=reservation)
        return True

    async def _reserve_desk_or_refuse(self, chat_id: int, desk_role: str):
        """§4: the queue place is taken BEFORE any task exists; a full queue is
        the busy notice, inline, and no task."""
        import specialist_desk
        desk = specialist_desk.DESKS.get_or_create(chat_id, desk_role)
        if desk.faulted:
            # S5 §14.8: no queueing on a faulted desk
            line = specialist_desk.faulted_line(specialist_desk.label_for(desk_role))
            await self.deliver_desk_notice(chat_id, line)
            specialist_desk.record_echo(chat_id, line)
            return None
        reservation = desk.reserve()
        if reservation is None:
            # S6 §2.2: a past event, composed within the echo cap (one-line S4 rewording)
            line = specialist_desk.bounded_line("{label}'s desk was full when the place was requested.",
                                                label=specialist_desk.label_for(desk_role))
            await self.deliver_desk_notice(chat_id, line)
            specialist_desk.record_echo(chat_id, line)
        return reservation

    def _spawn_desk_turn(self, **kwargs: Any) -> None:
        """One tracked background task per desk use (the shape of the
        engagement turn task), so the per-chat serial lock is released at
        once and a stop can drain it."""
        import specialist_desk
        task = asyncio.create_task(specialist_desk.handle_reply(
            channel=self, resident_role=self.default_agent, **kwargs))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        reservation = kwargs.get("reservation")
        if reservation is not None:
            # a task cancelled before its coroutine starts has no finally
            task.add_done_callback(reservation.release)

    async def _dispatch_desk_continuation(
        self, *, chat_id: int, user_id: int, desk_role: str, request_id: str, text: str,
    ) -> bool:
        """§7: an approval raised inside a desk turn continues the DESK — a
        new desk turn whose task is the continuation text — admitted by the
        route's own conditions at dispatch time: the approver is the
        authenticated operator and the specialist is still one the resident
        may delegate to. Otherwise one labelled notice and no specialist run
        (the pending grant is left to expire). Returns True when the operator
        was told either way."""
        import specialist_desk
        if (not self._user_id_is_operator(user_id)
                or not specialist_desk.desk_target_ok(self.default_agent, desk_role)):
            line = f"{specialist_desk.label_for(desk_role)} could not continue (not delegable)."
            await self.deliver_desk_notice(chat_id, line)
            specialist_desk.record_echo(chat_id, line)     # §9: the echo says the same
            return True
        reservation = await self._reserve_desk_or_refuse(chat_id, desk_role)
        if reservation is None:
            return True
        cid = new_cid()
        self._start_typing(str(chat_id), cid)
        self._spawn_desk_turn(
            chat_id=chat_id, user_id=user_id, user_name="operator", message_id=None,
            cid=cid, text=text, quoted_text=None, record=None, desk_role=desk_role,
            continuation=True, reservation=reservation)
        return True

    async def _on_proposal_callback(self, cq: Any, rid: str, idx: int | None) -> None:
        """S5 §4.1: the tap's admission, fail closed, nothing claimed before
        every check passes — presence, the live record, the chat, the
        MESSAGE the keyboard was posted as, the index, the operator (the one
        the proposal was posted for AND still the operator now), S5's own
        deadline — then claim and commit once. The handler NEVER edits or
        dispatches: the finish hook installed at post time owns everything
        after the commit, as the resident_ask contract does.

        #1305: a request with no message id yet (its landing was never
        confirmed) is matched by the rid in the callback data instead; a LIVE
        one is bound to the tapped message before the claim, and a settled one
        answers its settled word."""
        from verdict_broker import BROKER

        toast = "expired"
        try:
            if (cq.message is None or getattr(cq.message, "chat", None) is None
                    or cq.from_user is None):
                return
            chat = cq.message.chat.id
            scope = f"proposal:{chat}"
            # #1201: a settled proposal's own meta first — the broker's
            # tombstone holds a copy from settle time, without the message id
            # a send that landed after a supersede wrote to the original
            settled = self._settled_proposal(rid)
            meta = (settled[0] if settled is not None
                    else BROKER.get_meta(namespace="proposal", scope=scope, request_id=rid))
            if meta is None:
                return
            if meta.get("chat_id") != chat:
                return
            tapped = getattr(cq.message, "message_id", None)
            # #1305: a card whose landing was never confirmed has no message id
            # yet; the rid in its callback data proves the card instead
            unbound = meta.get("message_id") is None and isinstance(tapped, int)
            if not unbound and meta.get("message_id") != tapped:
                return
            options = meta.get("options") or []
            if idx is None or not (0 <= idx < len(options)):      # S6: by the buttons, not the calls
                toast = "invalid"
                return
            expected = meta.get("operator_id")
            if (expected is None or cq.from_user.id != expected
                    or not self._user_id_is_operator(cq.from_user.id)):
                toast = "not for you"
                return
            deadline = meta.get("deadline")
            if not isinstance(deadline, (int, float)) or asyncio.get_running_loop().time() >= deadline:
                return
            if settled is not None:
                toast = settled[1]          # what happened, only after every check passed
                return
            if unbound:
                # bind only a LIVE request's own meta, never a retired copy
                if not BROKER.is_live_unclaimed(namespace="proposal", scope=scope,
                                                request_id=rid):
                    return
                meta["message_id"] = tapped
                logger.info("proposal %s bound to its tapped message", rid[:8])
            kinds = meta.get("kinds") or []
            if idx < len(kinds) and kinds[idx] == "url":
                return          # slice 5f: a link button sends no callback; one naming it is void
            if idx < len(kinds) and kinds[idx] == "keep_card":
                # #1362: a keep_card tap claims and commits nothing — the card keeps its
                # text and every button; the file goes out through the same desk use
                if not BROKER.is_live_unclaimed(namespace="proposal", scope=scope,
                                                request_id=rid):
                    return
                toast = "✔"
                self._spawn_keep_tap(meta=meta, idx=idx, request_id=rid)
                return
            claim = BROKER.claim(namespace="proposal", scope=scope, request_id=rid,
                                 option_index=idx, actor_id=cq.from_user.id)
            if isinstance(claim, str):
                toast = {"duplicate": "already answered", "stale": "expired",
                         "forbidden": "not for you"}[claim]
                return
            if BROKER.commit(claim):
                toast = "✔"
                kinds = meta.get("kinds") or []
                if idx < len(kinds) and kinds[idx] == "arm_file":
                    # S6 §2.4: the arming is written HERE, synchronously, before any
                    # await — the finish hook runs as a separate task and a file can
                    # arrive before it; a later arming replaces this one (R4-1: no
                    # message follows; the edited keyboard is the acknowledgement)
                    self._armings[chat] = {
                        "chat_id": chat, "operator_id": expected,
                        "role": str(meta.get("role") or ""),
                        "artifact_id": str(meta.get("artifact_id") or ""),
                        "expires_at": time.monotonic() + self.FILE_ARM_TTL_S,
                    }
        finally:
            t = asyncio.create_task(_safe_answer(cq, toast))
            try:
                await asyncio.shield(t)
            except asyncio.CancelledError:
                await t
                raise

    async def _on_run_stop_callback(self, cq: Any, cid: str) -> None:
        """The Stop on a browsing request's start or status card (``v1|run_stop|<cid>|0``):
        fail closed, exactly one answer. The admission (durable thirty days, so a Stop after
        a restart works) must be ``admitted``; the tap must come from its chat and from its
        operator, who is still the operator as configured now. Then the toast at once, and
        one tracked task stops the request directly (``plugin_stop``): no desk, no model."""
        import plugin_bindings
        import plugin_stop
        import run_admission
        from operator_calls import is_configured_operator

        toast = "expired"
        try:
            if (cq.message is None or getattr(cq.message, "chat", None) is None
                    or cq.from_user is None):
                return
            c = run_admission.CONFIRMATIONS.peek(cid)
            if c is None or c.status != "admitted":
                return
            b = plugin_bindings.BINDINGS.peek(c.binding_ref)
            if b is None:
                return
            if (cq.message.chat.id != b.chat_id or cq.from_user.id != c.operator_id
                    or c.operator_id != b.operator_id
                    or not is_configured_operator(b.chat_id, b.operator_id)):
                toast = "not for you"
                return
            toast = "stopping…"
            tapped = getattr(cq.message, "message_id", None)
            role = b.chain[-1]

            async def _tell(outcome: dict) -> None:
                o = outcome.get("outcome")
                mark = plugin_stop.MARKS.get(o)
                if mark is not None:
                    await plugin_stop.settle_run_card(c, mark)
                if o in ("stopped", "ended", "not_taken", "unavailable"):
                    await plugin_stop.tell_line(b.chat_id, role, o, reply_to=tapped)

            async def _run() -> None:
                try:
                    await plugin_stop.stop(role, c.plugin, f"request:{c.request_key}",
                                           operator_id=c.operator_id, tell=_tell)
                except Exception:  # noqa: BLE001 — logged; the card keeps its Stop
                    logger.exception("run stop failed (cid=%s)", cid[:13])

            task = asyncio.create_task(_run())
            self._turn_tasks.add(task)
            task.add_done_callback(self._turn_tasks.discard)
        finally:
            t = asyncio.create_task(_safe_answer(cq, toast))
            try:
                await asyncio.shield(t)
            except asyncio.CancelledError:
                await t
                raise

    def proposal_finish_hook(self, *, rid: str, req: Any) -> Callable[[dict], Any]:
        """S5 §4.2/§4.4: the one owner of what follows a proposal's
        settlement. A tap: the keyboard edited away FIRST with the chosen
        label (``⏳ <label>`` for a call, ``☑ <label>`` for ``📎``), then the
        desk hand-off; every other outcome — the TTL, a
        supersede, a cancel — only edits the keyboard. The meta is read at
        settle time from the broker's own dict."""
        async def _finish(outcome: dict) -> None:
            meta = getattr(req, "meta", None) or {}
            chat_id, message_id = meta.get("chat_id"), meta.get("message_id")
            text = str(meta.get("text") or meta.get("label") or "")
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                reason = outcome.get("reason") if isinstance(outcome, dict) else None
                line = "↻ replaced" if reason == "superseded" else "⌛ expired"
                # settled while its own send is still in flight (a supersede
                # racing the post): the poster applies this terminal line the
                # moment the message lands. #1339: recorded for a BOUND record
                # too — an in-place edit binds its message id before the edit,
                # and that edit landing after this mark would otherwise restore
                # a keyboard whose record is gone (x1, both reviewers)
                meta["settled_line"] = line
                if not isinstance(message_id, int):
                    return
                if not isinstance(chat_id, int):
                    return
                await self._edit_proposal_message(meta, f"{text}\n{line}")
                return
            if not isinstance(chat_id, int) or not isinstance(message_id, int):
                return
            idx = outcome.get("option_index")
            labels = meta.get("options") or []
            label = labels[idx] if isinstance(idx, int) and 0 <= idx < len(labels) else "?"
            kinds = meta.get("kinds") or []
            if isinstance(idx, int) and idx < len(kinds) and kinds[idx] == "close":
                # #1375: a Close tap removes the keyboard and nothing else — the text stays
                # as posted, no desk use, no plugin call, no model turn, nothing sent; the
                # commit already makes every later tap "already answered"
                await self.clear_dm_keyboard(chat_id, message_id)
                return
            if isinstance(idx, int) and idx < len(kinds) and kinds[idx] == "arm_file":
                # S6 §2.4 / R4-1: an arm_file tap edits the keyboard and sends NOTHING; a
                # failed edit is told with past facts only — never "applying your tap",
                # never where the next file goes; nothing is dispatched
                import specialist_desk
                edited = await self._edit_proposal_message(meta, f"{text}\n☑ {label}")
                if not edited:
                    logger.warning("arm_file keyboard edit failed (rid=%s); telling by notice", rid[:8])
                    await self.deliver_desk_notice(chat_id, specialist_desk.bounded_line(
                        "{label} ☑ {button} — the buttons could not be cleared.",
                        label=specialist_desk.label_for(str(meta.get("role") or "")),
                        fields={"button": label}))
                return
            # A stored call of the declared stop tool starts at once, before any edit of the
            # card (a slow or flood-limited edit must not keep the run going): no desk, no
            # turn, and no ⏳ line — its own outcome mark is the card's one edit
            try:
                if await self._stop_tap(meta=meta, idx=idx):
                    return
            except Exception:  # noqa: BLE001 — never raise into the hook drain
                logger.exception("stop tap failed (rid=%s)", rid[:8])
                await self._edit_proposal_message(meta, f"{text}\n✖ failed")
                return
            # #1341: a call tap's card says it is working, not settled — the
            # call's answer decides what it becomes (☑, ✖, or the next card in
            # place); handle_tap writes that line
            edited = await self._edit_proposal_message(meta, f"{text}\n⏳ {label}")
            if not edited:
                # the keyboard could not be edited away (Terra, diff round 5):
                # the operator still sees which button won BEFORE any effect —
                # one labelled notice names it; the commit already excludes a
                # second execution, so the stale buttons only answer a toast
                import specialist_desk
                logger.warning("proposal keyboard edit failed (rid=%s); telling by notice", rid[:8])
                await self.deliver_desk_notice(
                    chat_id, f"{specialist_desk.label_for(str(meta.get('role') or ''))} ☑ {label} — "
                             "applying your tap (the buttons could not be cleared).")
            try:
                await self._dispatch_proposal_tap(meta=meta, idx=idx, request_id=rid)
            except Exception:  # noqa: BLE001 — never raise into the hook drain
                logger.exception("proposal tap dispatch failed (rid=%s)", rid[:8])
                await self._edit_proposal_message(meta, f"{text}\n✖ failed")

        def _settle(outcome: dict) -> Any:
            # #1201: the broker calls this synchronously inside its own
            # settlement, so a tap can never see the broker settled and this
            # memory not yet written; the edits stay in the task it returns
            self._remember_settled_proposal(rid, getattr(req, "meta", None), outcome)
            return _finish(outcome)
        return _settle

    def _remember_settled_proposal(self, rid: str, meta: Any, outcome: Any) -> None:
        """#1201: record a proposal's settlement for its tap toast — answered
        is "already answered", a supersede "replaced", anything else (the
        TTL, a shutdown) "expired", the split the finish hook's own line
        makes. Bounded: past its deadline an entry is dropped (the admission
        chain answers "expired" then anyway), and a chat keeps its latest
        ``_PROPOSAL_SETTLED_PER_CHAT``."""
        if not isinstance(meta, dict):
            return
        o = outcome.get("outcome") if isinstance(outcome, dict) else None
        if o == "answered":
            word = "already answered"
        elif o == "cancelled" and outcome.get("reason") == "superseded":
            word = "replaced"
        else:
            word = "expired"
        self._prune_settled_proposals()
        self._proposal_settled[rid] = (meta, word)
        chat = meta.get("chat_id")
        same = [k for k, (m, _) in self._proposal_settled.items() if m.get("chat_id") == chat]
        for k in same[:max(0, len(same) - _PROPOSAL_SETTLED_PER_CHAT)]:
            del self._proposal_settled[k]

    def _settled_proposal(self, rid: str) -> "tuple[dict, str] | None":
        self._prune_settled_proposals()
        return self._proposal_settled.get(rid)

    def _prune_settled_proposals(self) -> None:
        now = asyncio.get_running_loop().time()
        for k in [k for k, (m, _) in self._proposal_settled.items()
                  if not isinstance(m.get("deadline"), (int, float)) or now >= m["deadline"]]:
            del self._proposal_settled[k]

    def _spawn_keep_tap(self, *, meta: dict, idx: int, request_id: str) -> None:
        """#1362: a keep_card tap has no settlement, so no finish hook hands it to
        the desk — the callback does, as one tracked task (a stop drains it)."""
        async def _run() -> None:
            try:
                await self._dispatch_proposal_tap(meta=meta, idx=idx, request_id=request_id)
            except Exception:  # noqa: BLE001 — the card stays live; the tap is logged
                logger.exception("keep_card tap dispatch failed (rid=%s)", request_id[:8])
        task = asyncio.create_task(_run())
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _dispatch_proposal_tap(self, *, meta: dict, idx: int, request_id: str) -> bool:
        """S5 §4.2 steps 2–5: reserve the specialist's desk (a full queue ⇒
        the busy line on the keyboard and the busy notice, nothing runs), take
        the typing lease, and spawn ``specialist_desk.handle_tap`` as one
        tracked desk use with the reservation's done-callback."""
        import specialist_desk
        chat_id, desk_role = int(meta["chat_id"]), str(meta.get("role") or "")
        if await self._stop_tap(meta=meta, idx=idx):
            return True             # a stored call of the declared stop tool: no desk, no turn
        label = specialist_desk.label_for(desk_role)
        desk = specialist_desk.DESKS.get_or_create(chat_id, desk_role)
        # #1362: a keep_card tap never writes a line over its card
        keep = specialist_desk.keeps_card(meta, idx)
        if desk.faulted:
            line = specialist_desk.faulted_line(label)
            await self.deliver_desk_notice(chat_id, line)
            specialist_desk.record_echo(chat_id, line)
            if not keep:
                await self.mark_proposal(meta, "✖ faulted")
            return True
        reservation = desk.reserve()
        if reservation is None:
            labels = meta.get("options") or []
            button = str(labels[idx]) if isinstance(idx, int) and 0 <= idx < len(labels) else "?"
            await self.deliver_desk_notice(
                chat_id, f"{label} is busy; the specialist will propose again, or type your verdict.")
            # §10: the resident's line names the refused tap, as the desk's own refusals do
            specialist_desk.record_echo(chat_id, f"{label} refused your tap ({button}): busy.")
            if not keep:
                await self.mark_proposal(meta, "✖ busy")
            return True
        cid = new_cid()
        self._start_typing(str(chat_id), cid)
        task = asyncio.create_task(specialist_desk.handle_tap(
            channel=self, resident_role=self.default_agent, chat_id=chat_id,
            user_id=int(meta["operator_id"]), cid=cid, desk_role=desk_role, meta=meta,
            idx=idx, request_id=request_id, reservation=reservation))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        task.add_done_callback(reservation.release)
        return True

    async def _stop_tap(self, *, meta: dict, idx: int) -> bool:
        """A tapped button whose stored call names its plugin's declared ``casa.stopTool``
        (the bare name equal, the plugin segment and artifact the proposal's own) reserves no
        desk and runs no pinned turn: the stop is called directly in one tracked task
        (``plugin_stop``), the proposal is marked with its outcome, the reply line is posted
        under it, and the request's start or status card is marked too. False for any other
        call, which goes to the desk as before."""
        import plugin_stop
        calls = meta.get("calls") or []
        call = calls[idx] if isinstance(idx, int) and 0 <= idx < len(calls) else None
        args = call.get("arguments") if isinstance(call, dict) else None
        if not isinstance(args, dict) or set(args) != {"ref"} or not isinstance(args["ref"], str):
            return False
        role, seg = str(meta.get("role") or ""), str(meta.get("plugin_seg") or "")
        artifact = str(meta.get("artifact_id") or "")
        declared = next((tool for r, _cfg, rp, s, tool in await plugin_stop.stop_plugins()
                         if r == role and s == seg and str(rp.artifact_id) == artifact), None)
        if declared is None or call.get("wire_name") != declared:
            return False
        chat_id, operator_id = int(meta["chat_id"]), int(meta["operator_id"])
        ref = args["ref"]
        proposal_mid = meta.get("message_id")

        async def _tell(outcome: dict) -> None:
            o = outcome.get("outcome")
            mark = plugin_stop.MARKS.get(o) or {"unknown": "✖ expired",
                                                "unavailable": "✖ unavailable"}.get(o)
            if mark is not None:
                await self.mark_proposal(meta, mark)
                if o in plugin_stop.MARKS:
                    await plugin_stop.settle_run_card(
                        plugin_stop.confirmation_for(seg, operator_id, ref), mark)
            if o in ("stopped", "ended", "not_taken", "unavailable"):
                await plugin_stop.tell_line(chat_id, role, o, reply_to=proposal_mid)

        async def _run() -> None:
            try:
                out = await plugin_stop.stop(role, seg, ref, operator_id=operator_id,
                                             tell=_tell)
                if out.get("outcome") == "in_flight":
                    await self.mark_proposal(meta, "☑ Stop")
            except Exception:  # noqa: BLE001 — logged; the proposal says it failed
                logger.exception("stop tap failed")
                await self.mark_proposal(meta, "✖ failed")

        task = asyncio.create_task(_run())
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        return True

    async def mark_proposal(self, meta: dict, line: str) -> bool:
        """S5 §10: the proposal message's last line — ``⏳ <label>`` replaced
        by ``☑ <label>``, ``✖ <reason>`` or ``⌛ expired`` — edited in place from the live
        meta's text; the keyboard stays gone. True when the edit landed."""
        chat_id, message_id = meta.get("chat_id"), meta.get("message_id")
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return False
        return await self._edit_proposal_message(meta, f"{meta.get('text', '')}\n{line}")

    async def _edit_proposal_message(self, meta: dict, text: str) -> bool:
        """A proposal message's settle or mark edit, its keyboard removed. A photo
        card (slice 5b) is edited through its caption; when Telegram refuses that
        edit, its keyboard is still removed by a markup-only edit, so a settled
        photo card never keeps live buttons. True only when the text landed."""
        chat_id, message_id = meta.get("chat_id"), meta.get("message_id")
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return False
        if not meta.get("photo"):
            return await self.edit_dm_message(chat_id, message_id, text)
        if await self.edit_dm_message(chat_id, message_id, text, caption=True):
            return True
        await self.clear_dm_keyboard(chat_id, message_id)
        return False

    async def notify_operator(self, text: str) -> bool:
        """A Casa line to the configured operator chat (the memory wipe's
        restart and done lines). One attempt; a failure is logged."""
        if self._app is None or not self.chat_id:
            logger.warning("Telegram channel not ready; operator line not sent")
            return False
        try:
            await self._app.bot.send_message(chat_id=self.chat_id, text=text)
            return True
        except Exception as exc:  # noqa: BLE001 — a notice fault is logged
            logger.warning("operator line failed: %s", type(exc).__name__)
            return False

    async def pause_updates_for_restart(self) -> bool:
        """Stop taking updates before an app restart, so the update that
        carried the operator's consent is confirmed to Telegram and never
        delivered again after the restart. Polling: stopping the updater
        makes one last ``getUpdates`` with the next offset, which confirms
        every update fetched so far (PTB 22.7). A webhook update is already
        acknowledged when it is queued. Returns whether intake was paused."""
        app = self._app
        updater = getattr(app, "updater", None) if app is not None else None
        if (self._webhook_url or updater is None
                or not getattr(updater, "running", False)):
            return False
        await updater.stop()
        return True

    async def resume_updates(self) -> None:
        """Undo :meth:`pause_updates_for_restart` when no restart came."""
        app = self._app
        updater = getattr(app, "updater", None) if app is not None else None
        if (self._webhook_url or updater is None
                or getattr(updater, "running", False)):
            return
        await updater.start_polling()

    async def deliver_desk_notice(self, chat_id: int, text: str) -> bool:
        """§9: ONE Casa-composed, labelled, body-free notice to the operator's
        chat about a desk turn's outcome — a notice, never model text
        (INV-OUT-001's notice class). A failure is logged and nothing more is
        attempted."""
        if self._app is None:
            logger.warning("Telegram channel not started; cannot deliver desk notice")
            return False
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=text)
            return True
        except Exception as exc:  # noqa: BLE001 — a notice fault is logged
            logger.warning("desk notice failed: %s", type(exc).__name__)
            return False

    async def send_handoff_line(self, chat_id: int | str, text: str) -> bool:
        """#1350: Casa's one-line record that the operator's request was
        passed to a specialist or an executor — a notice, never model text,
        sent without a notification sound (the answer that follows notifies).
        A failure is logged and nothing more is attempted."""
        if self._app is None:
            return False
        try:
            await self._app.bot.send_message(
                chat_id=chat_id, text=text, disable_notification=True)
            return True
        except Exception as exc:  # noqa: BLE001 — a notice fault is logged
            logger.warning("hand-off line failed: %s", type(exc).__name__)
            return False

    async def send_step_line(self, chat_id: int, text: str) -> "int | None":
        """The configurator's step line in the operator's DM — Casa's
        text, silent. Returns the message id, or None when not posted."""
        if self._app is None:
            return None
        try:
            msg = await self._app.bot.send_message(
                chat_id=chat_id, text=text, disable_notification=True)
        except Exception as exc:  # noqa: BLE001 — progress is never the work's fault
            logger.info("step line not posted: %s", type(exc).__name__)
            return None
        return getattr(msg, "message_id", None)

    async def delete_dm_message(self, chat_id: int, message_id: int) -> bool:
        """Delete one message Casa posted in a DM; a failure is logged."""
        if self._app is None:
            return False
        try:
            return bool(await self._app.bot.delete_message(
                chat_id=chat_id, message_id=message_id))
        except Exception as exc:  # noqa: BLE001
            logger.info("DM message not deleted: %s", type(exc).__name__)
            return False

    # ------------------------------------------------------------------
    # Engagement routing (Task 11)
    # ------------------------------------------------------------------

    async def _route_to_assistant_default(self, update) -> None:
        """Default behavior for non-engagement chats: feed into existing _handle."""
        await self._handle(update, None)

    async def handle_update(
        self, update, _context: ContextTypes.DEFAULT_TYPE | None = None
    ) -> None:
        """PTB MessageHandler entry-point. Routes by chat_id: the operator's
        direct chat to the assistant; the log group to its read-only pointer.

        The optional ``_context`` parameter exists so PTB's
        ``Application.process_update`` can call this callback with its
        ``(update, context)`` two-arg convention. Direct callers (and
        the test suite) may invoke with just ``update``. The context
        object itself is unused — the channel reads everything it needs
        from ``update`` and Casa's bus/engagement registry.
        """
        msg = getattr(update, "message", None)
        if msg is None:
            return
        chat_id = msg.chat.id
        user_id = msg.from_user.id if msg.from_user else None

        # 1) 1:1 chat with the assistant — existing behaviour
        # Note: self.chat_id may be a string or int — coerce for comparison
        if str(chat_id) == str(self.chat_id):
            return await self._route_to_assistant(update)

        # 2) The log group is read-only: a person's message there, in any
        #    topic, gets one pointer to the direct chat and reaches nothing
        #    else.
        if self.engagement_supergroup_id and chat_id == self.engagement_supergroup_id:
            await self._redirect_from_log_group(msg)
            return

        # 3) Other chats. When telegram_chat_id is configured it is an
        #    allowlist (DOCS.md: "Telegram chat ID to restrict messages
        #    to. Leave empty to accept all chats.") — drop updates from
        #    any other chat. Empty chat_id = accept all (documented opt-in).
        #    Route 1 (the configured DM) and route 2 (the log group) already
        #    returned above, so reaching here means this chat is neither.
        if str(self.chat_id or "").strip():
            logger.info(
                "Dropping Telegram update from unauthorized chat_id=%s "
                "(user_id=%s); telegram_chat_id restriction active",
                chat_id, user_id,
            )
            return
        return await self._route_to_assistant(update)

    async def _post_lost_inbound_notice(
            self, rec, notice_target: "_NoticeTarget | None") -> None:
        if notice_target is None:
            # Every operator message a job is handed names the direct chat
            # message or card to reply to; one without is logged and
            # discharged.
            logger.warning("lost-message notice for %s has no direct chat "
                           "message to reply to; not posted", rec.id[:8])
            return
        ended = False
        reg = self._engagement_registry
        if reg is not None:
            try:
                status = await asyncio.wait_for(
                    reg.settled_status(rec.id),
                    _SETTLED_STATUS_TIMEOUT_S)
                ended = status in ("completed", "cancelled", "error")
            except Exception:  # noqa: BLE001 — the plain wording is still true
                logger.debug("settled read for a lost-message notice failed "
                             "for %s", rec.id[:8], exc_info=True)
        if notice_target.answer:
            text = _LOST_ANSWER_ENDED_NOTICE if ended else _LOST_ANSWER_NOTICE
        else:
            text = _LOST_REPLY_ENDED_NOTICE if ended else _LOST_REPLY_NOTICE
        await self._send_dm_reply(notice_target.chat_id,
                                  notice_target.message_id, text)

    async def _tell_not_delivered(
            self, rec, notice_target: "_NoticeTarget | None") -> None:
        """The one bounded "not delivered, please resend" attempt for an
        operator message a job did not accept. Never retried."""
        try:
            await asyncio.wait_for(
                self._post_lost_inbound_notice(rec, notice_target),
                _TURN_INCOMPLETE_NOTICE_TIMEOUT_S)
        except BaseException:  # noqa: BLE001 — bounded best-effort
            logger.debug("lost-message notice failed for %s", rec.id[:8],
                         exc_info=True)

    async def tell_journal_lost(self, rec, entry: dict) -> None:
        """The lost-message notice for one journaled operator message an
        ended job never read — the journal's terminal branch calls it once
        per such entry. The entry's ``notice`` names the direct chat message
        or tapped card to reply to; an entry without one is logged and
        discharged."""
        await self._tell_not_delivered(rec, _notice_target_from(entry.get("notice")))

    async def _send_dm_reply(self, chat_id: int, message_id: int,
                             text: str) -> None:
        """One Casa line in the operator's direct chat as a reply to
        *message_id* (sent even when that message is gone). Raises on
        failure; every caller bounds and logs it."""
        from telegram import ReplyParameters
        await self.bot.send_message(
            chat_id=chat_id, text=text,
            reply_parameters=ReplyParameters(
                message_id=message_id, allow_sending_without_reply=True))

    async def _post_turn_notice(self, rec, text: str,
                                notice_target: "_NoticeTarget | None") -> None:
        """A notice about one operator message, in the direct chat as a reply
        to it. Without a message to reply to it is logged, not posted."""
        if notice_target is None:
            logger.warning("notice for %s has no direct chat message to "
                           "reply to; not posted", rec.id[:8])
            return
        await self._send_dm_reply(notice_target.chat_id,
                                  notice_target.message_id, text)

    async def _deliver_to_job(
        self, rec, text: str, *, tg_message_id: int | None = None,
        notice_target: "_NoticeTarget | None" = None,
        inbound_reservation: "_InboundReservation | None" = None,
        courtesy: bool = True,
    ) -> bool:
        """Hand one operator message to a job and return: the driver's
        SYNCHRONOUS ``submit_operator`` journals it, admits it (a terminal
        record refuses), stamps the job's activity and queues it for the
        job's delivery worker, which writes it into the running session.
        Nothing here waits on the session. ``True`` when the job accepted
        the message.

        A message the job did not accept draws one bounded "not delivered"
        line, in reply to it (``notice_target``); the caller posts nothing. ``inbound_reservation`` (a tapped continuation's
        lease) is released in the same step the ticket is admitted, so the
        completion gate never sees both empty. ``courtesy``: while a memory
        wipe's restart is pending, the operator's own message is answered
        with its courtesy line instead of being delivered into a session
        that is about to stop."""
        drv = self._engagement_driver
        reg = self._engagement_registry
        if courtesy:
            from memory import maintenance as memory_maintenance
            if memory_maintenance.restart_pending():
                try:
                    await self._post_turn_notice(
                        rec, memory_maintenance.courtesy_line(), notice_target)
                except Exception:  # noqa: BLE001 — a notice fault is logged
                    logger.debug("courtesy line failed for %s", rec.id[:8],
                                 exc_info=True)
                return False
        if reg is not None:
            # A record whose strict terminal write is still pending counts as
            # live until that write settles: a message in that window is
            # accepted after a rollback and refused after a commit.
            pending = getattr(reg, "terminal_pending", None)
            if callable(pending) and pending(rec.id) is True:
                await reg.settle_pending(rec.id)
            rec = reg.get(rec.id) or rec
        if self._stopping or drv is None:
            await self._tell_not_delivered(rec, notice_target)
            return False
        try:
            drv.submit_operator(rec, text, tg_message_id=tg_message_id,
                                notice=_notice_dict(notice_target))
        except Exception as exc:  # noqa: BLE001 — refused: nothing was accepted
            logger.info("message for job %s not accepted: %s", rec.id[:8], exc)
            await self._tell_not_delivered(rec, notice_target)
            return False
        if inbound_reservation is not None:
            inbound_reservation.release()
        return True

    def engagement_lifecycle_lock(self, rec) -> asyncio.Lock:
        """The per-engagement lifecycle lock: the one every resume owner of a
        job's session takes — the driver's delivery worker, its recovery
        after a session death and the boot resume — so at most one client
        per job is ever opened. Keyed by engagement id; no Telegram handler
        holds it, so a slow resume never delays the next message being
        accepted."""
        return self._engagement_lifecycle_locks.setdefault(rec.id, asyncio.Lock())

    def engagement_inbound_reservation(
        self, engagement_id: str,
    ) -> "_InboundReservation":
        """#663: the SYNCHRONOUS ingress-reservation lease for one broker-driven
        system continuation.

        A continuation's text does not exist anywhere until
        :meth:`deliver_system_turn` admits it, so between the operator's tap
        committing and that admission there is nothing for the completion gate
        to be vetoed on — and a successful ``emit_completion`` landing in that
        window commits, the record goes terminal, and the continuation is
        dropped. The lease closes it by taking a reservation at the one
        SYNCHRONOUS instant inside the window (a challenge's
        ``on_commit_sync``, which runs with no await after ``BROKER.commit``)
        and handing it over to the ticket at the seam.
        """
        return _InboundReservation(self, engagement_id)

    async def _dispatch_engagement_continuation(
        self, *, engagement_id: str, text: str,
        inbound_reservation: "_InboundReservation | None" = None,
        notice_target: "tuple[int, int] | None" = None,
    ) -> bool:
        """#400: continue a job after the operator authorized (or denied) a
        protected plugin tool over the direct chat authorization keyboard.

        The authz finish hook calls this when the challenge was
        engagement-origin. It looks the record up by id and hands the
        ``[authorization approved|denied]`` message to
        :meth:`deliver_system_turn`, which journals, admits and queues it and
        returns at once.

        Returns ``True`` when the job accepted the message, ``False`` when the
        engagement is unknown (the finish hook then shows a delivery-failed
        note on the card) or did not accept it (the seam told the operator,
        in reply to the card). ``True`` is not a delivery receipt: the job's
        delivery worker writes the message later.

        ``notice_target`` — ``(chat_id, message_id)`` of the approval card —
        is where the continuation's one notice goes if the job never reads it;
        the seam or the journal's terminal branch posts it, never the caller."""
        reg = self._engagement_registry
        rec = reg.get(engagement_id) if reg is not None else None
        if rec is None:
            logger.info(
                "authz continuation: engagement %s unknown — cannot resume",
                str(engagement_id)[:8],
            )
            return False
        target = None
        if notice_target is not None:
            chat, mid = notice_target
            if type(chat) is int and type(mid) is int:
                target = _NoticeTarget(chat_id=chat, message_id=mid, answer=True)
        return await self.deliver_system_turn(
            rec, text, inbound_reservation=inbound_reservation,
            notice_target=target)

    async def deliver_system_turn(
        self, rec, text: str,
        *, inbound_reservation: "_InboundReservation | None" = None,
        notice_target: "_NoticeTarget | None" = None,
    ) -> bool:
        """An operator's tapped answer or an approval's continuation, into
        the job: journaled, admitted, queued, and this returns
        (:meth:`_deliver_to_job`). ``notice_target`` names the card the
        operator tapped; such a message ends a quiet run's quietness first,
        and if the job never reads it the one notice goes there.

        Returns whether the job accepted it. ``inbound_reservation`` is the
        tap's lease (#663), handed over to the ticket in the step that admits
        it; its owner releases it on every other path."""
        if notice_target is not None and not await self._end_quiet_run(rec):
            await self._tell_not_delivered(rec, notice_target)
            return False
        return await self._deliver_to_job(
            rec, text, notice_target=notice_target,
            inbound_reservation=inbound_reservation, courtesy=False)

    def engagement_continuation_tells_its_loss(self, engagement_id: str) -> bool:
        """Whether a continuation into *engagement_id* that the job never
        reads is told by this channel itself — the record resolves and a
        driver is wired. The approval card then shows no failure line of its
        own, so the operator is told once."""
        reg = self._engagement_registry
        return (reg is not None and self._engagement_driver is not None
                and reg.get(str(engagement_id or "")) is not None)

    async def _end_quiet_run(self, rec) -> bool:
        """An operator's reply or tap into a quiet scheduled run ends its
        quietness before the message is accepted, so the run's end is told
        like any job's. ``False`` refuses the interaction: the record went
        terminal (a terminal job receives nothing) or the stamp could not be
        persisted."""
        import background_jobs
        reg = self._engagement_registry
        if reg is None:
            return True
        latest = reg.get(rec.id) or rec
        if not background_jobs.was_quiet_run(latest):
            return True
        # Entered even when the stamp already reads not-quiet: another
        # promotion may have cleared it in memory with its write still in
        # flight. The registry's lock-held check waits for that write and
        # promotes again if it rolled back.
        return await reg.promote_from_quiet(rec.id)

    async def deliver_operator_reply(
        self, rec, text: str, *, chat_id: int, message_id: int,
        quoted_text: str | None = None, user_name: str = "unknown",
        user_id: int | None = None,
    ) -> bool:
        """The operator's reply to one of a live job's direct chat posts,
        into that job: a quiet run stops being quiet first, then the message
        is journaled, admitted and queued, and this returns. The message
        names the post it answers (``[replying to «…»]``).

        One notice per message: this seam when the job did not accept it,
        the journal's terminal branch when the job ended without reading it —
        a reply to their message in the direct chat. Returns whether the job
        accepted it; no caller posts anything on it."""
        notice_target = _NoticeTarget(chat_id=int(chat_id),
                                      message_id=int(message_id))
        first = ((quoted_text or "").strip().splitlines() or [""])[0].strip()
        sent = f"[replying to «{first[:200]}»]\n{text}" if first else text
        if not await self._end_quiet_run(rec):
            await self._tell_not_delivered(rec, notice_target)
            return False
        if not await self._deliver_to_job(
                rec, sent, tg_message_id=int(message_id),
                notice_target=notice_target):
            return False
        # INV-MEM-044: the operator's words to a specialist that keeps no
        # memory are their message, kept by memory. Detached.
        from delegated_memory import capture_job_reply
        capture_job_reply(
            channel=self, engagement=rec,
            user_id=user_id if user_id is not None else self.operator_user_id(),
            user_name=user_name, message_id=int(message_id),
            resident=self.default_agent, text=text)
        return True

    # -- a job's posts in the direct chat ---------------------------------

    @staticmethod
    def _job_chat(rec) -> "int | None":
        return strict_positive_id((getattr(rec, "origin", None) or {}).get("chat_id"))

    @staticmethod
    def _job_title(rec) -> str:
        job = (getattr(rec, "origin", None) or {}).get("job") or {}
        title = str(job.get("title") or getattr(rec, "task", "") or "job")
        return (title.splitlines() or ["job"])[0][:80]

    @staticmethod
    def _job_agent_name(rec) -> str:
        import tools as tools_mod
        return tools_mod._display_name_for_role(rec.role_or_type)

    async def post_job_start_line(self, rec) -> "int | None":
        """The silent ``📊 <Agent> started «<title>».`` line of a job the
        operator started, with its Cancel button — Casa's text. Filed as the
        job's keyboard post once it lands. Returns the message id, or
        ``None`` when nothing landed."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from engagement_registry import JOB_POST_START
        chat = self._job_chat(rec)
        if chat is None or self.bot is None:
            return None
        kbd = InlineKeyboardMarkup([[InlineKeyboardButton(
            text="Cancel", callback_data=job_cancel_callback(rec.id))]])
        try:
            msg = await self.bot.send_message(
                chat_id=chat,
                text=f"📊 {self._job_agent_name(rec)} started «{self._job_title(rec)}».",
                reply_markup=kbd, disable_notification=True)
        except Exception as exc:  # noqa: BLE001 — a start line is never the job's fault
            logger.info("job start line not posted: %s", type(exc).__name__)
            return None
        mid = getattr(msg, "message_id", None)
        if type(mid) is not int:
            return None
        await self._file_job_keyboard(rec, mid, JOB_POST_START)
        return mid

    @staticmethod
    def _job_start_post(rec) -> "int | None":
        """The message id of the job's start line, while it is filed."""
        from engagement_registry import JOB_POST_START
        for post in getattr(rec, "ui_posts", None) or ():
            if isinstance(post, (list, tuple)) and len(post) == 2 \
                    and post[1] == JOB_POST_START and type(post[0]) is int:
                return post[0]
        return None

    async def refresh_job_start_line(self, rec) -> None:
        """Edit a live job's start line, silently, to show how long it has
        run and its exact cost as of its last turn end (absent until a turn
        has ended): ``📊 Finance started «Accounting check». · 40 min ·
        $2.40``. The Cancel button stays. A job with no start line (scheduled
        or agent-started) has nothing to edit."""
        import background_jobs
        chat = self._job_chat(rec)
        mid = self._job_start_post(rec)
        if chat is None or mid is None or self.bot is None:
            return
        if getattr(rec, "status", None) not in ("active", "idle"):
            return
        text = (f"📊 {self._job_agent_name(rec)} started «{self._job_title(rec)}»."
                + background_jobs.cost_suffix(rec))
        await self.edit_dm_message(
            chat, mid, text, buttons=[("Cancel", job_cancel_callback(rec.id))])

    async def post_job_parked(self, rec, text: str) -> "int | None":
        """A parked job's one line in the operator's direct chat, with the
        job's Cancel button: a reply to the start line when the job has one,
        else a new post. Filed as the job's keyboard post, so a reply quoting
        it reaches the job. Returns the message id, or ``None`` when nothing
        landed."""
        from telegram import (
            InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters,
        )
        from engagement_registry import JOB_POST_PARKED
        chat = self._job_chat(rec)
        if chat is None or self.bot is None:
            return None
        kbd = InlineKeyboardMarkup([[InlineKeyboardButton(
            text="Cancel", callback_data=job_cancel_callback(rec.id))]])
        start = self._job_start_post(rec)
        extra = ({"reply_parameters": ReplyParameters(
            message_id=start, allow_sending_without_reply=True)}
            if start is not None else {})
        try:
            msg = await self.bot.send_message(
                chat_id=chat, text=text, reply_markup=kbd, **extra)
        except Exception as exc:  # noqa: BLE001 — the record still says it waits
            logger.warning("job parking line not posted: %s", type(exc).__name__)
            return None
        mid = getattr(msg, "message_id", None)
        if type(mid) is not int:
            return None
        await self._file_job_keyboard(rec, mid, JOB_POST_PARKED)
        return mid

    async def post_job_question(self, rec, qid: str, body: Any,
                                options: list, captions: list) -> "int | None":
        """A job's question in the direct chat: the admitted *body* (Casa's
        label line and the job's question) with one button per option and a
        Cancel job button, each naming the engagement — the options by
        question id and index. Filed as the job's keyboard post once it
        lands. Returns the message id, or ``None`` when nothing landed (a
        send that timed out may still have landed: its buttons still work)."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from engagement_registry import JOB_POST_QUESTION
        if _unadmitted("post_job_question", body):
            return None
        chat = self._job_chat(rec)
        if chat is None or self.bot is None:
            return None
        rows = [[InlineKeyboardButton(
            text=str(captions[i]),
            callback_data=job_question_callback(rec.id, qid, i))]
            for i in range(len(options))]
        rows.append([InlineKeyboardButton(
            text="Cancel job", callback_data=job_cancel_callback(rec.id))])
        kbd = InlineKeyboardMarkup(rows)
        display, entities = render(body)
        try:
            if entities is not None:
                try:
                    msg = await self.bot.send_message(
                        chat_id=chat, text=display, entities=entities,
                        reply_markup=kbd)
                except BadRequest as exc:
                    logger.warning("job question rich send fell back to plain: %s",
                                   type(exc).__name__)
                    msg = await self.bot.send_message(
                        chat_id=chat, text=str(body), reply_markup=kbd)
            else:
                msg = await self.bot.send_message(
                    chat_id=chat, text=_plain(body), reply_markup=kbd)
        except Exception as exc:  # noqa: BLE001 — the tool reports the failure
            logger.warning("job question not posted: %s", type(exc).__name__)
            return None
        mid = getattr(msg, "message_id", None)
        if type(mid) is not int:
            return None
        await self._file_job_keyboard(rec, mid, JOB_POST_QUESTION)
        return mid

    async def _file_job_keyboard(self, rec, message_id: int, kind: str) -> None:
        """File a landed keyboard post; one that landed after the record's
        terminal transition settled is retired here — whoever sees the record
        terminal last retires the post."""
        reg = self._engagement_registry
        if reg is None:
            return
        if not reg.record_job_post(rec.id, message_id, keyboard=kind):
            await self._retire_job_post(reg.get(rec.id) or rec, [message_id, kind])

    def retire_job_keyboards(self, rec) -> None:
        """Take a terminal job's keyboard posts off the record (synchronously,
        so each is retired once) and edit them away on a task of their own.
        Called by the registry's terminal observer, so it never awaits."""
        reg = self._engagement_registry
        if reg is None:
            return
        posts = reg.take_ui_posts(rec.id)
        if not posts:
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._retire_job_posts(rec, posts))
        except RuntimeError:  # no loop — nothing can be edited now
            return
        self._job_ui_tasks.add(task)
        task.add_done_callback(self._job_ui_tasks.discard)

    def retire_ended_job_keyboards(self) -> int:
        """Boot: retire the keyboards of every terminal job that still
        carries some. Returns how many records it scheduled."""
        reg = self._engagement_registry
        lister = getattr(reg, "records_with_ui_posts", None) if reg is not None else None
        if lister is None:
            return 0
        records = list(lister())
        for rec in records:
            self.retire_job_keyboards(rec)
        return len(records)

    async def _retire_job_posts(self, rec, posts: list) -> None:
        for post in posts:
            try:
                await self._retire_job_post(rec, post)
            except Exception:  # noqa: BLE001 — one post never stops the rest
                logger.warning("retiring a job keyboard failed for %s",
                               rec.id[:8], exc_info=True)

    async def _retire_job_post(self, rec, post) -> None:
        """Edit one keyboard post of an ended job: the start line to its
        outcome mark, a question to no buttons. A post this cannot edit keeps
        its keyboard; a tap on it reads the terminal record."""
        from engagement_registry import JOB_POST_START
        chat = self._job_chat(rec)
        message_id, kind = post[0], post[1]
        if chat is None:
            return
        if kind == JOB_POST_START:
            import background_jobs
            mark = {"completed": "✅", "cancelled": "🛑"}.get(rec.status, "❌")
            await self.edit_dm_message(
                chat, message_id,
                f"{mark} {self._job_agent_name(rec)} «{self._job_title(rec)}»"
                + background_jobs.cost_suffix(rec))
        else:
            await self.clear_dm_keyboard(chat, message_id)

    def _job_tap_allowed(self, cq: Any, rec) -> bool:
        user = getattr(cq, "from_user", None)
        if user is None or not self._sender_is_operator(user):
            return False
        owner = strict_positive_id((rec.origin or {}).get("user_id"))
        return owner is not None and owner == user.id

    async def _answer_once(self, cq: Any, toast: str) -> None:
        """Exactly one completed answer, even under cancellation."""
        t = asyncio.create_task(_safe_answer(cq, toast))
        try:
            await asyncio.shield(t)
        except asyncio.CancelledError:
            await t
            raise

    async def _on_job_question_callback(self, cq: Any, engagement_id: str,
                                        qid: str, index: int) -> None:
        """A tap on a job question's option. Resolved against the record —
        never a memory-only broker — so it works after a restart: the
        operator, the record counting as live, the question id it holds and
        the option. An id the record does not hold delivers nothing. Then,
        BEFORE the first await, the inbound reservation, so the completion
        gate vetoes a successful completion from this instant; then the toast,
        the keyboard showing the choice, and the answer as a system turn whose
        notice target is the question. This handler never posts a notice."""
        toast = "expired"
        lease = None
        rec = question = None
        msg = getattr(cq, "message", None)
        try:
            reg = self._engagement_registry
            if reg is None or msg is None or getattr(msg, "chat", None) is None:
                return
            rec = reg.get(engagement_id)
            if rec is None:
                return
            if not self._job_tap_allowed(cq, rec):
                toast = "not for you"
                return
            if not reg.counts_as_live(engagement_id):
                toast = "already finished"
                return
            question = rec.questions.get(qid)
            options = (question or {}).get("options") or []
            if question is None or not 0 <= index < len(options):
                toast = _QUESTION_GONE_TOAST
                return
            lease = self.engagement_inbound_reservation(engagement_id)
            lease.take()
            toast = "✔"
        finally:
            await self._answer_once(cq, toast)
        if lease is None:
            return
        try:
            option = question["options"][index]
            await self._mark_job_answer(msg, rec, qid, question, index)
            await self.deliver_system_turn(
                rec, f"[operator answered «{question['text']}»: {option}]",
                inbound_reservation=lease,
                notice_target=_NoticeTarget(chat_id=msg.chat.id,
                                            message_id=msg.message_id,
                                            answer=True))
        except Exception:  # noqa: BLE001 — the seam settled its own ticket
            logger.warning("job answer for %s not handed off", engagement_id[:8],
                           exc_info=True)
        finally:
            lease.release()

    async def _mark_job_answer(self, msg: Any, rec, qid: str, question: dict,
                               index: int) -> None:
        """Show the chosen option on the question's keyboard (``✔`` on its
        button). The buttons stay: a second tap is a second answer."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        options = question.get("options") or []
        captions = resolve_button_labels(list(options), multi=False)
        rows = [[InlineKeyboardButton(
            text=(f"✔ {captions[i]}" if i == index else str(captions[i])),
            callback_data=job_question_callback(rec.id, qid, i))]
            for i in range(len(options))]
        rows.append([InlineKeyboardButton(
            text="Cancel job", callback_data=job_cancel_callback(rec.id))])
        try:
            await asyncio.wait_for(self.bot.edit_message_reply_markup(
                chat_id=msg.chat.id, message_id=msg.message_id,
                reply_markup=InlineKeyboardMarkup(rows)),
                _TURN_INCOMPLETE_NOTICE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — cosmetic
            logger.info("job answer mark not shown: %s", type(exc).__name__)

    async def _on_job_cancel_callback(self, cq: Any, engagement_id: str) -> None:
        """The Cancel button on a job's start line or question: the operator,
        a live record, the toast, then the cancel funnel on a task of its own.
        A second tap while it runs answers "already finishing"."""
        toast = "expired"
        start = False
        rec = None
        msg = getattr(cq, "message", None)
        try:
            reg = self._engagement_registry
            if reg is None or msg is None or getattr(msg, "chat", None) is None:
                return
            rec = reg.get(engagement_id)
            if rec is None:
                return
            if not self._job_tap_allowed(cq, rec):
                toast = "not for you"
                return
            if engagement_id in self._job_cancels:
                toast = "already finishing"
                return
            if not reg.counts_as_live(engagement_id):
                toast = "already finished"
                return
            # A record whose other terminal write is still pending is
            # cancelled too: the funnel's transition waits for that write,
            # wins after a rollback, and ends harmlessly after a commit.
            self._job_cancels.add(engagement_id)
            start = True
            toast = "Cancelling…"
        finally:
            try:
                await self._answer_once(cq, toast)
            except BaseException:
                if start:
                    self._job_cancels.discard(engagement_id)
                raise
        if not start:
            return
        task = asyncio.create_task(
            self._run_job_cancel(rec, msg.chat.id, msg.message_id))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _run_job_cancel(self, rec, chat_id: int, message_id: int) -> None:
        """The cancel funnel for a Cancel tap. Its transition waits out any
        other terminal write still pending. A won transition is told by the
        terminal notification and its keyboards are retired by the terminal
        observer; one lost to another outcome needs nothing more. A write that rolled back
        leaves the record live and the button in place, and draws one line in
        reply to the tapped message."""
        try:
            fin = (await self._finalize_cancel(rec, reason="the operator")
                   if self._finalize_cancel is not None else None)
        except Exception:  # noqa: BLE001 — told below if still live
            logger.warning("job cancel failed for %s", rec.id[:8], exc_info=True)
            fin = None
        finally:
            self._job_cancels.discard(rec.id)
        if fin:
            return
        reg = self._engagement_registry
        latest = reg.get(rec.id) if reg is not None else None
        if latest is None or latest.status not in ("active", "idle"):
            return
        try:
            await asyncio.wait_for(
                self._send_dm_reply(chat_id, message_id, _CANCEL_NOT_SAVED_LINE),
                _TURN_INCOMPLETE_NOTICE_TIMEOUT_S)
        except BaseException:  # noqa: BLE001 — one bounded attempt
            logger.warning("cancel retry line failed for %s", rec.id[:8],
                           exc_info=True)

    async def _on_non_text_message(
        self, update, _context: ContextTypes.DEFAULT_TYPE | None = None,
    ) -> None:
        """#1036: every non-text message. Arrival stores and acknowledges; only
        a stored file's caption starts an agent turn (#1378), whose reply takes
        the acknowledgement's place. Files sent together share one
        acknowledgement, edited as each lands, and an album's caption covers
        the album (#1379). Every file ends in a turn that acts on it or a line
        of an acknowledgement — there is no silent path, and no silent list."""
        msg = getattr(update, "message", None)
        if msg is None:
            return
        chat_id = msg.chat.id
        user = msg.from_user

        async def reply(text: str, *, thread: int | None = None) -> None:
            kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
            if thread:
                kwargs["message_thread_id"] = thread
            for attempt in (1, 2):
                try:
                    await self.bot.send_message(**kwargs)
                    return
                except RetryAfter as exc:
                    # Flood control is Telegram asking for a retry after a stated
                    # wait; honour it once, capped. Every other failure is logged
                    # and not retried, as elsewhere in this channel.
                    if attempt == 2:
                        logger.warning("inbound-file reply failed: RetryAfter twice")
                        return
                    wait = getattr(exc, "retry_after", 1)
                    wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
                    await asyncio.sleep(min(max(float(wait or 0), 0.0), 10.0))
                except Exception as exc:  # noqa: BLE001 — a reply fault is logged
                    logger.warning("inbound-file reply failed: %s", type(exc).__name__)
                    return

        if self.engagement_supergroup_id and chat_id == self.engagement_supergroup_id:
            # Only a person's content draws the pointer. Chat notices (a topic
            # Casa created, a member joining) are not something anyone sent.
            if _carries_content(msg):
                await self._redirect_from_log_group(msg)
            return
        configured = str(self.chat_id or "").strip()
        if configured and str(chat_id) != configured:
            # The text path's rule, unchanged: an update from any other chat is
            # logged and dropped, never answered.
            logger.info("Dropping non-text Telegram update from unauthorized "
                        "chat_id=%s", chat_id)
            return
        if not self._sender_is_operator(user):
            await reply("Files are only accepted from the configured operator. "
                        "This one wasn't saved.")
            return

        # S6 §2.1: WHO the file is addressed to — resolved before the classification
        # refusal, synchronously (an arming is consumed here whatever follows)
        address = self._file_address(chat_id, user, msg)
        if address is not None:
            await self._route_addressed_file(chat_id, user, msg, address)
            return

        # #1379: from here on every outcome is one entry of the chat's
        # acknowledgement — files sent together share one message, edited as
        # each lands — or, for a caption, of the turn that acts on the files.
        cls = _classify_inbound(msg)
        entry, burst = self._ack_join(str(chat_id), msg, cls)
        caption = getattr(msg, "caption", None)
        owner = None
        if isinstance(caption, str) and caption.strip():
            owner = self._ack_claim(burst, entry, caption)
        try:
            entry.line, entry.stored = await self._store_inbound(cls)
        finally:
            if entry.line is None:      # cancelled or raised: never left pending
                import agent_inbox
                entry.line = _inbound_reply(
                    agent_inbox.Receipt(agent_inbox.Outcome.UNCERTAIN), cls)
            burst.touched = time.monotonic()
        await self._render_ack(chat_id, burst)
        if owner is not None:
            await self._run_claim(update, msg, user, burst, owner)
            await self._render_ack(chat_id, burst)

    async def _store_inbound(self, cls: "_InboundClass") -> tuple[str, tuple | None]:
        """The default agent's inbox takes the file: its one-line outcome, and
        its ``(name, kind, size, path)`` when it was stored."""
        import agent_inbox
        if not cls.accept:
            return cls.refusal, None
        inbox = agent_inbox.get_inbox(self.default_agent)
        if inbox is None:
            return _inbound_reply(
                agent_inbox.Receipt(agent_inbox.Outcome.STORAGE_FAILED), cls), None
        bot = self.bot
        try:
            receipt = await inbox.receive(
                lambda cap: agent_inbox.fetch_telegram_file(bot, cls.file_id, cap),
                ext=cls.ext, display_name=cls.display_name,
                declared_size=cls.declared_size,
            )
        except Exception:  # noqa: BLE001 — every path ends in a reply
            # Unexpected, so the outcome is unknown: it may have failed after
            # publication. Say so; never claim the file is absent.
            logger.warning("inbound file: unexpected failure", exc_info=True)
            receipt = agent_inbox.Receipt(agent_inbox.Outcome.UNCERTAIN)
        logger.info("inbound file: outcome=%s bytes=%d",
                    receipt.outcome.value, receipt.size)
        line = _inbound_reply(receipt, cls)
        if receipt.outcome is not agent_inbox.Outcome.STORED:
            return line, None
        name = agent_inbox._bounded_display(cls.display_name) or "a photo"
        kind = cls.ext.lstrip(".").upper() if cls.ext else "file"
        return line, (name, kind, _human_size(receipt.size),
                      os.path.join(inbox.ready_dir, receipt.name))

    def _ack_join(self, chat_key: str, msg: Any, cls: "_InboundClass"):
        """#1379: the arriving file's entry. An album has one group of its own,
        keyed by its album id (at most ten files); a lone file joins the chat's
        lone files when it lands within ``ACK_WINDOW_S`` of their last event, up
        to ``ACK_MAX_FILES``, else starts a new group. Synchronous, so arrival
        order is entry order."""
        import agent_inbox
        group = getattr(msg, "media_group_id", None) or None
        now = time.monotonic()
        key = (chat_key, group)
        burst = self._ack_bursts.get(key)
        if burst is None or (group is None and (
                now - burst.touched > ACK_WINDOW_S
                or len(burst.entries) >= ACK_MAX_FILES)):
            # groups long quiet are forgotten (a pending claim keeps its own)
            for k in [k for k, b in self._ack_bursts.items() if now - b.touched > 600]:
                del self._ack_bursts[k]
            burst = _AckBurst(entries=[], touched=now, claims={})
            self._ack_bursts[key] = burst
        doc = getattr(msg, "document", None)
        name = agent_inbox._bounded_display(
            cls.display_name or (getattr(doc, "file_name", None) or "") if doc else "")
        entry = _AckEntry(name=name, group=group)
        claim = burst.claims.get(group) if group is not None else None
        if claim is not None and not claim.sealed:
            entry.claim = claim
            claim.touched = now
        burst.entries.append(entry)
        burst.touched = now
        return entry, burst

    def _ack_claim(self, burst: _AckBurst, entry: _AckEntry, caption: str):
        """#1379: a caption claims its album (a lone file: itself). The claim's
        owner runs its turn; a second caption in an open claim adds its words.
        Returns the claim this file owns, or ``None``."""
        if entry.claim is not None:
            entry.claim.captions.append(caption)
            return None
        claim = _AckClaim(captions=[caption], touched=time.monotonic())
        entry.claim = claim
        if entry.group is not None:
            burst.claims[entry.group] = claim
            for e in burst.entries:
                if e.group == entry.group and e.claim is None:
                    e.claim = claim
        return claim

    ALBUM_SETTLE_S = 1.0
    ALBUM_MAX_WAIT_S = 60.0

    async def _run_claim(self, update: Any, msg: Any, user: Any,
                         burst: _AckBurst, claim: _AckClaim) -> None:
        """#1379: once every file of the album has landed (and none arrived for
        ``ALBUM_SETTLE_S``), ONE turn acts on the files it kept; they are left
        out of the acknowledgement only when that turn was dispatched."""
        album = bool(getattr(msg, "media_group_id", None))
        deadline = time.monotonic() + self.ALBUM_MAX_WAIT_S
        while time.monotonic() < deadline:
            members = [e for e in burst.entries if e.claim is claim]
            if all(e.line is not None for e in members) and (
                    not album or time.monotonic() - claim.touched >= self.ALBUM_SETTLE_S):
                break
            await asyncio.sleep(0.1)
        claim.sealed = True     # a later caption opens a turn of its own (r1, Terra)
        files = [e for e in burst.entries if e.claim is claim and e.stored is not None]
        try:
            if files and await self._caption_turn(
                    update, msg, user, "\n\n".join(claim.captions),
                    [e.stored for e in files], album=album):
                for e in files:
                    e.covered = True
        finally:
            claim.done = True

    async def _render_ack(self, chat_id: Any, burst: _AckBurst) -> None:
        """#1379: bring the group's one message up to date — sent the first
        time, edited after. An edit that fails sends the whole text afresh; a
        message whose every file a caption turn took over is deleted."""
        lock = self._ack_locks.setdefault(str(chat_id), asyncio.Lock())
        async with lock:
            text = _ack_text(burst)
            if text == burst.shown:
                return
            if not text:
                if await self._ack_call(self.bot.delete_message, chat_id=chat_id,
                                        message_id=burst.message_id) is not None:
                    burst.message_id, burst.shown = None, ""
                return
            if burst.message_id is not None and await self._ack_call(
                    self.bot.edit_message_text, chat_id=chat_id,
                    message_id=burst.message_id, text=text) is not None:
                burst.shown = text
                return
            sent = await self._ack_call(self.bot.send_message, chat_id=chat_id, text=text)
            if sent is not None:
                burst.message_id = getattr(sent, "message_id", None)
                burst.shown = text

    @staticmethod
    async def _ack_call(fn: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
        """One Bot API call for an acknowledgement: flood control honoured once,
        capped; any other failure logged. ``None`` when it did not go through."""
        for attempt in (1, 2):
            try:
                return await fn(**kwargs) or True
            except RetryAfter as exc:
                if attempt == 2:
                    logger.warning("inbound-file reply failed: RetryAfter twice")
                    return None
                wait = getattr(exc, "retry_after", 1)
                wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
                await asyncio.sleep(min(max(float(wait or 0), 0.0), 10.0))
            except Exception as exc:  # noqa: BLE001 — a reply fault is logged
                logger.warning("inbound-file reply failed: %s", type(exc).__name__)
                return None
        return None

    async def _caption_turn(self, update: Any, msg: Any, user: Any, caption: str,
                            files: list, *, album: bool) -> bool:
        """#1378: one ordinary turn of the default agent for stored files whose
        message carried a caption — the caption verbatim as the operator's
        words, the files' facts as Casa's note, admitted exactly as a text
        message is (the per-chat serial lock, then ``_dispatch_dm_turn``).
        #1379: an album's caption covers every file of it that was kept.
        ``False`` when no turn was dispatched, and the files are acknowledged."""
        chat_key = str(msg.chat.id)
        note = caption_file_note(files, album=album)
        received = {"files": [{"path": p, "name": n} for n, _k, _s, p in files],
                    "note": note}
        lock = self._chat_serial_locks.setdefault(chat_key, asyncio.Lock())
        try:
            async with lock:
                return await self._dispatch_dm_turn(
                    update, chat_key, caption, user=user, received_file=received)
        except Exception:  # noqa: BLE001 — the files are stored; they are acknowledged
            logger.warning("caption turn not dispatched", exc_info=True)
            return False

    FILE_ARM_TTL_S = 600.0

    def _file_address(self, chat_id: Any, user: Any, msg: Any):
        """S6 §2.1 step 1 — exactly one of: a swipe-reply on a retained specialist post for
        this operator (``("reply", record, role)``); a live 📎 arming in this chat for this
        operator (``("armed", None, role)``, consumed here — expired or not — before any
        await); or ``None``: the file is the default agent's, today's path — always for a
        reply to an ended job's post (#1522)."""
        import result_broker
        chat = strict_positive_id(chat_id)
        if chat is None or user is None:
            return None
        # the arming arms the NEXT file, whichever route that file takes: it is spent
        # here, before the reply check, so a swipe-reply file cannot leave it live for a
        # later file (INV-FILE-003; diff round 1, Terra)
        arming = self._armings.pop(chat, None)
        quoted = getattr(msg, "reply_to_message", None)
        if self._ended_job_quoted(chat_id, user, quoted) is not None:
            # #1522: a file sent as a reply to an ended job's post is the
            # default agent's, never the desk's, which cannot see the job
            return None
        if quoted is not None:
            record = result_broker.POST_MAP.get(chat, getattr(quoted, "message_id", None))
            if (record is not None and record.operator_id == getattr(user, "id", None)
                    and record.role != self.default_agent):
                return ("reply", record, record.role)
        if (arming is not None and arming.get("operator_id") == getattr(user, "id", None)
                and float(arming.get("expires_at", 0.0)) > time.monotonic()):
            return ("armed", None, str(arming.get("role") or ""))
        return None

    async def _route_addressed_file(self, chat_id: Any, user: Any, msg: Any, address: tuple) -> None:
        """S6 §2.1 step 2, under the per-(chat, specialist) intake lock (§5): delegable now,
        an inbox, an accepted kind, the download into THAT specialist's inbox, the
        post-download re-check, the desk place — then ONE desk turn with a Casa-composed
        task. Every refusal is a completed event, told once and echoed with the same
        string; nothing here ever reaches the default agent's inbox (F1)."""
        import agent_inbox
        import specialist_desk as sd
        kind, record, role = address
        chat = strict_positive_id(chat_id)
        label = sd.label_for(role)
        cls = _classify_inbound(msg)
        doc = getattr(msg, "document", None)
        name = cls.display_name or getattr(doc, "file_name", None) or "your file"
        ext = (cls.ext or "").lstrip(".").upper() or "file"

        async def tell(line: str) -> None:
            try:
                await self.deliver_desk_notice(chat, line)
            except Exception as exc:  # noqa: BLE001 — nothing more is attempted
                logger.warning("file route notice failed: %s", type(exc).__name__)
            sd.record_echo(chat, line)

        lock = self._intake_locks.setdefault((chat, role), asyncio.Lock())
        async with lock:
            if not sd.desk_target_ok(self.default_agent, role):
                await tell(sd.bounded_line("{label} could not take your file (not delegable).", label=label))
                return
            inbox = agent_inbox.get_inbox(role)
            if inbox is None:
                await tell(sd.bounded_line("{label} could not take your file (storage).", label=label))
                return
            if not cls.accept:
                await tell(sd.bounded_line("{label} could not take {name}: it was not a kind it can read.",
                                           label=label, fields={"name": name}))
                return
            bot = self.bot
            try:
                receipt = await inbox.receive(
                    lambda cap: agent_inbox.fetch_telegram_file(bot, cls.file_id, cap),
                    ext=cls.ext, display_name=cls.display_name, declared_size=cls.declared_size)
            except Exception:  # noqa: BLE001 — the outcome is unknown, never "absent"
                logger.warning("routed file: unexpected failure", exc_info=True)
                receipt = agent_inbox.Receipt(agent_inbox.Outcome.UNCERTAIN)
            logger.info("routed file: role=%s outcome=%s bytes=%d", role, receipt.outcome.value, receipt.size)
            line = sd.file_outcome(label, name, receipt.outcome, kind=ext)
            if line is not None:
                await tell(line)
                return
            stored_path = os.path.join(inbox.ready_dir, receipt.name)
            if not sd.desk_target_ok(self.default_agent, role):
                await tell(sd.bounded_line(
                    "{label} stored {name} in its inbox but was not delegable at the post-download check.",
                    label=label, fields={"name": name}))
                return
            desk = sd.DESKS.get_or_create(chat, role)
            if desk.faulted:
                await tell(sd.faulted_line(label))
                return
            reservation = desk.reserve()
            if reservation is None:
                await tell(sd.bounded_line(
                    "{label} stored {name} in its inbox; its desk was full when the place was requested.",
                    label=label, fields={"name": name}))
                return
        # the place is taken; the turn runs outside the intake lock
        task = (f"[casa file] The operator sent you a file: {name} ({ext}, {receipt.size} bytes). "
                f"It is in your inbox at {stored_path}. File it with your plugin and report what you did. "
                "The inbox path is for your tools, not for your reply.")
        caption = getattr(msg, "caption", None)
        if caption:
            task += f"\nThe operator wrote: {caption}"
        quoted = getattr(msg, "reply_to_message", None) if kind == "reply" else None
        quoted_text = (getattr(quoted, "text", None) or getattr(quoted, "caption", None)) if quoted else None
        cid = new_cid()
        self._start_typing(str(chat), cid)
        self._spawn_desk_turn(
            chat_id=chat, user_id=user.id, user_name=getattr(user, "first_name", None) or "unknown",
            message_id=getattr(msg, "message_id", None), cid=cid, text=task, quoted_text=quoted_text,
            record=record, desk_role=role, continuation=False, reservation=reservation,
            file_name=name, file_path=stored_path)

    async def _redirect_from_log_group(self, msg: Any) -> None:
        """The log group is read-only. A person who writes there (text, a
        command or a file, in any topic or in General) is pointed to the
        direct chat once per boot, in the thread they wrote in; nothing they
        wrote reaches a job, memory or a model. Bots are ignored. The
        pointer is recorded with the log's posts, so retention deletes it
        with them."""
        user = getattr(msg, "from_user", None)
        user_id = getattr(user, "id", None)
        if user_id is None or getattr(user, "is_bot", False):
            return
        if user_id in self._log_group_redirect_seen:
            return
        self._log_group_redirect_seen.add(user_id)
        kwargs: dict[str, Any] = {"chat_id": self.engagement_supergroup_id,
                                  "text": _LOG_GROUP_REDIRECT}
        thread_id = getattr(msg, "message_thread_id", None)
        if isinstance(thread_id, int) and not isinstance(thread_id, bool):
            kwargs["message_thread_id"] = thread_id
        try:
            sent = await self.bot.send_message(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("log group redirect send failed: %s", exc)
            return
        if self._agent_logs is not None:
            self._agent_logs.note_post(kwargs["chat_id"],
                                       getattr(sent, "message_id", None))

    # ------------------------------------------------------------------
    # E-12 (v0.37.0) Task 20 / v0.75.0 (W5/Sol B3,B4): inline-keyboard
    # callback dispatch — fail-closed contract, single `cq.answer()`.
    # ------------------------------------------------------------------

    async def _on_inline_callback(
        self, update: Any, context: Any = None,
    ) -> None:
        """Dispatch inline-keyboard callbacks (DM asks, proposals, run stops).

        v0.75.0 [D:§W5 callback, Sol B4/B5/r2-B3/r3-B5/r7-B2/B6]: EXACTLY ONE
        ``await cq.answer(toast)`` per path (via :func:`_safe_answer`, which
        swallows a transport failure so it never crashes the handler). Every
        namespace is DM-scoped and owned by its own branch; anything else
        answers ``expired``.

        This handler NEVER edits the keyboard — that is owned exclusively by
        the broker finish-hook set at post time (r3-B3), which fires once on
        outcome even if the posting hook task was cancelled.
        """
        cq = update.callback_query
        data = cq.data or ""
        # A job's buttons first: they are not of the generic parser's shape.
        job = _parse_job_callback(data)
        if job is not None:
            if job[0] == "q":
                await self._on_job_question_callback(cq, job[1], job[2], job[3])
            elif job[0] == "cancel":
                await self._on_job_cancel_callback(cq, job[1])
            else:
                await _safe_answer(cq, "expired")
            return
        ns, request_id, idx, _kind = _parse_callback_data(data)
        if ns is None:
            await _safe_answer(cq, "expired")
            return

        # v0.76.0 (W5b, r1-B2): resident_ask is DM-scoped (dm:/authz:) — it
        # is handled fully by its own single-owner branch and returns.
        if ns == "resident_ask":
            await self._on_resident_callback(cq, request_id, idx)
            return
        # S5 §4.1: a proposal keyboard is DM-scoped too — its own single-owner
        # branch; it owns every presence check.
        if ns == "proposal":
            await self._on_proposal_callback(cq, request_id, idx)
            return
        # The Stop on a browsing request's start or status card: DM-scoped, its own
        # fail-closed branch; the request id is the admission's confirmation id.
        if ns == "run_stop":
            await self._on_run_stop_callback(cq, request_id)
            return
        await _safe_answer(cq, "expired")

    async def _on_resident_callback(self, cq: Any, rid: str, idx: int) -> None:
        """resident_ask tap — SINGLE-OWNER commit contract [A:§2, r1-B2/r3-B1].

        DM-scoped (``dm:<chat>`` plain asks / ``authz:<chat>`` authorization
        challenges — DISJOINT scopes). Ordering is the frozen addendum:
        ``claim → commit() succeeds → on_commit_sync (mint) runs IMMEDIATELY
        after, with NO await in between`` — commit() is synchronous and
        schedules the finish-hook task, so nothing can run between commit and
        the sync step. The sync step mutates the BROKER-OWNED ``req.meta``
        (``get_meta`` returns it by reference for live requests; ``register``
        shallow-copies the creator's dict, so creators must never rely on
        their original). A sync-step exception is logged and swallowed —
        commit already succeeded, ``minted`` stays absent, and the finish hook
        edits the internal-error text.

        This handler NEVER edits the keyboard or dispatches — the broker
        finish hook (installed by each record creator in T4/T5) is the single
        serialized owner of ALL post-commit async work (edit-first → dispatch
        → overwrite-on-failure). The toast is an explicit task shielded so
        exactly one answer completes even under cancellation (r3-B2).
        """
        from verdict_broker import BROKER

        committed = False
        toast = "expired"
        try:
            if cq.message is None or cq.message.chat is None:
                return  # toast "expired" in finally
            chat = cq.message.chat.id
            meta = (
                BROKER.get_meta(
                    namespace="resident_ask", scope=f"dm:{chat}", request_id=rid,
                )
                or BROKER.get_meta(
                    namespace="resident_ask", scope=f"authz:{chat}", request_id=rid,
                )
                # browser executor §5.3/§5.5: a plugin's operator question
                or BROKER.get_meta(
                    namespace="resident_ask", scope=f"ask:{chat}", request_id=rid,
                )
            )
            if meta is None:
                return
            if meta.get("chat_id") != chat:
                return
            options = meta.get("options") or []
            if not (0 <= idx < len(options)):
                toast = "invalid"
                return
            expected = meta.get("operator_id")
            if (
                cq.from_user is None
                or expected is None
                or cq.from_user.id != expected
            ):
                toast = "not for you"
                return
            claim = BROKER.claim(
                namespace="resident_ask", scope=meta["_scope"], request_id=rid,
                option_index=idx, actor_id=cq.from_user.id,
            )
            if isinstance(claim, str):
                toast = {"duplicate": "already answered", "stale": "expired",
                         "forbidden": "not for you"}[claim]
                return
            committed = BROKER.commit(claim)
            if committed:
                # r3-B1: the SYNCHRONOUS record/mint step runs IMMEDIATELY
                # after a successful commit — commit() is synchronous and there
                # is NO await between it and this call, so the finish-hook task
                # scheduled by commit() cannot run first. Exception ⇒ logged;
                # minted stays absent; the finish hook edits the internal-error
                # text.
                step = meta.get("on_commit_sync")
                if step is not None:
                    try:
                        step(idx)
                    except Exception:  # noqa: BLE001
                        logger.exception("on_commit_sync failed")
                toast = "✔"
            # NO dispatch/edit here: the finish hook owns everything after.
        finally:
            # r3-B2: exactly one COMPLETED answer even under cancellation.
            t = asyncio.create_task(_safe_answer(cq, toast))
            try:
                await asyncio.shield(t)
            except asyncio.CancelledError:
                await t
                raise

    # ------------------------------------------------------------------
    # v0.76.0 (W5b/resident_ask): DM keyboard composer + plain DM-message
    # edit helper + button-continuation dispatch. Chat-addressed — the
    # resident-tap spine [A:§2, r1-B2/r3-B1].
    # ------------------------------------------------------------------

    async def post_dm_keyboard(
        self, *, chat_id: int, request_id: str, text: str, options: list[str],
        short_labels: bool = False, shorts: "list | None" = None,
    ) -> int | None:
        """Post a plain-text question to a DM chat with one tappable button
        per option (resident_ask).

        Chat-addressed (no topic thread), plain send (operator-/agent-authored free text, no MarkdownV2
        escaping pass). ``callback_data`` is ``v1|resident_ask|<request_id>|<i>``
        where ``i`` is the option's position. Returns the Telegram
        ``message_id``, or ``None`` on send failure — the broker's
        ``ensure_posted`` treats ``None`` as a delivery failure (r10-B3).

        ``short_labels`` (v0.81.0, W-R3b; v0.84.0 round 4 D2): when True, derive
        button labels via the whole-set ``short_option_labels`` from the
        parallel ``shorts`` (#1390: ``ask_user``'s per-option ``short``, ``None``
        where an option has none): each button shows ``n · <short>``, or its
        option's own words when no option has a short and they fit (#1386), and
        the set floors to the numbered placeholder otherwise. Telegram
        truncates long labels, which made ``ask_user`` options unpickable; the
        FULL options must already live VERBATIM in ``text`` (the caller renders
        them with ``render_ask_body``). Default False keeps the authz-challenge
        DM path (``authz_grants`` — short ``Approve``/``Deny`` options) untouched.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if _unadmitted("post_dm_keyboard", text):
            return None
        heuristic = short_option_labels(options, shorts) if short_labels else None
        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                text=(heuristic[i] if heuristic is not None else options[i]),
                callback_data=f"v1|resident_ask|{request_id}|{i}",
            )]
            for i in range(len(options))
        ])
        # v0.109.0 (G1): render the BODY rich — agent-authored ask bodies are markdown-heavy
        # and this was the dominant literal-``**`` DM surface. Buttons and
        # ``callback_data`` are untouched (index-addressed). Single-shot
        # ``render()``: no markup / over caps ⇒ ``plain_text`` sends plain
        # (#1330). An entity ``BadRequest`` posted NOTHING (Telegram rejected the
        # send), so ONE plain retry with the original text cannot duplicate the
        # ask; any other failure keeps the delivery-failure contract (None).
        display, entities = render(text)
        try:
            if entities is not None:
                try:
                    msg = await self.bot.send_message(
                        chat_id=chat_id, text=display, entities=entities,
                        reply_markup=kbd,
                    )
                except BadRequest as exc:
                    logger.warning(
                        "post_dm_keyboard rich send fell back to plain "
                        "(chat=%s): %s", chat_id, exc,
                    )
                    msg = await self.bot.send_message(
                        chat_id=chat_id, text=text, reply_markup=kbd,
                    )
            else:
                msg = await self.bot.send_message(
                    chat_id=chat_id, text=_plain(text), reply_markup=kbd,
                )
        except Exception as exc:  # noqa: BLE001 — delivery failure, not fatal
            logger.warning(
                "post_dm_keyboard send failed (chat=%s): %s", chat_id, exc,
            )
            return None
        return msg.message_id

    async def clear_dm_keyboard(self, chat_id: int, message_id: int) -> bool:
        """#1375: remove a posted DM message's inline keyboard by a markup-only edit
        (an EXPLICIT empty markup — PTB drops a None one), so its text is never
        re-sent. "Message is not modified" is success; any other failure is logged."""
        from telegram import InlineKeyboardMarkup
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=InlineKeyboardMarkup([]))
            return True
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return True
            logger.warning("clear_dm_keyboard failed (chat=%s message_id=%s): %s",
                           chat_id, message_id, exc)
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            logger.warning("clear_dm_keyboard failed (chat=%s message_id=%s): %s",
                           chat_id, message_id, type(exc).__name__)
        return False

    async def edit_dm_message(
        self, chat_id: int, message_id: int, text: str, *, caption: bool = False,
        buttons: "list[tuple[str, str]] | None" = None,
    ) -> bool:
        """Plain (no parse_mode, no reply_markup) text edit of a posted DM
        message — resident_ask finish-hook target.

        Chat-addressed. "Message is not modified" (an identical re-edit) is tolerated
        as success, not an error, since the desired end state already holds.

        v0.109.0 (G1): the edited body renders rich (the settle-edit rewrites
        the ask body posted by ``post_dm_keyboard`` — plain here would resurrect
        literal markers on settle). An entity ``BadRequest`` retries the SAME
        EDIT plain with the original text — never a new message (Sol+Terra
        design round: an edit must not become a duplicate post).

        #569: the edit carries an EXPLICIT EMPTY ``InlineKeyboardMarkup([])``,
        so the settled question drops its buttons. A bare ``edit_message_text``
        leaves them tappable — PTB drops the omitted ``reply_markup``, so
        ``editMessageText`` never touches the markup. That is the same defect
        v0.79.0 fixed for the (since removed) topic-ask path; the DM path was
        left behind, and it settles EVERY
        DM question Casa asks: ``ask_user``, ``wipe_memory``, the
        protected-action challenge, and callback / event / trigger / persona /
        specialist install consent. Unconditional rather than opt-in because
        every caller in the tree is a broker FINISH hook — a terminal outcome,
        never a mid-question repaint — and an opt-in flag is a thing the next
        call site forgets.

        ``caption`` (slice 5b): the message is a photo whose caption is the text
        (a proposal with a photo), so the same edit goes through
        ``editMessageCaption`` — Telegram refuses ``editMessageText`` on a photo.

        ``buttons`` (the start card's Stop): ``(label, callback data)`` pairs the
        edited message carries instead of the empty keyboard — the one caller
        that settles a question into a card that still acts.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        cleared = InlineKeyboardMarkup(
            [[InlineKeyboardButton(text=str(label), callback_data=str(data))]
             for label, data in (buttons or [])])
        display, entities = render(text)
        for attempt in (1, 2):
            try:
                if entities is not None:
                    try:
                        if caption:
                            await self.bot.edit_message_caption(
                                chat_id=chat_id, message_id=message_id, caption=display,
                                caption_entities=entities, reply_markup=cleared,
                            )
                        else:
                            await self.bot.edit_message_text(
                                chat_id=chat_id, message_id=message_id, text=display,
                                entities=entities, reply_markup=cleared,
                            )
                        return True
                    except BadRequest as exc:
                        if "not modified" in str(exc).lower():
                            return True
                        logger.warning(
                            "edit_dm_message rich edit fell back to plain "
                            "(chat=%s message_id=%s): %s",
                            chat_id, message_id, exc,
                        )
                if caption:
                    # a caption's fallback is its display without entities: the
                    # authored markers could push it past the caption limit
                    await self.bot.edit_message_caption(
                        chat_id=chat_id, message_id=message_id, caption=display,
                        reply_markup=cleared,
                    )
                else:
                    await self.bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id,
                        text=text if entities is not None else _plain(text),
                        reply_markup=cleared,
                    )
                return True
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return True
                logger.warning(
                    "edit_dm_message failed (chat=%s message_id=%s): %s",
                    chat_id, message_id, exc,
                )
                return False
            except (RetryAfter, NetworkError) as exc:
                # #1341: a settle edit that fails transiently leaves a card
                # reading as still working (its ⏳). An edit is idempotent, so
                # it is tried once more — after flood control's stated wait
                # (capped, as the inbound-file reply does), else after a second
                if attempt == 2:
                    logger.warning("edit_dm_message failed twice (chat=%s message_id=%s): %s",
                                   chat_id, message_id, type(exc).__name__)
                    return False
                wait = getattr(exc, "retry_after", 1)
                wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
                await asyncio.sleep(min(max(float(wait or 0), 0.0), 10.0))
            except Exception as exc:  # noqa: BLE001 — best-effort, never raise
                logger.warning(
                    "edit_dm_message failed (chat=%s message_id=%s): %s",
                    chat_id, message_id, exc,
                )
                return False
        return False

    async def _dispatch_button_continuation(
        self, *, chat_id: int, user_id: int, target_role: str,
        request_id: str, text: str,
        _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> bool:
        """Deliver a button answer/approval to ``target_role`` as a synthetic
        CHANNEL_IN turn (r1-B2/r1-B3).

        The bus ``button_answer`` marker carries ``request_id`` (r1-B3) so the
        target agent can correlate the tap to its detached ``ask_user`` /
        protected-tool call. Because the target role may still be
        (re)registering when the operator taps, retries up to 3 times on a
        ``no_target`` drop (backoff 0.5s / 1s — injectable ``_sleep`` so tests
        never touch the shared ``asyncio.sleep``). Returns ``True`` iff the bus
        accepted the message.

        This is INTERNAL, Casa-composed context (not external ingress), so the
        reserved ``synthetic`` / ``button_answer`` markers are set directly and
        NOT passed through ``sanitize_external_context`` (which would strip
        them).
        """
        delays = (0.5, 1.0)
        # r1-1/W3 REQUIREMENT: the continuation lease id MUST equal the cid the
        # dispatched turn carries, so the target agent's own delivery/teardown
        # releases it. Compute the cid ONCE and reuse it across retries — a
        # per-attempt new_cid() would desync the lease from the accepted turn.
        cid = new_cid()
        # #336: a tap is a TURN, and it must both RUN at the tapper's
        # clearance and be ATTRIBUTED to the tapper. Without the markers the
        # continuation fell through to the channel-wide telegram clearance
        # (private), and without the trusted origin it was persisted as the
        # unattributed ``system`` speaker — so a non-operator whose question
        # produced buttons got private recall AND anonymous attribution on the
        # follow-up turn, re-opening through the button path exactly what the
        # per-sender identity closed on the message path (Sol, review r2). The
        # broker already pins a tap to the user the request was bound to,
        # which is who ``user_id`` is.
        _clearance = self._origin_clearance_for_user_id(user_id)
        _trusted_origin = ingress_identity(
            "telegram",
            sender_id=str(user_id),
            sender_is_operator=self._user_id_is_operator(user_id),
        )
        for attempt in range(3):
            msg = BusMessage(
                type=MessageType.CHANNEL_IN,
                source="telegram",
                target=target_role,
                content=text,
                channel="telegram",
                context={
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "cid": cid,
                    "synthetic": "button",
                    "button_answer": request_id,
                    "_origin_route": "telegram",
                    "_origin_clearance": _clearance,
                    # #283: a button tap is a live operator action — same
                    # marker rule as _handle above, operator sender only.
                    **({"_operator_turn": True}
                       if self._user_id_is_operator(user_id) else {}),
                },
                trusted_user_origin=_trusted_origin,
            )
            try:
                if await self._bus.send_checked(msg) == "accepted":
                    # r1-1: start the typing lease ONLY after acceptance (retries
                    # must not flap the indicator), keyed by this turn's cid so
                    # it coexists with the original turn's still-active lease and
                    # is released by the continuation turn's own delivery.
                    self._start_typing(str(chat_id), cid)
                    return True
            except asyncio.CancelledError:
                # Cooperative cancellation must propagate, never be swallowed
                # as a failed attempt.
                raise
            except Exception as exc:  # noqa: BLE001 — counts as a failed attempt
                # An escaping exception would abort the finish hook before the
                # delivery-failure overwrite edit; treat it as a failed attempt
                # and let the retry/backoff loop run its course (return False on
                # exhaustion so the caller can overwrite the keyboard).
                logger.warning(
                    "button continuation dispatch attempt %d/3 raised "
                    "(target=%s request_id=%s): %s",
                    attempt + 1, target_role, request_id, exc,
                )
            if attempt < len(delays):
                await _sleep(delays[attempt])
        return False

    async def _dispatch_scheduled_continuation(
        self, *, session_scope: str, target_role: str, request_id: str,
        text: str, epoch: str | None = None, answered_by_operator: bool = False,
        _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> bool:
        """Deliver a scheduled ask's TERMINAL outcome back into the session
        that asked (#573).

        Deliberately NOT the button sibling above. That one is addressed to a
        DM chat and is attributed to the tapper; this one must land in the
        SCHEDULED session — the label in ``session_scope`` keys it — or the
        resident receives an answer in a session that never saw the question.
        So it reproduces the SHAPE of the firing turn instead: a ``SCHEDULED``
        message carrying the session label as ``chat_id``, the same
        ``_scheduled_delivery`` marker, and the epoch the question was ASKED
        under (replayed from the durable record, never re-resolved — a
        revocation that raced the tap would otherwise hand the continuation a
        fresh epoch).

        No ``trusted_user_origin``, no ``_operator_turn``, no
        ``_origin_clearance``: the operator's tap is reported in the CONTENT of
        the turn, and giving it a speaker identity would relabel every
        machine-authored turn in that session as operator-authored (the
        registry keeps ONE ``user_provenance`` per entry).

        ``answered_by_operator`` (#1277) adds the one reserved
        ``ANSWERED_BY_OPERATOR`` marker, so a job this turn starts records
        that the operator asked. It names no speaker and grants nothing.
        """
        delays = (0.5, 1.0)
        cid = new_cid()
        for attempt in range(3):
            msg = BusMessage(
                type=MessageType.SCHEDULED,
                source="scheduled-ask",
                target=target_role,
                content=text,
                channel="telegram",
                context={
                    "chat_id": session_scope,
                    "cid": cid,
                    "button_answer": request_id,
                    **scheduled_delivery_markers("telegram", epoch),
                    **({ANSWERED_BY_OPERATOR: True} if answered_by_operator else {}),
                },
            )
            try:
                if await self._bus.send_checked(msg) == "accepted":
                    return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — counts as a failed attempt
                logger.warning(
                    "scheduled continuation dispatch attempt %d/3 raised "
                    "(target=%s request_id=%s): %s",
                    attempt + 1, target_role, request_id, exc,
                )
            if attempt < len(delays):
                await _sleep(delays[attempt])
        return False

    # ------------------------------------------------------------------
    # Bot accessor (supports test injection via bot= kwarg)
    # ------------------------------------------------------------------

    @property
    def bot(self) -> Any:
        if self._bot is not None:
            return self._bot
        return self._app.bot if self._app is not None else None

    @property
    def ready_event(self) -> asyncio.Event:
        """v0.83.0 (§A3(b), Sol r10-3): the channel-readiness event the boot
        open-question reconcilers await — set at the first successful
        ``_rebuild`` (transport up, ``bot`` usable). Exposed so casa_core can
        gate reconcile EXECUTION on transport readiness without blocking replay."""
        return self._ready_event

    @property
    def is_ready(self) -> bool:
        """True once the PTB application is started and sends can reach
        Telegram. Sol r1-2 (#342): ``send()`` returns NORMALLY when the
        app is absent (log-and-drop) — callers that gate a "delivered"
        flag on send success must check this first, or a bring-up window
        counts a dropped message as sent."""
        return self._app is not None

    async def send(self, message: str, context: dict[str, Any]) -> DeliveryOutcome:
        """Send a complete message (block mode fallback).

        #556: reports whether anything reached Telegram. The availability guard
        below returns NORMALLY having made zero Bot API calls, which a caller
        suppressing a one-shot notice must be able to tell from success.
        """
        # Release THIS turn's lease BEFORE the availability guard — lease
        # bookkeeping is local and must not be skipped in a reconnect window.
        target_chat = _resolve_chat_id(context, self.chat_id)
        self._release_typing(context, target_chat)

        if _unadmitted("send", message):
            return DeliveryOutcome.NOT_DELIVERED
        if self._app is None:
            logger.warning("Telegram channel not started; cannot send message")
            return DeliveryOutcome.NOT_DELIVERED

        outcome = DeliveryOutcome.NOT_DELIVERED
        for chunk in _split_message(message):
            await self._app.bot.send_message(
                chat_id=target_chat,
                text=chunk,
            )
            # The head landed. Stamp it where it survives a LATER chunk raising:
            # a raising method returns no enum at all, so the returned value
            # cannot carry this fact (#556 design §2.2).
            if outcome is DeliveryOutcome.NOT_DELIVERED:
                context["_delivery_head_sent"] = True
                outcome = DeliveryOutcome.DELIVERED
        # An empty split (whitespace-only input) correctly stays NOT_DELIVERED.
        return outcome

    async def send_media(
        self, content: bytes, kind: str, filename: str, context: dict[str, Any],
        *, caption: str | None = None,
    ) -> None:
        """Deliver *content* as the given media *kind* to the resolved chat.

        Dispatches positionally to the kind's PTB send method (§3.1) — each
        takes the media as its 2nd positional arg under a different name
        (document/photo/audio/voice), so positional avoids per-kind kwargs.
        Several KINDS may share one method: `document` (PDF), `zip` and `text`
        all send as `document`; the kind, not the method, is what was gated.
        Raises when the channel isn't started (the tool maps that to
        ``channel_unavailable``); lets TelegramError propagate (the tool
        classifies it)."""
        # Release the lease before the availability guard (local bookkeeping
        # must survive a reconnect window even when the send itself can't run).
        target_chat = _resolve_chat_id(context, self.chat_id)
        self._release_typing(context, target_chat)
        if caption is not None and _unadmitted("send_media", caption):
            raise UnadmittedText("send_media: caption is not admitted text")
        if self._app is None:
            raise RuntimeError("Telegram channel not started; cannot send media")
        method = getattr(self._app.bot, MEDIA_POLICIES[kind].ptb_method)
        await method(
            target_chat,
            InputFile(BytesIO(content), filename=filename),
            caption=caption,
        )

    # ------------------------------------------------------------------
    # Rich-text response delivery (v0.70.0). ONLY reached via the
    # response-provenant methods below (agent.py gates on error_kind is None);
    # send()/finalize_stream() stay plain for tools, bus
    # routing, notices, permission prompts, and error text.
    # ------------------------------------------------------------------

    def _plain_fallback_chunks(self, display, entities) -> list[str]:
        """The plain form of a rich page, as physical messages (#831).

        Ordinarily ONE chunk: the display with every TEXT_LINK destination
        re-attached, which is byte-identical to the display when the page
        carries no link. The reconstruction is longer than the display, so a
        page already at the 4096-unit budget can overflow — and then the first
        chunk is the display EXACTLY as before and the destinations follow, one
        url per line. That shape is deliberate: ``_split_message`` cuts at the
        last newline before the limit and otherwise hard-cuts, so splitting the
        inline form would cut a url across two messages and lose it again.

        Only the MULTI-PAGE senders use this. Every single-page path keeps
        retrying with its authored text.
        """
        text = plain_with_link_targets(display, entities)
        if utf16_len(text) <= _TG_MAX_LENGTH:
            return [text]
        # A destination longer than one whole message cannot be delivered by
        # this platform in ANY shape, and emitting it anyway would hand the
        # reader fragments of an address instead of an address (both reviewers,
        # measured: a 5,021-unit url hard-cut into unusable pieces). Drop it
        # and say so in the log; the targets that DO fit still go out whole.
        targets = []
        for url in missing_link_targets(display, entities):
            if utf16_len(url) > _TG_MAX_LENGTH:
                logger.warning(
                    "link destination exceeds one message and cannot be "
                    "delivered plain: %d units", utf16_len(url))
                continue
            targets.append(url)
        return [display, *_split_message("\n".join(targets))]

    async def _send_overflow_line(self, chat_id, line: str) -> None:
        """#1038 §6.6 (plan round 3): the admission's lines as ONE plain message
        before the overflow pages of a head that did not land. The pages then go
        exactly as they do when the head landed — never re-split around a
        prefix, which is where four findings in three rounds lived. A failure
        here is logged and the pages still go: the model's words are never
        withheld (#1036 ruling 2); the pages keep their own policies — the rich
        finalizer's overflow sends log and continue, the plain finalizer's
        chunk sends propagate, as before. The callers send this only when an
        overflow send follows; ``UNKNOWN`` (an unconfirmed head) counts as not
        landed, deliberately — see ``_overflow_head``."""
        if not line:
            return
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=line)
        except TelegramError as exc:
            logger.warning("Final stream disclosure send failed: %s", exc)

    def _record_post(self, chat_id, sent, post) -> None:
        """S4 §2: file the message that just landed under *post* in the post
        map — as it lands, one physical message at a time — so the operator's
        swipe-reply on it routes to the poster. Nothing without a record (every
        resident reply today), nothing without a message id (a stub). A landed
        post of a specialist that keeps no memory also leaves Casa's one
        body-free line in memory (INV-MEM-040)."""
        if post is None:
            return
        message_id = getattr(sent, "message_id", None)
        try:
            chat = int(chat_id)
        except (TypeError, ValueError):
            return
        import result_broker
        result_broker.POST_MAP.record(chat, message_id, post)
        # A job's plugin post is also filed on the job's record, so a reply
        # to it reaches the job for as long as the job lives — a restart and
        # the post map's bound included. Anything else files nothing.
        reg = self._engagement_registry
        owner = getattr(post, "owner", "")
        if (reg is not None and isinstance(owner, str) and owner
                and type(message_id) is int):
            filer = getattr(reg, "record_job_post", None)
            if filer is not None:
                try:
                    filer(owner, message_id)
                except Exception:  # noqa: BLE001 — filing never fails a post
                    logger.warning("job post filing failed", exc_info=True)
        if message_id is not None:
            # INV-MEM-040: memory keeps that a specialist which keeps no
            # memory posted here, never what (one line per post).
            from delegated_memory import capture_post_line
            capture_post_line(channel=self.name, chat_id=chat,
                              operator_chat=self.chat_id,
                              resident=self.default_agent, post=post)

    async def _send_one(self, chat_id, original, display, entities, *,
                        record=None, **kw):
        """Send one ≤4096 message with entities; on entity BadRequest resend the
        ORIGINAL text plain (exactly one retry — a TimedOut etc. propagates so we
        never duplicate a message Telegram may already have accepted).

        Returns True iff that one retry happened, so a caller holding further
        fallback chunks knows whether it owes them — and can commit its own
        delivery latch BEFORE sending them (#831). ``record`` (S4), when given,
        receives the platform's return of each send that succeeded."""
        try:
            sent = await self._app.bot.send_message(
                chat_id=chat_id, text=display, entities=entities, **kw,
            )
            if record is not None:
                record(sent)
            return False
        except BadRequest as exc:
            # The class only (#1015): a delivered operator link rides through
            # here, and the platform's error text can quote the message it
            # refused — a log line must never echo a credential.
            logger.warning("rich-text send fell back to plain: %s",
                           type(exc).__name__)
            sent = await self._app.bot.send_message(
                chat_id=chat_id, text=original, **kw,
            )
            if record is not None:
                record(sent)
            return True

    async def deliver_operator_link(
        self, chat_id: int, text: str, entities, plain: str, *, post=None, button=None,
    ) -> DeliveryOutcome:
        """#1015: post ONE Casa-composed labelled-link message to *chat_id* —
        the chat of a capability call's grant identity — for a plugin slot
        declared ``operator_link``. The result broker composes ``text`` (the
        display text), ``entities`` (its one ``text_link``) and ``plain``
        (the fallback that spells the URL out) and never goes through
        ``render_paged``: the caption and label are sent as the bytes they
        are. Returns ``NOT_DELIVERED`` when the channel is not started;
        ``DELIVERED`` on any normal return of ``_send_one`` (its bool only
        says whether the plain retry happened); an exception — including a
        TimedOut after Telegram may already have accepted the message —
        propagates, and the broker reads it as not proven.

        v0.347.3: with ``button`` — ``(text, button text, url)`` from
        ``compose_operator_link_button`` — the link is first sent as ONE
        message carrying an inline URL button, as a card carries its buttons;
        only a ``BadRequest`` for it (the platform refusing the button, which
        sent nothing) falls back to the labelled-link message above."""
        if self._app is None:
            logger.warning(
                "Telegram channel not started; cannot deliver operator link")
            return DeliveryOutcome.NOT_DELIVERED
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
        if button is not None:
            body, label, url = button
            try:
                sent = await self._app.bot.send_message(
                    chat_id=chat_id, text=body,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton(text=label, url=url)]]))
            except BadRequest as exc:
                # the class only (#1015): the platform's text can quote the URL
                logger.warning("link button refused, sending the link message: %s",
                               type(exc).__name__)
            else:
                self._record_post(chat_id, sent, post)
                return DeliveryOutcome.DELIVERED
        # Slice 5g: no preview card — the label and the host already say where it goes, and a
        # preview of the redirect page doubled the message's height on the phone.
        await self._send_one(chat_id, plain, text, entities,
                             record=lambda sent: self._record_post(chat_id, sent, post),
                             link_preview_options=LinkPreviewOptions(is_disabled=True))
        return DeliveryOutcome.DELIVERED

    # ------------------------------------------------------------------
    # S3: a specialist's plugin output posted by Casa, labelled, verbatim.
    # Casa-composed notices like the delivered link: the text is the
    # plugin's, headed by Casa's label, never model-authored (INV-OUT-001's
    # notice class).
    # ------------------------------------------------------------------

    def _plan_operator_message(self, text: str):
        """The WHOLE physical plan of *text*, judged before the first send:
        every page ``render_paged`` gives, with the plain chunks a refused
        page would be resent as. ``None`` — refuse with zero sends — when
        the plan has more than ``MAX_MESSAGE_PAGES`` pages or any element
        of it would not reach the operator complete: a page over the
        budget, a fallback chunk over it, a ``text_link`` destination over
        one message, or a destination the plain fallback's chunks would
        carry in pieces. The destination checks read the RENDERED ENTITIES
        directly: ``_plain_fallback_chunks`` filters an oversized
        destination silently and returns no sign that it did, and its
        splitter can cut a fitting destination across two chunks, so a plan
        judged from its chunks' lengths alone would miss exactly the
        elements it must refuse."""
        from result_broker import MAX_MESSAGE_PAGES
        pages = render_paged(text)
        if len(pages) > MAX_MESSAGE_PAGES:
            logger.warning("operator message refused: %d pages over the cap of %d",
                           len(pages), MAX_MESSAGE_PAGES)
            return None
        plan = []
        for display, entities in pages:
            if utf16_len(display) > _TG_MAX_LENGTH:
                logger.warning("operator message refused: a page over the budget")
                return None
            chunks = [display]
            if entities:
                chunks = self._plain_fallback_chunks(display, entities)
                if any(utf16_len(chunk) > _TG_MAX_LENGTH for chunk in chunks):
                    logger.warning("operator message refused: a fallback chunk over the budget")
                    return None
                # Each chunk fitting is not each destination arriving. The
                # destinations are read from the RENDERED ENTITIES and each
                # must occur WHOLE in some chunk: the helper filters a
                # destination longer than one message silently (it is then
                # in no chunk), and its splitter cuts the joined overflow
                # form at a newline or hard, so two destinations that each
                # fit can leave the second across two messages. Either way
                # the plan is refused.
                for url in missing_link_targets(display, entities):
                    if not any(url in chunk for chunk in chunks):
                        logger.warning(
                            "operator message refused: a link destination the "
                            "plain fallback would split across messages")
                        return None
            plan.append((display, entities, chunks))
        return plan

    async def deliver_operator_message(self, chat_id: int, text: str, *,
                                       post=None) -> DeliveryOutcome:
        """S3: post *text* — Casa's label line and the plugin's body, as the
        result broker composed it — to *chat_id*, the chat of a capability
        call's grant identity, for a slot declared ``operator_message``.
        Two steps: the plan is judged whole (``_plan_operator_message``),
        then every page goes in order the way ``send_response``'s
        MULTI-PAGE loop sends, whatever the page count — ``_send_one``
        sends the rendered page and, if the platform refuses its entities,
        resends chunk 1 plain, and this method then sends every remaining
        fallback chunk itself. The single-page retry with the AUTHORED text
        is never used: it is not bounded by the page budget. ``DELIVERED``
        only when every page and every fallback chunk it needed returned
        normally; a refused plan is ``NOT_DELIVERED`` with zero sends; any
        exception, on a page or on a tail chunk, propagates and the broker
        reads it as not proven."""
        if self._app is None:
            logger.warning(
                "Telegram channel not started; cannot deliver operator message")
            return DeliveryOutcome.NOT_DELIVERED
        plan = self._plan_operator_message(text)
        if plan is None:
            return DeliveryOutcome.NOT_DELIVERED
        # S4 §2: every physical message is filed under ``post`` as it lands.
        def _landed(sent):
            self._record_post(chat_id, sent, post)
        for display, entities, chunks in plan:
            if not entities:
                _landed(await self._app.bot.send_message(chat_id=chat_id, text=display))
                continue
            fell_back = await self._send_one(chat_id, chunks[0], display, entities,
                                             record=_landed)
            if fell_back:
                for chunk in chunks[1:]:
                    _landed(await self._app.bot.send_message(chat_id=chat_id, text=chunk))
        return DeliveryOutcome.DELIVERED

    async def deliver_operator_page(self, chat_id: int, text: str, *,
                                    post=None) -> DeliveryOutcome:
        """#1377: one plain page before a card — *text* is Casa's label line and
        the page as the result broker composed it, one message by the deposit's
        rule. Sent as exactly ONE message, rendered rich like the card; when the
        platform refuses its entities the authored text is resent plain as one
        message (``_send_one``), never split into fallback chunks, so a reply on
        the page quotes the page whole. Filed under *post* as it lands."""
        if self._app is None:
            logger.warning("Telegram channel not started; cannot deliver operator page")
            return DeliveryOutcome.NOT_DELIVERED

        def _landed(sent):
            self._record_post(chat_id, sent, post)
        display, entities = render(text)
        if entities is not None:
            await self._send_one(chat_id, text, display, entities, record=_landed)
        else:
            _landed(await self._app.bot.send_message(chat_id=chat_id, text=_plain(text)))
        return DeliveryOutcome.DELIVERED

    async def deliver_operator_question(self, chat_id: int, text: str, labels: list,
                                        rid: str) -> "int | None":
        """Browser executor §5.3/§5.5: post a plugin's operator question
        (``operator_calls.ask_operator`` composed *text*: Casa's label line and
        the validated body — the delivered-slot class, not model text) with one
        button per label, ``callback_data = v1|resident_ask|<rid>|<i>``, so the
        tap settles the waiting request through the resident-ask handler.
        Returns the message id, or ``None`` when nothing landed."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if self._app is None:
            logger.warning("Telegram channel not started; cannot deliver operator question")
            return None
        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton(text=str(label),
                                  callback_data=f"v1|resident_ask|{rid}|{i}")]
            for i, label in enumerate(labels)])
        try:
            msg = await self._app.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=kbd)
        except Exception as exc:  # noqa: BLE001 — not proven
            logger.warning("deliver_operator_question send failed (chat=%s): %s",
                           chat_id, type(exc).__name__)
            return None
        mid = getattr(msg, "message_id", None)
        return mid if isinstance(mid, int) else None

    async def deliver_run_card(self, chat_id: int, text: str,
                               buttons: "list[tuple[str, str]]") -> "int | None":
        """The status card of browsing his own words started (``run_admission``): Casa's
        label line and its fixed text, with ``(label, callback data)`` buttons — the
        request's Stop. Returns the message id, or ``None`` when nothing landed."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if self._app is None:
            logger.warning("Telegram channel not started; cannot deliver the run card")
            return None
        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton(text=str(label), callback_data=str(data))]
            for label, data in buttons])
        try:
            msg = await self._app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kbd)
        except Exception as exc:  # noqa: BLE001 — not proven
            logger.warning("deliver_run_card send failed (chat=%s): %s",
                           chat_id, type(exc).__name__)
            return None
        mid = getattr(msg, "message_id", None)
        return mid if isinstance(mid, int) else None

    async def send_reply_line(self, chat_id: int, text: str, reply_to: "int | None") -> bool:
        """One Casa line (a Stop's outcome, under the specialist's label) as a reply to the
        card it answers; a failure is logged and nothing more is attempted."""
        if self._app is None:
            return False
        kwargs = {"reply_to_message_id": reply_to} if isinstance(reply_to, int) else {}
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
            return True
        except Exception as exc:  # noqa: BLE001 — a notice fault is logged
            logger.warning("reply line failed: %s", type(exc).__name__)
            return False

    async def deliver_operator_proposal(self, chat_id: int, text: str, labels: list,
                                        rid: str, *, post=None, photo=None, urls=None):
        """S5 §3.3: post *text* (Casa's label line and the proposal body, as the
        result broker composed it — ONE page by the deposit's rule) with one
        button per label, ``callback_data = v1|proposal|<rid>|<i>``, rendered
        rich with the plain fallback ``post_dm_keyboard`` uses, and file the
        sent message under *post* (so a swipe-reply on the proposal routes to
        the specialist's desk). Returns the message id, or ``None`` when
        nothing landed.

        Slice 5b: with *photo* — ``(bytes, file name)`` the result broker claimed
        from the plugin outbox through the photo policy — the card is ONE photo
        message: the rendered text as its caption, the same keyboard; when
        Telegram refuses the caption's entities, the caption's display text is
        sent without them."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if self._app is None:
            logger.warning("Telegram channel not started; cannot deliver operator proposal")
            return None
        # the text is the broker's composition of a validated deposit (the S3
        # delivered-slot class, as ``deliver_operator_message``): not model text
        kbd = _proposal_keyboard(labels, rid, urls)
        display, entities = render(text)
        try:
            if photo is not None:
                content, filename = photo
                try:
                    msg = await self._app.bot.send_photo(
                        chat_id, InputFile(BytesIO(content), filename=filename),
                        caption=display, caption_entities=entities, reply_markup=kbd)
                except BadRequest as exc:
                    if entities is None:
                        raise
                    logger.warning("deliver_operator_proposal photo caption fell back to plain "
                                   "(chat=%s): %s", chat_id, exc)
                    msg = await self._app.bot.send_photo(
                        chat_id, InputFile(BytesIO(content), filename=filename),
                        caption=display, reply_markup=kbd)
            elif entities is not None:
                try:
                    msg = await self._app.bot.send_message(
                        chat_id=chat_id, text=display, entities=entities, reply_markup=kbd)
                except BadRequest as exc:
                    logger.warning("deliver_operator_proposal rich send fell back to plain "
                                   "(chat=%s): %s", chat_id, exc)
                    msg = await self._app.bot.send_message(
                        chat_id=chat_id, text=text, reply_markup=kbd)
            else:
                msg = await self._app.bot.send_message(
                    chat_id=chat_id, text=_plain(text), reply_markup=kbd)
        except NetworkError as exc:
            if isinstance(exc, BadRequest):
                logger.warning("deliver_operator_proposal send failed (chat=%s): %s", chat_id, exc)
                return None
            # #1305: a transport failure after the request was sent may have
            # landed the card; the result broker keeps it registered
            raise UnconfirmedDelivery(type(exc).__name__) from exc
        except Exception as exc:  # noqa: BLE001 — not proven
            logger.warning("deliver_operator_proposal send failed (chat=%s): %s", chat_id, exc)
            return None
        self._record_post(chat_id, msg, post)
        mid = getattr(msg, "message_id", None)
        return mid if isinstance(mid, int) else None

    async def replace_operator_proposal(self, chat_id: int, message_id: int, text: str,
                                        labels: list, rid: str, *, post=None, urls=None):
        """#1339: edit the tapped proposal *message_id* in place to *text* (the
        result broker's composition of a validated next card, as
        ``deliver_operator_proposal``) with one button per label,
        ``callback_data = v1|proposal|<rid>|<i>``, rendered rich with the same
        plain fallback. The broker filed the message under *post* before the
        edit, so nothing is filed here. Returns *message_id* when the edit
        landed (a "not modified" answer included), ``None`` when Telegram
        refused it; a transport failure after the request was sent raises
        ``UnconfirmedDelivery``, as for a post (#1305)."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if self._app is None:
            logger.warning("Telegram channel not started; cannot replace operator proposal")
            return None
        kbd = _proposal_keyboard(labels, rid, urls)
        display, entities = render(text)
        try:
            try:
                if entities is not None:
                    try:
                        await self._app.bot.edit_message_text(
                            chat_id=chat_id, message_id=message_id, text=display,
                            entities=entities, reply_markup=kbd)
                        return message_id
                    except BadRequest as exc:
                        if "not modified" in str(exc).lower():
                            return message_id
                        logger.warning("replace_operator_proposal rich edit fell back to "
                                       "plain (chat=%s): %s", chat_id, type(exc).__name__)
                        await self._app.bot.edit_message_text(
                            chat_id=chat_id, message_id=message_id, text=text,
                            reply_markup=kbd)
                else:
                    await self._app.bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id, text=_plain(text),
                        reply_markup=kbd)
                return message_id
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return message_id
                logger.warning("replace_operator_proposal edit refused (chat=%s): %s",
                               chat_id, type(exc).__name__)
                return None
        except NetworkError as exc:
            # a transport failure after the request was sent may have landed the edit
            raise UnconfirmedDelivery(type(exc).__name__) from exc
        except Exception as exc:  # noqa: BLE001 — not proven
            logger.warning("replace_operator_proposal failed (chat=%s): %s", chat_id,
                           type(exc).__name__)
            return None

    async def deliver_operator_file(
        self, chat_id: int, content: bytes, kind: str, filename: str, caption: str,
        *, post=None,
    ) -> DeliveryOutcome:
        """S3: send *content* — already claimed from the plugin outbox and
        passed through the kind's policy by the result broker — as the media
        *kind* to *chat_id*, through the kind's PTB method exactly as
        ``send_media`` dispatches, with *caption* (Casa's label line, the
        plugin's caption beneath) as the bytes it is: no markdown
        rendering, no entities, no thread id. ``NOT_DELIVERED`` when the
        channel is not started; ``DELIVERED`` on a normal return; an
        exception propagates and the broker reads it as not proven."""
        if self._app is None:
            logger.warning(
                "Telegram channel not started; cannot deliver operator file")
            return DeliveryOutcome.NOT_DELIVERED
        method = getattr(self._app.bot, MEDIA_POLICIES[kind].ptb_method)
        sent = await method(
            chat_id,
            InputFile(BytesIO(content), filename=filename),
            caption=caption,
        )
        self._record_post(chat_id, sent, post)
        return DeliveryOutcome.DELIVERED

    async def send_response(
        self, message: str, context: dict[str, Any],
    ) -> DeliveryOutcome:
        """Block-mode agent response with rich-text rendering (plain fallback).

        v2 (RC3): an oversized response paginates through ``render_paged()`` —
        every page sends rendered (previously the whole message fell back to
        plain raw chunks via ``send()``). Single page keeps the v0.70.0
        contract (no entities → ``plain_text``, #1330; BadRequest → raw resend);
        multi-page fallbacks use each page's DISPLAY."""
        # Release the lease before the availability guard (reconnect-safe).
        target_chat = _resolve_chat_id(context, self.chat_id)
        self._release_typing(context, target_chat)
        if _unadmitted("send_response", message):
            return DeliveryOutcome.NOT_DELIVERED
        if self._app is None:
            logger.warning("Telegram channel not started; cannot send message")
            return DeliveryOutcome.NOT_DELIVERED
        pages = render_paged(message)
        # S4 §2/§5.5: a desk reply's context carries the post record its
        # pages are filed under as they land; every other reply carries none.
        post = context.get("_post")

        def _landed(sent):
            self._record_post(target_chat, sent, post)
        if len(pages) == 1:
            display, entities = pages[0]
            if entities is None:
                if post is None:
                    # Delegate's outcome, VERBATIM (#556 design §2.1) — rewrapping
                    # it here is how a delegation path loses the distinction.
                    return await self.send(_plain(message), context)
                _landed(await self._app.bot.send_message(
                    chat_id=target_chat, text=display))
                context["_delivery_head_sent"] = True
                return DeliveryOutcome.DELIVERED
            await self._send_one(target_chat, message, display, entities,
                                 record=_landed)
            context["_delivery_head_sent"] = True
            return DeliveryOutcome.DELIVERED
        outcome = DeliveryOutcome.NOT_DELIVERED
        for display, entities in pages:
            chunks, fell_back = [display], False
            if entities is None:
                _landed(await self._app.bot.send_message(
                    chat_id=target_chat, text=display))
            else:
                # #831: the plain retry carries this page's link destinations,
                # which its marker-free display cannot.
                chunks = self._plain_fallback_chunks(display, entities)
                fell_back = await self._send_one(
                    target_chat, chunks[0], display, entities, record=_landed)
            # Page 1 carries the notice: once it lands the delivery counts as
            # shown, however the remaining pages fare. Stamped BEFORE the
            # overflow chunks below, so a raising tail cannot erase the fact
            # that the head is on screen.
            if outcome is DeliveryOutcome.NOT_DELIVERED:
                context["_delivery_head_sent"] = True
                outcome = DeliveryOutcome.DELIVERED
            if fell_back:
                for chunk in chunks[1:]:
                    _landed(await self._app.bot.send_message(
                        chat_id=target_chat, text=chunk))
        return outcome

    async def finalize_response_stream(
        self, full_text: str, context: dict[str, Any], on_token: OnTokenCallback,
    ) -> DeliveryOutcome:
        """Streamed agent response: apply entities on the final edit only.

        Block mode / no streamed message → send_response(). Plain (no markup) or
        oversize → the existing plain finalize_stream(). Entity edit falls back to
        the ORIGINAL text edited into the SAME message on BadRequest."""
        # Release the lease before the availability guard (reconnect-safe).
        target_chat = _resolve_chat_id(context, self.chat_id)
        self._release_typing(context, target_chat)
        if _unadmitted("finalize_response_stream", full_text):
            return DeliveryOutcome.NOT_DELIVERED
        if self._app is None:
            return DeliveryOutcome.NOT_DELIVERED
        message_id = _peek_stream_message_id(on_token)
        if message_id is None:
            return await self.send_response(full_text, context)  # verbatim
        # v2 (RC3): an oversized SUCCESSFUL response no longer delegates to
        # the plain finalize_stream (raw-marker chunks); page 1 rich-edits the
        # streamed message, the rest send rendered. Single page keeps the
        # v0.70.0 contract (no markup → plain finalize of ``plain_text``,
        # #1330; BadRequest → raw edit).
        pages = render_paged(full_text)
        if len(pages) == 1 and pages[0][1] is None:
            return await self.finalize_stream(  # verbatim (§2.1)
                _plain(full_text), context, on_token)
        display0, entities0 = pages[0]
        # #831: a rejected multi-page page-1 edit carries its link
        # destinations; the single page keeps its authored text verbatim.
        chunks0 = ([full_text] if len(pages) == 1
                   else self._plain_fallback_chunks(display0, entities0))
        fallback0 = chunks0[0]
        # Page 1 carries a prepended operator notice, so page 1 decides the
        # outcome; the overflow sends below swallow their errors and cannot
        # downgrade it (#556 design §2.3).
        outcome = DeliveryOutcome.NOT_DELIVERED
        fell_back0 = False
        try:
            try:
                if entities0 is not None:
                    await self._app.bot.edit_message_text(
                        chat_id=target_chat, message_id=message_id,
                        text=display0, entities=entities0,
                    )
                else:
                    await self._app.bot.edit_message_text(
                        chat_id=target_chat, message_id=message_id,
                        text=fallback0,
                    )
            except BadRequest:
                fell_back0 = True
                await self._app.bot.edit_message_text(
                    chat_id=target_chat, message_id=message_id, text=fallback0,
                )
        except TelegramError as exc:
            if "not modified" not in str(exc).lower():
                logger.warning("Final stream edit failed: %s", exc)
                outcome = _edit_failure_outcome(exc)
            else:
                # Already displays this exact text, notice included.
                outcome = DeliveryOutcome.DELIVERED
                context["_delivery_head_sent"] = True
        else:
            outcome = DeliveryOutcome.DELIVERED
            context["_delivery_head_sent"] = True
        # An edit cannot be split, so page 1's overflow destinations follow as
        # ordinary messages — after the outcome above is decided, under the
        # same log-and-continue policy the overflow pages use.
        # #1038 §6.6: a head that did not land cannot carry the disclosure for
        # the pages that follow, so the admission's lines go out first, once,
        # as their own message — only when something follows; the pages
        # themselves are untouched.
        tail0 = chunks0[1:] if fell_back0 else []
        if tail0 or pages[1:]:
            await self._send_overflow_line(target_chat, _overflow_head(full_text, outcome))
        for chunk in tail0:
            try:
                await self._app.bot.send_message(
                    chat_id=target_chat, text=chunk)
            except TelegramError as exc:
                logger.warning("Final stream link-target send failed: %s", exc)
        for display, entities in pages[1:]:
            try:
                if entities is None:
                    await self._app.bot.send_message(
                        chat_id=target_chat, text=display)
                else:
                    chunks = self._plain_fallback_chunks(display, entities)
                    if await self._send_one(
                            target_chat, chunks[0], display, entities):
                        for chunk in chunks[1:]:
                            await self._app.bot.send_message(
                                chat_id=target_chat, text=chunk)
            except TelegramError as exc:
                logger.warning("Final stream overflow send failed: %s", exc)
        return outcome

    async def turn_finished(self, context: dict[str, Any]) -> None:
        """L7 (v0.52.0): teardown for turns that end WITHOUT delivery.

        When a turn produces empty / whitespace-only / `<silent/>` output the
        agent never calls send()/finalize_stream(), so the per-chat typing
        loop started in _handle would keep issuing send_chat_action forever
        (permanent 'typing…' plus one Bot API call every 4s). The agent calls
        this hook on the suppressed-turn path to stop it. In block mode the
        streaming first-token teardown never runs, so this is the only stop.
        """
        target_chat = _resolve_chat_id(context, self.chat_id)
        self._release_typing(context, target_chat)

    # ------------------------------------------------------------------
    # Outbound: streaming
    # ------------------------------------------------------------------

    def create_on_token(self, context: dict[str, Any]) -> OnTokenCallback:
        """Return a callback for streaming tokens to this chat.

        The callback receives the *accumulated* response text on each token and
        edits the message in place. v0.125.0 (#228) removed the
        ``telegram_delivery_mode`` option and with it the ``block`` mode, whose
        only behaviour was to make this a no-op and let the final send deliver
        everything at once.
        """
        target_chat = str(_resolve_chat_id(context, self.chat_id))
        # r1-1: release THIS turn's lease by its cid on first-token teardown
        # (captured once — the context is fixed for this callback's turn).
        _lease_cid = context.get("cid")
        _lease_cid = _lease_cid if isinstance(_lease_cid, str) and _lease_cid else None
        state: dict[str, Any] = {
            "message_id": None,
            "last_edit": 0.0,
        }

        async def _stream_token(accumulated_text: str) -> None:
            # First-token teardown releases THIS turn's lease BEFORE the
            # availability guard — local bookkeeping must not depend on channel
            # health. Idempotent, so repeated pre-message calls during a
            # reconnect window stay a no-op.
            if state["message_id"] is None:
                self._stop_typing(target_chat, _lease_cid)

            if self._app is None:
                return

            now = time.monotonic()

            if state["message_id"] is None:
                # First token: send new message (typing already stopped above).
                # #305 (Sol r1): the first send needs the same UTF-16 guard as
                # the edits — an oversized first payload would be rejected on
                # every callback; skip and let finalize_stream split it.
                if utf16_len(accumulated_text) > _TG_MAX_LENGTH:
                    return
                try:
                    result = await self._app.bot.send_message(
                        chat_id=target_chat,
                        text=accumulated_text,
                    )
                    state["message_id"] = result.message_id
                    state["last_edit"] = now
                except TelegramError as exc:
                    logger.warning("Stream send failed: %s", exc)
            elif now - state["last_edit"] >= _STREAM_THROTTLE:
                # Throttled edit (#305: UTF-16 units, Telegram's unit)
                if utf16_len(accumulated_text) > _TG_MAX_LENGTH:
                    return  # Stop editing, final send will split
                try:
                    await self._app.bot.edit_message_text(
                        chat_id=target_chat,
                        message_id=state["message_id"],
                        text=accumulated_text,
                    )
                    state["last_edit"] = now
                except TelegramError as exc:
                    # "Message is not modified" is fine (identical text)
                    if "not modified" not in str(exc).lower():
                        logger.warning("Stream edit failed: %s", exc)

        return _stream_token

    async def finalize_stream(
        self, full_text: str, context: dict[str, Any], on_token: OnTokenCallback
    ) -> DeliveryOutcome:
        """Send the final version of a streamed response.

        In stream mode, does a final edit to ensure the complete text
        is displayed.  Falls back to send() if streaming never started
        (e.g., empty response or stream mode was block).

        #556: the outcome describes the FINAL EDIT, not the token stream. A
        one-shot notice is prepended after streaming, so tokens the operator
        already watched never contained it — only this edit can show it.
        """
        # Release the lease before the availability guard (reconnect-safe).
        target_chat = _resolve_chat_id(context, self.chat_id)
        self._release_typing(context, target_chat)

        if _unadmitted("finalize_stream", full_text):
            return DeliveryOutcome.NOT_DELIVERED
        if self._app is None:
            return DeliveryOutcome.NOT_DELIVERED

        # Retrieve state from the closure
        state = getattr(on_token, "__self__", None)
        # For closures we peek at the cell variables
        message_id = None
        if hasattr(on_token, "__closure__") and on_token.__closure__:
            for cell in on_token.__closure__:
                try:
                    val = cell.cell_contents
                    if isinstance(val, dict) and "message_id" in val:
                        message_id = val["message_id"]
                        break
                except ValueError:
                    continue

        if message_id is None:
            # Streaming never sent a message — fall back to regular send
            return await self.send(full_text, context)   # verbatim (§2.1)

        # Final edit with complete text (#305: UTF-16 units)
        if utf16_len(full_text) <= _TG_MAX_LENGTH:
            try:
                await self._app.bot.edit_message_text(
                    chat_id=target_chat,
                    message_id=message_id,
                    text=full_text,
                )
            except TelegramError as exc:
                if "not modified" not in str(exc).lower():
                    logger.warning("Final stream edit failed: %s", exc)
                    return _edit_failure_outcome(exc)
                # "not modified" — the message ALREADY displays this exact
                # text, notice included. A positive outcome (§2.4 ruling 1).
            context["_delivery_head_sent"] = True
            return DeliveryOutcome.DELIVERED
        else:
            # Response exceeded the limit — edit first chunk, send the rest
            chunks = _split_message(full_text)
            if not chunks:
                # Whitespace-only overflow (#305 drops unsendable chunks) —
                # keep the streamed message as-is rather than index [].
                # Nothing was shown, so nothing may be suppressed on it.
                return DeliveryOutcome.NOT_DELIVERED
            outcome = DeliveryOutcome.NOT_DELIVERED
            try:
                await self._app.bot.edit_message_text(
                    chat_id=target_chat,
                    message_id=message_id,
                    text=chunks[0],
                )
            except TelegramError as exc:
                if "not modified" in str(exc).lower():
                    outcome = DeliveryOutcome.DELIVERED
                    context["_delivery_head_sent"] = True
                else:
                    outcome = _edit_failure_outcome(exc)
            else:
                outcome = DeliveryOutcome.DELIVERED
                context["_delivery_head_sent"] = True
            # #1038 §6.6: a head that did not land cannot carry the
            # disclosure for the chunks that follow — the lines go out first,
            # once, as their own message, only when a chunk follows; the
            # chunks are untouched.
            if chunks[1:]:
                await self._send_overflow_line(target_chat, _overflow_head(full_text, outcome))
            for chunk in chunks[1:]:
                await self._app.bot.send_message(
                    chat_id=target_chat,
                    text=chunk,
                )
            # The head chunk decides: later chunks cannot un-show it.
            return outcome


# A REFUSAL, not a transport failure: PTB builds each of these from a response
# Telegram actually sent, so the API evaluated the call and declined it and
# nothing was displayed. Membership is an explicit allowlist rather than a class
# test, because the hierarchy cuts across the distinction that matters here —
# `BadRequest` (HTTP 400, a refusal) and `TimedOut` (no response at all) are
# BOTH `NetworkError` subclasses in PTB 22.7, so classifying by base class gets
# it exactly backwards.
_SERVER_REFUSALS = (
    BadRequest, Forbidden, InvalidToken, RetryAfter, ChatMigrated, Conflict,
)


def _edit_failure_outcome(exc: TelegramError) -> DeliveryOutcome:
    """Classify a failed edit: established negative, or merely ambiguous?

    ``NOT_DELIVERED`` is a CLAIM — that nothing reached the operator — and a
    caller acts on it by re-offering a one-shot notice. Both errors are costly
    and they are costly in opposite directions, so the taxonomy has to be right
    in both:

    - A lost acknowledgement does not support the claim. PTB raises ``TimedOut``
      when no response arrives, but Telegram may well have applied the edit, in
      which case the operator has read the notice and re-offering it nags them
      about something they have seen. That is ``UNKNOWN``.
    - A refusal does support it. The call was evaluated and declined, so the
      notice was certainly not shown, and withholding it would leave a blocking
      issue unreported — the very defect #556 exists to fix. An operator who
      blocks the bot mid-stream produces ``Forbidden`` here, and must still be
      told once they unblock it.

    Both halves were found by review: collapsing ambiguity into NOT_DELIVERED
    was a regression against pre-contract behavior (Sol + Terra, diff r1), and
    the first fix then over-corrected by treating every non-``BadRequest``
    refusal as ambiguous (Sol, diff r2).
    """
    return (DeliveryOutcome.NOT_DELIVERED if isinstance(exc, _SERVER_REFUSALS)
            else DeliveryOutcome.UNKNOWN)


_TG_MAX_LENGTH = 4096


def _split_message(text: str) -> list[str]:
    """Split *text* into chunks that fit within Telegram's message limit.

    #305: the limit is measured in UTF-16 code units (Telegram's unit — an
    astral emoji counts 2), never Python code points, and a newline split
    consumes exactly the ONE split newline so blank-line separation survives
    into the next chunk. A whitespace-only chunk is never emitted (Telegram
    rejects an effectively-empty message) — including the degenerate case of
    whole-input whitespace, which returns ``[]`` (Sol r1: the old fast path
    returned ``[""]``, and send() then attempted an empty Bot API message).
    """
    if not text.strip():
        return []
    if utf16_len(text) <= _TG_MAX_LENGTH:
        return [text]

    chunks: list[str] = []
    while text:
        hard = utf16_prefix_end(text, 0, _TG_MAX_LENGTH)
        if hard >= len(text):
            chunks.append(text)
            break

        split_at = text.rfind("\n", 0, hard)
        if split_at <= 0:
            chunks.append(text[:hard])
            text = text[hard:]
        else:
            chunks.append(text[:split_at])
            text = text[split_at + 1:]

    return [c for c in chunks if c.strip()]


class _InboundReservation:
    """#663: one system continuation's SYNCHRONOUS ingress reservation, from
    the operator's tap-commit until the seam admits its text-bearing ticket.

    The window it closes is `[BROKER.commit() returns, deliver_system_turn's
    admission]`. On the two install-consent arms that is the finish hook's
    ``loop.create_task`` scheduling gap — one event-loop iteration, and
    unremovable; on the #400 authorization arm it is a whole Telegram
    ``edit_dm_message`` round trip, because the approval edit is awaited before
    the continuation is dispatched. Inside it the continuation's text exists
    nowhere, so ``emit_completion``'s gate and ``_finalize_engagement``'s
    terminal hook both read zero and a successful completion commits over a
    resume the operator has already approved.

    The lease OWNS the held/not-held bit rather than leaving it to its callers,
    which is what makes "released twice" and "released without taking"
    impossible by construction: the finish hook wraps its whole body in
    ``finally: release()`` to cover every arm that never reaches the seam, and
    the seam releases as soon as the ticket exists — so on the ordinary path
    ``release`` really is called twice, and the second one must be a no-op
    rather than a decrement of somebody else's reservation.

    Both methods are SYNCHRONOUS and neither raises: ``take`` is called from a
    broker ``on_commit_sync`` step, which runs in the Telegram callback with no
    await after the commit, and a raise there would abort the tap.
    """

    __slots__ = ("_channel", "_engagement_id", "_held")

    def __init__(self, channel, engagement_id: str) -> None:
        self._channel = channel
        self._engagement_id = str(engagement_id or "")
        self._held = False

    def take(self) -> bool:
        """Reserve, and say whether anything was reserved.

        ``False`` for an unknown record or a channel with no driver.
        """
        if self._held:
            return True
        reg = getattr(self._channel, "_engagement_registry", None)
        drv = getattr(self._channel, "_engagement_driver", None)
        if reg is None or drv is None or not self._engagement_id:
            return False
        try:
            rec = reg.get(self._engagement_id)
            if rec is None:
                return False
            drv.reserve_inbound(rec.id)
        except Exception:  # noqa: BLE001 — a tap must never abort on this
            logger.warning("inbound reservation failed for engagement %s",
                           self._engagement_id[:8], exc_info=True)
            return False
        self._held = True
        return True

    def release(self) -> None:
        """Release exactly once, or do nothing. Idempotent by design — see the
        class docstring for why it is genuinely called twice."""
        if not self._held:
            return
        self._held = False
        drv = getattr(self._channel, "_engagement_driver", None)
        if drv is None:
            return
        try:
            drv.release_inbound_reservation(self._engagement_id)
        except Exception:  # noqa: BLE001
            logger.debug("inbound reservation release failed", exc_info=True)
